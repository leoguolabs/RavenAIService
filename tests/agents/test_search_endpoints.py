"""Same-topic tail changes must be available without losing paginated evidence."""
import copy
import json

import pytest

from app.agents import evidence_tools, log_topic_context as topic
from app.agents.evidence_policy import execution_hooks


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/state.log").write_text("state=ready\n" * 30 + "state=stopped\n")
    return tmp_path


async def event(workspace, requests=None):
    requests = requests or [{"path": "logs/state.log", "pattern": "state", "limit": 2}]
    result = await evidence_tools.run_batch(str(workspace), requests, search=True)
    return {"hook_event_name": "PostToolUse", "tool_name": topic.MCP_SEARCH_TOOL,
            "tool_input": {"requests": requests},
            "tool_response": [{"type": "text", "text": evidence_tools.encode_evidence(result)}]}


def decode(result):
    text = result["hookSpecificOutput"]["additionalContext"]
    assert len(text.encode()) <= topic.MAX_OUTPUT_BYTES
    return json.loads(text.split("\n", 1)[1])


@pytest.mark.parametrize("literal", [False, True])
@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.asyncio
async def test_represented_topic_gets_last_state_without_replacing_head_or_paging(workspace, literal, locale):
    data = await event(workspace, [{"path": "logs/state.log", "pattern": "state", "literal": literal, "limit": 2}])
    before = copy.deepcopy(data)
    assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "off", {}) == {}
    hook = topic.LogTopicContextEnricher(str(workspace), locale, include_endpoints=True)
    item, = decode(await hook.after_tool(data, "on", {}))["topic_context"]
    assert item["sampled_match_lines"] == [31]
    assert item["matching_lines"] == 31 and item["original_preview_matches"] == 2
    assert item["reason"] == "endpoint_not_visible" and item["event_history_complete"] is False
    assert [31, "state=stopped"] in item["windows"][0]["rows"]
    assert data == before
    assert await hook.after_tool(data, "repeat", {}) == {}
    with (workspace / "logs/state.log").open("a") as stream:
        stream.write("state=restarted\n")
    fresh = decode(await hook.after_tool(await event(workspace), "changed", {}))["topic_context"][0]
    assert fresh["sampled_match_lines"] == [32]


@pytest.mark.asyncio
async def test_later_page_adds_only_unseen_first_endpoint(workspace):
    data = await event(workspace, [{"path": "logs/state.log", "pattern": "state", "offset": 30, "limit": 2}])
    item, = decode(await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "end", {}))["topic_context"]
    assert item["sampled_match_lines"] == [1]
    assert [1, "state=ready"] in item["windows"][0]["rows"]


@pytest.mark.asyncio
async def test_requested_context_already_contains_endpoint(workspace):
    data = await event(workspace, [{"path": "logs/state.log", "pattern": "state", "limit": 1, "context_lines": 40}])
    assert await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "visible", {}) == {}


@pytest.mark.asyncio
async def test_clipped_match_cannot_hide_the_final_value(workspace):
    line = "state " + "x" * 650 + " stopped"
    (workspace / "logs/state.log").write_text(line + "\n")
    data = await event(workspace)
    assert "stopped" not in data["tool_response"][0]["text"]
    item, = decode(await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "clipped", {}))["topic_context"]
    assert item["sampled_match_lines"] == [1]
    assert [1, line] in item["windows"][0]["rows"]


@pytest.mark.asyncio
async def test_file_scopes_and_duplicate_batch_requests_keep_correct_endpoints(workspace):
    (workspace / "logs/z.log").write_text("state=first\nstate=last\n")
    request = {"path": "logs", "pattern": "state", "limit": 2}
    data = await event(workspace, [request, request])
    items = decode(await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "batch", {}))["topic_context"]
    assert [(item["path"], item["sampled_match_lines"]) for item in items] == [
        ("logs/state.log", [31]), ("logs/z.log", [1, 2])]
    assert all(item["request_index"] == 0 for item in items)


@pytest.mark.asyncio
async def test_missing_topics_are_kept_when_represented_topic_also_needs_tail(workspace):
    (workspace / "logs/state.log").write_text("state=ready\n" * 30 + "rare issue\nstate=stopped\n")
    data = await event(workspace, [{"path": "logs/state.log", "pattern": "state|rare", "limit": 2}])
    items = decode(await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "mixed", {}))["topic_context"]
    assert [(item["literal"], item["reason"]) for item in items] == [
        ("rare", "topic_absent"), ("state", "endpoint_not_visible")]


@pytest.mark.parametrize("change", ["stale", "warning", "pattern", "budget", "external"])
@pytest.mark.asyncio
async def test_unverified_or_unsupported_endpoints_leave_results_untouched(workspace, monkeypatch, change):
    data = await event(workspace)
    if change == "stale":
        (workspace / "logs/state.log").write_text("different\n")
    elif change == "warning":
        body = json.loads(data["tool_response"][0]["text"])
        body["results"][0]["warning"] = "partial scan"
        data["tool_response"][0]["text"] = json.dumps(body)
    elif change == "pattern":
        data["tool_input"]["requests"][0]["pattern"] = "state.*"
    elif change == "budget":
        monkeypatch.setattr(topic, "MAX_TOTAL_BYTES", 1)
    else:
        (workspace / "outside.log").write_text((workspace / "logs/state.log").read_text())
        data["tool_input"]["requests"][0]["path"] = "outside.log"
    before = copy.deepcopy(data)
    assert await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, change, {}) == {}
    assert data == before


@pytest.mark.asyncio
async def test_unicode_crlf_and_unterminated_final_line_are_exact(workspace):
    (workspace / "logs/state.log").write_bytes("状态：运行\r\n状态：运行\r\n状态：停止\r".encode())
    # Chinese punctuation is outside the bounded literal grammar, so select
    # the shared literal word rather than broadening regex interpretation.
    data = await event(workspace, [{"path": "logs/state.log", "pattern": "状态", "limit": 1}])
    item, = decode(await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "utf8", {}))["topic_context"]
    assert [3, "状态：停止\r"] in item["windows"][0]["rows"]


def test_independent_default_off_switch_only_registers_structured_search(workspace):
    assert execution_hooks("zh", workspace=str(workspace)) == {}
    hooks = execution_hooks("zh", workspace=str(workspace), search_endpoints=True)
    matcher, = hooks["PostToolUse"]
    assert matcher.matcher == topic.MCP_SEARCH_TOOL
    assert matcher.hooks[0].__self__.include_endpoints is True
