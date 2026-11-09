"""Integration tests for the package-search chat endpoints + service.

Covers (per change `rebuild-package-search-with-project-context` task 5.4):

- the three endpoints (`/package-search/stream|cancel|result`) end to end
  with a mocked agent;
- HTTP 400 (`reason="project_repo_required"`) when a new session has no
  ``project_repo_id``;
- session project binding does not drift when a follow-up turn carries a
  different ``project_repo_id``;
- cancel / result ownership enforcement (PermissionError → 403).

``PackageSearchAgent`` is monkeypatched so the tests never reach the real
Claude SDK; ``project_repo_service.get_by_id`` is stubbed so no database
is needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.agents.package_search.workspace import WorkspaceContext

PROJECT_REPO_ID = 7


def _decode_sse_event(chunk: str) -> dict:
    assert chunk.startswith("data: ")
    return json.loads(chunk[len("data: "):].strip())


def _make_ctx(tmp_path: Path) -> WorkspaceContext:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    task_json = tmp_path / "task.json"
    task_json.write_text(
        json.dumps(
            {
                "question": "最新的重构包是哪个？",
                "hints": "",
                "repo_info": {
                    "project_code": "proj-a",
                    "repo_url": "https://git.example.com/proj-a.git",
                    "default_branch": "main",
                    "source": "user_selected_project_repo",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return WorkspaceContext(
        task_id="task-1",
        temp_dir=str(tmp_path),
        repo_dir=str(repo_dir),
        task_json_path=str(task_json),
        project_code="proj-a",
        metadata={"question": "最新的重构包是哪个？", "hints": ""},
    )


def _canned_result(status: str = "ok") -> Dict[str, Any]:
    return {
        "engine": "claude-agent-sdk",
        "model": "fake-model",
        "status": status,
        "error_kind": "cancelled" if status == "cancelled" else None,
        "answer": "推荐 pkg-1。\n```json\n{\"recommended_package_ids\": [\"pkg-1\"]}\n```",
        "recommended_package_ids": ["pkg-1"],
        "relevant_package_ids": ["pkg-1", "pkg-2"],
        "notes": "demo",
        "tool_trace": [],
        "trace_events": [],
        "trace_summary": {},
        "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
        "duration_seconds": 0.0,
        "session_id": "task-1",
    }


class FastAgent:
    """Mock agent: returns the canned package-search result immediately."""

    def run_sync(self, _ctx, _cancel_event=None, trace_emitter=None, _clarification_binding=None):
        if trace_emitter is not None:
            trace_emitter(
                {
                    "type": "run_start",
                    "task_id": "task-1",
                    "seq": 1,
                    "timestamp": time.time(),
                    "model": "fake-model",
                }
            )
        return _canned_result()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['new', 'running', 'completed'])
async def test_package_stream_releases_database_connection_before_subscribing(tmp_path, monkeypatch, mode):
    """Even a one-connection pool remains usable while the browser follows SSE."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.services.package_search_chat_service import AgentJob, PackageSearchChatService

    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/pool.db',
                                 pool_size=1, max_overflow=0, pool_timeout=0.1)
    sessions = async_sessionmaker(engine)
    service = PackageSearchChatService()
    ctx = _make_ctx(tmp_path)
    meta = {'project_repo_id': PROJECT_REPO_ID}
    _patch_service_for_fast_run(monkeypatch, service, ctx, meta)
    monkeypatch.setattr(service, '_register_chat_run', AsyncMock())
    monkeypatch.setattr(service, '_finalize_chat_run', AsyncMock())
    monkeypatch.setattr('app.services.package_search_chat_service.PackageSearchAgent', FastAgent)
    if mode != 'new':
        service._jobs['pool-session'] = AgentJob(
            session_id='pool-session', task_id=ctx.task_id, context_meta=meta,
            question='package', user_id=None, remember=False, started_at=time.monotonic(),
            owner_scope='anon:legacy', done=mode == 'completed',
        )
    checked = False

    async def subscribe(_job):
        nonlocal checked
        async with sessions() as other_browser:
            assert (await other_browser.execute(text('SELECT 1'))).scalar() == 1
        checked = True
        yield service._sse_event({'event': 'done'})

    monkeypatch.setattr(service, '_subscribe', subscribe)
    try:
        async with sessions() as db:
            await db.execute(text('SELECT 1'))  # connection held by auth lookup
            async for _ in service.stream(
                message='' if mode == 'completed' else 'continue', session_id='pool-session',
                history_json=None, remember=False, project_repo_id=PROJECT_REPO_ID, db=db, user=None,
            ):
                pass
            assert checked
        job = service._jobs['pool-session']
        if job.task:
            await job.task
    finally:
        await engine.dispose()


