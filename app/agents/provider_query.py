"""Provider-aware SDK entry point; existing tools and trace remain SDK-owned."""

from __future__ import annotations

import base64
import json
from contextlib import aclosing
from dataclasses import replace
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


async def query(*, prompt: Any, options: Any) -> AsyncIterator[Any]:
    from claude_agent_sdk import query as sdk_query

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
