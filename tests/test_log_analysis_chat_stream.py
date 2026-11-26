from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest

from app.agents.log_analysis.workspace import WorkspaceContext
from app.services.log_analysis_chat_service import AgentJob, LogAnalysisChatService
from app.utils.chat_workspace import workspace_lock


def _decode_sse_event(chunk: str) -> dict:
    assert chunk.startswith("data: ")
    return json.loads(chunk[len("data: "):].strip())


def _make_ctx(tmp_path):
    task_json = tmp_path / "task.json"
    task_json.write_text("{}", encoding="utf-8")
    logs_dir = tmp_path / "logs"
    repo_dir = tmp_path / "repo"
    logs_dir.mkdir()
    repo_dir.mkdir()
    return WorkspaceContext(
        task_id="task-1",
        temp_dir=str(tmp_path),
        logs_dir=str(logs_dir),
        repo_dir=str(repo_dir),
        task_json_path=str(task_json),
        metadata={"log_type": "oam_antenna", "question": "why failed?"},
    )


def _make_context_meta():
    return {
        "session_id": "session-1",
        "filename": "main_oam.tgz",
        "log_type": "oam_antenna",
    }


def _patch_common(monkeypatch, service, ctx, context_meta):
    monkeypatch.setattr(service, "_load_context", lambda *_a, **_kw: (ctx, context_meta))
    monkeypatch.setattr(service, "_bind_question_and_hints", lambda *_a, **_kw: None)
    monkeypatch.setattr(service, "_touch_context", lambda *_a, **_kw: None)

    async def fake_history_hint(**_kwargs):
        return ""

    monkeypatch.setattr(service, "_build_history_hint", fake_history_hint)
    monkeypatch.setattr(
        "app.services.log_analysis_chat_service._AGENT_PROGRESS_INTERVAL_SECONDS",
        0.01,
    )


def test_format_agent_result_recovers_answer_without_raw_json():
    service = LogAnalysisChatService()
    raw = (
        "```json\n"
        "{\n"
        '  "status": "ok",\n'
        '  "question_type": "root_cause",\n'
        '  "answer": "根因是 UPF 版本缓存未失效。",\n'
        '  "recommended_actions": ["重置缓存", "审查脚本'
    )

    rendered = service._format_agent_result(
        {
            "status": "schema_mismatch",
            "model": "deepseek-v4-flash",
            "raw": raw,
            "root_cause_hypotheses": [],
            "recommended_actions": [],
            "related_keywords": [],
        },
        question="为什么重构失败？",
        context_meta={"filename": "oam.tgz"},
    )

    assert "状态：`ok`" in rendered
    assert "根因是 UPF 版本缓存未失效" in rendered
    assert "schema_mismatch" not in rendered
    assert "## 原始输出" not in rendered
    assert "```json" not in rendered


def test_bind_locale_to_task_persists_response_language_contract(tmp_path):
    from app.i18n.prompts import response_language_directive

    ctx = _make_ctx(tmp_path)

    LogAnalysisChatService._bind_locale_to_task(ctx, "zh")

    task_data = json.loads((tmp_path / "task.json").read_text(encoding="utf-8"))
    assert task_data["response_locale"] == "zh"
    assert task_data["response_language_instruction"] == response_language_directive("zh")
    assert "answer" in task_data["response_language_instruction"]


def test_attach_trigger_context_adds_ai_chat_user_snapshot():
    service = LogAnalysisChatService()
    job = AgentJob(
        session_id="session-1",
        task_id="task-1",
        context_meta=_make_context_meta(),
        question="why failed?",
        user_id="user-1",
        user_snapshot={
            "id": "user-1",
            "username": "alice",
            "display_name": "Alice",
            "email": "alice@example.com",
        },
        remember=False,
        filename="main_oam.tgz",
        started_at=time.monotonic(),
        started_at_utc="2026-06-04T01:02:03",
        run_id="run-1",
    )

    enriched = service._attach_trigger_context(
        job,
        {"status": "ok", "answer": "done"},
    )

    assert enriched["triggered_by"]["source"] == "ai_chat"
    assert enriched["triggered_by"]["run_id"] == "run-1"
    assert enriched["triggered_by"]["session_id"] == "session-1"
    assert enriched["triggered_by"]["started_at"] == "2026-06-04T01:02:03"
    assert enriched["triggered_by"]["user"]["display_name"] == "Alice"
    assert enriched["triggered_by"]["user"]["username"] == "alice"


