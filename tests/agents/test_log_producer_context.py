import json
from pathlib import Path
import pytest
from app.agents import log_producer_context as p


def fixture(root):
    (root / "logs").mkdir()
    lines = []
    for thread, values in [("11", [8, 9, 10]), ("12", [20, 21])]:
        for v in values:
            lines.append(
                f"2026-10-01T00:00:00.{v:03},Tick=7 INFO Tid:{thread} worker.c report 10 value={v}"
            )
    (root / "logs/input.log").write_text("\n".join(lines) + "\n")
    return [("logs/input.log", 1, lines[0])], lines


def test_unmatched_later_thread_provides_discovery_pointer_not_query_match(tmp_path):
    rows, lines = fixture(tmp_path)
    d = p.collect(tmp_path, rows)
    a, b = d["producer_thread_groups"]
    assert (a["physical_records"], b["physical_records"]) == (3, 2)
    assert (a["seed_record_count"], b["seed_record_count"]) == (1, 0)
    assert b["first"][0] == 4 and b["last"][0] == 5
    assert b["last"][2] == lines[-1]
    assert "not files/events" in d["scope"]


@pytest.mark.parametrize("change", ["forged", "wrongline", "wrongproducer"])
def test_unverified_match_does_not_select_a_producer(tmp_path, change):
    rows, lines = fixture(tmp_path)
    if change == "forged":
        rows = [("logs/input.log", 1, lines[0] + " forged")]
    if change == "wrongline":
        rows = [("logs/input.log", 2, lines[0])]
    if change == "wrongproducer":
        rows = [("logs/input.log", 1, lines[0].replace("worker.c", "other.c"))]
    assert not p.collect(tmp_path, rows)["producer_thread_groups"]


def test_thread_reuse_counts_records_not_distinct_events(tmp_path):
    rows, lines = fixture(tmp_path)
    f = tmp_path / "logs/input.log"
    f.write_text(f.read_text() + lines[0] + "\n")
    d = p.collect(tmp_path, rows)
    g = d["producer_thread_groups"][0]
    assert g["physical_records"] == 4 and g["last"][0] == 6


@pytest.mark.parametrize(
    "limit,value",
    [
        ("MAX_FILE_BYTES", 1),
        ("MAX_TOTAL_BYTES", 1),
        ("MAX_LINES", 1),
        ("MAX_GROUPS", 1),
        ("MAX_KEYS", 0),
        ("MAX_SECONDS", -1),
        ("MAX_ROWS", 0),
        ("MAX_OUTPUT_BYTES", 1),
    ],
)
def test_limits_fail_without_partial_completeness(tmp_path, monkeypatch, limit, value):
    rows, _ = fixture(tmp_path)
    monkeypatch.setattr(p, limit, value)
    with pytest.raises(ValueError):
        p.collect(tmp_path, rows)


def test_symlink_and_traversal_rejected(tmp_path):
    rows, lines = fixture(tmp_path)
    f = tmp_path / "logs/input.log"
    f.rename(tmp_path / "outside.log")
    f.symlink_to(tmp_path / "outside.log")
    with pytest.raises(ValueError):
        p.collect(tmp_path, rows)
    with pytest.raises(ValueError):
        p.collect(tmp_path, [("logs/../outside.log", 1, lines[0])])


def test_same_print_function_different_source_lines_are_not_merged(tmp_path):
    rows, lines = fixture(tmp_path)
    f = tmp_path / "logs/input.log"
    f.write_text(f.read_text() + lines[0].replace("report 10", "report 11") + "\n")
    assert (
        sum(
            g["physical_records"]
            for g in p.collect(tmp_path, rows)["producer_thread_groups"]
        )
        == 5
    )


def test_native_head_without_line_numbers_is_verified(tmp_path):
    _, lines = fixture(tmp_path)
    rows = p.native_records(tmp_path, "grep value logs/input.log | head -1", lines[0])
    assert rows == [("logs/input.log", 1, lines[0])]
    assert len(p.collect(tmp_path, rows)["producer_thread_groups"]) == 2


