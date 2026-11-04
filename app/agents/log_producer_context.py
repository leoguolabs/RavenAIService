"""Bounded native log producer/thread observations; never infer events or actors."""

from pathlib import Path
import hashlib
import json
import re
import os
import stat
import time
from app.agents.log_overview import _RECORD

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 4
MAX_ROWS = 300
MAX_KEYS = 8
MAX_GROUPS = 256
MAX_LINES = 200000
MAX_SECONDS = 2.0
MAX_OUTPUT_BYTES = 8000


def lf_lines(text):
    """Native LF coordinates, with CRLF delimiters removed and other bytes kept."""
    rows = text.split("\n")
    if rows and not rows[-1]:
        rows.pop()
    return [row.removesuffix("\r") for row in rows]


def identity(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        mode = os.fstat(fd)
        if not stat.S_ISREG(mode.st_mode) or mode.st_size > MAX_FILE_BYTES:
            raise ValueError("not bounded regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            return stream.read(MAX_FILE_BYTES + 1)
    finally:
        if fd is not None:
            os.close(fd)


def track(budget, path, before, size):
    if budget is None:
        return
    if budget["bytes"] + size > MAX_TOTAL_BYTES:
        raise ValueError("shared byte bound")
    old = budget["identities"].setdefault(str(path), before)
    if old != before:
        raise ValueError("changed between stages")
    budget["bytes"] += size


def record(text):
    m = _RECORD.fullmatch(text)
    if not m:
        return None
    prefix = text[: m.start(3)]
    t = re.search(r"Tid:(\d+)\s+$", prefix)
    suffix = re.search(r"\(T(?:k|ick)=\d+,\s*Tid=(\d+)\)$", m[6])
    if t and suffix and t[1] != suffix[1]:
        return None
    thread = t[1] if t else suffix[1] if suffix else None
    return (m[3], m[4], int(m[5])), thread, m[1], m[6]


def sample(number, text, parsed):
    return [number, parsed[2], text[:240], len(text) > 240]


def collect(root, matches, budget=None):
    started = budget["started"] if budget is not None else time.monotonic()
    root = Path(root).resolve()

    def deadline():
        if time.monotonic() - started > MAX_SECONDS:
            raise ValueError("time bound")

    if len(matches) > MAX_ROWS:
        raise ValueError("match bound")
    selected = {}
    for path, number, text in matches:
        if not isinstance(path, str) or len(path) > 512 or not path.startswith("logs/"):
            continue
        if type(number) is not int or number < 1 or not isinstance(text, str):
            continue
        if len(text.encode("utf-8")) > 65536:
            raise ValueError("record bound")
        selected.setdefault(path, []).append((number, text))
    if len(selected) > MAX_FILES:
        raise ValueError("file bound")
    files, groups = [], []
    total = 0
    for relative, rows in sorted(selected.items()):
        deadline()
        p = root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("invalid path")
        if any(x.is_symlink() for x in [p, *p.parents] if x != root):
            raise ValueError("symlink")
        p = p.resolve(strict=True)
        p.relative_to(root / "logs")
        before = identity(p)
        if before[2] > MAX_FILE_BYTES or total + before[2] > MAX_TOTAL_BYTES:
            raise ValueError("byte bound")
        if budget is not None and budget["bytes"] + before[2] > MAX_TOTAL_BYTES:
            raise ValueError("shared byte bound")
        raw = read_regular(p)
        track(budget, p, before, len(raw))
        total += len(raw)
        if len(raw) != before[2] or identity(p) != before:
            raise ValueError("file changed")
        if b"\0" in raw:
            raise ValueError("binary file")
        lines = lf_lines(raw.decode("utf-8"))
        if len(lines) > MAX_LINES:
            raise ValueError("line bound")
        keys = set()
        verified = set()
        for number, text in rows:
            if number > len(lines) or lines[number - 1] != text:
                continue
            parsed = record(text)
            if parsed:
                keys.add(parsed[0])
                verified.add(number)
        if len(keys) > MAX_KEYS:
            raise ValueError("producer bound")
        current = {}
        unparsed = 0
        for number, text in enumerate(lines, 1):
            if number % 512 == 0:
                deadline()
            if len(text.encode("utf-8")) > 65536:
                raise ValueError("line byte bound")
            parsed = record(text)
            if not parsed:
                unparsed += 1
                continue
            key, thread, stamp, body = parsed
            if key not in keys:
                continue
            group_key = key, thread
            if group_key not in current:
                if len(current) >= MAX_GROUPS:
                    raise ValueError("thread group bound")
                current[group_key] = {
                    "path": relative,
                    "producer_labels": list(key),
                    "thread_label": thread,
                    "physical_records": 0,
                    "seed_record_count": 0,
                    "first": sample(number, text, parsed),
                }
            g = current[group_key]
            g["physical_records"] += 1
            g["seed_record_count"] += number in verified
            g["last"] = sample(number, text, parsed)
        if identity(p) != before:
            raise ValueError("file changed")
        groups.extend(current.values())
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "lines": len(lines),
                "unparsed_lines": unparsed,
                "selected_producers": len(keys),
                "file_scan_complete": True,
            }
        )
    groups.sort(key=lambda g: (g["path"], g["first"][0]))
    out = {
        "scope": "Only selected literal log producer labels and thread labels in scanned native files. Counts are physical records, not files/events, inventory completeness, execution, deletion, actor or causality. Thread reuse does not define an event boundary. Source labels do not prove compiled/deployed source identity. Unmatched sibling records are discovery pointers, not query matches.",
        "sample_columns": ["line", "timestamp", "exact_prefix", "excerpted"],
        "files": files,
        "producer_thread_groups": groups,
        "omitted_group_rows": 0,
    }
    # Preserve earliest and latest groups per producer before other groups.
    by_producer = {}
    for g in groups:
        by_producer.setdefault((g["path"], tuple(g["producer_labels"])), []).append(g)
    priority = []
    for values in by_producer.values():
        for g in [values[0], values[-1]]:
            if g not in priority:
                priority.append(g)
    priority.extend(g for g in groups if g not in priority)
    out["producer_thread_groups"] = []
    out["omitted_group_rows"] = len(groups)
    for g in priority:
        candidate = {
            **out,
            "producer_thread_groups": out["producer_thread_groups"] + [g],
            "omitted_group_rows": out["omitted_group_rows"] - 1,
        }
        if len(json.dumps(candidate, ensure_ascii=False).encode()) <= MAX_OUTPUT_BYTES:
            out = candidate
    if len(json.dumps(out, ensure_ascii=False).encode()) > MAX_OUTPUT_BYTES:
        raise ValueError("output metadata bound")
    out["producer_thread_groups"].sort(key=lambda g: (g["path"], g["first"][0]))
    deadline()
    return out


