"""Raven workspace function tools for the official OpenAI Agents SDK.

Authorization and hooks live in the shared Control; this module only implements
explicit workspace operations. Shell access has the same trust as Raven's Bash
permission and is never granted merely by selecting this executor.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import signal
from contextlib import suppress
from pathlib import Path

MAX_OUTPUT = 64 * 1024


def schema(required=(), **properties):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean"}
TOOLS = {
    "Read": ("Read a UTF-8 file with 1-based line numbers. offset and limit select a line window.",
        schema(("file_path",), file_path=STRING, offset=INTEGER, limit=INTEGER)),
    "Glob": ("Find paths by a glob pattern under path (default workspace).",
        schema(("pattern",), pattern=STRING, path=STRING)),
    "Grep": ("Search file contents using ripgrep. output_mode: content, files_with_matches, or count.",
        schema(("pattern",), pattern=STRING, path=STRING, glob=STRING, type=STRING,
               output_mode=STRING, head_limit=INTEGER, offset=INTEGER, multiline=BOOLEAN,
               **{"-i": BOOLEAN, "-n": BOOLEAN, "-A": INTEGER, "-B": INTEGER, "-C": INTEGER})),
    "Bash": ("Run a shell command in the workspace. timeout is in milliseconds; no background execution.",
        schema(("command",), command=STRING, timeout=INTEGER, description=STRING)),
    "Write": ("Write UTF-8 content to a workspace file, creating parent directories.",
        schema(("file_path", "content"), file_path=STRING, content=STRING)),
    "Edit": ("Replace an exact string in a UTF-8 workspace file; ambiguous matches require replace_all.",
        schema(("file_path", "old_string", "new_string"), file_path=STRING, old_string=STRING,
               new_string=STRING, replace_all=BOOLEAN)),
    "Skill": ("Read a materialized Skill's full instructions. Resolve its references relative to its directory.",
        schema(("skill",), skill=STRING, args=STRING)),
}


class WorkspaceTools:
    def __init__(self, options):
        self.cwd = Path(options.cwd).resolve()
        self.roots = [self.cwd, *(Path(path).resolve() for path in (options.add_dirs or []))]
        self.skills_enabled = "project" in (options.setting_sources or [])
        # Raven materializes trusted Skills as links to shared packages. Snapshot
        # only the links present before model execution, and grant read access;
        # never make shared packages writable or trust links created by tools.
        skills = self.cwd / ".claude" / "skills"
        self.skill_roots = [path.resolve() for path in skills.iterdir()
                            if path.is_symlink() and path.is_dir()] if self.skills_enabled and skills.is_dir() else []

    def path(self, value, *, read=False):
        path = Path(value)
        path = (path if path.is_absolute() else self.cwd / path).resolve()
        roots = self.roots + self.skill_roots if read else self.roots
        if not any(path.is_relative_to(root) for root in roots):
            raise ValueError("路径超出任务工作区 / Path is outside the task workspace")
        return path

    async def process(self, argv, timeout=120):
        # Do not pass provider keys, database passwords or per-run bridge tokens
        # to model-controlled commands. Application tools run in the parent.
        env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        process = await asyncio.create_subprocess_exec(*argv, cwd=str(self.cwd), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        output = bytearray()
        truncated = False

        async def drain():
            nonlocal truncated
            while chunk := await process.stdout.read(8192):
                remaining = MAX_OUTPUT - len(output)
                output.extend(chunk[:remaining])
                truncated |= len(chunk) > remaining
            return await process.wait()

        try:
            async with asyncio.timeout(timeout):
                code = await drain()
        finally:
            # The shell may already have exited while a child still owns stdout.
            # Always terminate this invocation's group, including such children.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            else:
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    pass
                finally:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
        text = output.decode("utf-8", errors="replace")
        if truncated:
            text += "\n[Output truncated; narrow the query / 输出已截断，请缩小查询范围]"
        return code, text

    async def call(self, name, args):
        if name == "Bash":
            code, text = await self.process(["/bin/bash", "-c", args["command"]],
                min(max(int(args.get("timeout", 120000)), 1), 600000) / 1000)
            return {"stdout": text, "stderr": "", "exit_code": code}
        if name == "Grep":
            argv = ["rg", "--no-heading", "--color", "never"]
            mode = args.get("output_mode", "files_with_matches")
            if mode == "files_with_matches":
                argv.append("-l")
            elif mode == "count":
                argv.append("-c")
            elif mode != "content":
                raise ValueError("Unknown Grep output_mode")
            if args.get("-n", True) and mode == "content":
                argv.append("-n")
            for flag in ("-i",):
                if args.get(flag):
                    argv.append(flag)
            if args.get("multiline"):
                argv.append("-U")
            for flag in ("-A", "-B", "-C"):
                if flag in args:
                    argv.extend([flag, str(max(0, int(args[flag])))])
            for key in ("glob", "type"):
                if args.get(key):
                    argv.extend(["--" + key, args[key]])
            argv.extend(["--", args["pattern"], str(self.path(args.get("path", "."), read=True))])
            code, text = await self.process(argv)
            if code not in (0, 1):
                raise ValueError(text)
            offset = max(0, int(args.get("offset", 0)))
            limit = min(max(int(args.get("head_limit", 200)), 1), 2000)
            return "\n".join(text.splitlines()[offset:offset + limit])
        if name == "Glob":
            root = self.path(args.get("path", "."), read=True)
            pattern = args["pattern"]
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ValueError("Glob pattern must be relative to path")
            paths = await asyncio.to_thread(lambda: list(itertools.islice(root.glob(pattern), 2001)))
            return "\n".join(str(path) for path in paths[:2000]) + ("\n[Truncated]" if len(paths) > 2000 else "")
        if name == "Skill":
            if not self.skills_enabled:
                raise ValueError("Project Skills are disabled / 项目 Skill 未启用")
            skill = args["skill"]
            if not skill or Path(skill).name != skill or skill in {".", ".."}:
                raise ValueError("Invalid Skill name")
            directory = self.path(f".claude/skills/{skill}", read=True)
            text = await asyncio.to_thread(self.path(directory / "SKILL.md", read=True).read_text, encoding="utf-8")
            if len(text) > MAX_OUTPUT:
                raise ValueError("Skill exceeds tool output limit / Skill 超出工具输出限制")
            return f"Skill directory: {directory}\n{text}"
        path = self.path(args["file_path"], read=name == "Read")
        if name == "Read":
            offset = max(1, int(args.get("offset", 1)))
            limit = min(max(int(args.get("limit", 2000)), 1), 4000)
            def read():
                lines = []
                size = 0
                with path.open(encoding="utf-8", errors="replace") as file:
                    for index, line in enumerate(file, 1):
                        if index < offset:
                            continue
                        if index >= offset + limit:
                            break
                        entry = f"{index}\t{line.rstrip()}\n"
                        if size + len(entry) > MAX_OUTPUT:
                            lines.append("[Output truncated; use offset/limit]\n")
                            break
                        lines.append(entry)
                        size += len(entry)
                return "".join(lines)
            return await asyncio.to_thread(read)
        if name == "Write":
            def write():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(args["content"], encoding="utf-8")
            await asyncio.to_thread(write)
            return f"Written: {path}"
        if name == "Edit":
            def edit():
                text = path.read_text(encoding="utf-8")
                old = args["old_string"]
                count = text.count(old)
                if not old or not count or (count != 1 and not args.get("replace_all", False)):
                    raise ValueError("old_string must match uniquely unless replace_all is true")
                path.write_text(text.replace(old, args["new_string"], -1 if args.get("replace_all") else 1), encoding="utf-8")
            await asyncio.to_thread(edit)
            return f"Edited: {path}"
        raise ValueError("Unsupported workspace tool")
