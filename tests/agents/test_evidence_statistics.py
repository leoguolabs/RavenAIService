import asyncio
import hashlib
import json

import pytest

from app.agents import evidence_statistics as stats


def test_numeric_extreme_inside_file_not_just_first_and_last(tmp_path):
    raw = "功率=-55.0\r\n功率=-43.19\n功率=-54.0".encode()
    (tmp_path / "log").write_bytes(raw)
    result = stats.scan_statistics(str(tmp_path), {
        "path": "log", "pattern": r"功率=(?P<value>-?\d+\.\d+)", "mode": "numeric",
    })
    assert result["complete"] and result["matches"] == result["matched_lines"] == 3
    assert result["minimum"]["value"] == "-55.0"
    assert result["maximum"]["value"] == "-43.19"
    assert result["maximum"]["line"] == 2
    assert result["first"]["line"] == 1 and result["last"]["line"] == 3
    assert result["scanned_sha256"] == hashlib.sha256(raw).hexdigest()


def test_counts_occurrences_separately_from_native_lf_lines(tmp_path):
    (tmp_path / "log").write_text("x x\rx\u2028x\nx")
    result = stats.scan_statistics(str(tmp_path), {"path": "log", "pattern": "x"})
    assert result["lines"] == result["matched_lines"] == 2
    assert result["matches"] == 5


def test_numeric_precision_and_hex_without_float_rounding(tmp_path):
    (tmp_path / "log").write_text("n=9007199254740993\nn=9007199254740992\nn=0x10\n")
    result = stats.scan_statistics(str(tmp_path), {
        "path": "log", "pattern": r"n=(?P<value>\S+)", "mode": "numeric",
    })
    assert result["minimum"]["value"] == "0x10"
    assert result["maximum"]["value"] == "9007199254740993"
    assert result["complete"]


def test_extreme_excerpt_coordinates_refer_to_original_characters(tmp_path):
    text = "前缀" * 150 + "value=42 end"
    (tmp_path / "log").write_text(text)
    result = stats.scan_statistics(str(tmp_path), {
        "path": "log", "pattern": r".*value=(?P<value>\d+)", "mode": "numeric",
    })
    sample = result["maximum"]
    assert sample["value_column"] == 306
    assert text[sample["column"]:sample["column"] + len(sample["text"])] == sample["text"]
    assert "42" in sample["text"] and sample["excerpted"]


def test_values_are_literal_and_capped_with_explicit_incompleteness(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "MAX_VALUES", 2)
    (tmp_path / "log").write_text("state=2\nstate=3\nstate=4\nstate=2\n")
    result = stats.scan_statistics(str(tmp_path), {
        "path": "log", "pattern": r"state=(?P<value>\d+)", "mode": "values",
    })
    assert result["matches_complete"] and result["matches"] == 4
    assert not result["complete"] and not result["values_complete"]
    assert result["unindexed_value_matches"] == 1
    assert result["values"] == [
        {"value": "2", "count": 2, "first_line": 1, "last_line": 4},
        {"value": "3", "count": 1, "first_line": 2, "last_line": 2},
    ]


@pytest.mark.parametrize("content", [b"x\n\xff\n", b"x\n\x00\n", b"x\n" + b"a" * 100 + b"\n"])
def test_unreadable_or_overlong_lines_never_establish_absence(tmp_path, monkeypatch, content):
    monkeypatch.setattr(stats, "MAX_LINE_BYTES", 32)
    (tmp_path / "log").write_bytes(content)
    result = stats.scan_statistics(str(tmp_path), {"path": "log", "pattern": "missing"})
    assert result["matches"] == 0
    assert not result["complete"] and not result["matches_complete"]
    assert result["skipped_lines"] == 1
    assert result["skipped_line_refs"][0]["line"] == 2


def test_skipped_line_references_are_bounded_without_hiding_the_count(tmp_path):
    (tmp_path / "log").write_bytes(b"\xff\n" * 9)
    result = stats.scan_statistics(str(tmp_path), {"path": "log", "pattern": "."})
    assert result["skipped_lines"] == 9
    assert result["skipped_line_refs"] == [{"line": n, "reason": "invalid_utf8"} for n in range(1, 5)]
    assert result["omitted_skipped_line_refs"] == 5


def test_byte_budget_does_not_count_a_cut_line_as_a_whole_record(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "MAX_FILE_BYTES", 5)
    (tmp_path / "log").write_bytes(b"x\nxxxxxx\nx\n")
    result = stats.scan_statistics(str(tmp_path), {"path": "log", "pattern": "x"})
    assert result["matches"] == result["lines"] == 1
    assert result["scanned_bytes"] == 5
    assert not result["complete"]


