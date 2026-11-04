"""Workspace-bound, read-only batches for evidence collection.

Search pages count matching lines (not individual regex occurrences). Stable
path ordering and explicit continuations let the Agent inspect every match.
Native tools remain available for binary files and specialized processing.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
from typing import Any

MAX_BATCH = 8
MAX_READ_CHARS = 8_000
MAX_SEARCH_MATCHES = 40
MAX_CONTEXT_MATCHES = 3
MAX_SEARCH_CONTEXT_CHARS = 4_000
SEARCH_TIMEOUT = 15
TOOL_NAMES = ["mcp__evidence__search_evidence", "mcp__evidence__read_evidence"]


def _path(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a non-empty string")
    path = (root / value).resolve(strict=True)
    path.relative_to(root)
    return path


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def _strip_line_ending(raw: str) -> str:
    """Remove one LF/CRLF terminator; preserve embedded or standalone CR."""
    if raw.endswith("\r\n"):
        return raw[:-2]
    return raw[:-1] if raw.endswith("\n") else raw


def read_page(root: Path, request: dict, *, _budget: int = MAX_READ_CHARS) -> dict:
    path = _path(root, request.get("path"))
    if not path.is_file():
        raise ValueError("read requires a regular file")
    total_lines = None
    if "tail_lines" in request:
        if any(key in request for key in ("start_line", "column", "line_count")):
            raise ValueError("tail_lines cannot be combined with start_line, column or line_count")
        tail = _integer(request["tail_lines"], "tail_lines", 1, 2000)
        total_lines = 0
        last = b""
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                total_lines += chunk.count(b"\n")
                last = chunk[-1:]
        total_lines += int(bool(last) and last != b"\n")
        start = max(1, total_lines - tail + 1)
    else:
        start = _integer(request.get("start_line", 1), "start_line", 1, 10**9)
    column = _integer(request.get("column", 0), "column", 0, 10**9)
    count = _integer(request.get("line_count", 80) if total_lines is None else tail, "line_count", 1, 2000)
    budget = _budget
    lines = []
    next_request = None
    relative = path.relative_to(root).as_posix()
    with path.open("rb") as stream:
        if b"\x00" in stream.read(8192):
            raise ValueError("binary file; use a format-aware native tool")
    # Match ripgrep/native line numbers. Universal newline mode incorrectly
    # treats terminal progress CR characters as additional numbered lines.
    with path.open(encoding="utf-8", errors="replace", newline="\n") as stream:
        for number, raw in enumerate(stream, 1):
            if number < start:
                continue
            if number >= start + count or budget == 0:
                next_request = {"path": relative, "start_line": number, "line_count": count}
                break
            text = _strip_line_ending(raw)
            offset = column if number == start else 0
            if offset > len(text):
                raise ValueError("column is beyond the requested line")
            fragment = text[offset:offset + budget]
            lines.append({"line": number, "column": offset, "text": fragment})
            budget -= max(1, len(fragment))
            if offset + len(fragment) < len(text):
                next_request = {
                    "path": relative, "start_line": number,
                    "column": offset + len(fragment), "line_count": count,
                }
                break
    result = {"path": relative, "encoding": "utf-8 (replacement on invalid bytes)",
              "lines": lines, "has_more": next_request is not None, "next_request": next_request}
    if total_lines is not None:
        result["total_lines"] = total_lines
    return result


def _search_context(root: Path, matches: list[dict], radius: int) -> dict:
    """Read a bounded set of exact ranges in the same retrieval round trip.

    Search matches are never dropped. Context is explicitly limited to the
    first three matches on this page, and overlapping ranges are merged.
    """
    ranges = []
    for match in matches[:MAX_CONTEXT_MATCHES]:
        start, end = max(1, match["line"] - radius), match["line"] + radius
        if ranges and ranges[-1][0] == match["path"] and start <= ranges[-1][2] + 1:
            ranges[-1][2] = max(ranges[-1][2], end)
        else:
            ranges.append([match["path"], start, end])
    budget = MAX_SEARCH_CONTEXT_CHARS
    windows = []
    for path, start, end in ranges:
        request = {"path": path, "start_line": start, "line_count": end - start + 1}
        window = {"path": path, "start_line": start, "end_line": end,
                  "lines": [], "range_complete": False, "next_request": request}
        if budget:
            try:
                page = read_page(root, request, _budget=budget)
                window["lines"] = page["lines"]
                budget -= sum(max(1, len(line["text"])) for line in page["lines"])
                continuation = page["next_request"]
                if continuation and continuation["start_line"] <= end:
                    window["next_request"] = {**continuation, "line_count": end - continuation["start_line"] + 1}
                else:
                    window.update(range_complete=True, next_request=None)
            except (OSError, ValueError) as exc:
                # Context failure must not discard successful search matches.
                window["error"] = str(exc)
        windows.append(window)
    return {"context_windows": windows,
            "context_matches_requested": min(len(matches), MAX_CONTEXT_MATCHES),
            "context_matches_not_expanded": max(0, len(matches) - MAX_CONTEXT_MATCHES)}


def search_arguments(root: Path, request: dict) -> list[str]:
    """One regex engine/options contract for pages and optional endpoint replay."""
    pattern = request["pattern"]
    path = _path(root, request.get("path", "."))
    executable = shutil.which("rg")
    if not executable:
        raise ValueError("ripgrep is unavailable; use native search tools")
    args = [executable, "--json", "--engine", "auto", "--sort", "path", "--hidden", "--no-ignore",
            "--glob", "!.git/**", "--glob", "!**/.git/**"]
    if request.get("literal", False):
        args.append("--fixed-strings")
    if request.get("ignore_case", False):
        args.append("--ignore-case")
    glob = request.get("glob")
    if glob is not None:
        if not isinstance(glob, str) or len(glob) > 512:
            raise ValueError("glob must be a string of at most 512 characters")
        args.extend(["--glob", glob])
    args.extend(["--regexp", pattern, "--", path.relative_to(root).as_posix()])
    return args


async def search_page(root: Path, request: dict) -> dict:
    pattern = request.get("pattern")
    if not isinstance(pattern, str) or not pattern or len(pattern) > 4096:
        raise ValueError("pattern must contain 1..4096 characters")
    path = _path(root, request.get("path", "."))
    offset = _integer(request.get("offset", 0), "offset", 0, 10**9)
    limit = _integer(request.get("limit", 20), "limit", 1, MAX_SEARCH_MATCHES)
    context_lines = _integer(request.get("context_lines", 0), "context_lines", 0, 40)
    args = search_arguments(root, request)
    process = await asyncio.create_subprocess_exec(
        *args, cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=2 * 1024 * 1024,
    )
    matches = []
    seen = 0
    more = False

    async def consume() -> None:
        nonlocal seen, more
        assert process.stdout is not None
        async for raw in process.stdout:
            event = json.loads(raw)
            if event.get("type") != "match":
                continue
            data = event["data"]
            seen += 1
            if seen <= offset:
                continue
            if len(matches) == limit:
                more = True
                break
            filename = data["path"].get("text")
            text = data["lines"].get("text")
            if filename is None or text is None:
                raise ValueError("non-UTF-8 search output; use native tools for these files")
            # rg does not follow directory symlinks; validate explicit targets and
            # returned filenames as well, before exposing their contents.
            safe_path = _path(root, filename).relative_to(root).as_posix()
            text = _strip_line_ending(text)
            byte_offset = data["submatches"][0]["start"]
            first_match = len(text.encode("utf-8")[:byte_offset].decode("utf-8", errors="replace"))
            begin = max(0, first_match - 100) if len(text) > 600 else 0
            excerpt = text[begin:begin + 600]
            matches.append({
                "path": safe_path, "line": data["line_number"], "column": begin,
                "text": excerpt, "line_excerpted": begin > 0 or len(excerpt) < len(text),
                "read_request": {"path": safe_path, "start_line": data["line_number"], "line_count": 20},
            })
        if not more:
            await process.wait()

    # Drain stderr concurrently: a directory full of unreadable files must not
    # deadlock the child while its stdout is being consumed.
    async def drain_errors() -> str:
        result = bytearray()
        assert process.stderr is not None
        while chunk := await process.stderr.read(4096):
            if len(result) < 2048:
                result.extend(chunk[:2048 - len(result)])
        return result.decode(errors="replace")

    errors = asyncio.create_task(drain_errors())
    try:
        await asyncio.wait_for(consume(), timeout=SEARCH_TIMEOUT)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        error_text = await errors
    if not more and process.returncode not in (0, 1):
        raise ValueError(f"search failed ({process.returncode}): {error_text}")
    next_request = {**request, "offset": offset + len(matches)} if more else None
    result = {"matches": matches, "matching_lines_total": None if more else seen,
              "matching_lines_at_least": seen, "complete": not more,
              "next_request": next_request, "warning": error_text or None,
              "scope": "text files; .git and symlinks excluded; pagination requires unchanged files"}
    if context_lines:
        result.update(await asyncio.to_thread(_search_context, root, matches, context_lines))
    return result


async def run_batch(workspace: str, requests: list, *, search: bool, regex_endpoints: bool = False) -> dict:
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_BATCH:
        return {"error": f"requests must contain 1..{MAX_BATCH} entries"}
    root = Path(workspace).resolve(strict=True)
    semaphore = asyncio.Semaphore(4)
    endpoint_scan = None
    if search and regex_endpoints:
        from app.agents.regex_endpoints import RegexEndpointBatch
        endpoint_scan = RegexEndpointBatch(root)

    async def one(index: int, request: Any) -> dict:
        async with semaphore:
            try:
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                result = (await endpoint_scan.search(request) if endpoint_scan else
                          await search_page(root, request) if search else
                          await asyncio.to_thread(read_page, root, request))
                return {"index": index, **result}
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                return {"index": index, "error": str(exc) or "search timed out; narrow the path", "complete": False}

    return {"results": await asyncio.gather(*(one(i, r) for i, r in enumerate(requests)))}


def encode_evidence(payload: dict) -> str:
    """Compact only the wire representation; retain every returned evidence row.

    Repeated field names and a second copy of each source path in read_request
    were a substantial part of actual MCP outputs. Column legends keep the
    representation self-describing without trimming source text or pages.
    """
    if "results" not in payload:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    for result in payload["results"]:
        lines = list(result.get("lines", []))
        for window in result.get("context_windows", []):
            lines.extend(window.get("lines", []))
        for line in lines:
            if set(line) != {"line", "column", "text"}:
                return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        for match in result.get("matches", []):
            if (set(match) != {"path", "line", "column", "text", "line_excerpted", "read_request"}
                    or match.get("read_request") != {"path": match["path"], "start_line": match["line"], "line_count": 20}):
                # A future/nonstandard range must not silently lose metadata.
                return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = {"results": []}
    for result in payload["results"]:
        row = dict(result)
        if "lines" in row:
            encoded["line_columns"] = ["line", "column", "text"]
            row["lines"] = [[line[key] for key in encoded["line_columns"]] for line in row["lines"]]
        if "matches" in row:
            encoded["match_columns"] = ["path", "line", "column", "text", "line_excerpted"]
            # read_request is exactly derivable from path/line; keep its shared
            # default explicit so even an empty search remains self-describing.
            encoded["read_match"] = {"path": "match.path", "start_line": "match.line", "line_count": 20}
            row["matches"] = [[match[key] for key in encoded["match_columns"]] for match in row["matches"]]
        if "context_windows" in row:
            encoded["line_columns"] = ["line", "column", "text"]
            row["context_windows"] = [
                {**window, "lines": [[line[key] for key in encoded["line_columns"]] for line in window["lines"]]}
                for window in row["context_windows"]
            ]
        encoded["results"].append(row)
    return json.dumps(encoded, ensure_ascii=False, separators=(",", ":"))


def get_evidence_mcp_server(workspace: str, *, statistics: bool = False, regex_endpoints: bool = False):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    def schema(properties: dict, required: list[str]) -> dict:
        return {"type": "object", "properties": {"requests": {
            "type": "array", "minItems": 1, "maxItems": MAX_BATCH,
            "items": {"type": "object", "properties": properties, "required": required,
                      "additionalProperties": False},
        }}, "required": ["requests"], "additionalProperties": False}

    async def invoke(args, search):
        payload = await run_batch(workspace, args.get("requests"), search=search, regex_endpoints=regex_endpoints)
        return {"content": [{"type": "text", "text": encode_evidence(payload)}]}

    @tool("search_evidence",
          "Batch 1-8 independent regex searches in this workspace. Results include exact file/line locations, "
          "bounded excerpts and next_request when more matches exist. A partial page is not exhaustive. "
          "Regex engine auto-selects PCRE2 for lookaround/backreferences when available. "
          "Each match is a row described by match_columns; read_match gives the range template. "
          "Group known independent questions in one call. Set context_lines (0-40 lines each side) to retrieve "
          "nearby evidence in this same call: first 3 matches per request, overlapping ranges merged, total 4000 "
          "context characters per request. Unexpanded matches and range continuations are explicit. "
          "Use read_evidence only for missing decisive context; range_complete never means whole-file coverage."
          + (" Optional regex_endpoints contains verified omitted first/last matching LF rows and counts; "
             "these are file-order samples, not a complete event history or causal proof." if regex_endpoints else ""),
          schema({"pattern": {"type": "string"}, "path": {"type": "string"},
                  "glob": {"type": "string"}, "literal": {"type": "boolean"},
                  "ignore_case": {"type": "boolean"}, "offset": {"type": "integer", "minimum": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_MATCHES},
                  "context_lines": {"type": "integer", "minimum": 0, "maximum": 40}}, ["pattern"]))
    async def search(args):
        return await invoke(args, True)

    @tool("read_evidence",
          "Read 1-8 independent UTF-8 file ranges together, with source line numbers. start_line is 1-based; "
          "column is a 0-based character offset for long-line continuation. Each range has an 8000-character "
          "budget; each line is a [line, column, text] row. Follow next_request to retrieve remaining content. "
          "For a file tail, use tail_lines (1-2000) instead of start_line/column/line_count; it returns native LF line numbers "
          "and total_lines, so no separate line-count request is needed. Original files remain available.",
          schema({"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
                  "line_count": {"type": "integer", "minimum": 1, "maximum": 2000},
                  "column": {"type": "integer", "minimum": 0},
                  "tail_lines": {"type": "integer", "minimum": 1, "maximum": 2000}}, ["path"]))
    async def read(args):
        return await invoke(args, False)

    tools = [search, read]
    if statistics:
        from app.agents.evidence_statistics import run_statistics_batch

        @tool("summarize_evidence",
              "Batch 1-8 independent full-file regex statistics. Each request names one UTF-8 file. "
              "Use count for occurrence/line counts, numeric for exact finite minimum/maximum, or values for "
              "a bounded literal value distribution. numeric/values require a Python regex named (?P<value>...) "
              "capture. Returns source LF line references, first/last samples and numeric extrema in one call. "
              "Check complete and coverage limits: a partial scan/distribution cannot prove absence or a global range. "
              "Counts are physical matches per file; overlapping attachments are not distinct events. "
              "A regex matches only its specified field/format, not every possible incident. "
              "Use original tools for unsupported statistics or decisive omitted evidence.",
              schema({"path": {"type": "string"}, "pattern": {"type": "string", "maxLength": 1000},
                      "mode": {"type": "string", "enum": ["count", "numeric", "values"]},
                      "ignore_case": {"type": "boolean"}}, ["path", "pattern"]))
        async def summarize(args):
            payload = await run_statistics_batch(workspace, args.get("requests"))
            return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]}

        tools.append(summarize)
    return create_sdk_mcp_server(name="evidence", version="1.3.0" if statistics else "1.2.0", tools=tools)