@pytest.mark.parametrize("prefix", ["1:", "logs/input.log:1:"])
def test_native_line_prefix_must_match_actual_bytes(tmp_path, prefix):
    _, lines = fixture(tmp_path)
    assert p.native_records(tmp_path, "grep -n value logs/input.log", prefix + lines[0])


def test_echo_or_forged_labels_are_not_source_evidence(tmp_path):
    _, lines = fixture(tmp_path)
    assert not p.native_records(
        tmp_path, "echo pretend logs/input.log", "04:00 final inventory"
    )
    assert not p.native_records(
        tmp_path, "grep value logs/input.log", lines[0] + "forged"
    )


def test_duplicate_exact_row_is_ambiguous(tmp_path):
    _, lines = fixture(tmp_path)
    f = tmp_path / "logs/input.log"
    f.write_text(f.read_text() + lines[0] + "\n")
    assert not p.native_records(tmp_path, "grep value logs/input.log", lines[0])


@pytest.mark.parametrize("kind", ["binary", "invalid_utf8"])
def test_binary_or_invalid_utf8_is_not_a_complete_record_file(tmp_path, kind):
    rows, _ = fixture(tmp_path)
    f = tmp_path / "logs/input.log"
    f.write_bytes(f.read_bytes() + (b"\0" if kind == "binary" else b"\xff"))
    with pytest.raises(ValueError):
        p.collect(tmp_path, rows)


def test_file_mutation_fails_before_emitting_counts(tmp_path, monkeypatch):
    rows, _ = fixture(tmp_path)
    original = p.identity
    calls = 0

    def changed(path):
        nonlocal calls
        calls += 1
        v = original(path)
        return v if calls == 1 else (*v[:-1], v[-1] + 1)

    monkeypatch.setattr(p, "identity", changed)
    with pytest.raises(ValueError):
        p.collect(tmp_path, rows)


def test_native_semicolon_is_token_boundary_not_shell_execution(tmp_path):
    _, lines = fixture(tmp_path)
    assert p.native_records(
        tmp_path, "echo note; grep value logs/input.log; echo done", lines[0]
    )


def test_output_budget_marks_omission_and_preserves_valid_sample_prefix(
    tmp_path, monkeypatch
):
    rows, _ = fixture(tmp_path)
    full = p.collect(tmp_path, rows)
    monkeypatch.setattr(
        p, "MAX_OUTPUT_BYTES", len(json.dumps(full, ensure_ascii=False).encode()) - 1
    )
    d = p.collect(tmp_path, rows)
    assert len(json.dumps(d, ensure_ascii=False).encode()) <= p.MAX_OUTPUT_BYTES
    assert d["omitted_group_rows"] > 0 and len(d["producer_thread_groups"]) < 2


def test_thread_suffix_is_not_tick_and_ambiguous_labels_are_rejected(tmp_path):
    rows, lines = fixture(tmp_path)
    changed = []
    for i, line in enumerate(lines):
        thread = "11" if i < 3 else "12"
        changed.append(line.replace(" Tid:" + thread, "") + f"(Tk=7, Tid={thread})")
    (tmp_path / "logs/input.log").write_text("\n".join(changed) + "\n")
    d = p.collect(tmp_path, [("logs/input.log", 1, changed[0])])
    assert [
        (g["thread_label"], g["physical_records"]) for g in d["producer_thread_groups"]
    ] == [("11", 3), ("12", 2)]
    assert p.record(lines[0] + "(Tk=7, Tid=99)") is None


def test_native_and_group_scan_share_one_byte_budget(tmp_path, monkeypatch):
    _, lines = fixture(tmp_path)
    size = (tmp_path / "logs/input.log").stat().st_size
    monkeypatch.setattr(p, "MAX_TOTAL_BYTES", size + 1)
    with pytest.raises(ValueError, match="shared byte bound"):
        p.collect_native(tmp_path, "grep value logs/input.log", lines[0])


