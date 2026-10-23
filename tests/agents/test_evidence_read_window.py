"""Native source read coalescing must preserve every requested physical line."""
from pathlib import Path

import pytest

from app.agents.evidence_read_window import MAX_SOURCE_BYTES, MAX_SOURCE_LINES, MAX_WINDOW_BYTES, MAX_WINDOW_LINES, SourceReadWindow


def source(root: Path, body: bytes | None = None, name: str = "repo/test.c") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body if body is not None else b"".join(f"row{i}\n".encode() for i in range(1, 401)))
    return path


def assert_preserved(path, requested, actual):
    rows = path.read_bytes().split(b"\n")
    if not rows[-1]:
        rows.pop()
    first, last = actual["offset"], actual["offset"] + actual["limit"] - 1
    start, end = requested["offset"], requested["offset"] + requested["limit"] - 1
    assert first <= start <= end <= last <= len(rows)
    assert actual["limit"] <= MAX_WINDOW_LINES
    assert sum(len(line) + 32 for line in rows[first - 1:last]) <= MAX_WINDOW_BYTES
    returned = rows[first - 1:last]
    assert returned[start - first:end - first + 1] == rows[start - 1:end]


@pytest.mark.parametrize("start,count", [(1, 1), (100, 30), (200, 79), (390, 10)])
@pytest.mark.parametrize("absolute", [False, True])
def test_expanded_range_contains_all_original_lines(tmp_path, start, count, absolute):
    path = source(tmp_path)
    request = {"file_path": str(path) if absolute else "repo/test.c", "offset": start, "limit": count}
    result = SourceReadWindow(str(tmp_path)).expand(request)
    assert result is not None
    assert result["file_path"] == request["file_path"]
    assert request["offset"] == start and request["limit"] == count
    assert_preserved(path, request, result)


def test_utf8_cr_and_unicode_separators_keep_native_lf_coordinates(tmp_path):
    path = source(tmp_path, ("源码\r片段\u2028仍同一行\n" * 200).encode())
    request = {"file_path": "repo/test.c", "offset": 80, "limit": 20}
    result = SourceReadWindow(str(tmp_path)).expand(request)
    assert_preserved(path, request, result)


def test_large_requested_row_is_never_replaced_or_truncated(tmp_path):
    source(tmp_path, b"first\n" + b"x" * MAX_WINDOW_BYTES + b"\nlast\n")
    assert SourceReadWindow(str(tmp_path)).expand({"file_path": "repo/test.c", "offset": 2, "limit": 1}) is None


def test_byte_budget_shrinks_only_added_context(tmp_path):
    path = source(tmp_path, b"".join(("行" * 100 + "\n").encode() for _ in range(200)))
    request = {"file_path": "repo/test.c", "offset": 70, "limit": 5}
    result = SourceReadWindow(str(tmp_path)).expand(request)
    assert 5 < result["limit"] < MAX_WINDOW_LINES
    assert_preserved(path, request, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,body,extra", [
    ("logs/test.c", None, {}), ("repo/.git/test.c", None, {}),
    ("repo/test.log", None, {}), ("repo/test.c", b"bad\0source\n", {}),
    ("repo/test.c", b"bad\xffsource\n", {}), ("repo/test.c", None, {"limit": 80}),
    ("repo/test.c", None, {"limit": True}), ("repo/test.c", None, {"offset": 0}),
    ("repo/test.c", None, {"offset": 450}), ("repo/test.c", None, {"offset": None}),
    ("repo/test.c", None, {"limit": None}), ("repo/test.c", None, {"file_path": "."}),
])
async def test_unsupported_read_uses_original_native_path(tmp_path, name, body, extra):
    source(tmp_path, body, name)
    request = {"file_path": name, "offset": 2, "limit": 5, **extra}
    result = await SourceReadWindow(str(tmp_path)).before_tool(
        {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": request}, "t1", {})
    assert result == {}


@pytest.mark.asyncio
async def test_path_and_symlink_escape_do_not_expand(tmp_path):
    root = tmp_path / "workspace"
    path = source(tmp_path)
    (root / "repo").mkdir(parents=True)
    (root / "repo/link.c").symlink_to(path)
    window = SourceReadWindow(str(root))
    for name in [str(path), "../repo/test.c", "repo/link.c", "repo/missing.c"]:
        assert await window.before_tool({"hook_event_name": "PreToolUse", "tool_name": "Read",
                                        "tool_input": {"file_path": name, "offset": 2, "limit": 5}}, None, {}) == {}


def test_changed_file_invalidates_cached_byte_counts(tmp_path):
    path = source(tmp_path)
    window = SourceReadWindow(str(tmp_path))
    request = {"file_path": "repo/test.c", "offset": 70, "limit": 5}
    first = window.expand(request)
    path.write_bytes((b"z" * 250 + b"\n") * 200)
    second = window.expand(request)
    assert second["limit"] < first["limit"]
    assert_preserved(path, request, second)


@pytest.mark.asyncio
async def test_hook_rewrites_only_read_range_and_describes_actual_lines(tmp_path):
    path = source(tmp_path)
    request = {"file_path": str(path), "offset": 100, "limit": 30}
    window = SourceReadWindow(str(tmp_path), locale="en")
    data = {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": request}
    result = (await window.before_tool(data, "read1", {}))["hookSpecificOutput"]
    assert result["hookEventName"] == "PreToolUse"
    assert result["permissionDecision"] == "allow"
    assert "100-129" in result["additionalContext"]
    assert_preserved(path, request, result["updatedInput"])
    assert await window.before_tool({**data, "tool_name": "Bash"}, None, {}) == {}
    assert await window.before_tool({**data, "hook_event_name": "PostToolUse"}, None, {}) == {}


def test_separate_runs_have_independent_bounded_caches(tmp_path):
    first, second = SourceReadWindow(str(tmp_path)), SourceReadWindow(str(tmp_path))
    for i in range(6):
        name = f"related_repos/project/file{i}.c"
        source(tmp_path, name=name)
        assert first.expand({"file_path": name, "offset": 100, "limit": 20}) is not None
    assert len(first.cache) == 4
    assert not second.cache


@pytest.mark.parametrize("body", [b"x" * (MAX_SOURCE_BYTES + 1), b"\n" * (MAX_SOURCE_LINES + 1)])
def test_oversized_source_keeps_native_request(tmp_path, body):
    source(tmp_path, body)
    assert SourceReadWindow(str(tmp_path)).expand({"file_path": "repo/test.c", "offset": 1, "limit": 1}) is None