# ───────────────────────── service-level tests ──────────────────────────


@pytest.mark.asyncio
async def test_service_requires_project_repo_for_new_session():
    from app.services.package_search_chat_service import PackageSearchChatService

    service = PackageSearchChatService()
    events = []
    async for chunk in service.stream(
        message="最新的包？",
        session_id="ps-session-required",
        history_json=None,
        remember=False,
        project_repo_id=None,
        db=None,
        user=None,
    ):
        event = _decode_sse_event(chunk)
        events.append(event)
        if event.get("event") == "error":
            break

    assert events[-1]["event"] == "error"
    assert events[-1]["reason"] == "project_repo_required"
    assert "ps-session-required" not in service._jobs


def _patch_service_for_fast_run(monkeypatch, service, ctx, meta):
    monkeypatch.setattr(service, "_load_context", lambda *_a, **_kw: (ctx, meta))
    monkeypatch.setattr(service, "_save_context", lambda *_a, **_kw: None)
    monkeypatch.setattr(service, "_touch_context", lambda *_a, **_kw: None)
    monkeypatch.setattr(service, "_persist_exchange", lambda *_a, **_kw: None)

    async def fake_history_hint(**_kwargs):
        return ""

    monkeypatch.setattr(service, "_build_history_hint", fake_history_hint)


@pytest.mark.asyncio
async def test_service_binding_does_not_drift_and_done_carries_package_ids(
    monkeypatch, tmp_path
):
    """Follow-up turn with a different project_repo_id keeps the first
    project (system_notice instead of switching), and the terminal ``done``
    event surfaces the package-search result contract."""
    from app.services.package_search_chat_service import PackageSearchChatService

    ctx = _make_ctx(tmp_path)
    meta = {
        "session_id": "ps-session-followup",
        "task_id": ctx.task_id,
        "temp_dir": ctx.temp_dir,
        "repo_dir": ctx.repo_dir,
        "task_json_path": ctx.task_json_path,
        "project_repo_id": PROJECT_REPO_ID,
        "project_code": "proj-a",
        "project_name": "Project A",
    }

    service = PackageSearchChatService()
    _patch_service_for_fast_run(monkeypatch, service, ctx, meta)
    monkeypatch.setattr(
        "app.services.package_search_chat_service.PackageSearchAgent",
        FastAgent,
    )

    events = []
    async for chunk in service.stream(
        message="继续看包",
        session_id="ps-session-followup",
        history_json=None,
        remember=False,
        project_repo_id=PROJECT_REPO_ID + 1,  # different from the bound project
        db=None,
        user=None,
    ):
        event = _decode_sse_event(chunk)
        events.append(event)
        if event.get("event") == "done":
            break

    notices = [
        event for event in events
        if event.get("event") == "agent_trace" and event.get("type") == "system_notice"
    ]
    assert any(event.get("kind") == "project_switch_ignored" for event in notices)

    done = events[-1]
    assert done["event"] == "done"
    assert done["recommended_package_ids"] == ["pkg-1"]
    assert done["relevant_package_ids"] == ["pkg-1", "pkg-2"]
    assert done["notes"] == "demo"
    assert service.get_status("ps-session-followup")["status"] == "done"
    # The job stays bound to the first turn's project.
    assert (
        service.get_status("ps-session-followup")["project_repo_id"]
        == PROJECT_REPO_ID
    )


