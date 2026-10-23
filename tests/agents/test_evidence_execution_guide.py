"""Execution feedback is isolated and never removes or rewrites tool evidence."""
import copy

import pytest

from app.agents.evidence_policy import EvidenceExecutionGuide, execution_hooks


def event(index, *, name="Bash", cwd="/workspace"):
    return {"hook_event_name": "PostToolUse", "tool_name": name, "cwd": cwd,
            "tool_input": {"command": f"read evidence-{index}"},
            "tool_response": {"stdout": f"unchanged evidence {index}", "exit_code": 0}}


@pytest.mark.asyncio
@pytest.mark.parametrize("locale,marker", [("zh", "证据检查点"), ("en", "Evidence checkpoint")])
async def test_periodic_feedback_preserves_tool_results_and_does_not_stop(locale, marker):
    guide = EvidenceExecutionGuide(locale)
    for index in range(1, 17):
        data = event(index)
        before = copy.deepcopy(data)
        result = await guide.after_tool(data, str(index), {})
        assert data == before
        if index % 8:
            assert result == {}
        else:
            assert set(result) == {"hookSpecificOutput"}
            feedback = result["hookSpecificOutput"]
            assert set(feedback) == {"hookEventName", "additionalContext"}
            assert feedback["hookEventName"] == "PostToolUse"
            assert marker in feedback["additionalContext"]
            assert "mcp__evidence__search_evidence" in feedback["additionalContext"]
    assert guide.calls == 16


@pytest.mark.asyncio
async def test_repeated_arguments_remind_without_exposing_content_or_suppressing_retry():
    guide = EvidenceExecutionGuide("en")
    data = event(1)
    data["tool_input"]["command"] = "private-command-token"
    assert await guide.after_tool(data, "first", {}) == {}
    assert await guide.after_tool(data, "first", {}) == {}  # Duplicate delivery.
    assert guide.calls == 1
    result = await guide.after_tool(data, "second", {})
    message = result["hookSpecificOutput"]["additionalContext"]
    assert "same tool arguments" in message and "retry" in message
    assert "private-command-token" not in message
    data["cwd"] = "/different-workspace"
    assert await guide.after_tool(data, "third", {}) == {}


@pytest.mark.asyncio
async def test_ignores_non_evidence_tools_and_other_hook_events():
    guide = EvidenceExecutionGuide("zh")
    for name in ("Skill", "mcp__project_repo__clone_project_repo", "AskUserQuestion"):
        assert await guide.after_tool(event(1, name=name), name, {}) == {}
    data = event(1)
    data["hook_event_name"] = "PostToolUseFailure"
    assert await guide.after_tool(data, "failure", {}) == {}
    assert guide.calls == 0


@pytest.mark.asyncio
async def test_sdk_hook_factory_keeps_each_run_independent():
    first = execution_hooks("zh")["PostToolUse"][0].hooks[0]
    second = execution_hooks("zh")["PostToolUse"][0].hooks[0]
    for index in range(1, 9):
        result = await first(event(index, name="mcp__evidence__read_evidence"), str(index), {})
    assert result["hookSpecificOutput"]
    assert await second(event(8), "8", {}) == {}
