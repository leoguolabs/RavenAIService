"""Publication tool for packages constructed by the Agent in its workspace."""

import json
from typing import Any, Awaitable, Callable

SDK_TOOL_NAME = "mcp__agent_package__PublishPackage"


def get_mcp_server(publish: Callable[..., Awaitable[dict[str, Any]]]):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "PublishPackage",
        "Validate your built TGZ and JSON review plan, show a concise package summary to the "
        "user for confirmation, then publish once. Read full-package-build SKILL.md "
        "for the plan schema. Both paths must be inside the current workspace. "
        "Business types/IDs/versions come from your investigation, not a catalog. "
        "A validation error is recoverable: fix the files and call again. Never "
        "claim publication without a successful tool result.",
        {"plan_path": str, "artifact_path": str},
    )
    async def publish_package(args):
        try:
            result = await publish(args["plan_path"], args["artifact_path"])
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}

    return create_sdk_mcp_server(name="agent_package", version="1.0.0", tools=[publish_package])
