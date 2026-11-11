"""Lightweight title / summary helper.

With Magpie enabled, title generation uses a single chat completion with its
own Yinhe and DeepSeek routes. Other deployments use the SDK small/fast model.

Two public entry points:

- :func:`summarize_user_message` — single-input summary (used by
  ``/chat/summarize`` for instant session-title generation).
- :func:`generate_session_title` — user/assistant pair summary
  (used by ``AIChatService`` after a full exchange completes).

Both are best-effort: on any failure (config missing, timeout, SDK error)
they fall back to a sensible truncation of the input.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import uuid
from typing import Any, Optional

import httpx

from app.services.magpie_routing_policy import TITLE_DEEPSEEK_GROUP, TITLE_YINHE_GROUP

logger = logging.getLogger(__name__)


_TITLE_YINHE_SECONDS = 2.0
_SUMMARY_PROMPT = (
    "你是会话标题生成助手。请根据下面用户输入的内容，生成一个简短的中文摘要标题，"
    "长度不超过 {max_length} 个字，必须概括用户的核心诉求或问题。\n"
    "要求：\n"
    "- 只输出标题文本，不要解释、不要引号、不要标点符号结尾、不要 emoji、不要换行。\n"
    "- 不要复述完整问题，使用名词短语或动宾短语。\n\n"
    "用户内容：\n{user_content}"
)


def _resolve_small_fast_model() -> Optional[str]:
    from app.agents.anthropic_client import PROVIDER_PROFILES
    from app.config import settings

    if settings.anthropic_provider == "openai":
        profile = PROVIDER_PROFILES["openai"]
        return settings.anthropic_model or profile.default_model
    if settings.anthropic_small_fast_model:
        return settings.anthropic_small_fast_model
    profile = PROVIDER_PROFILES.get(settings.anthropic_provider)
    if profile and profile.default_small_fast_model:
        return profile.default_small_fast_model
    return None


def _extract_text_from_messages(messages: list[Any]) -> str:
    """Pick the final answer from a list of SDK messages.

    Compatible gateways may expose model reasoning as assistant text before
    the SDK emits the authoritative ``ResultMessage.result``.  Persisting the
    concatenated assistant blocks therefore leaks reasoning into session
    titles (for example, ``"The user wants m"`` after truncation).  Prefer the
    successful terminal result and only fall back to the last assistant frame
    for SDK/provider variants that do not emit one.
    """
    for message in reversed(messages):
        result = getattr(message, "result", None)
        if (
            isinstance(result, str)
            and result.strip()
            and getattr(message, "is_error", False) is not True
        ):
            return result
        if isinstance(message, dict):
            result = message.get("result")
            if (
                isinstance(result, str)
                and result.strip()
                and message.get("is_error") is not True
            ):
                return result

    for message in reversed(messages):
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if not isinstance(content, list):
            continue
        out_parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text.strip():
                out_parts.append(text)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                out_parts.append(str(block["text"]))
        if out_parts:
            return "".join(out_parts)
    return ""


async def _record_title_usage(
    *,
    model: Optional[str],
    provider: Optional[str] = None,
    token_usage: dict,
    duration_seconds: float,
    status: str,
    error_kind: Optional[str],
    user_id: Optional[str],
    session_id: Optional[str],
) -> None:
    """Best-effort ``title_generator`` AI-usage metric. Never raises.

    Each call is a distinct invocation (no run_id), so the idempotency key is a
    fresh UUID; retries by the caller therefore count as separate invocations,
    which is the desired billing semantics for this cheap helper.
    """
    try:
        from app.config import settings
        from app.services import metrics_service

        await metrics_service.record_ai_usage(
            source="title_generator",
            agent_kind="title_generator",
            provider=provider or str(settings.anthropic_provider),
            model=model,
            status=status,
            error_kind=error_kind,
            usage=token_usage,
            user_id=user_id,
            session_id=session_id,
            duration_ms=max(0, int(duration_seconds * 1000)),
            idempotency_key=f"ai_usage:title_generator:{uuid.uuid4()}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("title_generator: metrics record skipped: %s", exc)


async def _run_query(
    prompt: str,
    *,
    system_prompt: str = "",
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Generate one title and record the outcome for the actual model.

    Callers supply the user-facing fallback when inference fails.
    """
    from app.agents.usage import accumulate_usage, new_token_usage
    from app.config import settings
    from app.services.agent_runtime_service import get_config

    runtime = get_config()
    model = None if runtime.enabled else _resolve_small_fast_model()
    max_tokens = int(getattr(settings, "anthropic_small_fast_max_tokens", 1024))
    timeout_s = int(
        getattr(settings, "anthropic_small_fast_request_timeout_seconds", 30)
    )

    token_usage = new_token_usage()
    start_ts = time.monotonic()
    # Which endpoint actually served the run, for the usage metric below. The
    # module-global provider would name the primary even when the backup answered.
    served: dict[str, Any] = {"provider": str(settings.anthropic_provider), "model": model}

    async def _magpie_title() -> str:
        from app.agents.magpie_task_routing import model_ref

        messages = [{"role": "user", "content": prompt}]
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})
        headers = {
            "Authorization": f"Bearer {runtime.api_key}",
            "User-Agent": "RavenAI/title_generator",
            "X-Magpie-Agent": "raven-title_generator",
            "X-Magpie-Session": str(uuid.uuid4()),
        }
        async with httpx.AsyncClient(timeout=timeout_s, trust_env=False, headers=headers) as client:
            async def complete(group: str) -> str:
                route = f"group/{group}"
                served.update(provider="magpie", model=route)
                response = await client.post(
                    f"{runtime.base_url}/v1/chat/completions",
                    json={"model": route, "messages": messages, "max_tokens": max_tokens, "stream": False},
                )
                response.raise_for_status()
                completion = response.json()
                try:
                    title = completion["choices"][0]["message"]["content"]
                except (IndexError, KeyError, TypeError):
                    title = None
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("Magpie returned no title text")
                actual = model_ref(response)
                if actual:
                    served.update(provider=actual.split("/", 1)[0], model=actual)
                accumulate_usage(completion.get("usage"), token_usage)
                return title

            try:
                return await asyncio.wait_for(complete(TITLE_YINHE_GROUP), timeout=_TITLE_YINHE_SECONDS)
            except (TimeoutError, httpx.HTTPError, ValueError):
                return await complete(TITLE_DEEPSEEK_GROUP)

    async def _legacy_title() -> str:
        from app.agents.anthropic_client import build_options
        from app.agents.routed_query import routed_query

        with tempfile.TemporaryDirectory(prefix="title-gen-") as tmpdir:
            def _make_options(endpoint: Any) -> Any:
                return build_options(
                    system_prompt=system_prompt,
                    allowed_tools=[],
                    cwd=tmpdir,
                    max_turns=1,
                    permission_mode="bypassPermissions",
                    model=endpoint.small_fast_model if endpoint is not None else model,
                    max_tokens=max_tokens,
                    request_timeout_seconds=timeout_s,
                    endpoint=endpoint,
                )

            def _on_endpoint(endpoint: Any) -> None:
                served["provider"] = endpoint.provider
                served["model"] = endpoint.small_fast_model or endpoint.model

            collected: list[Any] = []
            async for message in routed_query(
                prompt=prompt,
                make_options=_make_options,
                agent_kind="title_generator",
                require_small_fast=True,
                on_endpoint=_on_endpoint,
            ):
                collected.append(message)
                accumulate_usage(getattr(message, "usage", None), token_usage)
            return _extract_text_from_messages(collected)

    try:
        query = _magpie_title if runtime.enabled else _legacy_title
        title = await asyncio.wait_for(query(), timeout=max(timeout_s + 5, 10))
    except Exception as exc:  # noqa: BLE001
        error_kind = "timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
        await _record_title_usage(
            model=served["model"],
            provider=served["provider"],
            token_usage=token_usage,
            duration_seconds=time.monotonic() - start_ts,
            status="failed",
            error_kind=error_kind,
            user_id=user_id,
            session_id=session_id,
        )
        raise
    await _record_title_usage(
        model=served["model"],
        provider=served["provider"],
        token_usage=token_usage,
        duration_seconds=time.monotonic() - start_ts,
        status="succeeded",
        error_kind=None,
        user_id=user_id,
        session_id=session_id,
    )
    return title