@pytest.mark.asyncio
async def test_service_cancel_path(monkeypatch, tmp_path):
    from app.services.package_search_chat_service import PackageSearchChatService

    ctx = _make_ctx(tmp_path)
    meta = {
        "session_id": "ps-session-cancel",
        "task_id": ctx.task_id,
        "temp_dir": ctx.temp_dir,
        "repo_dir": ctx.repo_dir,
        "task_json_path": ctx.task_json_path,
        "project_repo_id": PROJECT_REPO_ID,
    }
    captured: Dict[str, Any] = {}

    class CancellableAgent:
        def run_sync(self, _ctx, cancel_event=None, _trace_emitter=None, _clarification_binding=None):
            captured["event"] = cancel_event
            for _ in range(200):
                if cancel_event is not None and cancel_event.is_set():
                    break
                time.sleep(0.01)
            return _canned_result(status="cancelled")

    service = PackageSearchChatService()
    _patch_service_for_fast_run(monkeypatch, service, ctx, meta)
    monkeypatch.setattr(
        "app.services.package_search_chat_service.PackageSearchAgent",
        CancellableAgent,
    )

    stream = service.stream(
        message="继续看包",
        session_id="ps-session-cancel",
        history_json=None,
        remember=False,
        project_repo_id=PROJECT_REPO_ID,
        db=None,
        user=None,
    )
    events = []

    async def consume():
        async for chunk in stream:
            event = _decode_sse_event(chunk)
            events.append(event)
            if event.get("event") == "done":
                break

    consumer = asyncio.create_task(consume())
    for _ in range(50):
        if captured.get("event") is not None:
            break
        await asyncio.sleep(0.02)

    assert service.get_status("ps-session-cancel")["status"] == "running"
    assert service.cancel("ps-session-cancel") is True
    await asyncio.wait_for(consumer, timeout=5)

    status = service.get_status("ps-session-cancel")
    assert status["status"] == "done"
    assert status["cancel_requested"] is True
    assert status["result"]["status"] == "cancelled"


def test_cancel_and_status_enforce_ownership():
    """Non-owner cancel/result raise PermissionError (endpoint maps to 403)."""
    from app.services.package_search_chat_service import (
        AgentJob,
        PackageSearchChatService,
    )

    service = PackageSearchChatService()
    job = AgentJob(
        session_id="ps-session-owned",
        task_id="task-1",
        context_meta={},
        question="q",
        user_id="owner-1",
        remember=False,
        started_at=time.monotonic(),
        cancel_event=threading.Event(),
    )
    service._jobs["ps-session-owned"] = job

    stranger = SimpleNamespace(id="stranger-2")
    with pytest.raises(PermissionError):
        service.cancel("ps-session-owned", user=stranger)
    with pytest.raises(PermissionError):
        service.get_status("ps-session-owned", user=stranger)

    owner = SimpleNamespace(id="owner-1")
    assert service.get_status("ps-session-owned", user=owner)["status"] == "running"
    assert service.cancel("ps-session-owned", user=owner) is True


