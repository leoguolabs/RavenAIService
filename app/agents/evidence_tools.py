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


def read_page(root: Path, request: dict) -> dict:
    path = _path(root, request.get("path"))
    if not path.is_file():
        raise ValueError("read requires a regular file")
    start = _integer(request.get("start_line", 1), "start_line", 1, 10**9)
    column = _integer(request.get("column", 0), "column", 0, 10**9)
    count = _integer(request.get("line_count", 80), "line_count", 1, 2000)
    budget = MAX_READ_CHARS
    lines = []
    next_request = None
    relative = path.relative_to(root).as_posix()
    with path.open("rb") as stream:
        if b"\x00" in stream.read(8192):
            raise ValueError("binary file; use a format-aware native tool")
    with path.open(encoding="utf-8", errors="replace") as stream:
        for number, raw in enumerate(stream, 1):
            if number < start:
                continue
            if number >= start + count or budget == 0:
                next_request = {"path": relative, "start_line": number, "line_count": count}
                break
            text = raw.rstrip("\r\n")
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
    return {"path": relative, "encoding": "utf-8 (replacement on invalid bytes)",
            "lines": lines, "has_more": next_request is not None, "next_request": next_request}


async def search_page(root: Path, request: dict) -> dict:
    pattern = request.get("pattern")
    if not isinstance(pattern, str) or not pattern or len(pattern) > 4096:
        raise ValueError("pattern must contain 1..4096 characters")
    path = _path(root, request.get("path", "."))
    offset = _integer(request.get("offset", 0), "offset", 0, 10**9)
    limit = _integer(request.get("limit", 20), "limit", 1, MAX_SEARCH_MATCHES)
    executable = shutil.which("rg")
    if not executable:
        raise ValueError("ripgrep is unavailable; use native search tools")
    args = [executable, "--json", "--sort", "path", "--hidden", "--no-ignore",
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
            text = text.rstrip("\r\n")
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
    return {"matches": matches, "matching_lines_total": None if more else seen,
            "matching_lines_at_least": seen, "complete": not more,
            "next_request": next_request, "warning": error_text or None,
            "scope": "text files; .git and symlinks excluded; pagination requires unchanged files"}


async def run_batch(workspace: str, requests: list, *, search: bool) -> dict:
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_BATCH:
        return {"error": f"requests must contain 1..{MAX_BATCH} entries"}
    root = Path(workspace).resolve(strict=True)
    semaphore = asyncio.Semaphore(4)

    async def one(index: int, request: Any) -> dict:
        async with semaphore:
            try:
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                result = (await search_page(root, request) if search else
                          await asyncio.to_thread(read_page, root, request))
                return {"index": index, **result}
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                return {"index": index, "error": str(exc) or "search timed out; narrow the path", "complete": False}

    return {"results": await asyncio.gather(*(one(i, r) for i, r in enumerate(requests)))}


def get_evidence_mcp_server(workspace: str):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    def schema(properties: dict, required: list[str]) -> dict:
        return {"type": "object", "properties": {"requests": {
            "type": "array", "minItems": 1, "maxItems": MAX_BATCH,
            "items": {"type": "object", "properties": properties, "required": required,
                      "additionalProperties": False},
        }}, "required": ["requests"], "additionalProperties": False}

    async def invoke(args, search):
        payload = await run_batch(workspace, args.get("requests"), search=search)
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}

    @tool("search_evidence",
          "Batch 1-8 independent regex searches in this workspace. Results include exact file/line locations, "
          "bounded excerpts and next_request when more matches exist. A partial page is not exhaustive. "
          "Group known independent questions in one call; use read_evidence for surrounding context.",
          schema({"pattern": {"type": "string"}, "path": {"type": "string"},
                  "glob": {"type": "string"}, "literal": {"type": "boolean"},
                  "ignore_case": {"type": "boolean"}, "offset": {"type": "integer", "minimum": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_MATCHES}}, ["pattern"]))
    async def search(args):
        return await invoke(args, True)

    @tool("read_evidence",
          "Read 1-8 independent UTF-8 file ranges together, with source line numbers. start_line is 1-based; "
          "column is a 0-based character offset for long-line continuation. Each range has an 8000-character "
          "budget; follow next_request to retrieve remaining content. Original files remain available.",
          schema({"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
                  "line_count": {"type": "integer", "minimum": 1, "maximum": 2000},
                  "column": {"type": "integer", "minimum": 0}}, ["path"]))
    async def read(args):
        return await invoke(args, False)

    return create_sdk_mcp_server(name="evidence", version="1.0.0", tools=[search, read])
