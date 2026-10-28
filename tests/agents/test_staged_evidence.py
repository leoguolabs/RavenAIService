"""Behavioral boundaries for the opt-in same-session SDK experiment."""
import asyncio
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, SystemMessage, TextBlock

from app.agents import staged_evidence as se


def options(**changes):
    return ClaudeAgentOptions(**{
        "model": "deepseek-flash[1m]", "system_prompt": "Original contract",
        "env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic", "KEEP": "value"},
        "max_turns": 150, **changes,
    })


def result(**changes):
    return ResultMessage(**{
        "subtype": "success", "is_error": False, "session_id": "same-session",
        "num_turns": 3, "duration_ms": 100, "duration_api_ms": 90,
        "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30},
        "total_cost_usd": 0.1, "result": "ready", **changes,
    })


@pytest.fixture(autouse=True)
def verified_sdk():
    with patch.object(se, "version", return_value="0.2.165"):
        yield


def wrap(query, phases=None, check=lambda: None, error=lambda text: False):
    return se.make_staged_query(
        query, locale="zh", check_cancel=check,
        on_phase=(phases if phases is not None else []).append, is_analysis_error=error,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", ["ready", '{"stage":"evidence_ready"}', '{"status":"ok","answer":"draft"}'])
async def test_same_session_original_tools_budget_and_only_final_result(draft):
    calls, phases, closed = [], [], []
    first = result(result=draft)
    final = result(num_turns=2, result="final answer", duration_ms=40, duration_api_ms=125, total_cost_usd=0.15)
    opts = options(hooks={"PostToolUse": ["original-hook"]}, mcp_servers={"evidence": "original-server"})
    assistant = AssistantMessage(content=[TextBlock("working")], model="deepseek-flash")

    async def sdk(**kwargs):
        calls.append(kwargs)
        try:
            yield assistant
            yield first if len(calls) == 1 else final
        finally:
            closed.append(len(calls))

    messages = [m async for m in wrap(sdk, phases)(prompt="original question", options=opts)]
    assert len(calls) == 2 and closed == [1, 2]
    assert phases == ["collection", "finalization"]
    assert messages[:2] == [assistant, assistant]
    merged = messages[-1]
    assert merged.result == "final answer" and merged.num_turns == 5
    assert merged.usage["input_tokens"] == 20 and merged.usage["output_tokens"] == 40
    assert merged.usage["cache_read_input_tokens"] == 60
    assert merged.duration_ms == 140 and merged.duration_api_ms == 125 and merged.total_cost_usd == 0.15
    a, b = [c["options"] for c in calls]
    assert a.resume is None and b.resume == first.session_id
    assert a.max_turns == 149 and b.max_turns == 147
    assert a.effort == "low" and b.effort == "high"
    assert a.env["CLAUDE_CODE_EFFORT_LEVEL"] == "low" and b.env["CLAUDE_CODE_EFFORT_LEVEL"] == "high"
    for o in (a, b):
        assert o.mcp_servers is opts.mcp_servers and o.hooks is opts.hooks
        assert o.model == opts.model and o.system_prompt == a.system_prompt
        assert o.env["KEEP"] == "value"
    assert "original question" in calls[0]["prompt"]
    assert calls[1]["prompt"].startswith("RAVEN_STAGE_FINAL")
    assert opts.resume is None and opts.effort is None and "CLAUDE_CODE_EFFORT_LEVEL" not in opts.env


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"model": "other"}, {"model": "deepseek-flash"},
    {"env": {"ANTHROPIC_BASE_URL": "https://unverified.invalid/anthropic"}},
    {"resume": "existing"}, {"continue_conversation": True}, {"fork_session": True},
    {"max_budget_usd": 1}, {"max_turns": 1}, {"max_turns": None},
    {"system_prompt": {"type": "preset", "preset": "claude_code"}},
])
async def test_unverified_options_pass_through_unchanged(changes):
    calls, phases = [], []
    terminal, opts = result(), options(**changes)

    async def sdk(**kwargs):
        calls.append(kwargs)
        yield terminal

    assert [m async for m in wrap(sdk, phases)(prompt="original", options=opts)] == [terminal]
    assert calls == [{"prompt": "original", "options": opts}]
    assert phases == ["original"]  # Clears collection state after endpoint failover.


@pytest.mark.asyncio
async def test_unverified_sdk_is_not_silently_opted_in():
    calls = []

    async def sdk(**kwargs):
        calls.append(kwargs)
        yield result()

    with patch.object(se, "version", return_value="0.2.166"):
        [m async for m in wrap(sdk)(prompt="original", options=options())]
    assert len(calls) == 1 and calls[0]["prompt"] == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_error", [True, False])
