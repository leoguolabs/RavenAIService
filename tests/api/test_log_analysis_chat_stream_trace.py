"""Integration tests for the agent_trace event stream on the chat SSE path.

These tests bypass FastAPI/TestClient and exercise ``LogAnalysisChatService``
directly (matching the existing ``tests/test_log_analysis_chat_stream.py``
pattern). They inject a fake Agent that synchronously calls the
``trace_emitter`` with a known sequence of ``AgentTraceEvent`` payloads, then
assert the SSE-shaped frames forwarded to the consumer preserve type,
order, ``seq`` monotonicity, and that the final ``done`` frame carries the
aggregated ``trace_events`` / ``trace_summary``.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.agents.log_analysis.workspace import WorkspaceContext
from app.services.log_analysis_chat_service import LogAnalysisChatService


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
        task_id="task-trace-1",
        temp_dir=str(tmp_path),
        logs_dir=str(logs_dir),
        repo_dir=str(repo_dir),
        task_json_path=str(task_json),
        metadata={"log_type": "oam_antenna", "question": "trace-please"},
    )


def _make_context_meta():
    return {
        "session_id": "session-trace-1",
        "filename": "trace_oam.tgz",
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


@pytest.mark.asyncio
async def test_idle_heartbeat_survives_suppressed_token_progress(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.agents.log_analysis.agent import _RunState, _emit_for_message
    from app.services.log_analysis_chat_service import AgentJob

    monkeypatch.setattr("app.services.log_analysis_chat_service.settings.code_repo_clone_base_dir", str(tmp_path))
    monkeypatch.setattr("app.services.log_analysis_chat_service._JOB_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr("app.services.log_analysis_chat_service._AGENT_PROGRESS_INTERVAL_SECONDS", 0)
    service = LogAnalysisChatService()
    job = AgentJob(session_id="progress", task_id="progress", context_meta={}, question="q",
                   user_id=None, remember=False, filename=None, started_at=time.monotonic())
    state = _RunState("progress", lambda event: job.events.append({"event": "agent_trace", **event}))
    for i in range(100):
        _emit_for_message(SimpleNamespace(subtype="thinking_tokens", data={"thinking_tokens": i}), state=state)
    assert job.events == []
    stream = service._subscribe(job)
    event = _decode_sse_event(await anext(stream))
    assert event["type"] == "system_notice" and event["kind"] == "heartbeat"
    job.done = True
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_emitter_events_flow_through_to_sse(monkeypatch, tmp_path):
    """The fake Agent emits a fixed sequence; the SSE stream MUST yield each
    event wrapped as ``{event: "agent_trace", ...payload}`` in original order,
    and the final ``done`` frame MUST carry ``trace_events`` + ``trace_summary``.
    """
    ctx = _make_ctx(tmp_path)
    context_meta = _make_context_meta()

    scripted_events = [
        {"type": "run_start", "seq": 1, "task_id": ctx.task_id, "timestamp": 1.0,
         "model": "fake-model", "provider": "fake"},
        {"type": "step_start", "seq": 2, "task_id": ctx.task_id, "timestamp": 1.1,
         "step_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"type": "step_delta", "seq": 3, "task_id": ctx.task_id, "timestamp": 1.2,
         "step_id": "s1", "output_chunk": "hello world"},
        {"type": "step_end", "seq": 4, "task_id": ctx.task_id, "timestamp": 1.3,
         "step_id": "s1", "status": "ok", "output_excerpt": "hello world",
         "duration_seconds": 0.2},
        {"type": "run_complete", "seq": 5, "task_id": ctx.task_id, "timestamp": 1.4,
         "trace_summary": {"thought_duration_seconds": 0.4, "tool_call_count": 1,
                           "thinking_chars": 0},
         "final_text": "done"},
    ]
    trace_summary = scripted_events[-1]["trace_summary"]

    class FakeAgent:
        def run_sync(self, _ctx, _cancel_event=None, trace_emitter=None, _clarification_binding=None):
            for event in scripted_events:
                if trace_emitter is not None:
                    trace_emitter(event)
                time.sleep(0.005)
            return {
                "engine": "claude-agent-sdk",
                "model": "fake-model",
                "status": "ok",
                "answer": "ok",
                "summary": "ok",
                "severity": "info",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
                "tool_trace": [],
                "trace_events": list(scripted_events),
                "trace_summary": trace_summary,
            }

    service = LogAnalysisChatService()
    _patch_common(monkeypatch, service, ctx, context_meta)
    monkeypatch.setattr(
        "app.services.log_analysis_chat_service.LogAnalysisAgent", FakeAgent
    )

    events: list[dict] = []
    async for chunk in service.stream(
        message="trace-please",
        session_id="session-trace-1",
        history_json=None,
        file=None,
        remember=False,
        db=None,
        user=None,
    ):
        events.append(_decode_sse_event(chunk))
        if events[-1].get("event") == "done":
            break

    # Extract just the agent_trace frames in arrival order.
    trace_frames = [
        ev for ev in events
        if ev.get("event") == "agent_trace" and ev.get("type") != "system_notice"
    ]
    assert [f.get("type") for f in trace_frames] == [
        "run_start", "step_start", "step_delta", "step_end", "run_complete",
    ], f"unexpected trace frame order: {[f.get('type') for f in trace_frames]}"

    # seq must be strictly increasing.
    seqs = [f.get("seq") for f in trace_frames]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs), "duplicate seqs detected"

    done = events[-1]
    assert done["event"] == "done"
    assert done.get("trace_summary") == trace_summary
    assert isinstance(done.get("trace_events"), list)
    assert len(done["trace_events"]) == len(scripted_events)


@pytest.mark.asyncio
async def test_reconnect_replays_trace_events_in_order(monkeypatch, tmp_path):
    """A late SSE subscriber MUST receive all previously emitted agent_trace
    frames (in original seq order) before any new events / heartbeats.
    """
    ctx = _make_ctx(tmp_path)
    context_meta = _make_context_meta()

    proceed = asyncio.Event()
    emitted_pre: list[dict] = [
        {"type": "run_start", "seq": 1, "task_id": ctx.task_id, "timestamp": 1.0,
         "model": "fake-model", "provider": "fake"},
        {"type": "thinking_start", "seq": 2, "task_id": ctx.task_id, "timestamp": 1.05,
         "step_id": "t1"},
        {"type": "thinking_delta", "seq": 3, "task_id": ctx.task_id, "timestamp": 1.10,
         "step_id": "t1", "text_chunk": "considering"},
    ]

    # The fake Agent emits the pre-events, then blocks until the test signals
    # `proceed`, then emits a `run_complete`.
    async def _proceed_signal_waiter():
        await proceed.wait()

    class GatedFakeAgent:
        def run_sync(self, _ctx, _cancel_event=None, trace_emitter=None, _clarification_binding=None):
            for event in emitted_pre:
                if trace_emitter:
                    trace_emitter(event)
            # Cross-thread wait: poll an asyncio.Event from a sync thread.
            for _ in range(500):
                if proceed.is_set():
                    break
                time.sleep(0.01)
            final = {
                "type": "run_complete",
                "seq": 4,
                "task_id": ctx.task_id,
                "timestamp": 1.5,
                "trace_summary": {"thought_duration_seconds": 0.5,
                                  "tool_call_count": 0, "thinking_chars": 11},
                "final_text": "done",
            }
            if trace_emitter:
                trace_emitter(final)
            return {
                "engine": "claude-agent-sdk",
                "model": "fake-model",
                "status": "ok",
                "answer": "ok",
                "summary": "ok",
                "severity": "info",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
                "tool_trace": [],
                "trace_events": [*emitted_pre, final],
                "trace_summary": final["trace_summary"],
            }

    service = LogAnalysisChatService()
    _patch_common(monkeypatch, service, ctx, context_meta)
    monkeypatch.setattr(
        "app.services.log_analysis_chat_service.LogAnalysisAgent",
        GatedFakeAgent,
    )

    # First subscriber: kick off the job, abandon after a few events.
    first = service.stream(
        message="why",
        session_id="session-reconnect-trace",
        history_json=None,
        file=None,
        remember=False,
        db=None,
        user=None,
    )
    consumed_first: list[dict] = []
    async for chunk in first:
        consumed_first.append(_decode_sse_event(chunk))
        if len(consumed_first) >= 4:  # session + status banner + ≥ 2 trace
            break
    await first.aclose()

    # Wait until at least all pre-events have landed in job.events.
    job = service._jobs["session-reconnect-trace"]
    for _ in range(200):
        if sum(1 for e in job.events if e.get("event") == "agent_trace") >= len(emitted_pre):
            break
        await asyncio.sleep(0.01)

    # Second subscriber: full replay.
    second = service.stream(
        message="",
        session_id="session-reconnect-trace",
        history_json=None,
        file=None,
        remember=False,
        db=None,
        user=None,
    )
    consumed_second: list[dict] = []

    async def drain():
        async for chunk in second:
            ev = _decode_sse_event(chunk)
            consumed_second.append(ev)
            if ev.get("event") == "done":
                break

    drainer = asyncio.create_task(drain())
    await asyncio.sleep(0.05)
    proceed.set()
    await asyncio.wait_for(drainer, timeout=5)

    trace_frames = [
        e for e in consumed_second
        if e.get("event") == "agent_trace" and e.get("type") != "system_notice"
    ]
    # All pre-events should appear in the replay in original seq order.
    pre_seqs = [e["seq"] for e in emitted_pre]
    replayed_seqs = [f["seq"] for f in trace_frames if f.get("seq") in pre_seqs]
    assert replayed_seqs == pre_seqs

    # Plus the run_complete that fired after reconnect.
    assert any(f.get("type") == "run_complete" for f in trace_frames)
    # done frame carries final trace.
    assert consumed_second[-1]["event"] == "done"
    assert consumed_second[-1].get("trace_summary", {}).get("thinking_chars") == 11
