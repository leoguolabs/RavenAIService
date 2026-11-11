import pytest
from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny

from app.agents.provider_query import _claude_query


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["allow", "deny", "unknown"])
async def test_broker_decision_is_translated_for_sdk_without_mutating_options(monkeypatch, behavior):
    observed = []

    async def permission(name, arguments, context):
        assert name == "mcp__device__write"
        return {"behavior": behavior, "updatedInput": {"safe": True}, "message": "User denied"}

    async def sdk_query(*, prompt, options):
        observed.append(await options.can_use_tool("mcp__device__write", {}, None))
        if False:
            yield None

    monkeypatch.setattr("claude_agent_sdk.query", sdk_query)
    options = ClaudeAgentOptions(can_use_tool=permission)
    _ = [message async for message in _claude_query(prompt="test", options=options)]
    assert options.can_use_tool is permission
    if behavior == "allow":
        assert isinstance(observed[0], PermissionResultAllow)
        assert observed[0].updated_input == {"safe": True}
    else:
        assert isinstance(observed[0], PermissionResultDeny)
        assert observed[0].message == "User denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_claude_closes_segment_before_waiting_and_resumes_same_session(monkeypatch, cancel):
    from claude_agent_sdk import ResultMessage

    from app.agents.clarification_pause import ClarificationPause, current_pause

    pause = ClarificationPause()
    calls = []
    closed = []

    async def wait_for_answer():
        assert closed == ["original task"]
        return None if cancel else "fixture answer"

    async def sdk_query(*, prompt, options):
        calls.append((prompt, options.resume, options.max_turns))
        try:
            if options.resume is None:
                pause.pending.append(wait_for_answer)
                hook = options.hooks["PostToolUse"][-1].hooks[0]
                assert (await hook({}, "tool", {}))["continue_"] is False
            yield ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=1, session_id="fixture-session", usage={"input_tokens": 5},
            )
        finally:
            closed.append(prompt)

    monkeypatch.setattr("claude_agent_sdk.query", sdk_query)
    opts = ClaudeAgentOptions(mcp_servers={"ask": {}}, max_turns=5)
    token = current_pause.set(pause)
    try:
        messages = [message async for message in _claude_query(prompt="original task", options=opts)]
    finally:
        current_pause.reset(token)

    assert len(messages) == 1
    if cancel:
        assert calls == [("original task", None, 5)]
        assert messages[0].subtype == "raven_clarification_cancelled"
    else:
        resumed_prompt, session_id, max_turns = calls[1]
        assert "fixture answer" in resumed_prompt
        assert session_id == "fixture-session"
        assert max_turns == 4
        assert messages[0].usage["input_tokens"] == 10
