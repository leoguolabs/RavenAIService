"""Integration tests for LogAnalysisAgent's trace_emitter contract.

Verifies the new ``trace_emitter`` parameter plumbed through ``run()`` /
``run_sync()`` plus the side-effect on the returned result dict (new
``trace_events`` and ``trace_summary`` fields, legacy ``tool_trace``
derived from the event stream).
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────── Fake SDK Message Types ────────────────────


@dataclass
class FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeThinkingBlock:
    thinking: str
    type: str = "thinking"


@dataclass
class FakeToolUseBlock:
    name: str
    input: Any
    id: Optional[str] = None
    type: str = "tool_use"


@dataclass
class FakeToolResultBlock:
    tool_use_id: str
    content: Any
    is_error: bool = False
    type: str = "tool_result"


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeContentMessage:
    content: List[Any] = field(default_factory=list)
    usage: Optional[FakeUsage] = None


@dataclass
class FakeSystemMessage:
    subtype: str
    data: Dict[str, Any]


@dataclass
class FakeResultMessage:
    result: str
    num_turns: int = 1
    stop_reason: str = "end_turn"


def _make_good_result_json() -> str:
    payload = {
        "status": "ok",
        "question_type": "qa",
        "answer": "All good",
        "summary": "Summary text",
        "severity": "info",
        "root_cause_hypotheses": [],
        "recommended_actions": [],
        "related_keywords": [],
    }
    return f"```json\n{json.dumps(payload)}\n```"


# ─────────────────────── Fixtures ──────────────────────────────────


@pytest.fixture
def workspace_ctx():
    import os
    import tempfile
    import json as _json
    from app.agents.log_analysis.workspace import WorkspaceContext

    tmp = tempfile.mkdtemp()
    task_json = os.path.join(tmp, "task.json")
    _json.dump(
        {"log_id": 1, "question": "What failed?", "log_type": "generic", "hints": ""},
        open(task_json, "w"),
    )
    os.makedirs(os.path.join(tmp, "repo"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "logs"), exist_ok=True)

    ctx = WorkspaceContext(
        task_id="trace-test-task",
        temp_dir=tmp,
        logs_dir=os.path.join(tmp, "logs"),
        repo_dir=os.path.join(tmp, "repo"),
        task_json_path=task_json,
    )
    ctx.metadata = {"log_type": "generic", "question": "What failed?"}
    yield ctx

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def _patch_settings(provider: str = "deepseek"):
    return patch(
        "app.config.settings",
        MagicMock(
            anthropic_model="deepseek-v4-pro",
            anthropic_provider=provider,
            anthropic_request_timeout_seconds=3600,
        ),
    )


def _patch_environment(
    *,
    loaded_skills: Optional[List[str]] = None,
    available_skills: Optional[List[str]] = None,
):
    """Patch all the side-effect modules we don't want exercised in unit tests."""
    return [
        patch("app.agents.anthropic_client.build_options", return_value=MagicMock()),
        patch("app.agents.log_analysis.mcp_tools.get_mcp_server", return_value=MagicMock()),
        patch(
            "app.agents.log_analysis.prompts.get_prompts",
            return_value=(
                "system prompt",
                "Question: {question} log_type: {log_type} task_id: {task_id} hints: {hints}",
            ),
        ),
        patch(
            "app.services.skills_service.materialize_relevant_enabled_skills",
            return_value=list(loaded_skills or []),
        ),
        patch(
            "app.services.skills_service.enabled_skill_overviews",
            return_value=[
                {"name": n, "description": ""}
                for n in (available_skills or loaded_skills or [])
            ],
        ),
        patch("app.services.model_router.candidates", return_value=[]),
    ]


