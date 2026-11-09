"""Per-run MCP transport and permission hooks for Antigravity.

SDK tools keep their original Python handlers and Raven approval callbacks.
The external process receives only an ephemeral loopback credential.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import socket
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.agents.openai_bridge import _LoopbackServer

TOOL_NAMES = {
    "run_command": "Bash", "command_status": "TaskOutput", "send_command_input": "Bash",
    "view_file": "Read", "read_file": "Read", "view_file_outline": "Read", "view_code_item": "Read",
    "list_dir": "Glob", "find_by_name": "Glob", "grep_search": "Grep",
    "write_to_file": "Write", "replace_file_content": "Edit", "multi_replace_file_content": "Edit",
    "read_url_content": "WebFetch", "search_web": "WebSearch",
    "ask_user_question": "AskUserQuestion", "notify_user": "AskUserQuestion",
}


def _mcp_arguments(args):
    # Some local models encode call_mcp_tool.Arguments as a JSON string.
    # Normalize before permission hooks, using the same value for tool IDs.
    if isinstance(args, str):
        try:
            return json.loads(args)
        except ValueError:
            pass
    return args


def normalize_tool(name: str, args: dict, mcp_names: dict[str, str] | None = None):
    if name == "call_mcp_tool":
        canonical = f"mcp__{args.get('ServerName', '')}__{args.get('ToolName', '')}"
        if canonical in (mcp_names or {}):
            return canonical, _mcp_arguments(args.get("Arguments", {}))
    canonical = (mcp_names or {}).get(name, TOOL_NAMES.get(name, name))
    converted = dict(args)
    if canonical == "Bash":
        converted["command"] = args.get("CommandLine", args.get("command", ""))
    if canonical in {"Read", "Write", "Edit"}:
        converted["file_path"] = args.get("Path", args.get("AbsolutePath", args.get("TargetFile", args.get("file_path", ""))))
    file_path = str(converted.get("file_path", ""))
    if (canonical == "Read" and Path(file_path).name == "SKILL.md"
            and any(root in file_path for root in ("/.claude/skills/", "/.agents/skills/", "/.gemini/config/skills/"))):
        canonical = "Skill"
        converted["skill"] = Path(converted["file_path"]).parent.name
    return canonical, converted


class Control:
    def __init__(self, options):
        self.options = options
        self.clients = {}
        self.catalog = {}
        self.mcp_names = {}
        system = options.system_prompt
        instruction = system.get("append", "") if isinstance(system, dict) else (system or "")
        self.pending_context = [str(instruction)] if instruction else []
        self.pending_tool_ids = {}
        self.last_assistant_message = ""

    async def hooks(self, event, name, args, tool_id, output=None):
        result = {}
        data = {"hook_event_name": event, "tool_name": name, "tool_input": args,
                "tool_use_id": tool_id, "cwd": str(self.options.cwd), "session_id": "",
                "tool_response": output}
        for matcher in (self.options.hooks or {}).get(event, []):
            if matcher.matcher and not re.search(matcher.matcher, name):
                continue
            for hook in matcher.hooks:
                value = await hook(data, tool_id, {"signal": None})
                specific = value.get("hookSpecificOutput", {}) if value else {}
                result.update(specific)
                if specific.get("additionalContext"):
                    self.pending_context.append(specific["additionalContext"])
                if specific.get("permissionDecision") == "deny":
                    return specific
        return result

    async def authorize(self, name, args, tool_id):
        from claude_agent_sdk.types import ToolPermissionContext
        if name in (self.options.disallowed_tools or []):
            return False, args, "Tool disabled by Raven"
        pre = await self.hooks("PreToolUse", name, args, tool_id)
        if pre.get("permissionDecision") == "deny":
            return False, args, pre.get("permissionDecisionReason", "Denied by Raven hook")
        args = pre.get("updatedInput", args)
        if self.options.can_use_tool:
            decision = await self.options.can_use_tool(name, args, ToolPermissionContext(tool_use_id=tool_id))
            if not isinstance(decision, dict):
                decision = vars(decision)
            if decision.get("behavior") != "allow":
                return False, args, decision.get("message", "Denied by Raven")
            args = decision.get("updated_input", args) or args
        elif self.options.permission_mode != "bypassPermissions" and name not in (self.options.allowed_tools or []):
            return False, args, "Tool is not approved for this Agent"
        return True, args, ""

    async def call(self, server, name, args):
        args = _mcp_arguments(args)
        if not isinstance(args, dict):
            return {"content": [{"type": "text", "text": "Tool arguments must be a JSON object"}], "isError": True}
        canonical = f"mcp__{server}__{name}"
        key = (canonical, json.dumps(args, sort_keys=True))
        pending = self.pending_tool_ids.get(key, [])
        tool_id = pending.pop(0) if pending else str(uuid.uuid4())
        allowed, args, reason = await self.authorize(canonical, args, tool_id)
        if not allowed:
            return {"content": [{"type": "text", "text": reason}], "isError": True}
        result = await self.clients[server].call_tool(name, args)
        output = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        post = await self.hooks("PostToolUse", canonical, args, tool_id, output)
        return post.get("updatedMCPToolOutput", output)

    async def native_hook(self, event, payload):
        if event == "Stop":
            for matcher in (self.options.hooks or {}).get("Stop", []):
                for callback in matcher.hooks:
                    result = await callback({"hook_event_name": "Stop", "cwd": str(self.options.cwd),
                        "session_id": payload.get("conversationId", ""),
                        "stop_hook_active": int(payload.get("executionNum", 1)) > 1,
                        "last_assistant_message": self.last_assistant_message}, None, {"signal": None})
                    if result and result.get("decision") == "block":
                        return {"decision": "continue", "reason": result.get("reason", "Complete Raven checks")}
            return {"decision": "allow"}
        if event == "PreInvocation":
            if self.options.max_turns and int(payload.get("invocationNum", 0)) >= self.options.max_turns:
                return {"terminationBehavior": "terminate", "injectSteps": [{"errorMessage": {
                    "userMessage": "Raven maximum Agent turns reached"}}]}
            contexts, self.pending_context = self.pending_context, []
            return {"injectSteps": [{"systemMessage": {"systemMessage": value}} for value in contexts]}
        call = payload.get("toolCall", {})
        raw_name, raw_args = call.get("name", ""), call.get("args", {})
        name, args = normalize_tool(raw_name, raw_args, self.mcp_names)
        tool_id = f"agy-{payload.get('conversationId', '')}-{payload.get('stepIdx', '')}"
        # MCP handlers enforce approval inside the Python process.
        if name in self.mcp_names:
            key = (name, json.dumps(args, sort_keys=True))
            pending = self.pending_tool_ids.setdefault(key, [])
            if tool_id not in pending:
                pending.append(tool_id)
            return {"decision": "auto_approve"}
        if raw_name not in TOOL_NAMES:
            return {"decision": "deny", "reason": "Tool unavailable in this Raven executor"}
        allowed, updated, reason = await self.authorize(name, args, tool_id)
        overwrite = {}
        if updated != args:
            # Raven's Read-window hook changes offset/limit. Translate them.
            if name in {"Read", "Skill"}:
                if "offset" in updated:
                    overwrite["StartLine"] = updated["offset"]
                if "limit" in updated:
                    overwrite["EndLine"] = int(updated.get("offset", 1)) + int(updated["limit"]) - 1
            elif name == "Bash" and "command" in updated:
                overwrite["CommandLine"] = updated["command"]
            else:
                return {"decision": "deny", "reason": "Unsupported permission argument transformation"}
        return {"decision": "auto_approve" if allowed else "deny", "reason": reason, "overwrite": overwrite}


@asynccontextmanager
async def control_bridge(options):
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    control = Control(options)
    token = secrets.token_urlsafe(32)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request, call_next):
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            return Response(status_code=401)
        return await call_next(request)

    @app.post("/hook")
    async def hook(request: Request):
        body = await request.json()
        return await control.native_hook(body["event"], body["payload"])

    @app.post("/mcp/{name}")
    async def mcp(name: str, request: Request):
        if name not in control.clients:
            return Response(status_code=404)
        body = await request.json()
        if "id" not in body:
            return Response(status_code=202)
        method, params = body.get("method"), body.get("params", {})
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": name, "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": control.catalog[name]}
        elif method == "tools/call":
            result = await control.call(name, params["name"], params.get("arguments", {}))
        elif method == "ping":
            result = {}
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "Method not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})

    async with AsyncExitStack() as stack:
        server_tasks = []
        try:
            for name, config in (options.mcp_servers or {}).items():
                if config.get("type") != "sdk":
                    raise ValueError("Antigravity requires Raven-owned SDK MCP servers")
                client_streams, server_streams = await stack.enter_async_context(create_client_server_memory_streams())
                instance = config["instance"]
                task = asyncio.create_task(instance.run(*server_streams, instance.create_initialization_options()))
                server_tasks.append(task)
                client = await stack.enter_async_context(ClientSession(*client_streams))
                await client.initialize()
                control.clients[name] = client
                result = await client.list_tools()
                control.catalog[name] = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in result.tools]
                for tool in result.tools:
                    canonical = f"mcp__{name}__{tool.name}"
                    for alias in (canonical, f"mcp_{name}_{tool.name}", f"{name}/{tool.name}"):
                        control.mcp_names[alias] = canonical
            if control.catalog:
                # Antigravity normally discovers MCP schemas by reading its internal
                # files. DeviceAgent correctly treats arbitrary Read/Bash as requiring
                # approval, so give it the schemas directly without weakening that gate.
                catalog = [{"ServerName": name, "ToolName": tool["name"],
                            "description": tool.get("description", ""),
                            "inputSchema": tool.get("inputSchema", {})}
                           for name, entries in control.catalog.items() for tool in entries]
                control.pending_context.append(
                    "Raven MCP tool catalog: use call_mcp_tool directly with ServerName, ToolName "
                    "and Arguments matching inputSchema below. Do not read or list internal MCP "
                    "schema files or use shell commands to discover tools; all required schemas "
                    "are provided here. Actual tool calls still require Raven permission checks.\n"
                    + json.dumps(catalog, ensure_ascii=False)
                )
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            server = _LoopbackServer(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off", timeout_graceful_shutdown=1))
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if serving.done():
                            serving.result()
                        await asyncio.sleep(.01)
                yield control, f"http://127.0.0.1:{port}", token
            finally:
                server.should_exit = True
                try:
                    await asyncio.wait_for(asyncio.shield(serving), 3)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    serving.cancel()
                    await asyncio.gather(serving, return_exceptions=True)
                sock.close()
        finally:
            # Close ClientSessions before cancelling their in-memory servers.
            await stack.aclose()
            for task in server_tasks:
                task.cancel()
            await asyncio.gather(*server_tasks, return_exceptions=True)
