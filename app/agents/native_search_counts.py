"""Bounded count summaries for native Bash searches verified against the log."""

import asyncio
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time

from app.agents.log_topic_context import MAX_FILE_BYTES, MAX_LINE_BYTES, MAX_LINES, SCAN_SECONDS, _identity, _path_token

_FIELD = re.compile(rb"(?<![\w-])([A-Za-z_][A-Za-z0-9_]{0,47})=(0[xX][0-9a-fA-F]+|[0-9]+)(?=[,;\s]|$)")
_MAX_ROWS = 200


class NativeSearchCounts:
    def __init__(self, workspace: str, locale: str):
        self.root = Path(workspace).resolve()
        self.locale = locale
        self.lock = asyncio.Lock()
        self.scanned = set()
        self.bytes_remaining = 2 * MAX_FILE_BYTES

    def collect(self, data: dict) -> dict | None:
        response = data.get("tool_response")
        arguments = data.get("tool_input")
        if not isinstance(response, dict) or not isinstance(arguments, dict):
            return None
        stdout = response.get("stdout")
        command = arguments.get("command")
        cwd = data.get("cwd")
        if (
            not isinstance(stdout, str) or not stdout or len(stdout) > 160_000
            or not isinstance(command, str) or len(command) > 1024
            or not isinstance(cwd, str) or not Path(cwd).is_absolute()
            or any(response.get(k) for k in ("stderr", "interrupted", "is_error", "isImage", "exit_code"))
            or any(c in command for c in "\r\n`")
        ):
            return None
        tokens = shlex.split(command)
        if len(tokens) != 4 or tokens[0] != "grep" or tokens[1] not in ("-n", "-nF", "-Fn", "-c", "-cF", "-Fc"):
            # Recognize only a count bracketed by explicit separators. Treat
            # the other commands and their output as opaque; never run them.
            parts = re.split(r";[ \t]*echo[ \t]+---[ \t]*;[ \t]*", command)
            sections = stdout.split("\n---\n")
            if len(parts) != 3 or len(sections) != 3 or "$(" in command:
                return None
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            shell_tokens = list(lexer)
            separator = [";", "echo", "---", ";"]
            positions = [
                i for i in range(len(shell_tokens))
                if shell_tokens[i:i + len(separator)] == separator
            ]
            if len(positions) != 2:
                return None
            tokens = shell_tokens[positions[0] + len(separator):positions[1]]
            if tokens != shlex.split(parts[1]):
                return None
            if len(tokens) != 4 or tokens[0] != "grep" or tokens[1] not in ("-c", "-cF", "-Fc"):
                return None
            stdout = sections[1]
        count_only = tokens[1] in ("-c", "-cF", "-Fc")
        pattern, target = tokens[2:]
        if not re.fullmatch(r"[A-Za-z0-9_ :/-]{1,80}", pattern) or pattern.startswith("-") or target.startswith("-") or not _path_token(target):
            return None
        directory = Path(cwd).resolve(strict=True)
        logs = self.root / "logs"
        path = (directory / target).resolve(strict=True)
        if not directory.is_relative_to(self.root) or not path.is_relative_to(logs):
            return None
        key = (path, pattern)
        if key in self.scanned or not self.bytes_remaining:
            return None
        self.scanned.add(key)
        deadline = time.monotonic() + SCAN_SECONDS
        rows = []
        encoded = pattern.encode()
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > min(MAX_FILE_BYTES, self.bytes_remaining):
                return None
            self.bytes_remaining -= before.st_size
            for number in range(1, MAX_LINES + 2):
                raw = stream.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_LINE_BYTES or number > MAX_LINES or b"\0" in raw or time.monotonic() > deadline:
                    return None
                if encoded in raw:
                    if len(rows) >= _MAX_ROWS:
                        return None
                    rows.append((number, raw.removesuffix(b"\n")))
            if _identity(before) != _identity(os.fstat(stream.fileno())) or _identity(before) != _identity(path.stat()):
                return None
        if len(rows) < 2:
            return None
        expected = "\n".join(f"{n}:{row.decode('utf-8', errors='strict')}" for n, row in rows)
        if count_only:
            expected = str(len(rows))
        if stdout not in (expected, expected + "\n"):
            return None

        fields = {}
        for _, row in rows:
            entries = _FIELD.findall(row)
            if len(entries) > 24:
                return None
            current = {key: value for key, value in entries if len(value) <= 32}
            for name in current:
                if sum(key == name for key, _ in entries) != 1:
                    current[name] = None
            for name, value in current.items():
                if value is not None:
                    fields.setdefault(name, []).append(value)
        counts = {
            key.decode(): len(set(values))
            for key, values in sorted(fields.items())
            if len(values) == len(rows) and len(set(values)) < len(rows)
        }
        return {
            "path": path.relative_to(self.root).as_posix(),
            "literal_pattern": pattern,
            "verification": "count" if count_only else "native_rows",
            "matched_physical_lines": len(rows),
            "distinct_literal_values_by_field": counts,
            "first_matching_line": rows[0][0],
            "last_matching_line": rows[-1][0],
        }

    async def after_tool(self, data, tool_use_id, context):
        if not isinstance(data, dict) or data.get("hook_event_name") != "PostToolUse" or data.get("tool_name") != "Bash":
            return {}
        try:
            async with self.lock:
                result = await asyncio.to_thread(self.collect, data)
        except (OSError, ValueError):
            return {}
        if not result:
            return {}
        note = (
            "Verified counts for this exact log search. Physical lines and distinct literal field values are separate. "
            "These counts do not prove incident totals, action semantics or operation success."
            if self.locale == "en" else
            "本次日志检索的已核验计数。物理匹配行数和字段不同字面取值数分别统计。"
            "这些计数不证明事件总数、动作语义或操作成功。"
        )
        return {"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": note + "\n" + json.dumps(result, ensure_ascii=False),
        }}
