import json
import pytest
from app.agents import component_packet as component


def snapshot(lines, path="logs/input.log"):
    return {"lines": lines, "metadata": {"path": path, "sha256": "hash"}}


def line(n, filename, function, body):
    return (
        f"2026-10-01T00:00:00.000,Tick={n} INFO Tid:2 {filename} {function} 10 {body}"
    )


def test_named_component_discovery_exposes_last_data_and_separate_timeout_without_inference():
    rows = [
        line(n, "LIB_transport.c", "receive", f"bytes={n * 1000}") for n in range(1, 10)
    ]
    rows += [line(10, "LIB_transport.c", "timeout", "confirmed=0")]
    rows += [line(20 + i, f"LIB_other{i}.c", "idle", "data=0") for i in range(8)]
    payload = component.collect(
        "Inspect transport in input.log. LIB background exists.",
        [snapshot(rows)],
        lambda: None,
    )
    assert payload["selected_source_labels"] == ["LIB_transport.c"]
    groups = payload["producer_thread_groups"]
    assert len(groups) == 2 and payload["omitted_group_rows"] == 0
    data = next(g for g in groups if g["producer_labels"][1] == "receive")
    assert data["physical_records"] == 9 and "bytes=9000" in data["last"][2]
    assert data["first"][0] == 1 and data["last"][0] == 9
    assert "not frames/files/events" in payload["scope"]


def test_component_payload_has_explicit_omissions_with_fixed_output_bound(monkeypatch):
    rows = [
        line(i, "worker.c", f"action{i}", "literal=" + "x" * 100) for i in range(20)
    ]
    monkeypatch.setattr(component, "MAX_OUTPUT", 1500)
    payload = component.collect(
        "worker",
        [snapshot(rows)],
        lambda: None,
        omitted_named_files=3,
        ambiguous_names=["repeat.log"],
    )
    assert (
        len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        <= 1500
    )
    assert payload["omitted_group_rows"] > 0
    assert payload["omitted_named_files"] == 3 and payload[
        "ambiguous_named_attachments"
    ] == ["repeat.log"]


def test_component_discovery_honors_shared_cancellation_or_time_check():
    def stopped():
        raise ValueError("time bound")

    with pytest.raises(ValueError, match="time bound"):
        component.collect(
            "worker", [snapshot([line(1, "worker.c", "run", "data=1")])], stopped
        )
