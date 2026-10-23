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
    for group in value["warning_groups"]:
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
    group = value["warning_groups"][0]
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
    assert value["warning_groups"][0]["count"] == 1
    assert value["files"][0]["scanned_bytes"] == len(data) - 10


def test_oversized_lines_preserve_following_native_line_numbers(logs, monkeypatch):
    monkeypatch.setattr(overview, "MAX_LINE_BYTES", 200)
    (logs / "logs/a.log").write_text("X" * 1000 + "\n" + record(1) + "\n")
    value = inspect(logs)
    assert value["files"][0]["complete"]
    assert value["files"][0]["lines"] == 2
    assert value["files"][0]["other_lines"] == 1
    assert value["warning_groups"][0]["first"][0] == 2


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
        assert "raven-log-overview-v1" in prompt
        assert "原生工具" in prompt if locale == "zh" else "native tools" in prompt
        assert "因果" in prompt if locale == "zh" else "causation" in prompt