def _normalize_title(raw: str, max_length: int) -> str:
    normalized = " ".join((raw or "").strip().split())
    normalized = normalized.split("\n", 1)[0].strip().strip("“”\"'`：:。.")
    if not normalized:
        return ""
    return normalized[:max_length]


_REASONING_PREFIXES = (
    "the user wants",
    "the user is asking",
    "we need",
    "we should",
    "i need",
    "i should",
    "let me",
    "用户想让我",
    "用户希望我",
    "我们需要",
    "我需要",
    "让我",
)


def _looks_like_reasoning_leak(title: str, source_text: str) -> bool:
    """Reject model preambles that cannot be valid titles.

    Some OpenAI-compatible gateways put the chain-of-thought preamble in both
    the assistant frame and the SDK terminal result.  The result can therefore
    still start with text such as ``"The user wants me ..."`` even when the
    prompt explicitly asks for a Chinese title.  Besides known preambles, a
    title with no Han characters is rejected when the source contains Han text;
    falling back to the user's own words is safer than persisting reasoning.
    """
    folded = (title or "").strip().casefold()
    if any(folded.startswith(prefix) for prefix in _REASONING_PREFIXES):
        return True

    source_has_han = any("\u4e00" <= char <= "\u9fff" for char in source_text)
    title_has_han = any("\u4e00" <= char <= "\u9fff" for char in title)
    return source_has_han and not title_has_han