@pytest.mark.asyncio
async def test_stream_reconnect_cannot_replay_another_owners_events():
    from app.services.package_search_chat_service import AgentJob, PackageSearchChatService

    service = PackageSearchChatService()
    job = AgentJob(
        session_id="private-package-session",
        task_id="private-task",
        context_meta={},
        question="secret",
        user_id="owner-1",
        remember=False,
        started_at=time.monotonic(),
        run_id="private-run",
        owner_scope="user:owner-1",
        events=[
            {
                "event": "agent_trace",
                "type": "clarification_request",
                "questions": [{"question": "secret-file.zip belongs to?"}],
            }
        ],
    )
    service._jobs[job.session_id] = job
    backing = io.BytesIO(b"ignored")
    upload = UploadFile(file=backing, filename="attacker.bin")
    stream = service.stream(
        message="",
        session_id=job.session_id,
        history_json=None,
        remember=False,
        project_repo_id=None,
        db=None,
        user=SimpleNamespace(id="attacker-2"),
        owner_scope="user:attacker-2",
        files=[upload],
    )

    with pytest.raises(PermissionError):
        await anext(stream)
    # Mismatched requests are rejected before replay. The API preflight catches
    # this before accepting files; direct service callers still own cleanup.
    await upload.close()
    assert backing.closed is True


# ───────────────────────── endpoint-level tests ──────────────────────────


@pytest.fixture
def chat_app(monkeypatch, tmp_path) -> FastAPI:
    from app.api import ai_chat as ai_chat_api
    from app.api.users import get_current_user, get_optional_user
    from app.models.database import get_db

    # Isolate the session registry under tmp_path.
    from app.services import package_search_chat_service as svc_module

    monkeypatch.setattr(
        svc_module.package_search_chat_service,
        "registry_dir",
        tmp_path / "registry",
    )
    (tmp_path / "registry").mkdir(parents=True, exist_ok=True)
    svc_module.package_search_chat_service._jobs.clear()

    application = FastAPI()
    application.include_router(ai_chat_api.router)

    user = SimpleNamespace(id="user-1", preferred_language=None)

    async def fake_db():
        yield SimpleNamespace(close=AsyncMock())

    application.dependency_overrides[get_db] = fake_db
    application.dependency_overrides[get_current_user] = lambda: user
    application.dependency_overrides[get_optional_user] = lambda: user
    return application


@pytest.fixture
def chat_client(chat_app: FastAPI) -> TestClient:
    return TestClient(chat_app)


@pytest.fixture
def fake_project_repo(monkeypatch):
    repo = SimpleNamespace(
        id=PROJECT_REPO_ID,
        project_code="proj-a",
        project_name="Project A",
        repo_url="https://git.example.com/proj-a.git",
        default_branch="main",
        enabled=True,
    )

    async def fake_get_by_id(_db, repo_id):
        return repo if repo_id == PROJECT_REPO_ID else None

    async def fake_supports_agent(_db, candidate, agent_key):
        return candidate is repo and agent_key == "package_search"

    from app.services import project_repo_service

    monkeypatch.setattr(project_repo_service, "get_by_id", fake_get_by_id)
    monkeypatch.setattr(project_repo_service, "supports_agent", fake_supports_agent)
    return repo


