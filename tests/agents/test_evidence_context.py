"""Automatic context is exact, bounded, repository-scoped and non-destructive."""
import copy
import json
from pathlib import Path

import pytest

from app.agents.evidence_context import MAX_CONTEXT_BYTES, MAX_SOURCE_BYTES, MCP_READ_TOOL, MCP_SEARCH_TOOL, SourceContextEnricher
from app.agents.evidence_policy import execution_hooks
from app.agents.evidence_tools import MAX_READ_CHARS, encode_evidence, read_page, run_batch


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


async def batch_event(workspace, requests, *, search=True):
    payload = await run_batch(str(workspace), requests, search=search)
    return {"hook_event_name": "PostToolUse", "tool_name": MCP_SEARCH_TOOL if search else MCP_READ_TOOL,
            "tool_input": {"requests": requests},
            "tool_response": [{"type": "text", "text": encode_evidence(payload)}]}


@pytest.mark.asyncio
async def test_batch_read_supplies_missing_branch_and_preserves_errors_and_continuations(workspace):
    source = "int identify(int fd) {\n    int id = 7;\n    if (fd < 0) {\n        return 0;\n    }\n    return id + 1;\n}\n"
    (workspace / "repo/first.c").write_text(source)
    data = await batch_event(workspace, [
        {"path": "repo/first.c", "start_line": 3, "line_count": 2},
        {"path": "missing", "start_line": 1, "line_count": 2},
    ], search=False)
    before = copy.deepcopy(data)
    hook = SourceContextEnricher(str(workspace))
    result = windows(await hook.after_tool(data, "read-1", {}))
    assert data == before
    assert len(result) == 1 and result[0]["source_read_line"] == 3
    assert result[0]["function"] == {"name": "identify", "start_line": 1, "end_line": 7, "range_complete": True}
    assert [row["text"] for row in result[0]["lines"]] == source.splitlines()
    assert await hook.after_tool(data, "read-2", {}) == {}
    assert await hook.after_tool(event("    return 0;", "sed -n 4,4p repo/first.c"), "shell-3", {}) == {}
    assert await hook.after_tool(event("repo/first.c:1:int identify(int fd) {"), "search-4", {}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["changed_text", "truncated", "partial_line", "reordered_rows", "row_bool",
    "column", "tail", "index", "index_bool", "path", "columns", "failed", "extra_result", "long", "complete", "malformed"])
async def test_batch_read_requires_exact_full_requested_range(workspace, mutation):
    source = "int value(void) {\n    int n = 7;\n    return n;\n}\n"
    (workspace / "repo/first.c").write_text(source)
    data = await batch_event(workspace, [{"path": "repo/first.c", "start_line": 2, "line_count": 2}], search=False)
    payload = json.loads(data["tool_response"][0]["text"])
    result = payload["results"][0]
    if mutation == "changed_text":
        result["lines"][1][2] = "    return 0;"
    elif mutation == "truncated":
        result["lines"].pop()
    elif mutation == "partial_line":
        result["lines"][-1][2] = "    return"
    elif mutation == "reordered_rows":
        result["lines"].reverse()
    elif mutation == "row_bool":
        result["lines"][0][1] = False
    elif mutation == "column":
        data["tool_input"]["requests"][0]["column"] = 1
    elif mutation == "tail":
        data["tool_input"]["requests"][0]["tail_lines"] = 2
    elif mutation in ("index", "index_bool"):
        result["index"] = 1 if mutation == "index" else False
    elif mutation == "path":
        result["path"] = "repo/second.c"
    elif mutation == "columns":
        payload["line_columns"] = ["line", "text", "column"]
    elif mutation == "failed":
        result["error"] = "read failed"
    elif mutation == "extra_result":
        payload["results"].append(copy.deepcopy(result))
    elif mutation == "long":
        data["tool_input"]["requests"][0]["line_count"] = 80
    elif mutation == "complete":
        data = await batch_event(workspace, [{"path": "repo/first.c", "line_count": 4}], search=False)
        payload = json.loads(data["tool_response"][0]["text"])
    data["tool_response"][0]["text"] = "not JSON" if mutation == "malformed" else json.dumps(payload)
    before = copy.deepcopy(data)
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "read", {}) == {}
    assert data == before


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
async def test_batch_read_shares_budget_and_retains_missing_context_after_partial_coverage(workspace, locale):
    from app.agents.source_function_spans import source_snapshot
    source = 'int value(void) {\n    /* ' + '源码\\"' * 180 + ' */\n    return 7;\n}\n'
    for name in ("first", "second", "third"):
        (workspace / "repo" / f"{name}.c").write_text(source)
    hook = SourceContextEnricher(str(workspace), locale=locale)
    path = workspace / "repo/first.c"
    hook.covered[(str(path), *source_snapshot(path)[0])] = [(1, 3)]
    data = await batch_event(workspace, [
        {"path": f"repo/{name}.c", "start_line": 3, "line_count": 1} for name in ("first", "second", "third")
    ], search=False)
    result = windows(await hook.after_tool(data, "batch", {}))
    assert len(result) == 2
    assert result[0]["path"] == "repo/first.c" and result[0]["function"]["end_line"] == 4
    assert all(window["function"]["range_complete"] for window in result)