async def summarize_user_message(
    user_content: str,
    max_length: int = 16,
    *,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Generate a short Chinese title for the given user message.

    Falls back to a truncated form of the message when the LLM call fails.
    """
    cleaned_input = " ".join((user_content or "").strip().split())
    fallback = cleaned_input[:max_length] if cleaned_input else "新对话"

    if not cleaned_input:
        return fallback

    try:
        prompt = _SUMMARY_PROMPT.format(
            user_content=cleaned_input[:1200],
            max_length=max_length,
        )
        raw = await _run_query(prompt, user_id=user_id, session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("title_generator: 生成摘要失败，使用回退: %s", exc)
        return fallback

    title = _normalize_title(raw, max_length)
    if _looks_like_reasoning_leak(title, cleaned_input):
        logger.warning("title_generator: 检测到推理文本，使用用户输入回退")
        return fallback
    return title or fallback


async def generate_session_title(
    user_content: str,
    ai_content: str,
    max_length: int = 24,
    *,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    locale: Optional[str] = None,
) -> Optional[str]:
    """Generate a session title from a completed user/assistant exchange.

    ``locale`` selects the per-language title prompt so the generated title is
    produced in the active language. Returns ``None`` on failure (callers
    typically retain the existing default title in that case).
    """
    from app.services.prompts_config_service import get_chat_title_prompt_template

    user_clean = " ".join((user_content or "").strip().split())
    ai_clean = " ".join((ai_content or "").strip().split())
    if not user_clean and not ai_clean:
        return None

    try:
        template = get_chat_title_prompt_template(locale)
        prompt = template.format(
            user_content=user_clean[:1200],
            ai_content=ai_clean[:1200],
            max_length=max_length,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("title_generator: 标题提示词渲染失败: %s", exc)
        return None

    try:
        raw = await _run_query(prompt, user_id=user_id, session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("title_generator: 生成会话标题失败: %s", exc)
        return None

    title = _normalize_title(raw, max_length)
    if _looks_like_reasoning_leak(title, user_clean):
        logger.warning("title_generator: 检测到推理文本，使用用户输入回退")
        return user_clean[:max_length] or None
    return title or None