def test_stream_endpoint_missing_project_returns_400(chat_client):
    resp = chat_client.post(
        "/package-search/stream",
        data={"message": "最新的包？", "session_id": "ps-new-session"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "project_repo_required"


def test_stream_endpoint_accepts_repeated_component_files_without_project(
    chat_client, monkeypatch
):
    from app.services.package_search_chat_service import package_search_chat_service

    captured: Dict[str, Any] = {}

    async def fake_stream(**kwargs):
        captured["project_repo_id"] = kwargs.get("project_repo_id")
        captured["filenames"] = [item.filename for item in kwargs.get("files") or []]
        yield 'data: {"event":"done","answer":"ok"}\n\n'

    monkeypatch.setattr(package_search_chat_service, "stream", fake_stream)
    response = chat_client.post(
        "/package-search/stream",
        data={"message": "请打包", "session_id": "package-files"},
        files=[
            ("files", ("one.bin", b"one", "application/octet-stream")),
            ("files", ("two.rar", b"Rar!\x1a\x07", "application/octet-stream")),
        ],
    )

    assert response.status_code == 200
    assert captured["project_repo_id"] is None
    assert captured["filenames"] == ["one.bin", "two.rar"]


def test_three_endpoints_flow_with_mock_agent(
    chat_client, monkeypatch, tmp_path, fake_project_repo
):
    """stream → result → cancel(no-op on finished job) end-to-end flow."""
    from app.config import settings as app_settings

    monkeypatch.setattr(
        app_settings, "code_repo_clone_base_dir", str(tmp_path / "clones")
    )
    monkeypatch.setattr(
        "app.services.package_search_chat_service.PackageSearchAgent",
        FastAgent,
    )

    session_id = "ps-endpoint-session"
    with chat_client.stream(
        "POST",
        "/package-search/stream",
        data={
            "message": "最新的包？",
            "session_id": session_id,
            "remember": "false",
            "project_repo_id": str(PROJECT_REPO_ID),
        },
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())

    events = [
        _decode_sse_event(chunk + "\n\n")
        for chunk in body.split("\n\n")
        if chunk.strip()
    ]
    kinds = [event.get("event") for event in events]
    assert kinds[0] == "session"
    assert "package_search_context" in kinds
    assert "agent_trace" in kinds
    assert kinds[-1] == "done"
    done = events[-1]
    assert done["recommended_package_ids"] == ["pkg-1"]
    assert done["result"]["status"] == "ok"

    # result endpoint (polling fallback) sees the terminal snapshot.
    resp = chat_client.get(
        "/package-search/result", params={"session_id": session_id}
    )
    assert resp.status_code == 200
    snapshot = resp.json()
    assert snapshot["status"] == "done"
    assert snapshot["result"]["recommended_package_ids"] == ["pkg-1"]
    assert snapshot["project_repo_id"] == PROJECT_REPO_ID

    # cancel endpoint on a finished job reports cancelled=False.
    resp = chat_client.post(
        "/package-search/cancel", json={"session_id": session_id}
    )
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is False


def test_cancel_endpoint_enforces_ownership_with_403(chat_client):
    from app.services.package_search_chat_service import (
        AgentJob,
        package_search_chat_service,
    )

    job = AgentJob(
        session_id="ps-foreign-session",
        task_id="task-x",
        context_meta={},
        question="q",
        user_id="someone-else",
        remember=False,
        started_at=time.monotonic(),
    )
    package_search_chat_service._jobs["ps-foreign-session"] = job
    try:
        resp = chat_client.post(
            "/package-search/cancel", json={"session_id": "ps-foreign-session"}
        )
        assert resp.status_code == 403

        resp = chat_client.get(
            "/package-search/result", params={"session_id": "ps-foreign-session"}
        )
        assert resp.status_code == 403
    finally:
        package_search_chat_service._jobs.pop("ps-foreign-session", None)


def test_stream_endpoint_hides_foreign_cached_session(chat_client):
    from app.services.package_search_chat_service import AgentJob, package_search_chat_service

    job = AgentJob(
        session_id="ps-foreign-stream",
        task_id="task-x",
        context_meta={},
        question="secret",
        user_id="another-user",
        remember=False,
        started_at=time.monotonic(),
        run_id="foreign-run",
        owner_scope="user:another-user",
        events=[{"event": "done", "answer": "secret download"}],
    )
    package_search_chat_service._jobs[job.session_id] = job
    try:
        response = chat_client.post(
            "/package-search/stream",
            data={"session_id": job.session_id, "message": ""},
        )
        assert response.status_code == 404
        assert "secret download" not in response.text
    finally:
        package_search_chat_service._jobs.pop(job.session_id, None)


def test_stream_endpoint_contract_matches_project_expert():
    import inspect

    from app.api.ai_chat import package_search_stream_endpoint

    signature = inspect.signature(package_search_stream_endpoint)
    assert "files" in signature.parameters
    assert {"message", "session_id", "history", "remember", "project_repo_id"}.issubset(
        signature.parameters
    )


@pytest.mark.asyncio
async def test_stage_multiple_inputs_preserves_duplicates_and_hashes(tmp_path):
    from app.services.package_search_chat_service import PackageSearchChatService

    ctx = _make_ctx(tmp_path)
    first = b"PK\x03\x04first-zip"
    second = b"PK\x03\x04second-zip"
    uploads = [
        UploadFile(file=io.BytesIO(first), filename="../same.zip"),
        UploadFile(file=io.BytesIO(second), filename="same.zip"),
    ]

    manifest = await PackageSearchChatService()._stage_uploaded_inputs(
        ctx, uploads, run_id="run-stage"
    )

    records = manifest["inputs"]
    assert len(records) == 2
    assert [item["original_name"] for item in records] == ["same.zip", "same.zip"]
    assert len({item["upload_id"] for item in records}) == 2
    assert len({item["path"] for item in records}) == 2
    assert records[0]["sha256"] == hashlib.sha256(first).hexdigest()
    assert records[1]["sha256"] == hashlib.sha256(second).hexdigest()
    assert all(item["detected_type"] == "zip" for item in records)
    assert all(Path(item["path"]).is_relative_to(tmp_path) for item in records)
    assert Path(manifest["manifest_path"]).is_file()
    task = json.loads(Path(ctx.task_json_path).read_text(encoding="utf-8"))
    assert task["inputs_manifest"] == manifest["manifest_path"]
    assert task["input_count"] == 2


@pytest.mark.asyncio
async def test_stage_total_limit_removes_partial_turn(tmp_path, monkeypatch):
    from app.config import settings
    from app.services.package_search_chat_service import PackageSearchChatService

    monkeypatch.setattr(settings, "max_file_size", 3)
    monkeypatch.setattr(settings, "disk_reserve_bytes", 0)
    ctx = _make_ctx(tmp_path)
    uploads = [
        UploadFile(file=io.BytesIO(b"aa"), filename="one.bin"),
        UploadFile(file=io.BytesIO(b"bb"), filename="two.bin"),
    ]

    with pytest.raises(ValueError, match="总量"):
        await PackageSearchChatService()._stage_uploaded_inputs(
            ctx, uploads, run_id="run-too-large"
        )

    assert not (tmp_path / "inputs" / "run-too-large").exists()


@pytest.mark.asyncio
async def test_failed_packaging_followup_reuses_inputs_without_new_upload(monkeypatch, tmp_path):
    from app.services.package_search_chat_service import PackageSearchChatService
    from unittest.mock import AsyncMock
    ctx = _make_ctx(tmp_path)
    manifest_path = tmp_path / "input-manifest.json"
    manifest_path.write_text(json.dumps({"manifest_path": str(manifest_path), "inputs": [
        {"upload_id": "original", "original_name": "release.zip", "path": str(tmp_path / "release.zip")}
    ]}))
    meta = {"session_id": "retry-package", "project_repo_id": 7, "project_code": "proj-a",
            "last_inputs_manifest": str(manifest_path), "last_result_status": "error"}
    service = PackageSearchChatService()
    _patch_service_for_fast_run(monkeypatch, service, ctx, meta)
    monkeypatch.setattr(service, "_discover_package_projects", AsyncMock(return_value=[{"id": 7, "project_code": "proj-a"}]))
    monkeypatch.setattr(service, "_register_chat_run", AsyncMock())
    async def finish(job, ctx):
        assert job.input_manifest["inputs"][0]["upload_id"] == "original"
        assert json.loads(Path(ctx.task_json_path).read_text())["packaging_requested"] is True
        job.events.append({"event": "done", "answer": "retried"})
        job.done = True
    monkeypatch.setattr(service, "_run_job_async", finish)
    events = [_decode_sse_event(chunk) async for chunk in service.stream(
        message="请继续打包", session_id="retry-package", history_json=None, remember=False,
        project_repo_id=7, db=None, user=None,
    )]
    assert events[-1]["event"] == "done"
    assert service._jobs["retry-package"].run_id
