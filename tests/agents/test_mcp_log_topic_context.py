"""Real evidence pages, stable scope, shared budgets and native/MCP dedup."""
import copy
import json

import pytest

from app.agents import evidence_tools, log_topic_context as topic


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/a.log").write_text("noise\n" * 25 + "rare first\ncontext\nrare last\nend\n")
    (tmp_path / "logs/b.log").write_text("noise\n" * 4 + "rare second file\n")
    return tmp_path


async def event(workspace, requests=None):
    requests = requests or [{"path": "logs", "pattern": "noise|rare", "limit": 2, "context_lines": 1}]
    result = await evidence_tools.run_batch(str(workspace), requests, search=True)
    return {"hook_event_name": "PostToolUse", "tool_name": topic.MCP_SEARCH_TOOL,
            "tool_input": {"requests": requests},
            "tool_response": [{"type": "text", "text": evidence_tools.encode_evidence(result)}]}


def decoded(value):
    text = value["hookSpecificOutput"]["additionalContext"]
    assert len(text.encode()) <= topic.MAX_OUTPUT_BYTES
    return json.loads(text.split("\n", 1)[1])


@pytest.mark.asyncio
async def test_exact_batch_supplement_uses_server_root_and_preserves_every_original_row(workspace):
    data = await event(workspace)
    data["cwd"] = "/unrelated/bash/cwd"
    before = copy.deepcopy(data)
    hook = topic.LogTopicContextEnricher(str(workspace))
    entries = decoded(await hook.after_tool(data, "one", {}))["topic_context"]
    assert [(e["path"], e["literal"], e["matching_lines"]) for e in entries] == [
        ("logs/a.log", "rare", 2), ("logs/b.log", "rare", 1),
    ]
    assert all(e["request_index"] == 0 and e["scan_complete"] for e in entries)
    assert [26, "rare first"] in entries[0]["windows"][0]["rows"]
    assert data == before
    assert await hook.after_tool(data, "two", {}) == {}
    native = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": str(workspace),
              "tool_input": {"command": r"grep -n 'noise\|rare' logs/a.log | head -2"},
              "tool_response": {"stdout": "1:noise\n2:noise"}}
    assert await hook.after_tool(native, "three", {}) == {}


@pytest.mark.asyncio
async def test_request_errors_do_not_remove_valid_sibling_context_and_duplicate_requests_share_output(workspace):
    request = {"path": "logs/a.log", "pattern": "noise|rare", "limit": 2}
    data = await event(workspace, [{"path": "missing", "pattern": "noise|rare"}, request, request])
    entries = decoded(await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "batch", {}))["topic_context"]
    assert len(entries) == 1 and entries[0]["request_index"] == 1


@pytest.mark.parametrize("change", ["index", "order", "text", "line", "column", "boolean_line", "clipped", "total",
    "at_least", "complete", "continuation", "warning", "columns", "context", "request_pattern", "request_path"])
@pytest.mark.asyncio
async def test_malformed_or_stale_page_never_adds_evidence(workspace, change):
    data = await event(workspace)
    body = json.loads(data["tool_response"][0]["text"])
    page = body["results"][0]
    if change == "index": page["index"] = 1
    elif change == "order": page["matches"].reverse()
    elif change == "text": page["matches"][0][3] += " changed"
    elif change == "line": page["matches"][0][1] = 3
    elif change == "column": page["matches"][0][2] = 1
    elif change == "boolean_line": page["matches"][0][1] = True
    elif change == "clipped": page["matches"][0][4] = True
    elif change == "total": page["matching_lines_total"] = 2
    elif change == "at_least": page["matching_lines_at_least"] += 1
    elif change == "complete": page["complete"] = True
    elif change == "continuation": page["next_request"]["offset"] = 99
    elif change == "warning": page["warning"] = "incomplete scan"
    elif change == "columns": body["match_columns"].reverse()
    elif change == "context": page["context_windows"][0]["lines"][0][2] += " changed"
    elif change == "request_pattern": data["tool_input"]["requests"][0]["pattern"] = "other|rare"
    elif change == "request_path": data["tool_input"]["requests"][0]["path"] = "logs/b.log"
    data["tool_response"][0]["text"] = json.dumps(body)
    assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, change, {}) == {}


@pytest.mark.parametrize("change", [{"ignore_case": True}, {"literal": True}, {"glob": "*.log"},
    {"pattern": "noise|rare.*"}, {"pattern": "(noise)|rare"}, {"offset": True}, {"limit": True},
    {"context_lines": True}, {"path": "repo/a.log"}])
@pytest.mark.asyncio
async def test_unsupported_requests_leave_native_result_unchanged(workspace, change):
    (workspace / "repo").mkdir()
    (workspace / "repo/a.log").write_text("noise\nnoise\nrare\n")
    data = await event(workspace)
    data["tool_input"]["requests"][0].update(change)
    before = copy.deepcopy(data)
    assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "unsupported", {}) == {}
    assert data == before