def _run_agent(
    workspace_ctx,
    fake_query,
    *,
    emitter=None,
    cancel_event=None,
    loaded_skills=None,
    available_skills=None,
):
    """Run LogAnalysisAgent.run synchronously, with all SDK dependencies mocked."""
    from app.agents.log_analysis.agent import LogAnalysisAgent

    fake_sdk = MagicMock()
    fake_sdk.query = fake_query

    patches = _patch_environment(
        loaded_skills=loaded_skills,
        available_skills=available_skills,
    ) + [
        patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}),
        _patch_settings(),
    ]
    contexts = [p.__enter__() for p in patches]
    try:
        return asyncio.run(
            LogAnalysisAgent().run(
                workspace_ctx,
                cancel_event=cancel_event,
                trace_emitter=emitter,
            )
        )
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)


# ─────────────────────── Tests ─────────────────────────────────────


class TestEmitterEventSequence:
    def test_run_start_run_complete_bookend(self, workspace_ctx):
        async def fake_query(*args, **kwargs):
            yield FakeResultMessage(result=_make_good_result_json())

        captured: List[Dict[str, Any]] = []
        result = _run_agent(workspace_ctx, fake_query, emitter=captured.append)

        assert result["status"] == "ok"
        assert captured[0]["type"] == "run_start"
        assert captured[0]["model"] == "deepseek-v4-pro"
        assert captured[-1]["type"] == "run_complete"
        assert "trace_summary" in captured[-1]

    def test_dict_usage_payload_is_accumulated(self, workspace_ctx):
        async def fake_query(*args, **kwargs):
            yield FakeContentMessage(
                content=[FakeTextBlock(_make_good_result_json())],
                usage={
                    "prompt_tokens": 41,
                    "completion_tokens": 13,
                    "cache_read_input_tokens": 17,
                    "cache_creation_input_tokens": 3,
                },
            )
            yield FakeResultMessage(result=_make_good_result_json())

        result = _run_agent(workspace_ctx, fake_query)

        assert result["token_usage"] == {
            "input_tokens": 41,
            "output_tokens": 13,
            "cache_read_tokens": 17,
            "cache_write_tokens": 3,
        }

    def test_full_available_catalog_is_separate_from_materialized_skills(
        self, workspace_ctx
    ):
        captured_prompt: Dict[str, str] = {}

        async def fake_query(*args, **kwargs):
            captured_prompt["prompt"] = kwargs.get("prompt") or (args[0] if args else "")
            yield FakeResultMessage(result=_make_good_result_json())

        captured: List[Dict[str, Any]] = []
        result = _run_agent(
            workspace_ctx,
            fake_query,
            emitter=captured.append,
            loaded_skills=["ka-phased-array-antenna"],
            available_skills=[
                "ka-phased-array-antenna",
                "payload-management-unit",
                "tcpt027-db-modify",
            ],
        )

        assert result["loaded_skills"] == ["ka-phased-array-antenna", "humanizer-zh"]
        assert captured[0]["type"] == "run_start"
        assert captured[0]["available_skills"] == [
            "ka-phased-array-antenna",
            "payload-management-unit",
            "tcpt027-db-modify",
            "humanizer-zh",
        ]
        assert captured[0]["loaded_skills"] == ["ka-phased-array-antenna", "humanizer-zh"]
        skill_events = [
            ev for ev in captured
            if ev["type"] == "system_notice" and ev.get("kind") == "skills_available"
        ]
        assert len(skill_events) == 1
        assert skill_events[0]["available_skills"] == [
            "ka-phased-array-antenna",
            "payload-management-unit",
            "tcpt027-db-modify",
            "humanizer-zh",
        ]
        assert skill_events[0]["loaded_skills"] == ["ka-phased-array-antenna", "humanizer-zh"]
        assert "可用的 Skill（按需加载）" in captured_prompt["prompt"]
        assert '"skill": "ka-phased-array-antenna"' in captured_prompt["prompt"]
        assert ".claude/skills/ka-phased-array-antenna/" in captured_prompt["prompt"]
        assert "payload-management-unit" not in captured_prompt["prompt"]
        assert "tcpt027-db-modify" not in captured_prompt["prompt"]
        assert "最终输出仍必须遵守第 6 步的围栏 JSON schema" in captured_prompt["prompt"]

    def test_seq_strictly_monotonic(self, workspace_ctx):
        async def fake_query(*args, **kwargs):
            yield FakeContentMessage(content=[FakeToolUseBlock(name="Bash", input={"command": "ls"}, id="tu1")])
            yield FakeContentMessage(content=[FakeToolResultBlock(tool_use_id="tu1", content="file1\nfile2")])
            yield FakeResultMessage(result=_make_good_result_json())

        captured: List[Dict[str, Any]] = []
        _run_agent(workspace_ctx, fake_query, emitter=captured.append)

        seqs = [ev["seq"] for ev in captured]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))

    def test_step_id_shared_across_start_delta_end(self, workspace_ctx):
        async def fake_query(*args, **kwargs):
            yield FakeContentMessage(
                content=[FakeToolUseBlock(name="Bash", input={"command": "ls"}, id="tu1")]
            )
            yield FakeContentMessage(
                content=[FakeToolResultBlock(tool_use_id="tu1", content="some output")]
            )
            yield FakeResultMessage(result=_make_good_result_json())

        captured: List[Dict[str, Any]] = []
        _run_agent(workspace_ctx, fake_query, emitter=captured.append)

        steps = [ev for ev in captured if ev["type"] in {"step_start", "step_delta", "step_end"}]
        step_ids = {ev["step_id"] for ev in steps}
        assert len(step_ids) == 1

        # Order constraint: step_end.seq > all step_delta.seq > step_start.seq
        starts = [ev for ev in steps if ev["type"] == "step_start"]
        ends = [ev for ev in steps if ev["type"] == "step_end"]
        deltas = [ev for ev in steps if ev["type"] == "step_delta"]
        assert starts[0]["seq"] < deltas[0]["seq"] if deltas else True
        assert ends[0]["seq"] > max((d["seq"] for d in deltas), default=starts[0]["seq"])

    def test_thinking_emitted_for_thinking_block(self, workspace_ctx):
        async def fake_query(*args, **kwargs):
            yield FakeContentMessage(
                content=[FakeThinkingBlock(thinking="Let me think about this...")]
            )
            yield FakeResultMessage(result=_make_good_result_json())

        captured: List[Dict[str, Any]] = []
        _run_agent(workspace_ctx, fake_query, emitter=captured.append)

        thinking_events = [ev for ev in captured if ev["type"].startswith("thinking")]
        assert any(ev["type"] == "thinking_start" for ev in thinking_events)
        assert any(ev["type"] == "thinking_end" for ev in thinking_events)


