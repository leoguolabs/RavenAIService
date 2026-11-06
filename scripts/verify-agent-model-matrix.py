"""Opt-in live interoperability test: real Agent entries, executors and Magpie.

Run one cell per isolated process/container, with a private runtime-settings copy.
Synthetic prompts keep the test small; device/project discovery use local fixtures.
This verifies routing/tool/trace interoperability, not domain-task answer quality.
No settings are saved and no real device, repository remote or MR is touched.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import tempfile
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


async def verify(args):
    from claude_agent_sdk import create_sdk_mcp_server, tool
    from app.config import settings
    from app.services import agent_runtime_service as runtime

    original = runtime.get_config()
    config = original.model_copy(update={"agent_overrides": {
        args.agent: runtime.AgentSelection(executor=args.executor, model=args.model)}})
    marker = "proof-" + uuid.uuid4().hex
    called = []

    @tool("discover_projects", "Read the verification project catalog", {})
    async def discover(arguments):
        called.append(True)
        return {"content": [{"type": "text", "text": marker}]}

    async def device_read(*args, **kwargs):
        called.append(True)
        return {"status": "ok", "result": {"text": marker}}

    with tempfile.TemporaryDirectory(prefix="raven-matrix-") as directory, ExitStack() as patches:
        base = Path(directory)
        for name in ("repo", "logs"):
            (base / name).mkdir()
        (base / "evidence.txt").write_text(marker)
        task = base / "task.json"
        task.write_text(json.dumps({"question": "Read evidence.txt and report its exact value.",
            "proposed_fixes": [], "repo_info": {"source": "user_selected_project_repo",
            "project_code": "matrix", "repo_url": "", "default_branch": "main"}}))
        patches.enter_context(patch.object(runtime, "get_config", return_value=config))
        patches.enter_context(patch.object(settings, "code_repo_clone_base_dir", directory))
        patches.enter_context(patch.object(settings, "anthropic_small_fast_request_timeout_seconds", args.timeout))
        patches.enter_context(patch.object(settings, "general_agent_max_turns", 12))
        for key in ("skills_data_dir", "project_skills_data_dir", "project_prompts_data_dir"):
            patches.enter_context(patch.object(settings, key, str(base / key)))
        # Keep normal agent option construction, tool restrictions, parser and trace handler.
        # Only the workload prompt and remote read-only data sources are fixtures.
        module = importlib.import_module(f"app.agents.{args.agent}.agent")
        prompts = importlib.import_module(f"app.agents.{args.agent}.prompts")
        instruction = ("This is a small model interoperability check. Read evidence.txt in the current "
            "workspace with a file tool, then copy its exact value into the answer and summary. "
            "Do not modify files, run git, clone, or contact external services. ")
        if args.agent == "general_agent":
            instruction = "Call the project discovery MCP tool once and return its exact text value. "
            from app.agents.log_analysis import mcp_tools
            patches.enter_context(patch.object(mcp_tools, "get_project_discovery_mcp_server",
                side_effect=lambda: create_sdk_mcp_server("project_repo", tools=[discover])))
        elif args.agent == "device_agent":
            tool_name = "write" if args.deny or args.approve_write else "read"
            instruction = f"Call the probe.{tool_name} device MCP tool once and return its exact text value or denial. "
            from app.services.device_link_service import device_link_manager
            from app.agents.device_agent import mcp_tools
            device = SimpleNamespace(name="matrix", capabilities={"protocol_version": 2, "mcp": {
                "servers": [{"name": "probe", "tools": [{"name": tool_name, "description": "Fixture evidence probe",
                "risk": "write" if args.deny or args.approve_write else "read",
                "inputSchema": {"type": "object", "properties": {}}}]}]}})
            patches.enter_context(patch.object(device_link_manager, "get_device", AsyncMock(return_value=device)))
            patches.enter_context(patch.object(mcp_tools, "default_dispatcher", device_read))
        schema = ('Return only a fenced JSON object with status="ok", question_type="qa", answer, summary, '
            'severity="info", root_cause_hypotheses=[], recommended_actions=[], related_keywords=[], '
            'recommended_package_ids=[], relevant_package_ids=[].')
        if args.agent == "bug_fix":
            schema = ('The sole proposal is to ensure evidence.txt exists; it is already implemented. '
                'Verify by reading it. Return fenced JSON with status="succeeded", merge_requests=[], '
                'fix_outcomes=[{"fix_index":1,"title":"Evidence file exists","outcome":"already_implemented",'
                '"reason":"the exact evidence text you read"}].')
        if args.agent in {"general_agent", "device_agent"}:
            schema = "Return the evidence text without additional tool calls."
        patches.enter_context(patch.object(prompts, "get_prompts", return_value=(instruction + schema, instruction)))

        started = time.monotonic()
        events, result, error = [], {}, ""
        try:
            async with asyncio.timeout(args.timeout):
                if args.agent == "general_agent":
                    ctx = module.GeneralAgentContext(session_id=str(uuid.uuid4()), user_message=instruction)
                    async for row in module.GeneralAgent().run_stream(ctx):
                        events.append(row)
                elif args.agent == "device_agent":
                    registry = {}
                    ctx = module.DeviceAgentContext(session_id=str(uuid.uuid4()), user_message=instruction,
                        target_device_id="matrix-fixture", clarification_enabled=False, broker_registry=registry)
                    async for row in module.DeviceAgent().run_stream(ctx):
                        events.append(row)
                        if (args.deny or args.approve_write) and row.get("type") == "tool_permission_request":
                            registry[ctx.session_id].resolve(row["request_id"],
                                {"decision": "deny" if args.deny else "allow", "message": "Matrix fixture decision"})
                else:
                    workspace = importlib.import_module(f"app.agents.{args.agent}.workspace")
                    values = dict(task_id=str(uuid.uuid4()), temp_dir=directory,
                        repo_dir=str(base / "repo"), task_json_path=str(task))
                    if args.agent == "bug_fix":
                        ctx = workspace.BugFixWorkspaceContext(**values, default_branch="main",
                            metadata={"proposed_fixes": [{"title": "Evidence file exists"}]})
                        agent = module.BugFixCodingAgent()
                    else:
                        if args.agent == "log_analysis":
                            values["logs_dir"] = str(base / "logs")
                        ctx = workspace.WorkspaceContext(**values)
                        agent = getattr(module, {"log_analysis": "LogAnalysisAgent",
                            "project_expert": "ProjectExpertAgent", "package_search": "PackageSearchAgent"}[args.agent])()
                    result = await agent.run(ctx)
                    events = result.get("trace_events", [])
        except Exception as exc:
            error = str(exc) or type(exc).__name__

        routes = [row for row in events if row.get("kind") == "raven_route"]
        serialized = json.dumps({"result": result, "events": events, "error": error}, ensure_ascii=False)
        tools = [row.get("tool_name") for row in events if row.get("type") == "step_start"]
        failed_events = [row for row in events if row.get("type") == "error"]
        if args.agent in {"general_agent", "device_agent"}:
            answer = "\n".join(row.get("final_text", "") for row in events if row.get("type") == "run_complete")
        else:
            answer = json.dumps({key: result.get(key) for key in ("answer", "summary", "fix_outcomes")}, ensure_ascii=False)
        if args.deny:
            passed = bool(routes and not called and not error and not failed_events
                and "callback must return PermissionResult" not in serialized
                and any(row.get("type") == "tool_permission_resolved" and row.get("decision") == "deny" for row in events))
        elif args.disabled:
            passed = "Selected provider is disabled" in serialized and not routes and not called
        else:
            passed = bool(marker in answer and routes and tools and not error and not failed_events)
            passed = passed and all(row.get("executor") == args.executor
                and row.get("model") == (args.actual_model or args.model)
                and row.get("requested_model") == args.model for row in routes)
            if args.agent in {"general_agent", "device_agent"}:
                passed = passed and bool(called)
            else:
                passed = passed and result.get("status") in {"ok", "succeeded"}
            if args.approve_write:
                passed = passed and len(called) == 1 and any(row.get("type") == "tool_permission_resolved"
                    and row.get("decision") == "allow" for row in events)
        record = {"agent": args.agent, "executor": args.executor, "model": args.model,
            "expected_disabled": args.disabled, "expected_denial": args.deny,
            "expected_approval": args.approve_write,
            "passed": passed, "elapsed": round(time.monotonic() - started, 2),
            "evidence_verified": marker in answer, "expected_evidence": marker,
            "tools": tools, "routes": routes,
            "status": result.get("status"), "error": error, "error_events": failed_events}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"check": record, "events": events, "result": result}, ensure_ascii=False, indent=2))
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, choices=["general_agent", "device_agent", "log_analysis",
        "project_expert", "package_search", "bug_fix"])
    parser.add_argument("--executor", required=True, choices=["claude_sdk", "antigravity_cli"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--actual-model", help="Expected served model when --model is a routing group")
    parser.add_argument("--disabled", action="store_true")
    parser.add_argument("--deny", action="store_true", help="Deny the device write fixture and assert no dispatch")
    parser.add_argument("--approve-write", action="store_true", help="Approve one synthetic device write and verify its result")
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument("--output", type=Path, required=True)
    logging.basicConfig(level=logging.ERROR)
    raise SystemExit(0 if asyncio.run(verify(parser.parse_args())) else 1)