@pytest.mark.asyncio
async def test_batch_read_never_enriches_logs_external_paths_or_oversize_functions(workspace, tmp_path):
    source = "int value(void) {\n    return 7;\n}\n"
    (workspace / "logs").mkdir()
    (workspace / "logs/first.c").write_text(source)
    outside = tmp_path.parent / (tmp_path.name + "-mcp-outside.c")
    outside.write_text(source)
    (workspace / "repo/link.c").symlink_to(outside)
    (workspace / "repo/first.c").write_text('int value(void) {\n    /* ' + 'x' * 8200 + ' */\n    return 7;\n}\n')
    data = await batch_event(workspace, [
        {"path": "logs/first.c", "start_line": 2, "line_count": 1},
        {"path": "repo/link.c", "start_line": 2, "line_count": 1},
        {"path": "repo/first.c", "start_line": 3, "line_count": 1},
    ], search=False)
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "read", {}) == {}


@pytest.mark.asyncio
async def test_batch_read_source_change_after_verification_keeps_original_only(workspace, monkeypatch):
    import app.agents.evidence_context as module
    path = workspace / "repo/first.c"
    source = "int value(void) {\n    int n = 7;\n    return n;\n}\n"
    path.write_text(source)
    data = await batch_event(workspace, [{"path": "repo/first.c", "start_line": 2, "line_count": 1}], search=False)
    original, calls = module.source_snapshot, 0

    def changed(p):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text(source.replace("return n", "return 0"))
        return original(p)

    monkeypatch.setattr(module, "source_snapshot", changed)
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "read", {}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "sed -n '3,5p' repo/first.c",
    "cd repo && sed -n 3,5p first.c",
    "cd repo; sed -n '3,5p' first.c; echo ====; sed -n 3,4p second.c",
])
async def test_literal_shell_read_adds_missing_function_branch_without_rewriting(workspace, command):
    source = "int identify(int fd) {\n    int id = 7;\n    if (fd < 0) {\n        id = 0;\n        return id;\n    }\n    return id + 1;\n}\n"
    (workspace / "repo/first.c").write_text(source)
    stdout = "".join(source.splitlines(keepends=True)[2:5])
    if "second.c" in command:
        stdout += "====\nline 3 second.c\nline 4 second.c\n"
    data = event(stdout, command)
    before = copy.deepcopy(data)
    hook = SourceContextEnricher(str(workspace))
    result = windows(await hook.after_tool(data, "read-1", {}))
    assert data == before
    assert len(result) == 1 and result[0]["source_read_line"] == 3
    assert result[0]["function"] == {"name": "identify", "start_line": 1, "end_line": 8, "range_complete": True}
    assert [row["text"] for row in result[0]["lines"]] == source.splitlines()
    assert await hook.after_tool(data, "read-2", {}) == {}
    assert await hook.after_tool(event("repo/first.c:1:int identify(int fd) {"), "search-3", {}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["changed_text", "truncated", "duplicate", "prefix", "pipeline",
                                        "substitution", "redirect", "failed", "interrupted", "complete", "log", "alias"])