def test_empty_token_progress_does_not_change_answer_or_evidence(workspace_ctx):
    """A token-progress flood must not occupy replay/storage or lose content."""
    def query_with_progress(count):
        async def fake_query(*args, **kwargs):
            yield FakeSystemMessage(subtype="init", data={})
            for i in range(count):
                yield FakeSystemMessage(subtype="thinking_tokens", data={"thinking_tokens": i})
            yield FakeContentMessage(
                content=[FakeThinkingBlock(thinking="检查日志中的反例。")],
                usage=FakeUsage(input_tokens=19, output_tokens=7),
            )
            yield FakeContentMessage(content=[FakeToolUseBlock(name="Read", input={"file_path": "logs/a.log"}, id="tu1")])
            yield FakeContentMessage(content=[FakeToolResultBlock(tool_use_id="tu1", content="line 1: observed")])
            yield FakeResultMessage(result=_make_good_result_json())
        return fake_query

    results = [_run_agent(workspace_ctx, query_with_progress(n)) for n in (0, 42683)]
    baseline, flooded = results
    for key in ("status", "answer", "summary", "raw", "token_usage", "tool_trace"):
        assert flooded[key] == baseline[key]
    assert flooded["trace_summary"]["tool_call_count"] == 1
    assert flooded["trace_summary"]["thinking_chars"] == baseline["trace_summary"]["thinking_chars"] > 0
    # IDs/times vary across executions; every meaningful payload stays identical.
    volatile = {"seq", "timestamp", "step_id", "duration_seconds", "trace_summary"}
    def payloads(result):
        return [{k: v for k, v in event.items() if k not in volatile} for event in result["trace_events"]]
    assert payloads(flooded) == payloads(baseline)
    assert [e["seq"] for e in flooded["trace_events"]] == list(range(1, len(flooded["trace_events"]) + 1))


