import asyncio
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolUseBlock

from app.agents import stage_checkpoint as sc


def terminal(**changes):
    return ResultMessage(**{
        "subtype": "success", "is_error": False, "session_id": "same-session",
        "num_turns": 3, "duration_ms": 100, "duration_api_ms": 90,
        "usage": {"input_tokens": 10, "output_tokens": 20, "output_tokens_details": {"thinking_tokens": 7}},
        "total_cost_usd": .1, "result": "", **changes,
    })


def announcement(*identities, background=False):
    return AssistantMessage(model="yinhe-thinking", content=[
        ToolUseBlock(id=i, name="Read", input={"file_path": "source.c"}) for i in identities
    ])


def data(identity, **changes):
    return {"tool_use_id": identity, "tool_name": "Read", "tool_input": {}, **changes}


@pytest.fixture(autouse=True)
def pinned_sdk():
    with patch.object(sc, "version", return_value="0.2.165"):
        yield


@pytest.fixture
def opts(tmp_path):
    return ClaudeAgentOptions(model="yinhe-thinking", system_prompt="Full original prompt and required Skills",
                              cwd=str(tmp_path), setting_sources=[], max_turns=150,
                              tools=["Bash", "Read"], allowed_tools=["Bash", "Read"],
                              env={"KEEP": "original"})


def wrapper(sdk, phases=None, check=lambda: None, threshold=2):
    return sc.make_checkpoint_query(sdk, provider="yinhe", locale="zh", check_cancel=check,
                                    on_phase=(phases if phases is not None else []).append,
                                    after_results=threshold)


async def execute_hooks(options, identities):
    pre = options.hooks["PreToolUse"][0].hooks[0]
    post = options.hooks["PostToolUse"][0].hooks[0]
    outputs = []
    for identity in identities:
        await pre(data(identity), identity, None)
    for identity in identities:
        outputs.append(await post(data(identity), identity, None))
    return outputs


@pytest.mark.asyncio
async def test_parallel_completion_dedup_waits_for_all_pending_and_bounds_state():
    checkpoint = sc.CompletionCheckpoint(2)
    checkpoint.observe(announcement("a", "b", "c"))
    assert await checkpoint.after_tool(data("a"), "a", None) == {}
    assert await checkpoint.after_tool(data("a"), "a", None) == {}
    assert await checkpoint.after_tool(data("b"), "b", None) == {}
    stopped = await checkpoint.after_tool(data("c"), "c", None)
    assert stopped["continue_"] is False and "hookSpecificOutput" not in stopped
    assert checkpoint.seen == {"a", "b", "c"} and not checkpoint.pending
    assert await checkpoint.after_tool(data("c"), "c", None) == {}


@pytest.mark.asyncio
async def test_checkpoint_resumes_original_session_tools_and_authoritative_budget(opts):
    calls, closed, phases = [], [], []
    first = terminal(num_turns=4)  # SDK turns are not the two observed tool results.
    final = terminal(num_turns=2, result="final answer", duration_ms=40, duration_api_ms=125, total_cost_usd=.2)

    async def sdk(**kw):
        calls.append(kw)
        try:
            if len(calls) == 1:
                yield announcement("a", "b")
                assert (await execute_hooks(kw["options"], ["a", "b"]))[-1]["continue_"] is False
                yield first
            else:
                yield final
        finally:
            closed.append(len(calls))

    messages = [m async for m in wrapper(sdk, phases)(prompt="original question", options=opts)]
    results = [m for m in messages if isinstance(m, ResultMessage)]
    assert len(results) == 1 and results[0].result == "final answer"
    assert results[0].num_turns == 6 and results[0].duration_ms == 140
    assert results[0].duration_api_ms == 125 and results[0].total_cost_usd == .2
    assert results[0].usage["input_tokens"] == 20
    assert results[0].usage["output_tokens_details"]["thinking_tokens"] == 14
    assert closed == [1, 2] and phases == ["finalization"]
    assert calls[0]["prompt"] == "original question"
    resumed = calls[1]["options"]
    assert resumed.resume == "same-session" and resumed.max_turns == 146
    for o in [calls[0]["options"], resumed]:
        assert o.system_prompt == opts.system_prompt and o.model == opts.model
        assert o.tools is opts.tools and o.allowed_tools is opts.allowed_tools and o.env is opts.env
        assert o.effort is opts.effort
    assert resumed.hooks is None and opts.hooks is None and opts.resume is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"hooks": {"PostToolUse": ["original"]}}, {"resume": "previous"},
    {"model": "other"}, {"max_budget_usd": 1}, {"max_turns": 1},
    {"fork_session": True}, {"continue_conversation": True},
    {"settings": '{"hooks":{}}'}, {"extra_args": {"anything": None}},
    {"setting_sources": ["user"]},
])
async def test_unsupported_options_keep_original_invocation(opts, changes):
    from dataclasses import replace
    opts = replace(opts, **changes)
    calls, end = [], terminal(result="original")

    async def sdk(**kw):
        calls.append(kw)
        yield end

    assert [m async for m in wrapper(sdk)(prompt="q", options=opts)] == [end]
    assert calls == [{"prompt": "q", "options": opts}]