async def test_shell_read_falls_back_without_verified_unambiguous_literal_output(workspace, mutation):
    source = "int value(void) {\n    int n = 7;\n    return n;\n}\n"
    (workspace / "repo/first.c").write_text(source)
    data = event("    int n = 7;\n    return n;\n", "sed -n '2,3p' repo/first.c")
    if mutation == "changed_text":
        data["tool_response"]["stdout"] = data["tool_response"]["stdout"].replace("7", "8")
    elif mutation == "truncated":
        data["tool_response"]["stdout"] = "    int n = 7;\n"
    elif mutation == "duplicate":
        data["tool_response"]["stdout"] *= 2
    elif mutation == "prefix":
        data["tool_response"]["stdout"] = "prefix" + data["tool_response"]["stdout"]
    elif mutation == "pipeline":
        data["tool_input"]["command"] += " | head"
    elif mutation == "substitution":
        data["tool_input"]["command"] = "sed -n '2,3p' $(echo repo/first.c)"
    elif mutation == "redirect":
        data["tool_input"]["command"] += " 2>/dev/null"
    elif mutation in ("failed", "interrupted"):
        data["tool_response"]["exit_code" if mutation == "failed" else "interrupted"] = 1
    elif mutation == "complete":
        data["tool_input"]["command"] = "sed -n '1,4p' repo/first.c"
        data["tool_response"]["stdout"] = source
    elif mutation == "log":
        (workspace / "logs").mkdir()
        (workspace / "logs/first.c").write_text(source)
        data["tool_input"]["command"] = "sed -n '2,3p' logs/first.c"
    elif mutation == "alias":
        data["tool_input"]["command"] = "echo sed -n '2,3p' repo/first.c"
    before = copy.deepcopy(data)
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "read", {}) == {}
    assert data == before


@pytest.mark.asyncio
async def test_shell_read_partial_prior_context_does_not_hide_missing_function_branch(workspace):
    source = "int value(void) {\n    int n = 7;\n    return n;\n}\n"
    path = workspace / "repo/first.c"
    path.write_text(source)
    from app.agents.source_function_spans import source_snapshot
    hook = SourceContextEnricher(str(workspace))
    hook.covered[(str(path), *source_snapshot(path)[0])] = [(1, 2)]
    data = event("    int n = 7;\n", "sed -n 2,2p repo/first.c")
    assert windows(await hook.after_tool(data, "read", {}))[0]["function"]["end_line"] == 4


@pytest.mark.asyncio
async def test_shell_read_accepts_sdk_final_lf_removal_but_not_missing_source_characters(workspace):
    source = "int value(void) {\n    int n = 7;\n    return n;\n}\n"
    (workspace / "repo/first.c").write_text(source)
    data = event("    int n = 7;\n    return n;", "sed -n 2,3p repo/first.c")
    original = copy.deepcopy(data)
    assert windows(await SourceContextEnricher(str(workspace)).after_tool(data, "trimmed-lf", {}))
    assert data == original
    data["tool_response"]["stdout"] = "    int n = 7;\n    return n"
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "missing-semicolon", {}) == {}


@pytest.mark.asyncio
async def test_shell_read_keeps_whole_function_budget_and_no_partial_fallback(workspace):
    source = 'int value(void) {\n    /* ' + 'x' * 8200 + ' */\n    return 7;\n}\n'
    (workspace / "repo/first.c").write_text(source)
    data = event("    return 7;", "sed -n 3,3p repo/first.c")
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "oversized", {}) == {}


@pytest.mark.asyncio
async def test_shell_read_ambiguous_base_paths_and_external_cd_keep_original_only(workspace, tmp_path):
    source = "int value(void) {\n    return 7;\n}\n"
    (workspace / "repo/first.c").write_text(source)
    (workspace / "repo/sub").mkdir()
    (workspace / "repo/sub/first.c").write_text(source)
    data = event("    return 7;", "sed -n 2,2p first.c", cwd=str(workspace / "repo/sub"))
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "ambiguous", {}) == {}
    data["tool_input"]["command"] = f"cd {tmp_path.parent}; sed -n 2,2p repo/first.c"
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "external-cd", {}) == {}


@pytest.mark.asyncio
async def test_shell_read_changed_source_and_external_symlink_are_not_added(workspace, tmp_path, monkeypatch):
    import app.agents.evidence_context as module
    source = "int value(void) {\n    int n = 7;\n    return n;\n}\n"
    outside = tmp_path.parent / (tmp_path.name + "-outside-read.c")
    outside.write_text(source)
    path = workspace / "repo/first.c"
    path.unlink()
    path.symlink_to(outside)
    data = event("    int n = 7;\n", "sed -n 2,2p repo/first.c")
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "outside", {}) == {}
    path.unlink()
    path.write_text(source)
    original = module.source_snapshot
    calls = 0

    def changed(p):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text(source.replace("return n", "return 0"))
        return original(p)

    monkeypatch.setattr(module, "source_snapshot", changed)
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "changed", {}) == {}


