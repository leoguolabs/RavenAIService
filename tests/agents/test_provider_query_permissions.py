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