def test_project_settings_with_unknown_hooks_not_selected(opts):
    from pathlib import Path
    root = Path(opts.cwd) / ".claude"
    root.mkdir()
    (root / "settings.json").write_text('{"hooks":{}}')
    assert not sc.supported(opts, "yinhe")


@pytest.mark.asyncio
async def test_background_activation_keeps_original_multiple_terminal_behavior(opts):
    calls = []
    ends = [terminal(result="first"), terminal(result="background completion")]

    async def sdk(**kw):
        calls.append(kw)
        yield announcement("a")
        pre = kw["options"].hooks["PreToolUse"][0].hooks[0]
        await pre(data("a", tool_name="Bash", tool_input={"run_in_background": True}), "a", None)
        for end in ends:
            yield end

    actual = [m async for m in wrapper(sdk)(prompt="q", options=opts)]
    assert [m for m in actual if isinstance(m, ResultMessage)] == ends
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_or_background_Bash_response_disables_checkpoint():
    checkpoint = sc.CompletionCheckpoint(1)
    checkpoint.observe(announcement("a"))
    assert await checkpoint.after_tool(data("a", tool_name="Bash", tool_response={"backgroundTaskId": "job"}), "a", None) == {}
    assert checkpoint.disabled and not checkpoint.triggered


@pytest.mark.asyncio
async def test_cancel_after_first_stage_cleanup_prevents_resume(opts):
    calls, closed, cancelled = [], [], False

    def check():
        if cancelled:
            raise asyncio.CancelledError()

    async def sdk(**kw):
        nonlocal cancelled
        calls.append(kw)
        try:
            yield announcement("a", "b")
            await execute_hooks(kw["options"], ["a", "b"])
            yield terminal()
        finally:
            closed.append(True)
            cancelled = True

    with pytest.raises(asyncio.CancelledError):
        [m async for m in wrapper(sdk, check=check)(prompt="q", options=opts)]
    assert len(calls) == 1 and closed == [True]


@pytest.mark.asyncio
async def test_cancel_from_phase_notification_prevents_final_request(opts):
    calls, cancelled = [], False

    def phase(_):
        nonlocal cancelled
        cancelled = True

    def check():
        if cancelled:
            raise asyncio.CancelledError()

    async def sdk(**kw):
        calls.append(kw)
        yield announcement("a", "b")
        await execute_hooks(kw["options"], ["a", "b"])
        yield terminal()

    run = sc.make_checkpoint_query(sdk, provider="yinhe", locale="zh", check_cancel=check,
                                   on_phase=phase, after_results=2)
    with pytest.raises(asyncio.CancelledError):
        [m async for m in run(prompt="q", options=opts)]
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [0, 150, 151, None])
async def test_invalid_first_stage_accounting_never_resumes(opts, budget):
    calls = []

    async def sdk(**kw):
        calls.append(kw)
        yield announcement("a", "b")
        await execute_hooks(kw["options"], ["a", "b"])
        yield terminal(num_turns=budget)

    with pytest.raises(RuntimeError, match="remaining session budget"):
        [m async for m in wrapper(sdk)(prompt="q", options=opts)]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_error_terminal_is_not_replayed_or_replaced(opts):
    calls = []
    failed = terminal(is_error=True, subtype="error_during_execution", result=None, errors=["failure"])

    async def sdk(**kw):
        calls.append(kw)
        yield announcement("a", "b")
        await execute_hooks(kw["options"], ["a", "b"])
        yield failed

    actual = [m async for m in wrapper(sdk)(prompt="q", options=opts)]
    assert actual[-1] is failed and len(calls) == 1