@pytest.mark.parametrize("field", ["summary", "description", "message"])
def test_descriptive_token_progress_and_other_system_notices_are_preserved(field):
    from app.agents.log_analysis.agent import _RunState, _emit_for_message

    state = _RunState(task_id="notices", emitter=None)
    for subtype, data in [
        ("thinking_tokens", {field: "progress at https://secret@example.test/repo"}),
        ("status", {}),
        ("compact_boundary", {}),
        ("endpoint_switch", {"message": "backup endpoint selected"}),
    ]:
        _emit_for_message(FakeSystemMessage(subtype=subtype, data=data), state=state)
    assert [e["subtype"] for e in state.trace_events] == ["thinking_tokens", "status", "compact_boundary", "endpoint_switch"]
    assert state.trace_events[0]["detail"] == "progress at https://***@example.test/repo"
    assert state.trace_events[-1]["detail"] == "backup endpoint selected"


class TestTraceMaskingAndPersistence:
    def test_tokens_masked_in_step_start_input(self, workspace_ctx):
        async def fake_query(*args, **kwargs):
            yield FakeContentMessage(
                content=[
                    FakeToolUseBlock(
                        name="Bash",
                        input={"command": "git clone https://abc123@gitlab.example/repo.git x"},
                        id="tu1",
                    )
                ]
            )
            yield FakeContentMessage(content=[FakeToolResultBlock(tool_use_id="tu1", content="ok")])
            yield FakeResultMessage(result=_make_good_result_json())

        captured: List[Dict[str, Any]] = []
        _run_agent(workspace_ctx, fake_query, emitter=captured.append)

        step_start = next(ev for ev in captured if ev["type"] == "step_start")
        flat = json.dumps(step_start)
        assert "abc123" not in flat
        assert "***" in flat

    def test_result_dict_has_trace_events_and_summary(self, workspace_ctx):
        async def fake_query(*args, **kwargs):
            yield FakeContentMessage(content=[FakeToolUseBlock(name="Read", input={"path": "/x"}, id="tu1")])
            yield FakeContentMessage(content=[FakeToolResultBlock(tool_use_id="tu1", content="contents")])
            yield FakeResultMessage(result=_make_good_result_json())

        result = _run_agent(workspace_ctx, fake_query)

        assert "trace_events" in result
        assert "trace_summary" in result
        assert isinstance(result["trace_events"], list) and result["trace_events"]
        assert result["trace_summary"]["tool_call_count"] == 1
        assert result["trace_events"][0]["type"] == "run_start"
        assert result["trace_events"][-1]["type"] == "run_complete"

    def test_tool_trace_derived_from_events(self, workspace_ctx):
        async def fake_query(*args, **kwargs):
            yield FakeContentMessage(
                content=[FakeToolUseBlock(name="Bash", input={"command": "ls"}, id="tu1")]
            )
            yield FakeContentMessage(
                content=[FakeToolResultBlock(tool_use_id="tu1", content="file1\nfile2")]
            )
            yield FakeResultMessage(result=_make_good_result_json())

        result = _run_agent(workspace_ctx, fake_query)

        assert len(result["tool_trace"]) == 1
        entry = result["tool_trace"][0]
        assert entry["name"] == "Bash"
        assert "file1" in entry["output_excerpt"]