@pytest.mark.asyncio
async def test_pagination_unicode_duplicates_crlf_and_final_partial_cr(workspace):
    (workspace / "logs/a.log").write_bytes("noise\r\nnoise\r\nnoise\r\n罕见命中\r\n罕见尾部\r".encode())
    data = await event(workspace, [{"path": "logs/a.log", "pattern": "noise|罕见|罕见", "offset": 1, "limit": 1}])
    entry, = decoded(await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "unicode", {}))["topic_context"]
    assert entry["literal"] == "罕见" and entry["matching_lines"] == 2
    assert [4, "罕见命中"] in entry["windows"][0]["rows"]
    assert [5, "罕见尾部\r"] in entry["windows"][0]["rows"]


@pytest.mark.asyncio
async def test_already_visible_requested_context_does_not_repeat_topic(workspace):
    (workspace / "logs/a.log").write_text("noise\nrare\nnoise\nrare\n")
    data = await event(workspace, [{"path": "logs/a.log", "pattern": "noise|rare", "limit": 1, "context_lines": 1}])
    assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "visible", {}) == {}


@pytest.mark.asyncio
async def test_hidden_alternative_in_verified_clipped_line_gets_exact_context(workspace):
    line = "noise" + "x" * 800 + "rare"
    (workspace / "logs/a.log").write_text(line + "\n")
    data = await event(workspace, [{"path": "logs/a.log", "pattern": "noise|rare", "limit": 1}])
    page = json.loads(data["tool_response"][0]["text"])["results"][0]
    assert page["matches"][0][4] is True and "rare" not in page["matches"][0][3]
    entry, = decoded(await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "clipped", {}))["topic_context"]
    assert [1, line] in entry["windows"][0]["rows"]


@pytest.mark.parametrize("change", ["file", "directory"])
@pytest.mark.asyncio
async def test_scope_changes_during_verification_are_rejected(workspace, monkeypatch, change):
    data = await event(workspace)
    original = evidence_tools._search_context
    def changed(*args):
        result = original(*args)
        if change == "file":
            with (workspace / "logs/a.log").open("a") as f: f.write("rare new\n")
        else:
            (workspace / "logs/new.log").write_text("rare new\n")
        return result
    monkeypatch.setattr(evidence_tools, "_search_context", changed)
    assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "changed", {}) == {}


@pytest.mark.parametrize("limit", ["MAX_FILE_BYTES", "MAX_TOTAL_BYTES", "MAX_LINES", "MAX_LINE_BYTES", "SCAN_SECONDS"])
@pytest.mark.asyncio
async def test_shared_processing_budget_falls_back(workspace, monkeypatch, limit):
    data = await event(workspace)
    monkeypatch.setattr(topic, limit, 0)
    assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "limit", {}) == {}


@pytest.mark.asyncio
async def test_directory_symlinks_and_git_are_excluded_like_ripgrep(workspace):
    (workspace / "outside").mkdir()
    (workspace / "outside/secret.log").write_text("rare hidden\n")
    (workspace / "logs/link").symlink_to(workspace / "outside", target_is_directory=True)
    (workspace / "logs/.git").mkdir()
    (workspace / "logs/.git/private").write_text("rare hidden\n")
    data = await event(workspace)
    entries = decoded(await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "links", {}))["topic_context"]
    assert {e["path"] for e in entries} == {"logs/a.log", "logs/b.log"}


@pytest.mark.asyncio
async def test_output_topics_share_one_budget_across_batch(workspace):
    (workspace / "logs/a.log").write_text("noise\nnoise\nrareA\nrareB\nrareC\nrareD\nrareE\n")
    request = {"path": "logs/a.log", "pattern": "noise|rareA|rareB|rareC|rareD|rareE", "limit": 2}
    data = await event(workspace, [request, request])
    body = decoded(await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "many", {}))
    assert len(body["topic_context"]) == 4 and body["omitted_topics"] == 1


@pytest.mark.asyncio
async def test_unmatched_invalid_bytes_preserve_verified_search_and_escape_added_context(workspace):
    (workspace / "logs/a.log").write_bytes(b"noise\nnoise\n\xff\nrare first\n")
    data = await event(workspace, [{"path": "logs/a.log", "pattern": "noise|rare", "limit": 1}])
    entry, = decoded(await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "unmatched", {}))["topic_context"]
    assert entry["matching_lines"] == 1
    assert [3, r"\xff"] in entry["windows"][0]["rows"]


@pytest.mark.asyncio
async def test_invalid_matching_line_beyond_preview_cannot_be_claimed_complete(workspace):
    (workspace / "logs/a.log").write_bytes(b"noise\nnoise\nnoise\nrare \xff\n")
    data = await event(workspace, [{"path": "logs/a.log", "pattern": "noise|rare", "limit": 1}])
    assert not json.loads(data["tool_response"][0]["text"])["results"][0].get("error")
    assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "matched", {}) == {}
