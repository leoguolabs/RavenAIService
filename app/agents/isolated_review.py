"""Optional independent evidence verification with atomic public narrative edits."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from pathlib import Path

from app.agents.verification_packet import build, PUBLIC_FIELDS
from app.agents.verification_edits import apply
from app.agents.verification_prompts import ROLE_EN, ROLE_ZH


class VerificationError(ValueError):
    """Do not mark an unverified or partially edited draft as verified."""


def isolated_mode(settings, snapshot):
    return bool(
        snapshot is not None
        and settings.agent_evidence_final_review_enabled is True
        and getattr(settings, "agent_evidence_final_review_mode", "session")
        == "isolated"
    )


def review_options(primary, workspace, required_prompt, locale):
    if primary is None or not isinstance(primary.model, str) or not primary.model:
        raise VerificationError("Effective primary model unavailable")
    options = copy.copy(primary)
    options.system_prompt = (
        required_prompt + "\n\n" + (ROLE_EN if locale == "en" else ROLE_ZH)
    )
    options.env = dict(primary.env or {})
    for key in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        options.env[key] = primary.model
    options.cwd = str(workspace)
    options.allowed_tools = []
    options.tools = []
    options.mcp_servers = {}
    options.hooks = {}
    options.setting_sources = []
    for name, value in [
        ("resume", None),
        ("continue_conversation", False),
        ("fork_session", False),
        ("session_id", None),
        ("agents", None),
        ("add_dirs", None),
        ("can_use_tool", None),
        ("include_partial_messages", False),
    ]:
        if hasattr(options, name):
            setattr(options, name, value)
    return options


def parse_edits(text):
    if not isinstance(text, str) or len(text.encode()) > 64 * 1024:
        raise VerificationError("Invalid verification response")
    block = re.fullmatch(r"\s*```(?:json)?\s*([\s\S]*?)```\s*", text)
    try:

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate verification key")
                result[key] = value
            return result

        return json.loads(block[1] if block else text, object_pairs_hook=unique)
    except ValueError as exc:
        raise VerificationError("Malformed verification JSON") from exc


async def verify(
    *,
    workspace,
    draft,
    primary_options,
    required_prompt,
    locale,
    check_cancel,
    query=None,
):
    if query is None:
        from app.agents.routed_query import routed_query

        query = routed_query
    started = time.monotonic()
    frozen = copy.deepcopy({key: draft[key] for key in PUBLIC_FIELDS if key in draft})
    check_cancel()
    packet = await asyncio.to_thread(build, workspace, frozen)
    check_cancel()
    folder = Path(workspace) / ".verification"
    if folder.is_symlink():
        raise VerificationError("Unsafe verification workspace")
    folder.mkdir(exist_ok=True, mode=0o700)
    folder.resolve().relative_to(Path(workspace).resolve())
    options = review_options(primary_options, folder, required_prompt, locale)
    prompt = (
        "Return minimal edits to draft only. Do not return draft or read-only evidence.\n"
        + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    )
    model_started = time.monotonic()
    terminals = []

    async def consume():
        async for message in query(
            prompt=prompt,
            make_options=lambda _: options,
            agent_kind="log_analysis_verifier",
            candidates=[],
        ):
            check_cancel()
            for block in getattr(message, "content", []) or []:
                if (
                    getattr(block, "type", None) == "tool_use"
                    or type(block).__name__ == "ToolUseBlock"
                ):
                    raise VerificationError("Verifier tools are unavailable")
            if hasattr(message, "is_error") and hasattr(message, "result"):
                terminals.append(message)

    task = asyncio.create_task(consume())
    try:
        while not task.done():
            check_cancel()
            await asyncio.wait({task}, timeout=0.1)
        await task
        check_cancel()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if len(terminals) != 1 or terminals[0].is_error:
        raise VerificationError("Verification did not complete successfully")
    if frozen != {key: draft[key] for key in PUBLIC_FIELDS if key in draft}:
        raise VerificationError("Draft changed during verification")
    patch = parse_edits(terminals[0].result)
    try:
        result = apply(frozen, patch)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise VerificationError("Invalid literal verification edits") from exc
    return {**draft, **result}, {
        "model": options.model,
        "edits": len(patch["edits"]),
        "seconds": time.monotonic() - started,
        "model_seconds": time.monotonic() - model_started,
        "packet_bytes": len(
            json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode()
        ),
        "usage": getattr(terminals[0], "usage", None),
    }
