"""Literal source-label discovery in named native logs, without event inference."""

from collections import Counter
from pathlib import Path
import json
import re

from app.agents import log_producer_context as native

MAX_SOURCES = 256
MAX_GROUPS = 256
MAX_OUTPUT = 8000


def collect(text, snapshots, check, *, omitted_named_files=0, ambiguous_names=()):
    """Snapshots are contained, bounded and identity-checked by the packet owner."""
    names = set()
    for snapshot in snapshots:
        for number, line in enumerate(snapshot["lines"], 1):
            if number % 256 == 0:
                check()
            if len(line.encode()) > 65536:
                raise ValueError("component line bound")
            parsed = native.record(line)
            if parsed:
                names.add(parsed[0][0])
                if len(names) > MAX_SOURCES:
                    raise ValueError("component source bound")
    tokens = {
        name: set(re.findall(r"[A-Za-z][A-Za-z0-9]*", Path(name).stem.lower()))
        for name in names
    }
    frequency = Counter(
        token for terms in tokens.values() for token in terms if len(token) >= 3
    )
    words = set(re.findall(r"[a-z][a-z0-9]*", text.lower()))
    selected = {
        name
        for name, terms in tokens.items()
        if name in text
        or any(
            len(token) >= 3
            and token in words
            and frequency[token] <= max(1, len(names) // 4)
            for token in terms
        )
    }
    groups = {}
    for snapshot in snapshots:
        for number, line in enumerate(snapshot["lines"], 1):
            if number % 256 == 0:
                check()
            parsed = native.record(line)
            if not parsed or parsed[0][0] not in selected:
                continue
            producer, thread, _, _ = parsed
            key = snapshot["metadata"]["path"], producer, thread
            if key not in groups:
                if len(groups) >= MAX_GROUPS:
                    raise ValueError("component group bound")
                groups[key] = {
                    "path": key[0],
                    "producer_labels": list(producer),
                    "thread_label": thread,
                    "physical_records": 0,
                    "first": native.sample(number, line, parsed),
                }
            group = groups[key]
            group["physical_records"] += 1
            group["last"] = native.sample(number, line, parsed)
    by_component = {}
    for group in groups.values():
        by_component.setdefault(
            (group["path"], group["producer_labels"][0]), []
        ).append(group)
    priority = []
    for values in by_component.values():
        values.sort(key=lambda group: group["first"][0])
        for group in [
            values[0],
            values[-1],
            max(values, key=lambda group: group["physical_records"]),
        ]:
            if group not in priority:
                priority.append(group)
    priority.extend(
        group
        for group in sorted(
            groups.values(), key=lambda group: group["physical_records"]
        )
        if group not in priority
    )
    payload = {
        "omitted_named_files": omitted_named_files,
        "ambiguous_named_attachments": list(ambiguous_names),
        "scope": "Literal source-label groups in selected named native attachments. Selection uses identifier overlap and source-name rarity, not semantic coverage. Counts are physical records, not frames/files/events. Samples are discovery evidence, not proof of operations, actors, absence or runtime identity.",
        "sample_columns": ["line", "timestamp", "exact_prefix", "excerpted"],
        "files": [
            {
                "path": snap["metadata"]["path"],
                "sha256": snap["metadata"]["sha256"],
                "total_lf_lines": len(snap["lines"]),
                "selected_components_scan_complete": True,
            }
            for snap in snapshots
        ],
        "selected_source_labels": sorted(selected),
        "producer_thread_groups": [],
        "omitted_group_rows": len(groups),
    }
    for group in priority:
        candidate = {
            **payload,
            "producer_thread_groups": payload["producer_thread_groups"] + [group],
            "omitted_group_rows": payload["omitted_group_rows"] - 1,
        }
        if (
            len(
                json.dumps(
                    candidate, ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
            <= MAX_OUTPUT
        ):
            payload = candidate
    check()
    if (
        len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        > MAX_OUTPUT
    ):
        raise ValueError("component metadata bound")
    return payload
