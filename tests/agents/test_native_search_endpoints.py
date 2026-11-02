"""Native head previews retain late evidence without claiming a complete glob scan."""
import copy
import hashlib
import json
import subprocess

import pytest

from app.agents import log_topic_context as topic


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "repo").mkdir()
    (tmp_path / "logs/a.log").write_text("MOM totalItems=71\n" * 50 + "MOM totalItems=54\n")
    (tmp_path / "logs/z.log").write_text("Mom other file\n")
    return tmp_path


def event(workspace, command=r'grep -rn "MOM\|Mom" ../logs/*.log | grep -iv "moment" | head -40'):
    result = subprocess.run(["/bin/sh", "-c", command], cwd=workspace / "repo",
                            capture_output=True, text=True, check=True)
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": str(workspace / "repo"),
            "tool_input": {"command": command},
            "tool_response": {"stdout": result.stdout.removesuffix("\n"), "stderr": result.stderr}}


def decode(result):
    text = result["hookSpecificOutput"]["additionalContext"]
    assert len(text.encode()) <= topic.MAX_OUTPUT_BYTES
    return json.loads(text.split("\n", 1)[1])


@pytest.mark.asyncio
async def test_unfiltered_native_context_deduplicates_across_mcp(workspace):
    from app.agents.evidence_tools import encode_evidence, run_batch

    hook = topic.LogTopicContextEnricher(str(workspace), include_endpoints=True)
    assert await hook.after_tool(event(workspace, r"grep -rn 'MOM' ../logs/*.log | head -40"), "native", {})
    requests = [{"path": "logs/a.log", "pattern": "MOM", "limit": 2}]
    result = await run_batch(str(workspace), requests, search=True)
    data = {"hook_event_name": "PostToolUse", "tool_name": topic.MCP_SEARCH_TOOL,
            "tool_input": {"requests": requests},
            "tool_response": [{"type": "text", "text": encode_evidence(result)}]}
    assert await hook.after_tool(data, "mcp", {}) == {}


@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.asyncio
async def test_original_glob_exclusion_head_shape_supplements_last_state(workspace, locale):
    data = event(workspace)
    before = copy.deepcopy(data)
    assert "totalItems=54" not in data["tool_response"]["stdout"]
    assert await topic.LogTopicContextEnricher(str(workspace)).after_tool(data, "off", {}) == {}
    hook = topic.LogTopicContextEnricher(str(workspace), locale, include_endpoints=True)
    item, = decode(await hook.after_tool(data, "on", {}))["topic_context"]
    assert item["sampled_match_lines"] == [51] and item["matching_lines"] == 51
    assert item["original_preview_matches"] == 40 and item["event_history_complete"] is False
    assert item["native_page_scope"] == {"query_scope_complete": False, "verified_files": 1,
        "unseen_files": "not_scanned", "exclusion_literal_ignore_case": "moment"}
    assert [51, "MOM totalItems=54"] in item["windows"][0]["rows"]
    assert item["source_sha256"] == hashlib.sha256((workspace / "logs/a.log").read_bytes()).hexdigest()
    assert data == before and await hook.after_tool(data, "again", {}) == {}


@pytest.mark.asyncio
async def test_unseen_files_are_not_scanned_or_claimed_complete(workspace, monkeypatch):
    data = event(workspace)
    (workspace / "logs/z.log").write_bytes(b"\0" * 4096)
    monkeypatch.setattr(topic, "MAX_FILES", 1)
    monkeypatch.setattr(topic, "MAX_FILE_BYTES", 2048)
    item, = decode(await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "bounded", {}))["topic_context"]
    assert item["path"] == "logs/a.log" and not item["native_page_scope"]["query_scope_complete"]


@pytest.mark.asyncio
async def test_filter_changes_per_file_counts_and_last_endpoint(workspace):
    with (workspace / "logs/a.log").open("a") as stream:
        stream.write("MOM moment excluded\nMom final relevant\nMOM MOMENT excluded\n")
    item1, item2 = decode(await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(event(workspace), "filtered", {}))["topic_context"]
    assert (item1["literal"], item1["sampled_match_lines"], item1["matching_lines"]) == ("Mom", [53], 1)
    assert (item2["literal"], item2["sampled_match_lines"], item2["matching_lines"]) == ("MOM", [51], 51)


@pytest.mark.asyncio
async def test_two_visible_files_respect_native_prefixes(workspace):
    (workspace / "logs/a.log").write_text("MOM first\n")
    (workspace / "logs/z.log").write_text("MOM next\n" * 50 + "Mom last\n")
    data = event(workspace)
    items = decode(await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "two", {}))["topic_context"]
    assert len(items) == 2 and all(i["path"] == "logs/z.log" for i in items)
    assert all(i["native_page_scope"]["verified_files"] == 2 for i in items)


