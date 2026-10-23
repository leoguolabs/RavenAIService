"""Automatic context is exact, bounded, repository-scoped and non-destructive."""
import copy
import json
from pathlib import Path

import pytest

from app.agents.evidence_context import MAX_CONTEXT_BYTES, MAX_SOURCE_BYTES, SourceContextEnricher
from app.agents.evidence_policy import execution_hooks
from app.agents.evidence_tools import MAX_READ_CHARS, read_page


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "repo").mkdir()
    for name in ("first.c", "second.c", "third.c"):
        (tmp_path / "repo" / name).write_text("\n".join(f"line {n} {name}" for n in range(1, 501)))
    return tmp_path


def event(stdout, command="rg -n target repo", **extra):
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": command},
            "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False}, **extra}


def windows(result):
    text = result["hookSpecificOutput"]["additionalContext"]
    assert len(text.encode("utf-8")) <= MAX_CONTEXT_BYTES
    result = json.loads(text.split("\n", 1)[1])["source_context"]
    for window in result:
        assert all(isinstance(line, list) and len(line) == 3 for line in window["lines"])
        window["lines"] = [{"line": n, "column": c, "text": t} for n, c, t in window["lines"]]
    return result


@pytest.mark.asyncio
async def test_verified_search_expands_context_without_rewriting_original_result(workspace):
    hook = SourceContextEnricher(str(workspace))
    data = event("repo/first.c:20:line 20 first.c")
    before = copy.deepcopy(data)
    result = await hook.after_tool(data, "tool-1", {})
    assert data == before
    assert set(result["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}
    result = windows(result)[0]
    assert result["path"] == "repo/first.c" and result["search_match_line"] == 20
    assert result["lines"][0] == {"line": 12, "column": 0, "text": "line 12 first.c"}
    assert result["has_more"]
    continuation = result["next_request"]
    last = result["lines"][-1]
    assert continuation["start_line"] == last["line"] + (not continuation.get("column", 0))


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["rg -n target repo/first.c", "cd repo && rg -n target first.c | head", "grep -n target repo/first.c"])
async def test_single_file_output_without_filename_and_explicit_cd(workspace, command):
    hook = SourceContextEnricher(str(workspace))
    result = await hook.after_tool(event("20:line 20 first.c", command), "1", {})
    assert windows(result)[0]["path"] == "repo/first.c"


@pytest.mark.asyncio
async def test_deduplicates_only_added_context_and_refreshes_changed_source(workspace):
    hook = SourceContextEnricher(str(workspace))
    data = event("repo/first.c:20:line 20 first.c")
    assert await hook.after_tool(data, "1", {})
    assert await hook.after_tool(data, "1", {}) == {}
    assert await hook.after_tool(data, "2", {}) == {}
    assert await hook.after_tool(event("repo/first.c:21:line 21 first.c"), "3", {}) == {}
    source = workspace / "repo/first.c"
    source.write_text(source.read_text() + "\nchanged")
    assert await hook.after_tool(data, "4", {})
    assert data["tool_response"]["stdout"] == "repo/first.c:20:line 20 first.c"


@pytest.mark.asyncio
async def test_windows_and_characters_are_bounded_and_exact(workspace):
    for name in ("first.c", "second.c", "third.c"):
        (workspace / "repo" / name).write_text("match\n" + "天" * 12000)
    data = event("\n".join(f"repo/{n}.c:1:match" for n in ("first", "second", "third")))
    result = windows(await SourceContextEnricher(str(workspace)).after_tool(data, "1", {}))
    assert len(result) == 2
    for item in result:
        assert sum(len(line["text"]) for line in item["lines"]) <= MAX_READ_CHARS
        assert item["has_more"] and item["next_request"]["column"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
async def test_entire_utf8_payload_fits_sdk_and_continuations_lose_no_characters(workspace, locale):
    source = '源代码\\"\t' * 2500
    for name in ("first.c", "second.c"):
        (workspace / "repo" / name).write_text("match\n" + source)
    data = event("repo/first.c:1:match\nrepo/second.c:1:match")
    result = windows(await SourceContextEnricher(str(workspace), locale=locale).after_tool(data, "1", {}))
    assert len(result) == 2
    for window in result:
        chunks = [line["text"] for line in window["lines"] if line["line"] == 2]
        continuation = window["next_request"]
        while continuation:
            page = read_page(workspace, continuation)
            chunks.extend(line["text"] for line in page["lines"] if line["line"] == 2)
            continuation = page["next_request"]
        assert "".join(chunks) == source


@pytest.mark.asyncio
async def test_large_prefix_does_not_hide_match_or_deduplicate_unseen_source(workspace):
    source = workspace / "repo/first.c"
    source.write_text("天" * 12000 + "\nmatch\n" + "天" * 12000 + "\nlast")
    hook = SourceContextEnricher(str(workspace))
    first = windows(await hook.after_tool(event("repo/first.c:2:match"), "1", {}))[0]
    assert first["lines"][0] == {"line": 2, "column": 0, "text": "match"}
    assert first["next_request"]["start_line"] == 3
    assert first["next_request"]["column"] > 0
    second = windows(await hook.after_tool(event("repo/first.c:4:last"), "2", {}))[0]
    assert second["lines"][0] == {"line": 4, "column": 0, "text": "last"}


@pytest.mark.asyncio
async def test_rejects_external_symlink_task_data_and_unverified_text(workspace, tmp_path):
    secret = tmp_path.parent / (tmp_path.name + "-private.c")
    secret.write_text("secret")
    (workspace / "private.c").write_text("secret")
    (workspace / "repo/escape.c").symlink_to(secret)
    hook = SourceContextEnricher(str(workspace))
    for index, value in enumerate(("repo/escape.c:1:secret", "../private.c:1:secret", "private.c:1:secret",
                                   "repo/first.c:20:invented", "repo/first.c:99999:invented")):
        assert await hook.after_tool(event(value), str(index), {}) == {}


@pytest.mark.asyncio
async def test_ignores_non_search_failed_interrupted_and_huge_source(workspace):
    hook = SourceContextEnricher(str(workspace))
    text = "repo/first.c:20:line 20 first.c"
    data = event(text)
    data["tool_response"]["exit_code"] = 2
    assert await hook.after_tool(data, "1", {}) == {}
    data = event(text)
    data["tool_response"]["interrupted"] = True
    assert await hook.after_tool(data, "2", {}) == {}
    assert await hook.after_tool(event(text, "cat report.txt"), "3", {}) == {}
    assert await hook.after_tool(event(text, "rg target repo"), "4", {}) == {}
    (workspace / "repo/large.c").write_bytes(b"x" * (MAX_SOURCE_BYTES + 1))
    assert await hook.after_tool(event("repo/large.c:1:x"), "5", {}) == {}


@pytest.mark.asyncio
async def test_hook_factory_and_different_runs_keep_independent_context(workspace):
    first = execution_hooks("zh", "task-1", str(workspace))["PostToolUse"][0].hooks[0]
    second = execution_hooks("zh", "task-2", str(workspace))["PostToolUse"][0].hooks[0]
    data = event("repo/first.c:20:line 20 first.c")
    assert await first(data, "1", {})
    assert await second(data, "1", {})
    assert execution_hooks("zh") == {}
