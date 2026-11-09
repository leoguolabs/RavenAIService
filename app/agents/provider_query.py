"""Provider-aware SDK entry point; existing tools and trace remain SDK-owned."""

from __future__ import annotations

import base64
import asyncio
import json
import os
import uuid
from contextlib import aclosing
from dataclasses import is_dataclass, replace
from pathlib import Path
from typing import Any, AsyncIterator

from app.agents.openai_bridge import OpenAIConfig, openai_bridge


def workspace_image_blocks(cwd: str) -> list[dict]:
    """Read only backend-materialized image files, never paths from model text."""
    from app.models.chat import ImageAttachment, validate_image, validate_image_count, validate_images

    directory = Path(cwd) / "images"
    manifest = directory / "manifest.json"
    if directory.is_symlink() or manifest.is_symlink():
        raise ValueError("Unsafe image manifest path / 图片清单路径不安全")
    if not manifest.is_file():
        return []
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Invalid image manifest / 图片清单无效")
    validate_image_count(len(data))
    attachments = []
    for index, row in enumerate(data, start=1):
        name = row["file"]
        path = directory / name
        if Path(name).name != name or path.is_symlink() or path.resolve().parent != directory.resolve():
            raise ValueError("Unsafe image manifest path / 图片清单路径不安全")
        if not path.is_file():
            raise ValueError("Original image unavailable / 截图原件不可用")
        validate_image(row["media_type"], path.stat().st_size, index=index)
        attachments.append(ImageAttachment(media_type=row["media_type"],
            data=base64.b64encode(path.read_bytes()).decode("ascii")))
    validate_images(attachments)
    return [{"type": "image", "source": {"type": "base64", "media_type": image.media_type,
             "data": image.data}} for image in attachments]


async def _claude_query(*, prompt: Any, options: Any) -> AsyncIterator[Any]:
    from claude_agent_sdk import query as sdk_query

    callback = getattr(options, "can_use_tool", None)
    if is_dataclass(options) and callback is not None:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        async def sdk_permission(name, arguments, context):
            decision = await callback(name, arguments, context)
            if not isinstance(decision, dict):
                return decision
            if decision.get("behavior") == "allow":
                return PermissionResultAllow(updated_input=decision.get("updated_input", decision.get("updatedInput")))
            return PermissionResultDeny(message=decision.get("message") or "Denied by Raven")

        # Raven's broker returns dictionaries shared with the CLI adapter; the
        # SDK control protocol requires typed decisions, including on approval.
        options = replace(options, can_use_tool=sdk_permission)

    env = dict(getattr(options, "env", {}) or {})
    if env.get("RAVEN_API_PROTOCOL") != "openai_responses":
        async with aclosing(sdk_query(prompt=prompt, options=options)) as stream:
            async for message in stream:
                yield message
        return

    config = OpenAIConfig(base_url=env["ANTHROPIC_BASE_URL"], api_key=env["ANTHROPIC_API_KEY"],
        model=str(options.model), max_output_tokens=int(env.get("RAVEN_OPENAI_MAX_OUTPUT_TOKENS", 8192)),
        timeout_seconds=float(env.get("API_TIMEOUT_MS", "3600000")) / 1000)
    images = workspace_image_blocks(str(options.cwd))
    if images:
        if not isinstance(prompt, str):
            raise ValueError("Image input requires a replayable text prompt")
        text = prompt

        async def multimodal_prompt():
            yield {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": text}, *images]}}

        prompt = multimodal_prompt()

    async with openai_bridge(config) as (base_url, token):
        # The real OpenAI key stays in this process, outside the SDK subprocess.
        env = {key: value for key, value in env.items() if not key.startswith("RAVEN_")}
        env.update({"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_API_KEY": token,
                    "ANTHROPIC_AUTH_TOKEN": "", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        adapted = replace(options, env=env)
        async with aclosing(sdk_query(prompt=prompt, options=adapted)) as stream:
            async for message in stream:
                yield message


async def query(*, prompt: Any, options: Any) -> AsyncIterator[Any]:
    """Run the selected backend; gateway notices share the same event ordering."""
    env = dict(getattr(options, "env", {}) or {})
    if env.get("RAVEN_GATEWAY") != "magpie":
        async with aclosing(_claude_query(prompt=prompt, options=options)) as stream:
            async for message in stream:
                yield message
        return

    from claude_agent_sdk import ResultMessage, SystemMessage
    from app.agents.magpie_bridge import GatewayConfig, gateway_bridge
    from app.services.agent_runtime_service import get_config

    executor = env.get("RAVEN_EXECUTOR", "claude_sdk")
    agent_kind = env.get("RAVEN_AGENT_KIND", "")
    events: asyncio.Queue = asyncio.Queue(maxsize=128)
    # A route is reported only when it changes; headers precede response bytes.
    async def on_route(data: dict) -> None:
        await events.put(("message", SystemMessage(subtype="raven_route", data=data)))

    last_gateway_error = None

    def on_error(error):
        nonlocal last_gateway_error
        last_gateway_error = error

    config = GatewayConfig(base_url=env["ANTHROPIC_BASE_URL"], api_key=env["ANTHROPIC_API_KEY"],
        model=options.model, executor=executor, session_id=str(uuid.uuid4()), agent_kind=agent_kind,
        timeout_seconds=float(env.get("API_TIMEOUT_MS", "3600000")) / 1000,
        task_routing=get_config().task_routing)
    yield SystemMessage(subtype="raven_executor", data={"executor": executor, "agent_key": agent_kind,
        "requested_model": options.model})
    async with gateway_bridge(config, on_route, on_error) as (base_url, token):
        env = {key: value for key, value in env.items() if not key.startswith("RAVEN_")}
        # Claude Code merges options.env into the application environment.
        # Explicitly clear inherited provider credentials in that subprocess.
        env.update({key: "" for key in os.environ if key.endswith(("API_KEY", "API_KEYS", "AUTH_TOKEN"))})
        env.update({"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_API_KEY": token,
                    "ANTHROPIC_AUTH_TOKEN": "", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        adapted = replace(options, env=env)
        if executor == "antigravity_cli":
            from app.agents.antigravity_executor import query as run
        elif executor == "claude_sdk":
            run = _claude_query
        else:
            raise ValueError("Unsupported Agent executor")

        if executor == "claude_sdk":
            images = workspace_image_blocks(str(options.cwd))
            if images and isinstance(prompt, str):
                text = prompt
                async def multimodal_prompt():
                    yield {"type": "user", "message": {"role": "user", "content": [
                        {"type": "text", "text": text}, *images]}}
                prompt = multimodal_prompt()

        async def produce():
            from app.agents.routed_query import _upstream_error_candidate

            try:
                async with aclosing(run(prompt=prompt, options=adapted)) as stream:
                    async for message in stream:
                        # Claude may emit an assistant-shaped API rejection before its
                        # terminal result; routed_query can stop at that first frame.
                        if last_gateway_error and (
                            (isinstance(message, ResultMessage) and message.is_error)
                            or _upstream_error_candidate(message) is not None
                        ):
                            raise last_gateway_error
                        await events.put(("message", message))
            except Exception as exc:
                await events.put(("error", last_gateway_error or exc))
            finally:
                # Do not block cancellation if the consumer closed a full queue.
                if not asyncio.current_task().cancelling():
                    await events.put(("end", None))

        producer = asyncio.create_task(produce())
        try:
            while True:
                kind, value = await events.get()
                if kind == "end":
                    break
                if kind == "error":
                    raise value
                from app.agents.runtime_secrets import redact
                yield redact(value, (token, config.api_key))
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
