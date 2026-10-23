"""Coalesce short native source reads without dropping the requested range."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
from pathlib import Path

from app.agents.evidence_context import MAX_SOURCE_BYTES, _SOURCE_SUFFIXES

logger = logging.getLogger(__name__)
MAX_WINDOW_LINES = 160
MAX_WINDOW_BYTES = 8_000
SHORT_READ_LINES = 80
MAX_SOURCE_LINES = 50_000


class SourceReadWindow:
    def __init__(self, workspace: str, task_id: str = "", locale: str = "zh"):
        self.root = Path(workspace).resolve()
        self.task_id = task_id
        self.locale = locale
        self.lock = asyncio.Lock()
        # Cache only byte-count prefixes, never source text. Each run owns it.
        self.cache: OrderedDict[Path, tuple[tuple, list[int]]] = OrderedDict()

    @staticmethod
    def _identity(path: Path) -> tuple:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def expand(self, arguments: dict) -> dict | None:
        value, start, count = arguments.get("file_path"), arguments.get("offset"), arguments.get("limit")
        if (not isinstance(value, str) or not value or type(start) is not int or start < 1
                or type(count) is not int or not 1 <= count < SHORT_READ_LINES):
            return None
        path = (self.root / value).resolve(strict=True)
        relative = path.relative_to(self.root)
        if (not relative.parts or relative.parts[0] not in ("repo", "related_repos") or ".git" in relative.parts
                or path.suffix.lower() not in _SOURCE_SUFFIXES or not path.is_file()):
            return None
        identity = self._identity(path)
        if identity[2] > MAX_SOURCE_BYTES:
            return None
        cached = self.cache.get(path)
        if cached is not None and cached[0] == identity:
            costs = cached[1]
            self.cache.move_to_end(path)
        else:
            with path.open("rb") as stream:
                raw = stream.read(MAX_SOURCE_BYTES + 1)
            if len(raw) > MAX_SOURCE_BYTES or b"\0" in raw or self._identity(path) != identity:
                return None
            # An encoding we cannot measure reliably keeps the native request.
            raw.decode("utf-8")
            lines = raw.split(b"\n")
            if not lines[-1]:
                lines.pop()
            if len(lines) > MAX_SOURCE_LINES:
                return None
            costs = [0]
            for line in lines:
                # Reserve native line-number formatting overhead as well.
                costs.append(costs[-1] + len(line) + 32)
            self.cache[path] = identity, costs
            self.cache.move_to_end(path)
            while len(self.cache) > 4:
                self.cache.popitem(last=False)
        requested_end = start + count - 1
        total_lines = len(costs) - 1
        if requested_end > total_lines or costs[requested_end] - costs[start - 1] >= MAX_WINDOW_BYTES:
            return None
        first = max(1, start - 32)
        last = min(total_lines, first + MAX_WINDOW_LINES - 1)
        while costs[last] - costs[first - 1] > MAX_WINDOW_BYTES:
            before, after = start - first, last - requested_end
            if after >= before and after:
                last -= 1
            elif before:
                first += 1
            else:
                return None
        if first == start and last == requested_end:
            return None
        return {**arguments, "offset": first, "limit": last - first + 1}

    async def before_tool(self, input_data, tool_use_id, context):
        if (not isinstance(input_data, dict) or input_data.get("hook_event_name") != "PreToolUse"
                or input_data.get("tool_name") != "Read" or not isinstance(input_data.get("tool_input"), dict)):
            return {}
        original = input_data["tool_input"]
        try:
            async with self.lock:
                expanded = await asyncio.to_thread(self.expand, original)
        except (OSError, ValueError, UnicodeError):
            return {}
        if expanded is None:
            return {}
        first, last = expanded["offset"], expanded["offset"] + expanded["limit"] - 1
        start, end = original["offset"], original["offset"] + original["limit"] - 1
        explanation = (
            f"This source Read includes bounded nearby context, lines {first}-{last}; "
            f"the entire requested range {start}-{end} is retained. Use the returned line numbers. "
            "This excerpt does not establish whole-file coverage."
            if self.locale == "en" else
            f"本次源码 Read 合并了有界的周边上下文，返回第 {first}–{last} 行，完整保留原请求第 {start}–{end} 行。"
            "引用以实际返回的行号为准；片段不代表全文覆盖。"
        )
        logger.info("Expanded source read task_id=%s requested=%d-%d returned=%d-%d",
                    self.task_id, start, end, first, last)
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "updatedInput": expanded, "additionalContext": explanation,
        }}