@pytest.mark.asyncio
async def test_log_analysis_stream_sends_elapsed_status_while_agent_runs(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    context_meta = _make_context_meta()

    class FakeLogAnalysisAgent:
        def run_sync(self, _ctx, _cancel_event=None, _trace_emitter=None, _clarification_binding=None):
            time.sleep(0.05)
            return {
                "status": "ok",
                "model": "fake-model",
                "answer": "分析完成",
                "summary": "分析完成",
                "severity": "info",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
            }

    service = LogAnalysisChatService()
    _patch_common(monkeypatch, service, ctx, context_meta)
    monkeypatch.setattr(
        "app.services.log_analysis_chat_service.LogAnalysisAgent",
        FakeLogAnalysisAgent,
    )

    events = []
    async for chunk in service.stream(
        message="why failed?",
        session_id="session-1",
        history_json=None,
        file=None,
        remember=False,
        db=None,
        user=None,
    ):
        event = _decode_sse_event(chunk)
        events.append(event)
        if event.get("event") == "done":
            break

    assert any("elapsed_seconds" in event for event in events)
    assert events[-1]["event"] == "done"
    assert events[-1]["answer"]


@pytest.mark.asyncio
async def test_sse_disconnect_does_not_cancel_agent_job(monkeypatch, tmp_path):
    """If the SSE consumer stops iterating early, the Agent Job must keep
    running in the background and reach 'done' state."""
    ctx = _make_ctx(tmp_path)
    context_meta = _make_context_meta()

    agent_started = threading.Event()
    agent_finished = threading.Event()
    finish_agent = threading.Event()

    class SlowAgent:
        def run_sync(self, _ctx, cancel_event=None, trace_emitter=None, _clarification_binding=None):  # noqa: ARG002
            agent_started.set()
            finish_agent.wait(timeout=5)
            agent_finished.set()
            return {
                "status": "ok",
                "model": "fake-model",
                "answer": "迟到的分析",
                "summary": "迟到的分析",
                "severity": "info",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
            }

    service = LogAnalysisChatService()
    _patch_common(monkeypatch, service, ctx, context_meta)
    monkeypatch.setattr(
        "app.services.log_analysis_chat_service.LogAnalysisAgent",
        SlowAgent,
    )

    # Start streaming and break after seeing the first non-session event.
    stream = service.stream(
        message="why failed?",
        session_id="session-disconnect",
        history_json=None,
        file=None,
        remember=False,
        db=None,
        user=None,
    )
    seen = 0
    async for chunk in stream:
        _decode_sse_event(chunk)
        seen += 1
        if agent_started.is_set() or seen >= 5:
            break
    # Close the generator to simulate the SSE consumer abandoning the stream.
    await stream.aclose()

    for _ in range(50):
        if agent_started.is_set():
            break
        await asyncio.sleep(0.02)
    assert agent_started.is_set()

    # Job should still be in the registry and still running (or just finishing).
    job = service._jobs.get("session-disconnect")
    assert job is not None

    # The workspace remains leased by the background job after the SSE closes.
    try:
        with workspace_lock(tmp_path, exclusive=True) as locked:
            assert not locked
    finally:
        finish_agent.set()
    await asyncio.wait_for(job.task, timeout=5)
    if os.name == "posix":
        with workspace_lock(tmp_path, exclusive=True) as locked:
            assert locked

    assert job.done, "Agent task must complete even after SSE disconnect"
    assert agent_finished.is_set()
    assert job.result is not None
    assert job.answer
    assert any(event.get("event") == "done" for event in job.events)


@pytest.mark.asyncio
async def test_reconnect_subscribes_to_running_job_and_replays_events(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    context_meta = _make_context_meta()

    proceed = threading.Event()

    class GatedAgent:
        def run_sync(self, _ctx, _cancel_event=None, _trace_emitter=None, _clarification_binding=None):
            proceed.wait(timeout=5)
            return {
                "status": "ok",
                "model": "fake-model",
                "answer": "ok",
                "summary": "ok",
                "severity": "info",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
            }

    service = LogAnalysisChatService()
    _patch_common(monkeypatch, service, ctx, context_meta)
    monkeypatch.setattr(
        "app.services.log_analysis_chat_service.LogAnalysisAgent",
        GatedAgent,
    )

    # Start the first SSE; abandon after first heartbeat / status event.
    first_stream = service.stream(
        message="why failed?",
        session_id="session-reconnect",
        history_json=None,
        file=None,
        remember=False,
        db=None,
        user=None,
    )
    initial_events = []
    async for chunk in first_stream:
        initial_events.append(_decode_sse_event(chunk))
        if len(initial_events) >= 3:
            break
    await first_stream.aclose()

    # Reconnect with the same session_id and no file: should subscribe to in-flight Job.
    second_stream = service.stream(
        message="",
        session_id="session-reconnect",
        history_json=None,
        file=None,
        remember=False,
        db=None,
        user=None,
    )
    second_events: list[dict] = []

    async def drain():
        async for chunk in second_stream:
            ev = _decode_sse_event(chunk)
            second_events.append(ev)
            if ev.get("event") == "done":
                break

    drainer = asyncio.create_task(drain())
    # Release the agent so the second consumer can observe the 'done' event.
    await asyncio.sleep(0.05)
    proceed.set()
    await asyncio.wait_for(drainer, timeout=5)

    # Reconnect should have replayed at least the session + log_analysis_status banner
    # plus the new 'done' event.
    assert second_events[-1]["event"] == "done"
    assert any(e.get("event") == "log_analysis_status" for e in second_events)


@pytest.mark.asyncio
async def test_cancel_signals_agent_and_get_status_reflects_state(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    context_meta = _make_context_meta()

    captured_cancel_event: dict = {}

    class CancellableAgent:
        def run_sync(self, _ctx, cancel_event=None, _trace_emitter=None, _clarification_binding=None):
            captured_cancel_event["evt"] = cancel_event
            # Spin until cancelled.
            for _ in range(200):
                if cancel_event is not None and cancel_event.is_set():
                    break
                time.sleep(0.01)
            return {
                "engine": "claude-agent-sdk",
                "model": "fake-model",
                "schema_version": 3,
                "status": "cancelled",
                "error_kind": "cancelled",
                "question_type": "other",
                "answer": "本轮分析已被用户取消。",
                "summary": "本轮分析已被用户取消。",
                "severity": "info",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
                "tool_trace": [],
                "raw": "cancelled",
                "duration_seconds": 0.0,
                "token_usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
            }

    service = LogAnalysisChatService()
    _patch_common(monkeypatch, service, ctx, context_meta)
    monkeypatch.setattr(
        "app.services.log_analysis_chat_service.LogAnalysisAgent",
        CancellableAgent,
    )

    stream = service.stream(
        message="why failed?",
        session_id="session-cancel",
        history_json=None,
        file=None,
        remember=False,
        db=None,
        user=None,
    )
    events: list[dict] = []

    async def consume():
        async for chunk in stream:
            event = _decode_sse_event(chunk)
            events.append(event)
            if event.get("event") == "done":
                break

    consumer = asyncio.create_task(consume())

    # Wait for agent to actually start.
    for _ in range(50):
        if captured_cancel_event.get("evt") is not None:
            break
        await asyncio.sleep(0.02)
    assert captured_cancel_event.get("evt") is not None

    status_running = service.get_status("session-cancel")
    assert status_running["status"] == "running"
    assert status_running["cancel_requested"] is False

    cancelled = service.cancel("session-cancel")
    assert cancelled is True

    await asyncio.wait_for(consumer, timeout=5)

    status_done = service.get_status("session-cancel")
    assert status_done["status"] == "done"
    assert status_done["cancel_requested"] is True
    assert status_done["result"]["status"] == "cancelled"
    assert events[-1]["event"] == "done"
    assert events[-1]["result"]["status"] == "cancelled"


def test_cancel_returns_false_when_no_job():
    service = LogAnalysisChatService()
    assert service.cancel("no-such-session") is False


def test_get_status_returns_not_found_for_unknown_session():
    service = LogAnalysisChatService()
    snapshot = service.get_status("unknown")
    assert snapshot["status"] == "not_found"


def test_evict_old_jobs_removes_done_jobs_past_retention(monkeypatch):
    service = LogAnalysisChatService()
    monkeypatch.setattr(
        "app.services.log_analysis_chat_service._JOB_RETENTION_SECONDS",
        0.0,
    )
    job = AgentJob(
        session_id="stale",
        task_id="t",
        context_meta={},
        question="q",
        user_id=None,
        remember=False,
        filename=None,
        started_at=0.0,
    )
    job.done = True
    job.finished_at = 0.0
    service._jobs["stale"] = job
    service._evict_old_jobs()
    assert "stale" not in service._jobs


def _bug_fix_job():
    return AgentJob(
        session_id="session-bf",
        task_id="task-bf",
        context_meta={"log_id": "log-bf", "project_id": 5, "filename": "x.zip"},
        question="parse this config",
        user_id="u1",
        remember=False,
        filename="x.zip",
        started_at=0.0,
        result={
            "status": "ok",
            "requires_code_fix": True,
            "proposed_fixes": [{"title": "fix", "description": "d", "rationale": "r"}],
        },
    )


def test_dispatch_bug_fix_sync_delegates_with_resolved_inputs(monkeypatch):
    """The chat path resolves the source log + project_repo and forwards them
    to the shared dispatch policy (ai_analysis._maybe_dispatch_bug_fix)."""
    import app.tasks.ai_analysis as ai_analysis

    fake_log_record = object()
    fake_session = type(
        "S", (), {"get": lambda self, model, pk: fake_log_record, "close": lambda self: None}
    )()
    monkeypatch.setattr(ai_analysis, "SessionLocal", lambda: fake_session)

    calls = {}

    def _fake_dispatch(session, **kwargs):
        calls["session"] = session
        calls.update(kwargs)

    monkeypatch.setattr(ai_analysis, "_maybe_dispatch_bug_fix", _fake_dispatch)

    job = _bug_fix_job()
    LogAnalysisChatService._dispatch_bug_fix_sync(job)

    assert calls["session"] is fake_session
    assert calls["analysis_result"] is job.result
    assert calls["log_record"] is fake_log_record
    assert calls["analysis_task_id"] == "task-bf"
    assert calls["project_repo_id"] == 5


def test_maybe_dispatch_bug_fix_swallows_errors(monkeypatch):
    service = LogAnalysisChatService()
    # Flag must be on (and signal present) for the dispatch to reach the thread.
    monkeypatch.setattr(
        "app.services.log_analysis_chat_service.settings.bug_fix_auto_dispatch",
        True,
    )

    def _boom(_job):
        raise RuntimeError("broker down")

    monkeypatch.setattr(service, "_dispatch_bug_fix_sync", _boom)
    # Must not raise — the answer is already persisted.
    asyncio.run(service._maybe_dispatch_bug_fix(_bug_fix_job()))


def test_maybe_dispatch_bug_fix_skips_when_no_result():
    service = LogAnalysisChatService()
    job = _bug_fix_job()
    job.result = None
    called = {"n": 0}
    # _dispatch_bug_fix_sync must not be invoked for a missing/invalid result.
    service._dispatch_bug_fix_sync = lambda _j: called.__setitem__("n", called["n"] + 1)  # type: ignore[assignment]
    asyncio.run(service._maybe_dispatch_bug_fix(job))
    assert called["n"] == 0