@pytest.mark.parametrize("command", [
    r"grep -n 'MOM' ../logs/a.log | head -2",
    r"grep -nE 'MOM|Mom' ../logs/a.log | head -n 2",
    r'for f in ../logs/a.log ../logs/z.log; do echo "=== $f"; grep -n "MOM\|Mom" $f | head -2; done',
    r"grep -rnE 'MOM|Mom' ../logs/*.log | head -n2",
    r'grep -rIn --binary-files=text -E "MOM|Mom" ../logs/a.log | head -40',
    r'grep -rIn --binary-files=text -E "MOM|Mom" ../logs/ | head -40',
    r'grep -r -I -n --binary-files=text -E "MOM|Mom" ../logs/*.log | head -40',
])
@pytest.mark.asyncio
async def test_literal_native_head_variants_keep_original_output(workspace, command):
    data = event(workspace, command)
    before = copy.deepcopy(data)
    items = decode(await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "native", {}))["topic_context"]
    assert any([51, "MOM totalItems=54"] in w["rows"] for i in items for w in i["windows"])
    assert data == before


@pytest.mark.asyncio
async def test_recursive_directory_unicode_literals_and_subdirectories(workspace):
    nested = workspace / "logs/nested"
    nested.mkdir()
    (nested / "state.log").write_text("状态 运行\n" * 10 + "状态 停止\n")
    data = event(workspace, r'grep -rIn --binary-files=text -E "状态|运行" ../logs/ | head -2')
    items = decode(await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "unicode", {}))["topic_context"]
    assert all(i["path"] == "logs/nested/state.log" for i in items)
    assert any([11, "状态 停止"] in w["rows"] for i in items for w in i["windows"])
    assert all(i["native_page_scope"]["query_scope_complete"] is False for i in items)


@pytest.mark.asyncio
async def test_pinned_sdk_filename_prefix_variant_is_verified_not_rewritten(workspace):
    data = event(workspace, r'grep -rIn --binary-files=text -E "MOM|Mom" ../logs/a.log | head -40')
    data["tool_response"]["stdout"] = "\n".join("../logs/a.log:" + row for row in data["tool_response"]["stdout"].split("\n"))
    before = copy.deepcopy(data)
    assert await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "prefixed", {})
    assert data == before
    data["tool_response"]["stdout"] = data["tool_response"]["stdout"].replace("../logs/a.log:", "../logs/wrong.log:")
    assert await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "wrong", {}) == {}


@pytest.mark.asyncio
async def test_recursive_query_does_not_read_external_symlink(workspace):
    outside = workspace / "outside"
    outside.mkdir()
    (outside / "a.log").write_text((workspace / "logs/a.log").read_text())
    data = event(workspace, r'grep -rIn --binary-files=text -E "MOM|Mom" ../logs/ | head -40')
    (workspace / "logs/a.log").unlink()
    (workspace / "logs/a.log").symlink_to(outside / "a.log")
    assert await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, "external", {}) == {}


@pytest.mark.parametrize("change", ["stdout", "offset", "scope", "regex", "extra_command", "stderr", "nonascii_filter", "file_change", "budget"])
@pytest.mark.asyncio
async def test_unverifiable_page_never_adds_context(workspace, monkeypatch, change):
    data = event(workspace)
    if change == "stdout": data["tool_response"]["stdout"] += "\nextra"
    elif change == "offset": data["tool_response"]["stdout"] = data["tool_response"]["stdout"].replace(":1:", ":2:", 1)
    elif change == "scope": data["tool_input"]["command"] = data["tool_input"]["command"].replace("../logs", "../elsewhere")
    elif change == "regex": data["tool_input"]["command"] = data["tool_input"]["command"].replace("moment", "mom.nt")
    elif change == "extra_command": data["tool_input"]["command"] += "; pwd"
    elif change == "stderr": data["tool_response"]["stderr"] = "error"
    elif change == "nonascii_filter":
        with (workspace / "logs/a.log").open("a") as stream: stream.write("MOM 非ASCII\n")
    elif change == "file_change": (workspace / "logs/a.log").write_text("changed\n")
    else: monkeypatch.setattr(topic, "MAX_TOTAL_BYTES", 1)
    before = copy.deepcopy(data)
    assert await topic.LogTopicContextEnricher(str(workspace), include_endpoints=True).after_tool(data, change, {}) == {}
    assert data == before
