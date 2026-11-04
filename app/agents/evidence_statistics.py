"""Bounded physical-file statistics, isolated from potentially expensive regexes.

No generated diagnosis, cross-file deduplication or inference of signal absence.
Run the same file as a subprocess so even one pathological regex can be stopped.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from decimal import Decimal, InvalidOperation

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_VALUES = 32
MAX_COMPARE_VALUES = 256
MAX_COMPARE_VALUE_CHARS = 512
MAX_VALUE_CHARS = 128
MAX_OUTPUT_BYTES = 16_000
SCAN_SECONDS = 2.0
WORKER_TIMEOUT = 5.0
TOOL_NAME = "mcp__evidence__summarize_evidence"


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _number(value: str) -> Decimal:
    if len(value) > MAX_VALUE_CHARS:
        raise ValueError("numeric capture exceeds the value limit")
    result = Decimal(int(value, 16)) if re.fullmatch(r"[+-]?0[xX][0-9a-fA-F]+", value) else Decimal(value)
    if not result.is_finite():
        raise ValueError("numeric capture must be finite")
    return result


def scan_statistics(workspace: str, request: dict) -> dict:
    """Worker entry point. Only a stable, fully processed file can be complete."""
    root = Path(workspace).resolve(strict=True)
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    value = request.get("path")
    if not isinstance(value, str) or not value:
        raise ValueError("path must name one regular file")
    path = (root / value).resolve(strict=True)
    path.relative_to(root)
    pattern = request.get("pattern")
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 1000:
        raise ValueError("pattern must contain 1..1000 characters")
    mode = request.get("mode", "count")
    if mode not in ("count", "numeric", "values", "compare"):
        raise ValueError("mode must be count, numeric, values or compare")
    ignore_case = request.get("ignore_case", False)
    if type(ignore_case) is not bool:
        raise ValueError("ignore_case must be boolean")
    regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    if mode != "count" and "value" not in regex.groupindex:
        raise ValueError(f"{mode} mode requires a named (?P<value>...) capture")

    selected_groups = None
    if mode == "compare":
        selected_groups = (request.get("left_group"), request.get("right_group"))
        if ("group" not in regex.groupindex or any(not isinstance(g, str) or not 1 <= len(g) <= 128 for g in selected_groups)
                or selected_groups[0] == selected_groups[1]):
            raise ValueError("compare requires literal capture names (?P<group>...) and (?P<value>...). "
                             "left_group/right_group must be two distinct captured VALUES from original rows, "
                             "not capture names. Correct the request or use original tools.")
    grouped = {g: {} for g in selected_groups or ()}
    group_refs = {}

    result = {"path": path.relative_to(root).as_posix(), "pattern": pattern, "mode": mode,
              "complete": False, "scanned_bytes": 0, "lines": 0, "matched_lines": 0,
              "matches": 0, "skipped_lines": 0, "invalid_values": 0,
              "scope": "Physical matches in this file only; overlapping files are not deduplicated. "
                       "Complete means this regex scan, not event coverage or causal certainty."}
    values, minimum, maximum = {}, None, None
    digest = hashlib.sha256()
    deadline = time.monotonic() + SCAN_SECONDS

    def skip_line(reason):
        result["skipped_lines"] += 1
        references = result.setdefault("skipped_line_refs", [])
        if len(references) < 4:
            references.append({"line": result["lines"], "reason": reason})
        else:
            result["omitted_skipped_line_refs"] = result.get("omitted_skipped_line_refs", 0) + 1

    # O_NONBLOCK avoids blocking on a FIFO if a path changes between checks.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        os.close(descriptor)
        raise ValueError("path must name one regular file")
    with os.fdopen(descriptor, "rb") as stream:
        path.resolve(strict=True).relative_to(root)
        result["bytes"] = before.st_size
        pending, oversized = b"", False
        while result["scanned_bytes"] < min(before.st_size, MAX_FILE_BYTES):
            if time.monotonic() >= deadline:
                result["limit"] = "scan_time"
                break
            raw = stream.readline(min(MAX_LINE_BYTES + 1, MAX_FILE_BYTES - result["scanned_bytes"]))
            if not raw:
                break
            digest.update(raw)
            result["scanned_bytes"] += len(raw)
            if not oversized:
                pending += raw
                if len(pending) > MAX_LINE_BYTES:
                    oversized, pending = True, b""
            at_eof = result["scanned_bytes"] == before.st_size
            if not raw.endswith(b"\n") and not at_eof:
                continue
            result["lines"] += 1
            if oversized or b"\0" in pending:
                skip_line("line_bytes" if oversized else "binary")
                pending, oversized = b"", False
                continue
            try:
                text = pending.decode("utf-8")
            except UnicodeDecodeError:
                skip_line("invalid_utf8")
                pending = b""
                continue
            pending = b""
            text = text[:-2] if text.endswith("\r\n") else text.removesuffix("\n")
            matched = False
            for match in regex.finditer(text):
                if time.monotonic() >= deadline:
                    result["limit"] = "scan_time"
                    break
                matched = True
                result["matches"] += 1
                anchor = match.start("value") if mode != "count" and match.group("value") is not None else match.start()
                column, end_column = max(0, anchor - 40), anchor + 200
                sample = {"line": result["lines"], "column": column, "match_column": match.start(),
                          "text": text[column:end_column],
                          "excerpted": column > 0 or len(text) > end_column}
                if mode != "count" and match.group("value") is not None:
                    sample["value_column"] = anchor
                result.setdefault("first", sample)
                result["last"] = sample
                if mode == "count":
                    continue
                captured = match.group("value")
                if mode == "compare":
                    group = match.group("group")
                    if group is None:
                        result["invalid_values"] += 1
                        continue
                    if group not in grouped:
                        result["outside_selected_group_matches"] = result.get("outside_selected_group_matches", 0) + 1
                        continue
                if captured is None or len(captured) > (MAX_COMPARE_VALUE_CHARS if mode == "compare" else MAX_VALUE_CHARS):
                    result["invalid_values"] += 1
                    continue
                if mode == "compare":
                    bucket = grouped[group]
                    if captured not in bucket:
                        if len(bucket) >= MAX_COMPARE_VALUES:
                            result["unindexed_value_matches"] = result.get("unindexed_value_matches", 0) + 1
                            continue
                        bucket[captured] = dict(value=captured, count=0, first_line=result["lines"], last_line=result["lines"])
                    bucket[captured]["count"] += 1
                    bucket[captured]["last_line"] = result["lines"]
                    ref = dict(line=result["lines"], prefix=text[:160], prefix_excerpted=len(text) > 160)
                    group_refs.setdefault(group, {"first": ref})["last"] = ref
                    continue
                sample = {**sample, "value": captured}
                if mode == "values":
                    if captured not in values:
                        if len(values) >= MAX_VALUES:
                            result["unindexed_value_matches"] = result.get("unindexed_value_matches", 0) + 1
                            continue
                        values[captured] = {"value": captured, "count": 0,
                                            "first_line": result["lines"], "last_line": result["lines"]}
                    values[captured]["count"] += 1
                    values[captured]["last_line"] = result["lines"]
                    continue
                try:
                    number = _number(captured)
                except (ValueError, InvalidOperation):
                    result["invalid_values"] += 1
                    continue
                if minimum is None or number < minimum:
                    minimum, result["minimum"] = number, sample
                if maximum is None or number > maximum:
                    maximum, result["maximum"] = number, sample
            result["matched_lines"] += int(matched)
            if result.get("limit"):
                break
        after = os.fstat(stream.fileno())
    unchanged = _identity(before) == _identity(after) == _identity(path.stat())
    scanned_all = result["scanned_bytes"] == before.st_size and not result.get("limit")
    scan_complete = unchanged and scanned_all and not result["skipped_lines"]
    result.update(unchanged_during_scan=unchanged, scanned_sha256=digest.hexdigest(),
                  scan_complete=scan_complete, matches_complete=scan_complete,
                  complete=scan_complete and not result["invalid_values"] and not result.get("unindexed_value_matches"))
    if not scanned_all and "limit" not in result:
        result["limit"] = "file_bytes_or_early_eof"
    if mode == "values":
        result["values"] = list(values.values())
        result["values_complete"] = result["complete"] and not result.get("unindexed_value_matches")
    if mode == "compare":
        left_id, right_id = selected_groups
        left, right = grouped[left_id], grouped[right_id]
        comparison_complete = result["complete"] and all(g in group_refs for g in selected_groups)
        def group_summary(identifier, bucket):
            return dict(group=identifier, unique_values=len(bucket),
                        indexed_matches=sum(v["count"] for v in bucket.values()),
                        observed=identifier in group_refs, **group_refs.get(identifier, {}))
        result["comparison"] = dict(
            left=group_summary(left_id, left), right=group_summary(right_id, right), complete=comparison_complete,
            common_unique_values=len(left.keys() & right.keys()) if comparison_complete else None,
            only_left=[left[k] for k in sorted(left.keys() - right.keys())] if comparison_complete else None,
            only_right=[right[k] for k in sorted(right.keys() - left.keys())] if comparison_complete else None,
            scope="Captured literal value sets in the two selected regex groups only; not physical deletion, chronology or causal proof.")
        if not all(g in group_refs for g in selected_groups):
            result["comparison"]["warning"] = (
                "Selected group value not observed; differences withheld. left_group/right_group are captured "
                "values, not capture names. Verify both identifiers in original rows or use original tools; "
                "do not repeat the same unobserved selection.")
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_OUTPUT_BYTES:
        raise ValueError("statistics output budget exceeded; narrow the capture")
    return result


async def _worker(workspace: str, request: dict) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        raw = json.dumps({"workspace": workspace, "request": request}).encode()
        output, _ = await asyncio.wait_for(process.communicate(raw), WORKER_TIMEOUT)
        if process.returncode or len(output) > MAX_OUTPUT_BYTES + 1:
            raise ValueError("statistics worker failed; use original tools")
        return json.loads(output)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def run_statistics_batch(workspace: str, requests: list) -> dict:
    if not isinstance(requests, list) or not 1 <= len(requests) <= 8:
        return {"error": "requests must contain 1..8 entries"}
    semaphore = asyncio.Semaphore(2)

    async def one(index, request):
        async with semaphore:
            try:
                return {"index": index, **await _worker(workspace, request)}
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                return {"index": index, "complete": False,
                        "error": str(exc) or "statistics timed out; narrow the pattern or use original tools"}

    return {"results": await asyncio.gather(*(one(i, request) for i, request in enumerate(requests)))}


if __name__ == "__main__":
    try:
        job = json.loads(sys.stdin.buffer.read(20_000))
        response = scan_statistics(job["workspace"], job["request"])
    except (OSError, ValueError, KeyError, re.error) as exc:
        response = {"complete": False, "error": str(exc)}
    print(json.dumps(response, ensure_ascii=False))
