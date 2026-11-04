import pytest
from app.agents.evidence_progress import EvidenceProgress
from app.agents.evidence_policy import execution_hooks


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
async def test_two_soft_checkpoints_preserve_uncertainty_and_never_mutate_tool_input(
    locale,
):
    progress = EvidenceProgress(locale)
    notices = []
    for i in range(200):
        data = {
            "hook_event_name": "PostToolUse",
            "tool_use_id": str(i),
            "tool_response": "PRIVATE_RAW",
        }
        result = await progress.after_tool(data, str(i), {})
        assert data["tool_response"] == "PRIVATE_RAW"
        if result:
            text = result["hookSpecificOutput"]["additionalContext"]
            assert "PRIVATE_RAW" not in text
            assert "turn limit" in text or "轮数上限" in text
            assert result["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
            assert not any(k in result for k in ["decision", "continue", "stopReason"])
            notices.append(i + 1)
    assert notices == [80, 120] and len(progress.seen) == 120


@pytest.mark.asyncio
async def test_duplicate_results_and_wrong_events_do_not_advance_budget():
    progress = EvidenceProgress()
    assert (
        await progress.after_tool({"hook_event_name": "PreToolUse"}, "wrong", {}) == {}
    )
    for i in range(79):
        data = {"hook_event_name": "PostToolUse", "tool_use_id": str(i)}
        assert await progress.after_tool(data, str(i), {}) == {}
        assert await progress.after_tool(data, str(i), {}) == {}
    assert progress.calls == 79
    assert await progress.after_tool({"hook_event_name": "PostToolUse"}, "80th", {})


def test_default_off_and_separate_per_run_state(tmp_path):
    assert execution_hooks("zh", workspace=str(tmp_path)) == {}
    assert execution_hooks("zh", progress_budget=True) == {}
    a = execution_hooks("zh", workspace=str(tmp_path), progress_budget=True)
    b = execution_hooks("zh", workspace=str(tmp_path), progress_budget=True)
    assert set(a) == {"PostToolUse"} and len(a["PostToolUse"]) == 1
    assert (
        a["PostToolUse"][0].hooks[0].__self__
        is not b["PostToolUse"][0].hooks[0].__self__
    )
