"""Bounded citation-grounded review packet; no reference answers or runtime claims."""

from pathlib import Path
import hashlib
import json
import re
import time
from app.agents import log_producer_context as producer
from app.agents.source_function_spans import CFunctionSpans, CEnumSpans

PUBLIC_FIELDS = (
    "status",
    "question_type",
    "answer",
    "summary",
    "severity",
    "root_cause_hypotheses",
    "recommended_actions",
    "related_keywords",
    "requires_code_fix",
    "proposed_fixes",
)
MAX_REFS = 64
MAX_FILES = 16  # Citation files plus bounded typed-relation includes.
MAX_ENTRIES = 256
MAX_BYTES = 32 * 1024 * 1024
MAX_PACKET = 32768
MAX_SECONDS = 2.0
EXPLICIT = re.compile(r"\b(log|repo):([^\s,:;()（），；：`]+):(\d{1,7})")
QUALIFIED = re.compile(
    r"\b((?:logs|repo|related_repos)/[A-Za-z0-9_./\-\[\]]+):(\d{1,7})"
)


class PacketChanged(ValueError):
    pass


SOURCE_SUFFIXES = {
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".py",
    ".rs",
    ".go",
    ".java",
    ".ts",
    ".js",
    ".txt",
}


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)


def citations(draft):
    refs = set()
    for text in strings(draft):
        for kind, path, line in EXPLICIT.findall(text):
            path = (
                path
                if path.startswith(("logs/", "repo/", "related_repos/"))
                else ("logs/" if kind == "log" else "repo/") + path
            )
            refs.add((path, int(line)))
        for path, line in QUALIFIED.findall(text):
            refs.add((path, int(line)))
    if len(refs) > MAX_REFS:
        raise ValueError("citation bound")
    return sorted(refs, key=lambda r: (not r[0].startswith("logs/"), r[0], r[1]))


def short_log_citations(draft, inventory):
    """Resolve shorthand only when one explicitly named native attachment is unique."""
    text = "\n".join(strings(draft))
    numbers = sorted(
        {
            int(n)
            for n in re.findall(r"(?<![A-Za-z0-9_./:-])log:(\d{1,7})(?![\d:])", text)
        }
    )
    by_name = {}
    for item in inventory:
        path = item["path"]
        name = Path(path).name
        if re.search(
            r"(?<![A-Za-z0-9_.-])" + re.escape(name) + r"(?![A-Za-z0-9_.-])", text
        ):
            by_name.setdefault(name, []).append(path)
    candidates = [paths[0] for paths in by_name.values() if len(paths) == 1]
    unique = len(candidates) == 1 and len(by_name) == 1
    return [(candidates[0], n) for n in numbers] if unique else [], [
        {
            "reference": "log:" + str(n),
            "line": n,
            "reason": "no unique explicitly named attachment",
        }
        for n in numbers
        if not unique
    ]


def safe_file(root, relative):
    parts = Path(relative).parts
    if (
        Path(relative).is_absolute()
        or not parts
        or parts[0] not in ("logs", "repo", "related_repos")
        or any(x == ".." or x.startswith(".") for x in parts)
        or len(relative) > 512
    ):
        raise ValueError("unsafe citation path")
    if parts[0] != "logs" and Path(relative).suffix.lower() not in SOURCE_SUFFIXES:
        raise ValueError("unsupported source")
    path = root / relative
    if any(p.is_symlink() for p in [path, *path.parents] if p != root):
        raise ValueError("symlink")
    path = path.resolve(strict=True)
    path.relative_to(root)
    return path