class TestEmitterFaultIsolation:
    def test_emitter_exception_does_not_break_run(self, workspace_ctx):
        async def fake_query(*args, **kwargs):
            yield FakeContentMessage(content=[FakeToolUseBlock(name="Read", input={"path": "/x"}, id="tu1")])
            yield FakeContentMessage(content=[FakeToolResultBlock(tool_use_id="tu1", content="data")])
            yield FakeResultMessage(result=_make_good_result_json())

        def bad_emitter(_event):
            raise RuntimeError("emitter exploded")

        result = _run_agent(workspace_ctx, fake_query, emitter=bad_emitter)

        # Run still completed normally despite the emitter raising on every event.
        assert result["status"] == "ok"
        # trace_events are still accumulated internally even when emitter explodes.
        assert result["trace_events"]
        assert result["trace_summary"]["tool_call_count"] == 1


class TestCancelTwoPhase:
    @pytest.mark.parametrize("during_empty_progress", [False, True])
    def test_cancel_sends_cancel_requested_then_cancelled(self, workspace_ctx, during_empty_progress):
        cancel_event = threading.Event()

        async def fake_query(*args, **kwargs):
            yield FakeContentMessage(
                content=[FakeToolUseBlock(name="Bash", input={"command": "sleep 999"}, id="tu1")]
            )
            cancel_event.set()
            # Subsequent message would arrive but cancel check happens first.
            if during_empty_progress:
                yield FakeSystemMessage(subtype="thinking_tokens", data={"thinking_tokens": 1})
            else:
                yield FakeContentMessage(content=[FakeToolResultBlock(tool_use_id="tu1", content="never")])
            yield FakeResultMessage(result=_make_good_result_json())

        captured: List[Dict[str, Any]] = []
        result = _run_agent(workspace_ctx, fake_query, emitter=captured.append, cancel_event=cancel_event)

        assert result["status"] == "cancelled"

        # Find the cancel_requested system_notice and the terminal cancelled event.
        cancel_requested = [
            ev for ev in captured
            if ev["type"] == "system_notice" and ev.get("kind") == "cancel_requested"
        ]
        cancelled = [ev for ev in captured if ev["type"] == "cancelled"]
        assert len(cancel_requested) == 1
        assert len(cancelled) == 1
        # cancel_requested precedes cancelled
        assert cancel_requested[0]["seq"] < cancelled[0]["seq"]
        # Nothing after the cancelled terminal event.
        assert captured[-1]["type"] == "cancelled"

    def test_cancel_result_dict_has_trace_summary(self, workspace_ctx):
        cancel_event = threading.Event()

        async def fake_query(*args, **kwargs):
            yield FakeContentMessage(
                content=[FakeToolUseBlock(name="Read", input={"path": "x"}, id="tu1")]
            )
            cancel_event.set()
            yield FakeContentMessage(content=[FakeToolResultBlock(tool_use_id="tu1", content="x")])

        result = _run_agent(workspace_ctx, fake_query, cancel_event=cancel_event)
        assert result["status"] == "cancelled"
        assert "trace_summary" in result
        assert "trace_events" in result


class TestNoEmitterBackwardCompat:
    def test_omitting_emitter_still_returns_full_result(self, workspace_ctx):
        async def fake_query(*args, **kwargs):
            yield FakeContentMessage(content=[FakeToolUseBlock(name="Read", input={"path": "/x"}, id="tu1")])
            yield FakeContentMessage(content=[FakeToolResultBlock(tool_use_id="tu1", content="data")])
            yield FakeResultMessage(result=_make_good_result_json())

        # No emitter passed — must still build trace_events internally.
        result = _run_agent(workspace_ctx, fake_query, emitter=None)
        assert result["status"] == "ok"
        assert result["trace_events"]
        assert result["tool_trace"]