@pytest.mark.parametrize("text", ["NaN", "Infinity", "", "a" * 129])
def test_bad_numeric_values_are_reported_not_silently_dropped(tmp_path, text):
    (tmp_path / "log").write_text("v=" + text)
    result = stats.scan_statistics(str(tmp_path), {
        "path": "log", "pattern": r"v=(?P<value>.*)", "mode": "numeric",
    })
    assert result["matches_complete"] and result["invalid_values"] == 1
    assert not result["complete"] and "minimum" not in result


def test_empty_regular_file_is_a_complete_zero_match_scan(tmp_path):
    (tmp_path / "log").write_bytes(b"")
    result = stats.scan_statistics(str(tmp_path), {"path": "log", "pattern": "x"})
    assert result["complete"] and result["matches"] == result["lines"] == 0


def test_external_symlink_and_directory_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "private").write_text("secret")
    (root / "escape").symlink_to(tmp_path / "private")
    with pytest.raises(ValueError):
        stats.scan_statistics(str(root), {"path": "escape", "pattern": "."})
    with pytest.raises(ValueError):
        stats.scan_statistics(str(root), {"path": ".", "pattern": "."})


def test_changed_file_cannot_be_reported_complete(tmp_path, monkeypatch):
    path = tmp_path / "log"
    path.write_text("x\n")
    original = stats._identity
    calls = []

    def altered_identity(value):
        calls.append(True)
        identity = original(value)
        return identity if len(calls) == 1 else (*identity[:-1], identity[-1] + 1)

    monkeypatch.setattr(stats, "_identity", altered_identity)
    result = stats.scan_statistics(str(tmp_path), {"path": "log", "pattern": "x"})
    assert not result["unchanged_during_scan"] and not result["complete"]


@pytest.mark.asyncio
async def test_real_worker_batch_keeps_order_errors_and_independent_results(tmp_path):
    (tmp_path / "log").write_text("a=2\na=4\n")
    result = await stats.run_statistics_batch(str(tmp_path), [
        {"path": "log", "pattern": r"a=(?P<value>\d+)", "mode": "numeric"},
        {"path": "missing", "pattern": "."},
        {"path": "log", "pattern": "a=", "mode": "count"},
    ])
    assert [r["index"] for r in result["results"]] == [0, 1, 2]
    assert result["results"][0]["maximum"]["value"] == "4"
    assert not result["results"][1]["complete"] and result["results"][1]["error"]
    assert result["results"][2]["matches"] == 2


@pytest.mark.asyncio
async def test_pathological_regex_timeout_reaps_its_worker(tmp_path, monkeypatch):
    (tmp_path / "log").write_text("a" * 5000 + "!")
    monkeypatch.setattr(stats, "WORKER_TIMEOUT", 0.5)
    original = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await stats.run_statistics_batch(str(tmp_path), [{"path": "log", "pattern": "(a+)+$"}])
    assert not result["results"][0]["complete"] and "timed out" in result["results"][0]["error"]
    assert len(processes) == 1 and processes[0].returncode is not None


@pytest.mark.asyncio
async def test_cancelling_regex_scan_reaps_worker(tmp_path, monkeypatch):
    (tmp_path / "log").write_text("a" * 5000 + "!")
    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(stats.run_statistics_batch(str(tmp_path), [{"path": "log", "pattern": "(a+)+$"}]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(processes) == 1 and processes[0].returncode is not None


@pytest.mark.asyncio
async def test_statistics_tool_is_opt_in_and_returns_real_scan(tmp_path, monkeypatch):
    import claude_agent_sdk
    from app.agents.evidence_tools import get_evidence_mcp_server

    monkeypatch.setattr(claude_agent_sdk, "create_sdk_mcp_server", lambda **kwargs: kwargs)
    ordinary = get_evidence_mcp_server(str(tmp_path))
    assert [tool.name for tool in ordinary["tools"]] == ["search_evidence", "read_evidence"]
    enabled = get_evidence_mcp_server(str(tmp_path), statistics=True)
    tool = enabled["tools"][-1]
    assert tool.name == "summarize_evidence"
    (tmp_path / "log").write_text("value=2\nvalue=7\nvalue=3")
    response = await tool.handler({"requests": [{
        "path": "log", "pattern": r"value=(?P<value>\d+)", "mode": "numeric",
    }]})
    result = json.loads(response["content"][0]["text"])["results"][0]
    assert result["complete"] and result["maximum"]["value"] == "7"
