import httpx
import pytest
from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, create_sdk_mcp_server, tool

from app.agents.antigravity_control import Control, control_bridge
from app.agents.antigravity_executor import EventAdapter


def test_stream_deduplicates_steps_and_keeps_final_failure():
    adapter = EventAdapter("selected")
    adapter.messages({"event": "init", "conversation_id": "one", "init": {}})
    active = {"event": "step_update", "step_update": {"step_index": 2, "step_type": "tool", "state": "ACTIVE",
        "tool_info": {"name": "run_command", "parameters": {"CommandLine": "cat logs.txt"}}}}
    assert adapter.messages(active)[0].content[0].name == "Bash"
    assert not adapter.messages(active)
    active["step_update"].update(state="DONE")
    active["step_update"]["tool_info"]["output"] = "verified output"
    assert adapter.messages(active)[0].content[0].content == "verified output"
    assert not adapter.messages(active)
    terminal = adapter.messages({"event": "result", "result": {"status": "ERROR", "error": "permission denied"}})[0]
    assert terminal.is_error
    assert terminal.errors == ["permission denied"]


def test_skill_activation_and_incremental_text():
    adapter = EventAdapter("selected")
    event = {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "tool",
        "tool_info": {"name": "view_file", "parameters": {"AbsolutePath": "/workspace/.agents/skills/demo/SKILL.md"}, "output": "instructions"}}}
    assert adapter.messages(event)[0].content[0].input["skill"] == "demo"
    first = adapter.messages({"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "first "}})
    last = adapter.messages({"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "agent_response", "text_delta": "last"}})
    assert first[0].event["delta"]["text"] == "first "
    assert last[-1].content[0].text == "first last"


@pytest.mark.asyncio
async def test_real_memory_mcp_bridge_enforces_denial_and_post_hooks():
    called = []

    @tool("read", "Read a fixture", {"value": str})
    async def read(args):
        called.append(args)
        return {"content": [{"type": "text", "text": args["value"]}]}

    async def can_use(name, args, context):
        assert name == "mcp__fixture__read"
        return {"behavior": "deny" if args["value"] == "denied" else "allow"}

    async def post(data, tool_id, context):
        return {"hookSpecificOutput": {"updatedMCPToolOutput": {"content": [{"type": "text", "text": "validated"}]}}}

    options = ClaudeAgentOptions(cwd="/tmp", mcp_servers={"fixture": create_sdk_mcp_server("fixture", tools=[read])},
        can_use_tool=can_use, hooks={"PostToolUse": [HookMatcher(hooks=[post])]})
    async with control_bridge(options) as (control, url, token):
        startup = await control.native_hook("PreInvocation", {"invocationNum": 0})
        catalog = startup["injectSteps"][0]["systemMessage"]["systemMessage"]
        assert '"ServerName": "fixture"' in catalog and '"ToolName": "read"' in catalog
        assert '"inputSchema"' in catalog and 'call_mcp_tool directly' in catalog
        assert not (await control.native_hook("PreInvocation", {"invocationNum": 1}))["injectSteps"]
        async with httpx.AsyncClient() as client:
            assert (await client.post(url + "/mcp/fixture", json={})).status_code == 401
            for value in ("denied", "allowed"):
                response = await client.post(url + "/mcp/fixture", headers={"Authorization": "Bearer " + token},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read", "arguments": {"value": value}}})
                body = response.json()["result"]
                if value == "denied":
                    assert body["isError"]
                else:
                    assert body["content"][0]["text"] == "validated"
    assert called == [{"value": "allowed"}]


@pytest.mark.asyncio
async def test_unknown_and_disabled_native_tools_fail_closed():
    control = Control(ClaudeAgentOptions(cwd="/tmp", permission_mode="bypassPermissions", disallowed_tools=["Bash"]))
    for name in ("run_command", "unknown_new_tool"):
        result = await control.native_hook("PreToolUse", {"toolCall": {"name": name, "args": {}}})
        assert result["decision"] == "deny"


@pytest.mark.asyncio
async def test_system_prompt_and_turn_limit_use_invocation_hook():
    control = Control(ClaudeAgentOptions(cwd="/tmp", system_prompt="Raven policy", max_turns=2))
    first = await control.native_hook("PreInvocation", {"invocationNum": 0})
    assert first["injectSteps"] == [{"systemMessage": {"systemMessage": "Raven policy"}}]
    assert not (await control.native_hook("PreInvocation", {"invocationNum": 1}))["injectSteps"]
    assert (await control.native_hook("PreInvocation", {"invocationNum": 2}))["terminationBehavior"] == "terminate"


@pytest.mark.asyncio
async def test_mcp_permission_id_matches_visible_step():
    observed = []
    async def deny(name, args, context):
        observed.append(context.tool_use_id)
        return {"behavior": "deny"}
    control = Control(ClaudeAgentOptions(cwd="/tmp", can_use_tool=deny))
    control.mcp_names["mcp__fixture__write"] = "mcp__fixture__write"
    reply = await control.native_hook("PreToolUse", {"conversationId": "run", "stepIdx": 3,
        "toolCall": {"name": "call_mcp_tool", "args": {"ServerName": "fixture", "ToolName": "write", "Arguments": {}}}})
    assert reply["decision"] == "auto_approve"
    assert (await control.call("fixture", "write", {}))["isError"]
    assert observed == ["agy-run-3"]


def test_transport_secrets_are_removed_before_trace_persistence():
    from claude_agent_sdk import AssistantMessage, TextBlock
    from app.agents.runtime_secrets import redact
    message = AssistantMessage(content=[TextBlock(text="private-token control-secret")], model="model")
    safe = redact(message, ("private-token", "control-secret"))
    assert safe.content[0].text == "[redacted] [redacted]"
    assert message.content[0].text == "private-token control-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal,code,expected", [
    ({"status": "ERROR", "error": "specific CLI failure"}, 3, None),
    ({"status": "SUCCESS", "response": "ok"}, 0, None),
    (None, 3, "no terminal event"),
    ({"status": "SUCCESS"}, 3, "after a successful terminal event"),
])
async def test_real_process_terminal_status_is_not_overwritten(tmp_path, monkeypatch, terminal, code, expected):
    import json
    import os
    import sys
    from app.agents.antigravity_executor import query

    binary = tmp_path / "agy"
    event = json.dumps({"event": "result", "result": terminal}) if terminal else ""
    binary.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.read()\n"
                      f"event = {event!r}\nif event: print(event, flush=True)\nsys.exit({code})\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    options = ClaudeAgentOptions(cwd=str(tmp_path), model="fixture", env={
        "ANTHROPIC_BASE_URL": "http://unused", "ANTHROPIC_API_KEY": "token", "API_TIMEOUT_MS": "5000"})
    if expected:
        with pytest.raises(RuntimeError, match=expected):
            _ = [message async for message in query(prompt="test", options=options)]
    else:
        messages = [message async for message in query(prompt="test", options=options)]
        assert len(messages) == 1
        assert messages[0].is_error == (terminal["status"] == "ERROR")
        if messages[0].is_error:
            assert messages[0].errors == ["specific CLI failure"]
