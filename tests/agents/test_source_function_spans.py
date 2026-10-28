"""Whole-function context preserves branches, native coordinates and fallback."""
import copy
import json
from pathlib import Path
import sys

import pytest

from app.agents.evidence_context import MAX_CONTEXT_BYTES, MAX_CONTEXT_FILES, SourceContextEnricher
from app.agents.evidence_read_window import SourceReadWindow
from app.agents.source_function_spans import CFunctionSpans
from app.agents import source_function_spans


def write_source(root: Path, text: str, name: str = "repo/test.c") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    return path


def search_event(stdout: str) -> dict:
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": "rg -n target repo"},
            "tool_response": {"stdout": stdout, "exit_code": 0, "interrupted": False}}


def excerpt(result: dict) -> list:
    text = result["hookSpecificOutput"]["additionalContext"]
    assert len(text.encode()) <= MAX_CONTEXT_BYTES
    return json.loads(text.split("\n", 1)[1])["source_context"]


def test_c_parser_keeps_braces_in_comments_strings_and_conditional_branches(tmp_path):
    path = write_source(tmp_path, '''#if TARGET
int target(int ready) {
    const char *literal = "}";
    /* { is not an opening brace here */
    if (!ready) {
        return -1;
    } else {
        return 0;
    }
}
#endif
int next(void) { return 2; }
''')
    spans = CFunctionSpans().get(path)
    assert [(s.name, s.start_line, s.end_line) for s in spans] == [("target", 2, 10), ("next", 12, 12)]


def test_syntax_errors_unsupported_language_and_encoding_retain_native_fallback(tmp_path):
    parser = CFunctionSpans()
    assert parser.get(write_source(tmp_path, "int broken(void) { if (")) == ()
    assert parser.get(write_source(tmp_path, "def target():\n    pass\n", "repo/test.py")) == ()
    path = write_source(tmp_path, "int target(void) { return 1; }")
    path.write_bytes(path.read_bytes() + b"\xff")
    assert parser.get(path) == ()


def test_missing_parser_is_an_optional_capability(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "tree_sitter", None)
    path = write_source(tmp_path, "int target(void) { return 1; }\n")
    assert CFunctionSpans().get(path) == ()
    result = SourceContextEnricher(str(tmp_path)).collect(search_event("repo/test.c:1:int target(void) { return 1; }"))
    assert result[0]["lines"][0]["text"] == "int target(void) { return 1; }"
    assert "function" not in result[0]


def test_changed_file_invalidates_function_ranges_and_caches_are_bounded(tmp_path, monkeypatch):
    parser = CFunctionSpans()
    path = write_source(tmp_path, "int first(void) { return 1; }\n")
    # Some Docker filesystems give both writes the same timestamp. Exercise
    # this case deterministically, without making the test depend on timing.
    identity = source_function_spans.source_identity(path)
    monkeypatch.setattr(source_function_spans, "source_identity", lambda _: identity)
    assert parser.get(path)[0].name == "first"
    path.write_text("int other(void) { return 1; }\n")
    assert parser.get(path)[0].name == "other"
    for i in range(6):
        parser.get(write_source(tmp_path, "int target(void) { return 0; }\n", f"repo/test{i}.c"))
    assert len(parser.cache) == 4
    assert not CFunctionSpans().cache


def test_file_changed_while_reading_is_not_cached(tmp_path, monkeypatch):
    path = write_source(tmp_path, "int first(void) { return 1; }\n")
    original = source_function_spans._read_source_bytes
    identity = source_function_spans.source_identity(path)
    monkeypatch.setattr(source_function_spans, "source_identity", lambda _: identity)

    def changing_read(current):
        data = original(current)
        if current == path:
            current.write_text("int other(void) { return 2; }\n")
        return data

    monkeypatch.setattr(source_function_spans, "_read_source_bytes", changing_read)
    parser = CFunctionSpans()
    assert parser.get(path) == ()
    assert not parser.cache


@pytest.mark.asyncio
async def test_definition_precedes_callsite_and_includes_failure_branch(tmp_path):
    text = "int wrapper(void) {\n    return target();\n}\nint target(void) {\n    if (!ready) return -1;\n    return 0;\n}\nint unrelated(void) { return 7; }\n"
    write_source(tmp_path, text)
    data = search_event("repo/test.c:2:    return target();\nrepo/test.c:4:int target(void) {")
    before = copy.deepcopy(data)
    windows = excerpt(await SourceContextEnricher(str(tmp_path)).after_tool(data, "t1", {}))
    assert data == before
    assert windows[0]["function"] == {"name": "target", "start_line": 4, "end_line": 7, "range_complete": True}
    assert windows[0]["lines"][-1] == [7, 0, "}"]
    assert [5, 0, "    if (!ready) return -1;"] in windows[0]["lines"]
    assert all(row[0] < 8 for row in windows[0]["lines"])


