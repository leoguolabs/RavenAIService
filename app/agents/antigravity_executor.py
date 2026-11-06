"""Antigravity headless runtime adapted to Raven's existing message protocol."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import uuid
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage
from claude_agent_sdk.types import StreamEvent

from app.agents.antigravity_control import control_bridge, normalize_tool
from app.agents.runtime_secrets import redact


class EventAdapter:
    def __init__(self, model, names=None):
        self.model = model
        self.names = names or {}
        self.session_id = ""
        self.started = set()
        self.finished = set()
        self.text = {}
        self.result = None

    def messages(self, row):
        kind = row.get("event")
        if kind == "init":
            self.session_id = row.get("conversation_id", "")
            return [SystemMessage(subtype="init", data={"session_id": self.session_id, **row.get("init", {})})]
        if kind == "result":
            data = row["result"]
            self.result = data
            usage = dict(data.get("usage") or {})
            usage["cache_read_input_tokens"] = usage.pop("cache_read_tokens", 0)
            return [ResultMessage(subtype="success" if data.get("status") == "SUCCESS" else "error_during_execution",
                duration_ms=int(float(data.get("duration_seconds", 0)) * 1000), duration_api_ms=0,
                is_error=data.get("status") != "SUCCESS", num_turns=data.get("num_turns", 1),
                session_id=data.get("conversation_id", self.session_id), usage=usage,
                result=data.get("response", ""), errors=[data["error"]] if data.get("error") else None)]
        if kind != "step_update":
            return []
        step = row.get("step_update", {})
        index = step.get("step_index")
        tool_id = f"agy-{self.session_id}-{index}"
        messages = []
        if step.get("step_type") == "agent_response":
            delta = step.get("text_delta", "")
            if delta:
                self.text[index] = self.text.get(index, "") + delta
                messages.append(StreamEvent(uuid=str(uuid.uuid4()), session_id=self.session_id,
                    event={"type": "content_block_delta", "index": index,
                           "delta": {"type": "text_delta", "text": delta}}))
            if step.get("state") == "DONE" and index not in self.finished:
                self.finished.add(index)
                text = self.text.get(index, "")
                if text:
                    messages.append(AssistantMessage(content=[TextBlock(text=text)], model=self.model))
        elif step.get("step_type") == "tool":
            info = step.get("tool_info", {})
            name, args = normalize_tool(info.get("name") or step.get("tool_name", ""),
                                        info.get("parameters") or {}, self.names)
            if name and index not in self.started:
                self.started.add(index)
                messages.append(AssistantMessage(content=[ToolUseBlock(id=tool_id, name=name, input=args)], model=self.model))
            if step.get("state") == "DONE" and index not in self.finished:
                self.finished.add(index)
                output = info.get("output", "")
                if not isinstance(output, (str, list)):
                    output = json.dumps(output, ensure_ascii=False)
                if info.get("error"):
                    output = json.dumps(info["error"], ensure_ascii=False)
                messages.append(UserMessage(content=[ToolResultBlock(tool_use_id=tool_id,
                    content=output, is_error=bool(info.get("error")))]))
        return messages


async def prompt_text(prompt):
    if isinstance(prompt, str):
        return prompt
    chunks = []
    async for item in prompt:
        content = item.get("message", {}).get("content", "")
        if isinstance(content, str):
            chunks.append(content)
        else:
            for block in content:
                if block.get("type") != "text":
                    raise ValueError("Antigravity input requires text; use workspace attachments")
                chunks.append(block["text"])
    return "\n".join(chunks)


def write_config(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


async def terminate(process):
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), 3)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def query(*, prompt, options):
    text = await prompt_text(prompt)
    timeout = float((options.env or {}).get("API_TIMEOUT_MS", "3600000")) / 1000
    with tempfile.TemporaryDirectory(prefix="raven-agy-") as directory:
        task_home = Path(directory)
        settings_dir = task_home / ".gemini" / "antigravity-cli"
        model_name = "magpie/" + str(options.model)
        write_config(settings_dir / "settings.json", {
            "modelProvider": "gemini", "model": model_name,
            "customModelsConfig": {"customModels": {model_name: {
                "apiProvider": "API_PROVIDER_GOOGLE_GEMINI", "modelName": str(options.model)}}},
            "permissions": {"allow": ["mcp(*)"], "deny": [], "ask": []},
        })
        skills = Path(options.cwd) / ".claude" / "skills"
        if skills.is_dir() and "project" in (options.setting_sources or []):
            shutil.copytree(skills, task_home / ".gemini" / "config" / "skills", symlinks=False)
        async with control_bridge(options) as (control, control_url, control_token):
            write_config(task_home / ".gemini" / "config" / "mcp_config.json", {"mcpServers": {
                name: {"serverUrl": f"{control_url}/mcp/{name}", "headers": {"Authorization": "Bearer " + control_token},
                       "timeoutSeconds": int(timeout)} for name in control.clients}})
            hook_command = shlex.join([sys.executable, str(Path(__file__).with_name("antigravity_hook.py"))])
            write_config(task_home / ".gemini" / "config" / "hooks.json", {"raven": {
                "PreToolUse": [{"matcher": ".*", "hooks": [{"command": hook_command + " PreToolUse", "timeout": int(timeout)}]}],
                "PreInvocation": [{"command": hook_command + " PreInvocation", "timeout": 30}],
                "Stop": [{"command": hook_command + " Stop", "timeout": int(timeout)}],
            }})
            # Do not inherit upstream keys or another user's CLI state.
            env = {key: value for key, value in os.environ.items() if key in {
                "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "PYTHONPATH"}}
            env.update({"HOME": directory, "XDG_CONFIG_HOME": str(task_home / ".config"),
                "GEMINI_API_KEY": options.env["ANTHROPIC_API_KEY"],
                "GOOGLE_GEMINI_BASE_URL": options.env["ANTHROPIC_BASE_URL"],
                "RAVEN_CONTROL_URL": control_url, "RAVEN_CONTROL_TOKEN": control_token})
            argv = ["agy", "--model", model_name, "--input-format", "stream-json", "--output-format", "stream-json"]
            process = await asyncio.create_subprocess_exec(*argv, cwd=str(options.cwd), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, limit=4 * 1024 * 1024)
            # Drain diagnostics without persisting prompts, credentials or CLI internals.
            async def drain():
                while await process.stderr.read(65536):
                    pass
            stderr_task = asyncio.create_task(drain())
            adapter = EventAdapter(str(options.model), control.mcp_names)
            try:
                process.stdin.write((json.dumps({"event": "user", "message": {"content": text}}) + "\n").encode())
                await process.stdin.drain()
                process.stdin.close()
                async with asyncio.timeout(timeout):
                    while line := await process.stdout.readline():
                        try:
                            row = json.loads(line)
                        except ValueError as exc:
                            raise RuntimeError("Invalid Antigravity event stream") from exc
                        for message in adapter.messages(row):
                            if isinstance(message, AssistantMessage):
                                texts = [block.text for block in message.content if isinstance(block, TextBlock)]
                                if texts:
                                    control.last_assistant_message = "\n".join(texts)
                            yield redact(message, (control_token, options.env["ANTHROPIC_API_KEY"]))
                        step = row.get("step_update", {})
                        if step.get("step_type") == "tool" and step.get("state") == "DONE":
                            info = step.get("tool_info", {})
                            name, args = normalize_tool(info.get("name", step.get("tool_name", "")),
                                info.get("parameters", {}), control.mcp_names)
                            if not name.startswith("mcp__"):
                                await control.hooks("PostToolUse", name, args,
                                    f"agy-{adapter.session_id}-{step.get('step_index')}", info.get("output"))
                    code = await process.wait()
                if code != 0 or adapter.result is None:
                    raise RuntimeError(f"Antigravity failed (exit {code}); no successful terminal event")
            finally:
                await terminate(process)
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
