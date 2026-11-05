import json

import pytest

from app.agents.native_search_counts import NativeSearchCounts


@pytest.mark.asyncio
async def test_verified_plain_search_reports_separate_counts_and_deduplicates(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "run.log").write_text(
        "row code=0x12 action=1\nunrelated\nrow code=0x12 action=2\nrow code=0x13 action=1\n"
    )
    data = {
        "tool_name": "Bash",
        "hook_event_name": "PostToolUse",
        "cwd": str(tmp_path),
        "tool_input": {"command": "grep -n row logs/run.log"},
        "tool_response": {"stdout": "1:row code=0x12 action=1\n3:row code=0x12 action=2\n4:row code=0x13 action=1"},
    }
    hook = NativeSearchCounts(str(tmp_path), "en")
    result = await hook.after_tool(data, "first", None)
    note = result["hookSpecificOutput"]["additionalContext"]
    payload = json.loads(note.split("\n", 1)[1])
    assert payload["matched_physical_lines"] == 3
    assert payload["distinct_literal_values_by_field"] == {"action": 2, "code": 2}
    assert payload["first_matching_line"] == 1 and payload["last_matching_line"] == 4
    assert await hook.after_tool(data, "repeat", None) == {}


@pytest.mark.parametrize("command,stdout", [
    ("grep -n row logs/run.log | head -1", "1:row"),
    ("grep -n row logs/run.log", "1:row"),
    ("grep -n row repo/run.log", "1:row\n2:row"),
    ("grep -n 'r.w' logs/run.log", "1:row\n2:row"),
])
def test_unverified_or_out_of_scope_search_is_not_supplemented(tmp_path, command, stdout):
    for directory in ("logs", "repo"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "run.log").write_text("row\nrow\n")
    hook = NativeSearchCounts(str(tmp_path), "en")
    result = hook.collect({
        "cwd": str(tmp_path),
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout},
    })
    assert result is None