def native_records(root, command, stdout, budget=None):
    """Treat literal contained file tokens as hints; verify unique exact output rows.

    No command execution, shell evaluation, glob expansion or output provenance
    from echo/print labels. File/line hints are accepted only after byte equality.
    """
    import shlex

    root = Path(root).resolve()
    started = budget["started"] if budget is not None else time.monotonic()
    if (
        not isinstance(command, str)
        or len(command) > 8192
        or not isinstance(stdout, str)
        or len(stdout) > 200000
    ):
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
        if len(tokens) > 128:
            raise ValueError("token bound")
    except ValueError:
        return []
    paths = set()
    for token in tokens:
        if time.monotonic() - started > MAX_SECONDS:
            raise ValueError("native time bound")
        if len(token) > 512 or any(c in token for c in "$`*?[];&|<>\\\n\r"):
            continue
        for base in [root, root / "logs"]:
            p = base / token
            try:
                if any(x.is_symlink() for x in [p, *p.parents] if x != root):
                    continue
                p = p.resolve(strict=True)
                p.relative_to(root / "logs")
                if p.is_file():
                    paths.add(p)
            except (OSError, ValueError):
                pass
    if not paths or len(paths) > MAX_FILES:
        return []
    outputs = set()
    for line in lf_lines(stdout)[:MAX_ROWS]:
        stripped = re.sub(r"^(?:(?:[^\s:]{1,512}[:-])?\d+[:-])(?=\d{4}-)", "", line)
        if record(stripped):
            outputs.add(stripped)
    if not outputs:
        return []
    index = {}
    total = 0
    started = budget["started"] if budget is not None else time.monotonic()
    for p in sorted(paths):
        before = identity(p)
        if before[2] > MAX_FILE_BYTES or total + before[2] > MAX_TOTAL_BYTES:
            raise ValueError("native byte bound")
        if budget is not None and budget["bytes"] + before[2] > MAX_TOTAL_BYTES:
            raise ValueError("shared byte bound")
        raw = read_regular(p)
        total += len(raw)
        track(budget, p, before, len(raw))
        if len(raw) != before[2] or identity(p) != before or b"\0" in raw:
            raise ValueError("native file changed/binary")
        for number, line in enumerate(lf_lines(raw.decode("utf-8")), 1):
            if number > MAX_LINES or time.monotonic() - started > MAX_SECONDS:
                raise ValueError("native line/time bound")
            if line in outputs:
                index.setdefault(line, []).append(
                    (p.relative_to(root).as_posix(), number, line)
                )
    return [values[0] for values in index.values() if len(values) == 1]


def collect_native(root, command, stdout):
    budget = {"started": time.monotonic(), "bytes": 0, "identities": {}}
    rows = native_records(root, command, stdout, budget=budget)
    return collect(root, rows, budget=budget)