@pytest.mark.asyncio
async def test_single_complete_function_can_use_more_than_half_payload_budget(tmp_path):
    rows = ["int target(void) {"] + ["    /* " + "x" * 85 + " */" for _ in range(50)] + ["    if (!ready) return -1;", "    return 0;", "}"]
    write_source(tmp_path, "\n".join(rows) + "\n")
    hook = SourceContextEnricher(str(tmp_path))
    windows = excerpt(await hook.after_tool(search_event("repo/test.c:1:int target(void) {"), "t1", {}))
    assert len(windows) == 1 and windows[0]["function"]["range_complete"]
    assert len(json.dumps(windows[0], separators=(",", ":")).encode()) > hook.window_budget
    assert [len(rows) - 2, 0, "    if (!ready) return -1;"] in windows[0]["lines"]
    assert windows[0]["lines"][-1] == [len(rows), 0, "}"]


@pytest.mark.asyncio
async def test_oversized_function_keeps_exact_partial_window_without_completeness_claim(tmp_path):
    rows = ["int target(void) {"] + ["    /* " + "天" * 100 + " */" for _ in range(100)] + ["    return 0;", "}"]
    write_source(tmp_path, "\n".join(rows) + "\n")
    windows = excerpt(await SourceContextEnricher(str(tmp_path)).after_tool(search_event("repo/test.c:1:int target(void) {"), "t1", {}))
    assert "function" not in windows[0]
    assert windows[0]["lines"][0] == [1, 0, rows[0]]
    assert windows[0]["next_request"] is not None


def test_native_read_returns_whole_containing_function_without_neighbor(tmp_path):
    rows = ["int target(void) {"] + ["    /* retained context */" for _ in range(70)] + ["    return 0;", "}"]
    write_source(tmp_path, "\n".join(rows) + "\nint unrelated(void) { return 1; }\n")
    request = {"file_path": "repo/test.c", "offset": 65, "limit": 4}
    actual = SourceReadWindow(str(tmp_path)).expand(request)
    assert actual == {"file_path": "repo/test.c", "offset": 1, "limit": len(rows)}
    assert request == {"file_path": "repo/test.c", "offset": 65, "limit": 4}


def test_read_already_covering_complete_function_does_not_add_unrelated_code(tmp_path):
    write_source(tmp_path, "int target(void) {\n    return 0;\n}\nint unrelated(void) { return 1; }\n")
    assert SourceReadWindow(str(tmp_path)).expand({"file_path": "repo/test.c", "offset": 1, "limit": 3}) is None


def test_read_cost_cache_detects_same_stat_same_size_content_change(tmp_path, monkeypatch):
    path = write_source(tmp_path, ("x" * 50 + "\n") * 100)
    identity = source_function_spans.source_identity(path)
    monkeypatch.setattr(source_function_spans, "source_identity", lambda _: identity)
    window = SourceReadWindow(str(tmp_path))
    request = {"file_path": "repo/test.c", "offset": 10, "limit": 3}
    first = window.expand(request)
    path.write_text(("x" * 101 + "\n") * 50)
    second = window.expand(request)
    assert second["limit"] == 50 < first["limit"]


def test_context_dedup_detects_same_stat_same_size_content_change(tmp_path, monkeypatch):
    path = write_source(tmp_path, "int target(void) {\n    return 1;\n}\n")
    identity = source_function_spans.source_identity(path)
    monkeypatch.setattr(source_function_spans, "source_identity", lambda _: identity)
    context = SourceContextEnricher(str(tmp_path))
    event = search_event("repo/test.c:1:int target(void) {")
    assert "return 1" in str(context.collect(event))
    path.write_text("int target(void) {\n    return 2;\n}\n")
    assert "return 2" in str(context.collect(event))


def test_search_context_bounds_files_without_truncating_original_matches(tmp_path, monkeypatch):
    hits = []
    for i in range(20):
        write_source(tmp_path, "int target(void) { return 0; }\n", f"repo/test{i}.c")
        hits.append(f"repo/test{i}.c:1:int target(void) {{ return 0; }}")
    context = SourceContextEnricher(str(tmp_path))
    parsed = []
    original = context.function_spans.get

    def recording(path, snapshot=None):
        parsed.append(path)
        return original(path, snapshot)

    monkeypatch.setattr(context.function_spans, "get", recording)
    event = search_event("\n".join(hits))
    before = copy.deepcopy(event)
    assert context.collect(event)
    assert len(parsed) == MAX_CONTEXT_FILES
    assert event == before