def build(workspace, raw_draft):
    started = time.monotonic()
    root = Path(workspace).resolve()
    functions = CFunctionSpans()
    enums = CEnumSpans()
    draft = {k: raw_draft[k] for k in PUBLIC_FIELDS if k in raw_draft}
    if not isinstance(draft.get("answer"), str):
        raise ValueError("missing answer")

    def deadline():
        if time.monotonic() - started > MAX_SECONDS:
            raise ValueError("time bound")

    available = []
    entries = 0
    pending = [root / "logs"]
    while pending:
        deadline()
        directory = pending.pop()
        if directory.is_symlink():
            raise ValueError("inventory symlink")
        for path in sorted(directory.iterdir()):
            entries += 1
            if entries > MAX_ENTRIES:
                raise ValueError("inventory bound")
            if path.is_symlink():
                continue
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                available.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "bytes": path.stat().st_size,
                    }
                )
    short_refs, unresolved_short = short_log_citations(draft, available)
    refs = sorted(
        set(citations(draft) + short_refs),
        key=lambda r: (not r[0].startswith("logs/"), r[0], r[1]),
    )
    if len(refs) > MAX_REFS:
        raise ValueError("citation bound")
    files = {}
    windows = []
    omitted = []
    seeds = []
    budget = {"started": started, "bytes": 0, "identities": {}}

    def load(relative):
        deadline()
        path = safe_file(root, relative)
        if relative not in files:
            if len(files) >= MAX_FILES:
                raise ValueError("file bound")
            before = producer.identity(path)
            if not relative.startswith("logs/") and before[2] > 2 * 1024 * 1024:
                raise ValueError("source byte bound")
            if budget["bytes"] + before[2] > MAX_BYTES:
                raise ValueError("read byte bound")
            if (
                str(path) in budget["identities"]
                and budget["identities"][str(path)] != before
            ):
                raise PacketChanged("changed between packet reads")
            raw = producer.read_regular(path)
            if len(raw) != before[2] or producer.identity(path) != before:
                raise PacketChanged("changed file")
            producer.track(budget, path, before, len(raw))
            binary = b"\0" in raw
            try:
                lines = [] if binary else producer.lf_lines(raw.decode("utf-8"))
            except UnicodeDecodeError:
                binary = True
                lines = []
            files[relative] = {
                "raw": raw,
                "path": path,
                "identity": before,
                "lines": lines,
                "source_snapshot": ((*before, hashlib.sha256(raw).digest()), raw)
                if not relative.startswith("logs/")
                else None,
                "metadata": {
                    "path": relative,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "binary_or_non_utf8": binary,
                    "total_lf_lines": None if binary else len(lines),
                    "semantic_whole_file_review": False,
                },
            }
        snapshot = files[relative]
        if producer.identity(path) != snapshot["identity"]:
            raise PacketChanged("changed cached file")
        return snapshot

    for relative, number in refs:
        deadline()
        try:
            path = safe_file(root, relative)
            load(relative)
            snapshot = files[relative]
            lines = snapshot["lines"]
            if snapshot["metadata"]["binary_or_non_utf8"]:
                raise ValueError("binary citation metadata only")
            if not 1 <= number <= len(lines):
                raise ValueError("line outside file")
            if relative.startswith("logs/"):
                seeds.append((relative, number, lines[number - 1]))
            start, end = (
                max(1, number - (4 if relative.startswith("logs/") else 12)),
                min(len(lines), number + 12),
            )
            complete_syntax = None
            if snapshot["source_snapshot"] is not None:
                spans = list(functions.get(path, snapshot["source_snapshot"])) + list(
                    enums.get(path, snapshot["source_snapshot"])
                )
                for span in spans:
                    if (
                        span.start_line <= number <= span.end_line
                        and span.end_line - span.start_line < 160
                    ):
                        source_rows = [
                            [n, lines[n - 1]]
                            for n in range(span.start_line, span.end_line + 1)
                        ]
                        if (
                            len(json.dumps(source_rows, ensure_ascii=False).encode())
                            <= 8000
                        ):
                            start, end = span.start_line, span.end_line
                            complete_syntax = {
                                "kind": "function" if hasattr(span, "name") else "enum",
                                "start_line": start,
                                "end_line": end,
                                "syntactic_range_only": True,
                            }
                            break
            if any(
                w["path"] == relative
                and w["start_line"] <= start
                and end <= w["end_line"]
                for w in windows
            ):
                continue
            windows.append(
                {
                    "path": relative,
                    "start_line": start,
                    "end_line": end,
                    "range_complete": True,
                    "complete_source_ranges": [complete_syntax]
                    if complete_syntax
                    else [],
                    "rows": [[n, lines[n - 1]] for n in range(start, end + 1)],
                }
            )
        except PacketChanged:
            raise
        except (OSError, ValueError) as exc:
            omitted.append(
                {
                    "path": relative,
                    "line": number,
                    "reason": type(exc).__name__ + ": " + str(exc),
                }
            )
    try:
        from app.agents.source_field_relations import SourceFieldRelations

        def source_reader(path):
            snapshot = load(Path(path).relative_to(root).as_posix())
            return snapshot["source_snapshot"]

        def log_reader(path):
            return load(Path(path).relative_to(root).as_posix())["raw"]

        scanner = SourceFieldRelations(
            root, source_reader=source_reader, log_reader=log_reader
        )
        scanner.started = started
        relations = scanner.collect(seeds) if seeds else None
        relations_error = None
    except PacketChanged:
        raise
    except (ImportError, OSError, ValueError) as exc:
        if "mutation" in str(exc):
            raise PacketChanged("changed during source relations") from exc
        relations = None
        relations_error = type(exc).__name__ + ": " + str(exc)
    try:
        observations = producer.collect(root, seeds, budget=budget) if seeds else None
        producer_error = None
    except (OSError, ValueError) as exc:
        observations = None
        producer_error = type(exc).__name__ + ": " + str(exc)
    merged = []
    for w in sorted(windows, key=lambda w: (w["path"], w["start_line"])):
        if (
            merged
            and merged[-1]["path"] == w["path"]
            and w["start_line"] <= merged[-1]["end_line"] + 1
        ):
            prev = merged[-1]
            prev["end_line"] = max(prev["end_line"], w["end_line"])
            prev["complete_source_ranges"].extend(
                x
                for x in w["complete_source_ranges"]
                if x not in prev["complete_source_ranges"]
            )
            lines = files[w["path"]]["lines"]
            prev["rows"] = [
                [n, lines[n - 1]]
                for n in range(prev["start_line"], prev["end_line"] + 1)
            ]
        else:
            merged.append(w)
    byfile = {}
    for w in merged:
        byfile.setdefault(w["path"], []).append(w)
    priority = []
    for path, values in sorted(
        byfile.items(), key=lambda x: (not x[0].startswith("logs/"), x[0])
    ):
        for w in [values[0], values[-1]]:
            if w not in priority:
                priority.append(w)
    priority.extend(w for w in merged if w not in priority)
    windows = []
    packet = {
        "draft": draft,
        "evidence_limits": {
            "source_runtime_identity": "not_established",
            "attachment_semantic_coverage": "not_established",
            "cited_windows_are_partial": True,
            "absence_of_logged_action_does_not_exclude_actor": True,
            "producer_counts_are_physical_records_not_unique_files_or_events": True,
        },
        "available_attachment_inventory": available,
        "cited_files": [x["metadata"] for x in files.values()],
        "cited_windows": windows,
        "omitted_citations": omitted,
        "unresolved_short_citations": unresolved_short,
        "resolved_short_citations": [
            {"reference": "log:" + str(n), "path": p, "line": n} for p, n in short_refs
        ],
        "source_field_relations": relations,
        "source_relations_unavailable_reason": relations_error,
        "producer_observations": observations,
        "producer_unavailable_reason": producer_error,
    }

    def size():
        return len(
            json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode()
        )

    original_omissions = {(x["path"], x["line"]): x for x in omitted}

    def refresh_omissions():
        missing = []
        for path, number in refs:
            if not any(
                w["path"] == path and w["start_line"] <= number <= w["end_line"]
                for w in packet["cited_windows"]
            ):
                missing.append(
                    original_omissions.get(
                        (path, number),
                        {
                            "path": path,
                            "line": number,
                            "reason": "bounded packet omitted cited evidence",
                        },
                    )
                )
        packet["omitted_citations"] = missing
        packet["literal_citation_window_coverage"] = {
            "requested": len(refs),
            "included": len(refs) - len(missing),
            "omitted": len(missing),
        }

    refresh_omissions()
    if size() > MAX_PACKET and packet["producer_observations"] is not None:
        packet["producer_observations"] = None
        packet["producer_unavailable_reason"] = "packet budget"
    if size() > MAX_PACKET and packet["source_field_relations"] is not None:
        packet["source_field_relations"] = None
        packet["source_relations_unavailable_reason"] = "packet budget"
    if size() > MAX_PACKET:
        raise ValueError("packet metadata/draft bound")
    for w in priority:
        packet["cited_windows"].append(w)
        refresh_omissions()
        if size() > MAX_PACKET:
            packet["cited_windows"].pop()
            refresh_omissions()
    if size() > MAX_PACKET:
        raise ValueError("packet metadata/draft bound")
    for snapshot in files.values():
        if producer.identity(snapshot["path"]) != snapshot["identity"]:
            raise PacketChanged("file changed before emission")
    deadline()
    return packet