def test_native_and_group_scan_share_one_deadline(tmp_path, monkeypatch):
    _, lines = fixture(tmp_path)
    clock = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 10.0])
    monkeypatch.setattr(p.time, "monotonic", lambda: next(clock, 10.0))
    with pytest.raises(ValueError):
        p.collect_native(tmp_path, "grep value logs/input.log", lines[0])


def test_fifo_is_rejected_without_blocking(tmp_path):
    import os

    (tmp_path / "logs").mkdir()
    os.mkfifo(tmp_path / "logs/input.log")
    with pytest.raises(ValueError):
        p.collect(tmp_path, [("logs/input.log", 1, "x")])


def test_identity_change_between_native_and_group_scan_rejected(tmp_path, monkeypatch):
    _, lines = fixture(tmp_path)
    original = p.identity
    calls = 0

    def changed(path):
        nonlocal calls
        calls += 1
        v = original(path)
        return v if calls <= 2 else (*v[:-1], v[-1] + 1)

    monkeypatch.setattr(p, "identity", changed)
    with pytest.raises(ValueError):
        p.collect_native(tmp_path, "grep value logs/input.log", lines[0])


def test_excessive_shell_token_hints_fall_back_without_expansion(tmp_path):
    _, lines = fixture(tmp_path)
    assert not p.native_records(
        tmp_path, "echo " + "x " * 130 + "logs/input.log", lines[0]
    )


def native_event(root, lines):
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "cwd": str(root),
        "tool_input": {"command": "grep value logs/input.log | head -1"},
        "tool_response": {"stdout": lines[0], "exit_code": 0},
    }


@pytest.mark.asyncio
async def test_optional_hook_keeps_original_result_deduplicates_facts_across_seed_changes(
    tmp_path,
):
    import copy
    from app.agents.log_topic_context import LogTopicContextEnricher

    _, lines = fixture(tmp_path)
    event = native_event(tmp_path, lines)
    before = copy.deepcopy(event)
    assert (
        await LogTopicContextEnricher(str(tmp_path)).after_tool(event, "off", {}) == {}
    )
    hook = LogTopicContextEnricher(str(tmp_path), producer_context=True)
    result = await hook.after_tool(event, "first", {})
    text = result["hookSpecificOutput"]["additionalContext"]
    facts = json.loads(text.split("\n", 1)[1])
    assert len(facts["producer_thread_groups"]) == 2 and len(text.encode()) <= 8000
    assert event == before
    event["tool_response"]["stdout"] = lines[1]
    assert await hook.after_tool(event, "different-seed-same-facts", {}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["missing_cwd", "outside_cwd", "failed", "interrupted"]
)
async def test_native_hook_rejects_unproved_execution_scope(tmp_path, change):
    from app.agents.log_topic_context import LogTopicContextEnricher

    _, lines = fixture(tmp_path)
    event = native_event(tmp_path, lines)
    if change == "missing_cwd":
        event.pop("cwd")
    if change == "outside_cwd":
        event["cwd"] = "/outside-workspace"
    if change == "failed":
        event["tool_response"]["exit_code"] = 1
    if change == "interrupted":
        event["tool_response"]["interrupted"] = True
    assert (
        await LogTopicContextEnricher(str(tmp_path), producer_context=True).after_tool(
            event, change, {}
        )
        == {}
    )


