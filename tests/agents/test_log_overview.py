"""Mechanical overview preserves file boundaries, original lines and limits."""
import hashlib
import json
from pathlib import Path

import pytest

from app.agents import log_overview as overview


def record(n, message="failed", level="ERROR", location=10):
    return f"2026-01-01T00:00:{n:02}.000,Tk={n} {level} Tid:2 test.c test_fn {location} {message}"


@pytest.fixture
def logs(tmp_path):
    (tmp_path / "logs").mkdir()
    return tmp_path


def inspect(root):
    return overview.collect_log_overview(str(root), str(root / "logs"))


def decoded_groups(value):
    groups = []
    for row in value["warning_groups"]:
        group = dict(zip(value["warning_group_columns"], row, strict=True))
        group["path"] = value["files"][group.pop("file_index")]["path"]
        if group["last"] is None:
            group["last"] = group["first"]
        groups.append(group)
    return groups


def test_full_scan_counts_records_per_file_and_preserves_exact_lf_samples(logs):
    data = (record(1, "first\rprogress") + "\nunknown\u2028line\n" + record(2, "last") + "\r\n" + record(3, "success", "INFO") + "\r").encode()
    for name in ("a.log", "overlap.log"):
        (logs / "logs" / name).write_bytes(data)
    value = inspect(logs)
    assert value["scan_complete"] is True
    assert len(value["files"]) == len(value["warning_groups"]) == 2
    for file in value["files"]:
        assert file["lines"] == 4 and file["parsed_lines"] == 3 and file["other_lines"] == 1
        assert file["levels"] == {"ERROR": 2, "INFO": 1}
        assert file["scanned_sha256"] == hashlib.sha256(data).hexdigest()
    for group in decoded_groups(value):
        assert group["count"] == 2
        assert group["first"] == [1, "2026-01-01T00:00:01.000", "first\rprogress"]
        assert group["last"] == [3, "2026-01-01T00:00:02.000", "last"]


def test_tick_format_and_plain_format_are_recognized_without_timestamp_rewriting(logs):
    (logs / "logs/a.log").write_text(
        "2026-06-20T10:34:25.445.204,Tick=10840   ERROR Tid:1575    source.c  fn  3200    first\n"
        "2026-06-20T10:34:26.123 ERROR source.c fn 3200 last\n"
    )
    value = inspect(logs)
    assert value["files"][0]["parsed_lines"] == 2
    group = decoded_groups(value)[0]
    assert group["count"] == 2
    assert group["first"] == [1, "2026-06-20T10:34:25.445.204", "first"]
    assert group["last"] == [2, "2026-06-20T10:34:26.123", "last"]


def test_byte_limit_never_counts_a_partial_record_or_claims_whole_file(logs, monkeypatch):
    data = (record(1) + "\n" + record(2) + "\n").encode()
    (logs / "logs/a.log").write_bytes(data)
    monkeypatch.setattr(overview, "MAX_FILE_BYTES", len(data) - 10)
    value = inspect(logs)
    assert value["scan_complete"] is False
    assert value["files"][0]["lines"] == 1
    assert decoded_groups(value)[0]["count"] == 1
    assert value["files"][0]["scanned_bytes"] == len(data) - 10


def test_oversized_lines_preserve_following_native_line_numbers(logs, monkeypatch):
    monkeypatch.setattr(overview, "MAX_LINE_BYTES", 200)
    (logs / "logs/a.log").write_text("X" * 1000 + "\n" + record(1) + "\n")
    value = inspect(logs)
    assert value["files"][0]["complete"]
    assert value["files"][0]["lines"] == 2
    assert value["files"][0]["other_lines"] == 1
    assert decoded_groups(value)[0]["first"][0] == 2


def test_binary_and_escaping_symlinks_remain_explicitly_unscanned(logs, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.log").write_text(record(1, "DO NOT READ"))
    (logs / "logs/binary.bin").write_bytes(b"\0binary")
    (logs / "logs/link").symlink_to(outside, target_is_directory=True)
    (logs / "logs/secret.log").symlink_to(outside / "secret.log")
    value = inspect(logs)
    assert value["skipped_paths"] == 2
    assert not value["scan_complete"]
    assert value["files"][0]["skipped"] == "binary"
    assert "DO NOT READ" not in json.dumps(value)
    with pytest.raises(ValueError):
        overview.collect_log_overview(str(logs), str(outside))


def test_limits_bound_scan_and_encoded_payload_without_hiding_omissions(logs, monkeypatch):
    for i in range(3):
        (logs / "logs" / f"{i}.log").write_text("\n".join(record(n, "异常" * 300, location=n) for n in range(20)))
    monkeypatch.setattr(overview, "MAX_FILES", 2)
    monkeypatch.setattr(overview, "MAX_GROUPS_PER_FILE", 4)
    monkeypatch.setattr(overview, "MAX_PROMPT_BYTES", 1800)
    value = inspect(logs)
    assert not value["enumeration_complete"] and not value["scan_complete"]
    assert len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) <= 1800
    assert value["omitted_group_rows"] > 0
    assert value["files"][0]["unindexed_warning_records"] == 16
    assert value["files"][0]["levels"]["ERROR"] == 20