@pytest.mark.asyncio
async def test_batch_search_adds_complete_function_without_changing_requested_evidence(workspace):
    source = "void before(void) {\n    target();\n}\n\nint target(void) {\n    int value = 7;\n    return value;\n}\n\nint after(void) { return 9; }\n"
    (workspace / "repo/first.c").write_text(source)
    data = await batch_event(workspace, [
        {"path": "repo/first.c", "pattern": "target", "context_lines": 1},
        {"path": "missing", "pattern": "target"},
    ])
    before = copy.deepcopy(data)
    hook = SourceContextEnricher(str(workspace))
    result = windows(await hook.after_tool(data, "batch-1", {}))
    assert data == before  # Search matches, requested context, error and paging stay intact.
    assert result[0]["function"] == {"name": "target", "start_line": 5, "end_line": 8, "range_complete": True}
    lines = source.splitlines()
    assert all(row["text"] == lines[row["line"] - 1] for w in result for row in w["lines"])
    # The same hook instance deduplicates added context across native and MCP tools.
    assert await hook.after_tool(event("repo/first.c:5:int target(void) {"), "native-2", {}) == {}
    (workspace / "repo/first.c").write_text(source + "// changed\n")
    assert await hook.after_tool(data, "batch-3", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
async def test_batch_search_uses_one_context_budget_across_requests(workspace, locale):
    for name in ("first", "second", "third"):
        (workspace / "repo" / f"{name}.c").write_text("match\n" + '源代码\\"\t' * 2500)
    data = await batch_event(workspace, [
        {"path": f"repo/{name}.c", "pattern": "match"} for name in ("first", "second", "third")
    ])
    original = copy.deepcopy(data)
    result = windows(await SourceContextEnricher(str(workspace), locale=locale).after_tool(data, "batch", {}))
    assert len(result) == 2  # Limit applies to the entire batch, not separately to every request.
    assert data == original
    assert all(window["next_request"] for window in result)
    for window in result:
        assert window["lines"][0]["text"] == "match"


@pytest.mark.asyncio
async def test_batch_search_never_enriches_logs_or_external_symlink(workspace, tmp_path):
    (workspace / "logs").mkdir()
    (workspace / "logs/file.c").write_text("match\nlog evidence\n")
    external = tmp_path.parent / (tmp_path.name + "-outside.c")
    external.write_text("match\nsecret source\n")
    (workspace / "repo/link.c").symlink_to(external)
    data = await batch_event(workspace, [{"path": "logs/file.c", "pattern": "match"}])
    payload = json.loads(data["tool_response"][0]["text"])
    payload["results"][0]["matches"].append(["repo/link.c", 1, 0, "match", False])
    data["tool_response"][0]["text"] = json.dumps(payload)
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "batch", {}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["columns", "excerpted", "wrong_text", "invalid_line", "object_row", "json", "read_tool"])
async def test_unsupported_or_unverified_batch_output_retains_original_only(workspace, mutation):
    data = await batch_event(workspace, [{"path": "repo/first.c", "pattern": "line 20 "}])
    payload = json.loads(data["tool_response"][0]["text"])
    if mutation == "columns":
        payload["match_columns"] = list(reversed(payload["match_columns"]))
    elif mutation == "excerpted":
        payload["results"][0]["matches"][0][4] = True
    elif mutation == "wrong_text":
        payload["results"][0]["matches"][0][3] = "not the original source line"
    elif mutation == "invalid_line":
        payload["results"][0]["matches"][0][1] = True
    elif mutation == "object_row":
        payload["results"][0]["matches"][0] = {"path": "repo/first.c"}
    elif mutation == "read_tool":
        data["tool_name"] = "mcp__evidence__read_evidence"
    data["tool_response"][0]["text"] = "invalid json" if mutation == "json" else json.dumps(payload)
    before = copy.deepcopy(data)
    assert await SourceContextEnricher(str(workspace)).after_tool(data, "batch", {}) == {}
    assert data == before


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
async def test_source_verification_and_context_use_native_lf_line_numbers(workspace):
    (workspace / "repo/first.c").write_bytes("banner\rprogress\nmatch\u2028tail\rdata\nlast\r\n".encode())
    result = windows(await SourceContextEnricher(str(workspace)).after_tool(
        event("repo/first.c:2:match\u2028tail\rdata\n"), "1", {}
    ))[0]
    assert result["search_match_line"] == 2
    assert result["lines"] == [
        {"line": 1, "column": 0, "text": "banner\rprogress"},
        {"line": 2, "column": 0, "text": "match\u2028tail\rdata"},
        {"line": 3, "column": 0, "text": "last"},
    ]


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
    first = execution_hooks("zh", "task-1", str(workspace), source_context=True)["PostToolUse"][0].hooks[0]
    second = execution_hooks("zh", "task-2", str(workspace), source_context=True)["PostToolUse"][0].hooks[0]
    data = event("repo/first.c:20:line 20 first.c")
    assert await first(data, "1", {})
    assert await second(data, "1", {})
    assert execution_hooks("zh") == {}