@pytest.mark.asyncio
async def test_original_supplement_priority_and_no_premature_dedup(
    tmp_path, monkeypatch
):
    from app.agents.log_topic_context import LogTopicContextEnricher
    from app.agents import log_topic_context

    _, lines = fixture(tmp_path)
    event = native_event(tmp_path, lines)
    hook = LogTopicContextEnricher(str(tmp_path), producer_context=True)
    original = hook.collect
    monkeypatch.setattr(hook, "collect", lambda _: "original topic evidence")
    assert (await hook.after_tool(event, "existing-topic", {}))["hookSpecificOutput"][
        "additionalContext"
    ] == "original topic evidence"
    assert not hook.producer_seen
    monkeypatch.setattr(hook, "collect", original)
    monkeypatch.setattr(log_topic_context, "MAX_OUTPUT_BYTES", 100)
    assert (
        await hook.after_tool(event, "too-small", {}) == {} and not hook.producer_seen
    )
    monkeypatch.setattr(log_topic_context, "MAX_OUTPUT_BYTES", 8000)
    assert await hook.after_tool(event, "fits", {})


def test_native_lf_coordinates_do_not_split_vertical_tabs(tmp_path):
    _, lines = fixture(tmp_path)
    lines[0] += "\vcontinued"
    (tmp_path / "logs/input.log").write_text("\n".join(lines) + "\n")
    d = p.collect(tmp_path, [("logs/input.log", 1, lines[0])])
    assert d["files"][0]["lines"] == 5
    assert d["producer_thread_groups"][1]["last"][0] == 5


@pytest.mark.asyncio
async def test_concurrent_callbacks_do_not_repeat_identical_facts(tmp_path):
    import asyncio
    from app.agents.log_topic_context import LogTopicContextEnricher

    _, lines = fixture(tmp_path)
    event = native_event(tmp_path, lines)
    hook = LogTopicContextEnricher(str(tmp_path), producer_context=True)
    results = await asyncio.gather(
        hook.after_tool(event, "one", {}), hook.after_tool(event, "two", {})
    )
    assert sum(bool(x) for x in results) == 1


@pytest.mark.asyncio
async def test_old_topic_scan_and_producer_share_read_budget(tmp_path, monkeypatch):
    from app.agents.log_topic_context import LogTopicContextEnricher

    _, lines = fixture(tmp_path)
    event = native_event(tmp_path, lines)
    hook = LogTopicContextEnricher(str(tmp_path), producer_context=True)

    def old_scan(_):
        hook._topic_read_bytes = p.MAX_TOTAL_BYTES
        return None

    monkeypatch.setattr(hook, "collect", old_scan)
    monkeypatch.setattr(
        p, "read_regular", lambda _: pytest.fail("No remaining shared read budget")
    )
    assert await hook.after_tool(event, "shared-old", {}) == {}


@pytest.mark.asyncio
async def test_structured_search_rows_and_column_validation(tmp_path):
    from app.agents.evidence_tools import encode_evidence
    from app.agents.log_topic_context import LogTopicContextEnricher
    rows,lines=fixture(tmp_path)
    def event(column):
        response=encode_evidence({'results':[{'matches':[{'path':rows[0][0],'line':1,'column':column,'text':lines[0],'line_excerpted':False,'read_request':{'path':rows[0][0],'start_line':1,'line_count':20}}]}]})
        return {'hook_event_name':'PostToolUse','tool_name':'mcp__evidence__search_evidence','tool_input':{'requests':[]},'tool_response':[{'type':'text','text':response}]}
    hook=LogTopicContextEnricher(str(tmp_path),producer_context=True)
    assert await hook.after_tool(event(1),'bad-column',{})=={}
    assert await hook.after_tool(event(0),'complete-row',{})


@pytest.mark.asyncio
async def test_changed_log_identity_does_not_reuse_emitted_counts(tmp_path):
    from app.agents.log_topic_context import LogTopicContextEnricher
    _,lines=fixture(tmp_path);event=native_event(tmp_path,lines)
    hook=LogTopicContextEnricher(str(tmp_path),producer_context=True)
    assert await hook.after_tool(event,'first',{})
    f=tmp_path/'logs/input.log';f.write_text(f.read_text()+lines[-1]+'\n')
    result=await hook.after_tool(event,'changed',{})
    facts=json.loads(result['hookSpecificOutput']['additionalContext'].split('\n',1)[1])
    assert [g['physical_records'] for g in facts['producer_thread_groups']]==[3,3]