def test_budget_and_deadline_do_not_silently_claim_success(logs, monkeypatch):
    (logs / "logs/a.log").write_text(record(1))
    monkeypatch.setattr(overview, "MAX_TOTAL_BYTES", 0)
    value = inspect(logs)
    assert not value["scan_complete"] and value["files"][0]["scanned_bytes"] == 0
    monkeypatch.setattr(overview, "MAX_SCAN_SECONDS", 0)
    value = inspect(logs)
    assert not value["scan_complete"] and not value["enumeration_complete"]


def test_file_changed_during_scan_is_not_a_complete_snapshot(logs, monkeypatch):
    path = logs / "logs/a.log"
    path.write_text(record(1))
    real_stat = Path.stat
    calls = 0

    def changed_stat(p, *args, **kwargs):
        nonlocal calls
        if p == path:
            calls += 1
            if calls == 2:
                with p.open("a") as stream:
                    stream.write("\n" + record(2))
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", changed_stat)
    info, _ = overview._scan_file(path, logs, 10000, float("inf"))
    assert not info["unchanged_during_scan"] and not info["complete"]


def test_overview_guidance_does_not_turn_structural_counts_into_diagnosis(logs):
    value = inspect(logs)
    for locale in ("en", "zh"):
        prompt = overview.log_overview_prompt(value, locale)
        assert "raven-log-overview-v2" in prompt
        assert "原生工具" in prompt if locale == "zh" else "native tools" in prompt
        assert "因果" in prompt if locale == "zh" else "causation" in prompt


def test_rare_critical_group_survives_more_than_twelve_frequent_groups(logs):
    lines = [record(1, f"warning {i}", "WARNING", i) for i in range(13) for _ in range(4)]
    lines += [record(2, "rare critical event", "CRIT", 99)]
    (logs / "logs/a.log").write_text("\n".join(lines))
    value = inspect(logs)
    groups = decoded_groups(value)
    assert value["omitted_group_rows"] == 0 and len(groups) == 14
    rare = next(g for g in groups if g["level"] == "CRIT")
    assert rare["count"] == 1
    assert rare["first"] == rare["last"] == [53, "2026-01-01T00:00:02.000", "rare critical event"]
    assert len(overview._encode(value).encode()) <= overview.MAX_PROMPT_BYTES


def test_preview_budget_prioritizes_severity_and_counts_omitted_groups(logs, monkeypatch):
    lines = [record(1, '频繁 "警告"' * 25, "WARNING", i) for i in range(20) for _ in range(5)]
    lines += [record(2, "rare critical event", "CRIT", 99)]
    (logs / "logs/a.log").write_text("\n".join(lines))
    monkeypatch.setattr(overview, "MAX_PROMPT_BYTES", 1800)
    value = inspect(logs)
    groups = decoded_groups(value)
    assert len(overview._encode(value).encode()) <= 1800
    assert groups[0]["level"] == "CRIT" and groups[0]["first"][0] == 101
    assert value["omitted_group_rows"] == 21 - len(groups) > 0
    assert value["files"][0]["levels"] == {"WARNING": 100, "CRIT": 1}


def test_single_sample_compression_and_removed_inventory_keep_valid_file_references(logs, monkeypatch):
    for n in range(15):
        (logs / "logs" / f"{n:02}.log").write_text(record(1, "single event"))
    value = inspect(logs)
    assert value["warning_groups"][0][value["warning_group_columns"].index("last")] is None
    monkeypatch.setattr(overview, "MAX_PROMPT_BYTES", 1800)
    value = inspect(logs)
    groups = decoded_groups(value)  # raises if an index references an omitted file
    assert value["omitted_file_rows"] == 15 - len(value["files"]) > 0
    assert value["omitted_group_rows"] == 15 - len(groups)
    assert len(overview._encode(value).encode()) <= 1800


def test_impossible_preview_budget_fails_instead_of_suppressing_coverage(logs, monkeypatch):
    monkeypatch.setattr(overview, "MAX_PROMPT_BYTES", 1)
    with pytest.raises(ValueError, match="coverage metadata"):
        inspect(logs)


@pytest.mark.parametrize("budget", [1200, 1800, 2400, 4000, 8000, 12000])
def test_utf8_preview_budget_covers_escaping_and_omission_digit_changes(logs, monkeypatch, budget):
    (logs / "logs/a.log").write_text("\n".join(
        record(1, ('异常 "quoted" \\tab\t' * (i % 3 + 1)), location=i) for i in range(31)
    ))
    monkeypatch.setattr(overview, "MAX_PROMPT_BYTES", budget)
    value = inspect(logs)
    encoded = overview._encode(value).encode("utf-8")
    assert len(encoded) <= budget and json.loads(encoded) == value
    assert len(decoded_groups(value)) + value["omitted_group_rows"] == 31
