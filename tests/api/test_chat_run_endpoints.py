"""Integration tests for the new Chat Agent Run HTTP endpoints (task 6.7).

Covers:

- ``GET /chat/sessions/{session_id}/active-run`` — 200 with snapshot when a
  matching active run exists; 404 otherwise.
- ``GET /chat/runs/{run_id}`` — owner-scope filtered snapshot; 404 on mismatch.
- ``GET /chat/runs/{run_id}/stream`` — replay-and-follow SSE; 404 on mismatch.
- ``POST /chat/runs/{run_id}/cancel`` — cancels a running job and surfaces a
  ``cancelled`` terminal event to subscribers.

The tests pre-register :class:`ChatRunJob` instances directly in
``chat_run_service`` so we don't spin up DeviceAgent or hit the Anthropic SDK.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ai_chat as ai_chat_api
from app.api.users import get_optional_user
from app.models.database import get_db
from app.services.chat_run_service import (
    ChatRunJob,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    chat_run_service,
)


_CLIENT_SCOPE_TOKEN = "test-runs-scope"
_OWNER_SCOPE = f"anon:{_CLIENT_SCOPE_TOKEN}"


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(ai_chat_api.router)
    application.dependency_overrides[get_optional_user] = lambda: None

    async def _no_db():
        yield None

    application.dependency_overrides[get_db] = _no_db
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def clean_registry():
    chat_run_service._jobs.clear()  # noqa: SLF001
    chat_run_service._active_by_owner_session.clear()  # noqa: SLF001
    chat_run_service._brokers.clear()  # noqa: SLF001
    yield
    chat_run_service._jobs.clear()  # noqa: SLF001
    chat_run_service._active_by_owner_session.clear()  # noqa: SLF001
    chat_run_service._brokers.clear()  # noqa: SLF001


def _register_running_job(
    *,
    session_id: str,
    owner_scope: str = _OWNER_SCOPE,
    answer_so_far: str = "",
    seed_events: List[Dict] | None = None,
) -> ChatRunJob:
    run_id = str(uuid.uuid4())
    job = ChatRunJob(
        run_id=run_id,
        session_id=session_id,
        user_id=None,
        owner_scope=owner_scope,
        agent_kind="device",
        status=RUN_STATUS_RUNNING,
        started_at=time.monotonic(),
        user_message="hi",
        request_payload={},
    )
    job.answer = answer_so_far
    if seed_events:
        for ev in seed_events:
            job.append_event(ev)
    chat_run_service._jobs[run_id] = job  # noqa: SLF001
    chat_run_service._active_by_owner_session[(owner_scope, session_id)] = run_id  # noqa: SLF001
    return job


def _headers() -> Dict[str, str]:
    return {"X-Client-Scope": _CLIENT_SCOPE_TOKEN}


def _parse_sse_events(body: str) -> List[Dict]:
    out: List[Dict] = []
    for chunk in body.split("\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith("data:"):
            continue
        payload = chunk[len("data:"):].strip()
        try:
            out.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return out


# ─────────────────────── active-run ─────────────────────────────────


def test_active_run_returns_404_when_no_active_run(client, clean_registry):
    resp = client.get(
        "/chat/sessions/sess-empty/active-run", headers=_headers()
    )
    assert resp.status_code == 404


def test_active_run_returns_snapshot_when_session_has_active_run(
    client, clean_registry
):
    job = _register_running_job(session_id="sess-active")
    resp = client.get(
        "/chat/sessions/sess-active/active-run", headers=_headers()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == job.run_id
    assert body["session_id"] == "sess-active"
    assert body["status"] == RUN_STATUS_RUNNING
    assert body["agent_kind"] == "device"


def test_active_run_does_not_leak_across_owner_scopes(client, clean_registry):
    _register_running_job(
        session_id="sess-shared", owner_scope="anon:other-user"
    )
    resp = client.get(
        "/chat/sessions/sess-shared/active-run", headers=_headers()
    )
    assert resp.status_code == 404


# ─────────────────────── snapshot ───────────────────────────────────


def test_run_snapshot_returns_payload_for_owner(client, clean_registry):
    job = _register_running_job(
        session_id="sess-snap",
        answer_so_far="partial",
        seed_events=[
            {"event": "run_start", "run_id": "x", "session_id": "sess-snap"},
        ],
    )
    resp = client.get(f"/chat/runs/{job.run_id}", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == job.run_id
    assert body["session_id"] == "sess-snap"
    assert body["answer_so_far"] == "partial"
    assert isinstance(body["events"], list)
    assert len(body["events"]) >= 1


def test_run_snapshot_404_for_other_owner(client, clean_registry):
    job = _register_running_job(
        session_id="sess-priv", owner_scope="anon:other-user"
    )
    resp = client.get(f"/chat/runs/{job.run_id}", headers=_headers())
    assert resp.status_code == 404


# Note: the unknown-run snapshot fallback path queries ``chat_agent_runs``
# via the DB session injected by FastAPI; that DB-backed branch is exercised
# by the model/migration tests in ``tests/services/test_chat_history_service``
# and by integration tests that wire up a real engine. Owner-scope 404 above
# already covers the snapshot endpoint's primary access control.


# ─────────────────────── stream replay ──────────────────────────────


@pytest.mark.asyncio
async def test_replay_stream_releases_auth_connection(tmp_path, clean_registry, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from starlette.requests import Request

    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/pool.db',
                                 pool_size=1, max_overflow=0, pool_timeout=0.1)
    sessions = async_sessionmaker(engine)
    job = _register_running_job(session_id='pool-replay')
    checked = False

    async def subscribe(*args, **kwargs):
        nonlocal checked
        async with sessions() as other_browser:
            assert (await other_browser.execute(text('SELECT 1'))).scalar() == 1
        checked = True
        yield 'data: {"event":"done"}\n\n'

    monkeypatch.setattr(chat_run_service, 'subscribe', subscribe)
    request = Request({'type': 'http', 'headers': [(b'x-client-scope', _CLIENT_SCOPE_TOKEN.encode())]})
    try:
        async with sessions() as db:
            await db.execute(text('SELECT 1'))
            response = await ai_chat_api.chat_run_stream_endpoint(job.run_id, request, db, None, 'en')
            async for _ in response.body_iterator:
                pass
            assert checked
    finally:
        await engine.dispose()


def test_run_stream_replays_buffered_events_and_done(client, clean_registry):
    """Stream endpoint must replay the full buffer then emit the run's
    ``done`` frame once the job is terminal."""
    job = _register_running_job(
        session_id="sess-stream",
        seed_events=[
            {"event": "run_start", "run_id": "x", "session_id": "sess-stream"},
            {
                "event": "run_complete",
                "run_id": "x",
                "session_id": "sess-stream",
                "final_text": "all done",
            },
        ],
    )
    # Mark the job terminal + append the ``done`` frame the subscriber loop
    # expects so it can exit cleanly.
    job.answer = "all done"
    job.mark_status(RUN_STATUS_SUCCEEDED)
    job.append_event(
        {
            "event": "done",
            "run_id": job.run_id,
            "session_id": "sess-stream",
            "status": RUN_STATUS_SUCCEEDED,
            "answer": "all done",
        }
    )

    resp = client.get(
        f"/chat/runs/{job.run_id}/stream", headers=_headers()
    )
    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    event_kinds = [ev.get("event") for ev in events]
    assert "run_start" in event_kinds
    assert "run_complete" in event_kinds
    assert event_kinds[-1] == "done"
    done = events[-1]
    assert done["status"] == RUN_STATUS_SUCCEEDED
    assert done["answer"] == "all done"


def test_run_stream_404_for_other_owner(client, clean_registry):
    job = _register_running_job(
        session_id="sess-priv-stream", owner_scope="anon:other-user"
    )
    resp = client.get(
        f"/chat/runs/{job.run_id}/stream", headers=_headers()
    )
    assert resp.status_code == 404


# ─────────────────────── cancel ─────────────────────────────────────


class _StubTask:
    """asyncio.Task lookalike that records ``cancel()`` calls.

    The endpoint only invokes ``cancel()`` and ``done()`` on ``job.task``;
    we don't need a real task to exercise the HTTP layer.
    """

    def __init__(self) -> None:
        self.cancel_calls = 0
        self._done = False

    def cancel(self) -> bool:  # noqa: D401
        self.cancel_calls += 1
        return True

    def done(self) -> bool:
        return self._done


def test_cancel_running_run_signals_task_and_sets_cancel_event(
    client, clean_registry
):
    """``POST /chat/runs/{run_id}/cancel`` MUST:

    1. Return ``{"cancelled": true, "run_id": ...}``.
    2. Invoke ``task.cancel()`` exactly once.
    3. Set the job's ``cancel_event`` so any waiters wake.

    The terminal ``cancelled`` SSE frame + ``status='cancelled'`` transition
    happens in :meth:`ChatRunService._run_device_job`'s ``except`` arm and is
    covered by the service-level concurrency tests; here we only verify the
    HTTP-facing contract.
    """
    job = _register_running_job(session_id="sess-cancel")
    stub = _StubTask()
    job.task = stub  # type: ignore[assignment]

    resp = client.post(f"/chat/runs/{job.run_id}/cancel", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] is True
    assert body["run_id"] == job.run_id
    assert stub.cancel_calls == 1
    assert job.cancel_event.is_set()


def test_cancel_404_for_other_owner_leaves_job_untouched(client, clean_registry):
    """A cancel attempt with a mismatched ``owner_scope`` MUST return 404 and
    MUST NOT signal the underlying task."""
    job = _register_running_job(
        session_id="sess-other-cancel", owner_scope="anon:other-user"
    )
    stub = _StubTask()
    job.task = stub  # type: ignore[assignment]

    resp = client.post(f"/chat/runs/{job.run_id}/cancel", headers=_headers())

    assert resp.status_code == 404
    assert stub.cancel_calls == 0
    assert not job.cancel_event.is_set()
    assert job.status == RUN_STATUS_RUNNING


def test_cancel_unknown_run_returns_not_cancelled_message(client, clean_registry):
    """An unknown ``run_id`` returns 200 with ``cancelled=false`` instead of
    404 — the endpoint is idempotent for terminal/unknown ids."""
    resp = client.post(
        "/chat/runs/does-not-exist/cancel", headers=_headers()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] is False
