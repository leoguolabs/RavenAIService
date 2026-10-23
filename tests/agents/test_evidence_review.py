"""Final review is bounded, per-run, and does not replace terminal results."""
import copy
import json

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, UserMessage

from app.agents.evidence_policy import execution_hooks
from app.agents.evidence_review import FinalEvidenceReview
from app.agents.log_analysis.agent import _RunState, _emit_for_message, _extract_fenced_json


def response(message_id=None, **kwargs):
    return AssistantMessage(content=[TextBlock(text="draft")], model="test", message_id=message_id, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("locale,expected", [("zh", "证据"), ("en", "evidence")])
async def test_review_is_once_and_does_not_rewrite_input(locale, expected):
    review = FinalEvidenceReview(locale, "task", 150)
    data = {"hook_event_name": "Stop", "stop_hook_active": False}
    before = copy.deepcopy(data)
    output = await review.before_stop(data, None, {})
    assert output["decision"] == "block" and expected in output["reason"]
    assert data == before
    assert await review.before_stop(data, None, {}) == {}
    assert await review.before_stop({**data, "stop_hook_active": True}, None, {}) == {}


@pytest.mark.asyncio
async def test_active_stop_hook_cannot_start_another_review():
    review = FinalEvidenceReview("en", "task", 150)
    assert await review.before_stop({"hook_event_name": "Stop", "stop_hook_active": True}, None, {}) == {}
    assert await review.before_stop({"hook_event_name": "Stop", "stop_hook_active": False}, None, {}) == {}


@pytest.mark.asyncio
async def test_turn_headroom_counts_responses_not_stream_fragments_or_subagents():
    review = FinalEvidenceReview("en", "task", 10)
    for _ in range(20):
        review.observe(response("same-response"))
        review.observe(response("subagent", parent_tool_use_id="parent-tool"))
        review.observe(UserMessage(content="tool output"))
    assert (await review.before_stop({"hook_event_name": "Stop"}, None, {}))["decision"] == "block"

    near_limit = FinalEvidenceReview("en", "task", 10)
    for i in range(5):
        near_limit.observe(response(str(i)))
    assert await near_limit.before_stop({"hook_event_name": "Stop"}, None, {}) == {}


@pytest.mark.asyncio
async def test_old_sdk_without_response_ids_skips_conservatively_and_runs_are_independent():
    first = FinalEvidenceReview("zh", "first", 10)
    second = FinalEvidenceReview("zh", "second", 10)
    for _ in range(5):
        first.observe(response())
    assert await first.before_stop({"hook_event_name": "PostToolUse"}, None, {}) == {}
    assert await first.before_stop({"hook_event_name": "Stop"}, None, {}) == {}
    assert (await second.before_stop({"hook_event_name": "Stop"}, None, {}))["decision"] == "block"


@pytest.mark.asyncio
async def test_hook_wiring_is_optional_and_retains_source_hook(tmp_path):
    review = FinalEvidenceReview("zh", "task", 150)
    hooks = execution_hooks("zh", "task", str(tmp_path), source_context=True, final_review=review)
    assert hooks["PostToolUse"][0].matcher == "Bash"
    stop = hooks["Stop"][0]
    assert stop.matcher is None
    assert (await stop.hooks[0]({"hook_event_name": "Stop"}, None, {}))["decision"] == "block"
    assert "Stop" not in execution_hooks("zh", "task", str(tmp_path))
    assert execution_hooks("zh", final_review=review) == {}


@pytest.mark.asyncio
async def test_review_and_source_context_can_be_enabled_independently(tmp_path):
    assert execution_hooks("zh", "task", str(tmp_path)) == {}
    source_only = execution_hooks("zh", "task", str(tmp_path), source_context=True)
    assert set(source_only) == {"PostToolUse"}
    review = FinalEvidenceReview("zh", "task", 150)
    review_only = execution_hooks("zh", "task", str(tmp_path), final_review=review)
    assert set(review_only) == {"Stop"}
    assert (await review_only["Stop"][0].hooks[0]({"hook_event_name": "Stop"}, None, {}))["decision"] == "block"


def test_terminal_reviewed_result_is_parsed_instead_of_first_draft():
    state = _RunState(task_id="review-test", emitter=None)
    first = '{"status":"ok","answer":"unconfirmed completion asserted"}'
    _emit_for_message(AssistantMessage(content=[TextBlock(text=first)], model="test"), state=state)
    final = {"status": "ok", "answer": "completion is unknown", "summary": "no terminal evidence"}
    result = "```json\n" + json.dumps(final) + "\n```"
    _emit_for_message(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                   is_error=False, num_turns=2, session_id="session", result=result), state=state)
    assert _extract_fenced_json(state.final_text) == final