async def test_sdk_error_keeps_terminal_fields_without_replay(first_error):
    calls = []
    failed = result(is_error=True, subtype="error_during_execution", api_error_status=402, errors=["API rejection"], result=None)

    async def sdk(**kwargs):
        calls.append(kwargs)
        yield failed if first_error or len(calls) == 2 else result()

    messages = [m async for m in wrap(sdk)(prompt="original", options=options())]
    assert len(calls) == (1 if first_error else 2)
    assert len(messages) == 1 and messages[0].is_error
    assert messages[0].api_error_status == 402 and messages[0].errors == failed.errors
    assert messages[0].result is None


@pytest.mark.asyncio
async def test_analysis_error_does_not_start_another_phase():
    calls = []
    terminal = result(result='{"status":"error","error_kind":"missing_project_identity"}')

    async def sdk(**kwargs):
        calls.append(kwargs)
        yield terminal

    assert [m async for m in wrap(sdk, error=lambda s: s == terminal.result)(prompt="q", options=options())] == [terminal]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_after_collection_cleanup_never_starts_finalization():
    cancelled, calls, closed = False, [], []

    def check():
        if cancelled:
            raise asyncio.CancelledError

    async def sdk(**kwargs):
        nonlocal cancelled
        calls.append(kwargs)
        try:
            yield result()
        finally:
            closed.append(True)
            cancelled = True

    with pytest.raises(asyncio.CancelledError):
        [m async for m in wrap(sdk, check=check)(prompt="q", options=options())]
    assert len(calls) == 1 and closed == [True]


@pytest.mark.asyncio
async def test_outer_cancellation_reaps_active_sdk_iterator():
    started, closed = asyncio.Event(), asyncio.Event()

    async def sdk(**kwargs):
        try:
            yield SystemMessage(subtype="init", data={})
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def consume():
        return [m async for m in wrap(sdk)(prompt="q", options=options())]

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing", "duplicate", "wrong_session", "bad_turns"])
async def test_invalid_terminal_never_promotes_collection_draft(bad):
    calls = []

    async def sdk(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            yield result(result="draft")
        elif bad != "missing":
            yield result(session_id="wrong" if bad == "wrong_session" else "same-session", num_turns=999 if bad == "bad_turns" else 1)
            if bad == "duplicate":
                yield result()

    messages = []
    with pytest.raises(RuntimeError):
        async for m in wrap(sdk)(prompt="q", options=options()):
            messages.append(m)
    assert not any(isinstance(m, ResultMessage) for m in messages) and len(calls) == 2


@pytest.mark.asyncio
async def test_collection_at_its_cap_leaves_exactly_one_final_turn():
    calls = []

    async def sdk(**kwargs):
        calls.append(kwargs)
        yield result(num_turns=149 if len(calls) == 1 else 1)

    messages = [m async for m in wrap(sdk)(prompt="q", options=options())]
    assert [c["options"].max_turns for c in calls] == [149, 1]
    assert messages[-1].num_turns == 150


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_router_keeps_rejection_failover_and_never_replays_committed_stage(committed):
    from app.agents import routed_query as rq
    from app.agents.anthropic_client import PROVIDER_PROFILES
    from app.services.model_router import EndpointChoice
    from app.config import settings

    endpoints = [EndpointChoice(
        slot=slot, provider="deepseek", base_url="https://api.deepseek.com/anthropic",
        api_key="fake", model="deepseek-flash", small_fast_model=None,
        profile=PROVIDER_PROFILES["deepseek"],
    ) for slot in ("primary", "backup")]
    calls, phases = [], []

    async def sdk(**kwargs):
        calls.append((kwargs["options"].env["SLOT"], kwargs["options"].resume))
        if calls[-1][0] == "primary":
            if committed and kwargs["options"].resume is None:
                yield AssistantMessage(content=[TextBlock("Evidence collected")], model="deepseek-flash")
                yield result()
                return
            yield AssistantMessage(content=[TextBlock("API Error: 402 payment required")], model="deepseek-flash", error="billing_error")
            yield result(is_error=True, subtype="error_during_execution", api_error_status=402, result="API Error: 402 payment required")
        else:
            yield result(result="backup answer")

    def make_options(endpoint):
        return options(env={"ANTHROPIC_BASE_URL": endpoint.base_url, "SLOT": endpoint.slot},
                       model="deepseek-flash[1m]" if endpoint.slot == "primary" else "other")

    with patch.object(settings, "model_router_first_token_deadline_ms", 0), \
         patch.object(rq.model_router, "record_outcome"):
        messages = [m async for m in rq.routed_query(
            prompt="q", make_options=make_options, agent_kind="log_analysis",
            candidates=endpoints, sdk_query=wrap(sdk, phases),
        )]
    if committed:
        assert calls == [("primary", None), ("primary", "same-session")]
        assert not any(isinstance(m, rq.EndpointSwitchNotice) for m in messages)
    else:
        assert calls == [("primary", None), ("backup", None)]
        assert any(isinstance(m, rq.EndpointSwitchNotice) for m in messages)
        assert phases == ["collection", "original"]
        assert messages[-1].result == "backup answer"
