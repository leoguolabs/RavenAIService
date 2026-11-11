"""Unit tests for :mod:`app.services.title_generator_service`.

These cover title normalization, user-facing fallback and isolated Magpie routing.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.services import agent_runtime_service as runtime
from app.services import title_generator_service as tg


@pytest.fixture
def title_gateway(monkeypatch):
    config = runtime.RuntimeConfig(enabled=True, api_key="caller-key",
        model="raven-openai/gpt-6.1-sol", executor="openai_agents")
    monkeypatch.setattr(runtime, "get_config", lambda: config)
    records = []

    async def record(**values):
        records.append(values)

    monkeypatch.setattr(tg, "_record_title_usage", record)
    client = httpx.AsyncClient

    def connect(handler):
        monkeypatch.setattr(tg.httpx, "AsyncClient",
            lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))

    return connect, records


@pytest.mark.parametrize("yinhe", ["ok", "slow", "http_error", "empty", "transport_error"])
async def test_magpie_title_routes_only_to_yinhe_and_deepseek(monkeypatch, title_gateway, yinhe):
    from app.services import magpie_routing_policy, openai_task_admission

    connect, records = title_gateway
    calls = []

    async def unexpected(*args):
        pytest.fail("Titles must not use shared cooldown or OpenAI admission")

    monkeypatch.setattr(magpie_routing_policy, "cooling", unexpected)
    monkeypatch.setattr(magpie_routing_policy, "trip", unexpected)
    monkeypatch.setattr(openai_task_admission.TaskAdmission, "prepare", unexpected)
    if yinhe == "slow":
        monkeypatch.setattr(tg, "_TITLE_YINHE_SECONDS", 0.02)

    async def reply(request):
        payload = json.loads(request.content)
        calls.append(payload["model"])
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer caller-key"
        assert openai_task_admission.ADMISSION_HEADER not in request.headers
        assert request.headers["X-Magpie-Agent"] == "raven-title_generator"
        assert payload["stream"] is False and "tools" not in payload
        assert "网络一直断连" in payload["messages"][-1]["content"]
        provider, model = "raven-deepseek", "deepseek-v4-flash"
        if payload["model"] == "group/raven-title-yinhe":
            if yinhe == "slow":
                await asyncio.sleep(60)
            if yinhe == "http_error":
                return httpx.Response(503)
            if yinhe == "empty":
                return httpx.Response(200, json={"choices": []})
            if yinhe == "transport_error":
                raise httpx.ConnectError("unavailable")
            provider, model = "raven-yinhe", "yinhe-thinking"
        return httpx.Response(200, headers={"X-Magpie-Provider": provider, "X-Magpie-Model": model},
            json={"choices": [{"message": {"content": "网络故障排查"}}],
                  "usage": {"prompt_tokens": 12, "completion_tokens": 4}})

    connect(reply)
    assert await tg.summarize_user_message("网络一直断连，请帮我排查") == "网络故障排查"
    assert calls == (["group/raven-title-yinhe"] if yinhe == "ok"
                     else ["group/raven-title-yinhe", "group/raven-title-deepseek"])
    assert len(records) == 1
    assert records[0]["model"] == ("raven-yinhe/yinhe-thinking" if yinhe == "ok"
                                   else "raven-deepseek/deepseek-v4-flash")
    assert records[0]["token_usage"]["input_tokens"] == 12
    assert records[0]["status"] == "succeeded"


async def test_magpie_title_preserves_text_fallback_when_both_routes_fail(title_gateway):
    connect, records = title_gateway
    calls = []

    async def unavailable(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(404)

    connect(unavailable)
    assert await tg.summarize_user_message("请检查侧边栏标题异常", max_length=6) == "请检查侧边栏"
    assert calls == ["group/raven-title-yinhe", "group/raven-title-deepseek"]
    assert records[0]["status"] == "failed"


async def test_cancelling_magpie_title_does_not_start_backup(title_gateway):
    connect, records = title_gateway
    started = asyncio.Event()
    calls = []

    async def waiting(request):
        calls.append(json.loads(request.content)["model"])
        started.set()
        await asyncio.sleep(60)

    connect(waiting)
    title = asyncio.create_task(tg.summarize_user_message("网络故障"))
    await asyncio.wait_for(started.wait(), timeout=1)
    title.cancel()
    with pytest.raises(asyncio.CancelledError):
        await title
    assert calls == ["group/raven-title-yinhe"]
    assert not records


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _AssistantMessage:
    def __init__(self, *parts: str) -> None:
        self.content = [_TextBlock(part) for part in parts]


class _ResultMessage:
    def __init__(self, result: str, *, is_error: bool = False) -> None:
        self.result = result
        self.is_error = is_error


def test_extract_text_prefers_terminal_result_over_reasoning_text():
    messages = [
        _AssistantMessage("The user wants me to summarize the request."),
        _ResultMessage("修复生产环境会话标题"),
    ]

    assert tg._extract_text_from_messages(messages) == "修复生产环境会话标题"


def test_extract_text_falls_back_to_last_assistant_message():
    messages = [
        _AssistantMessage("intermediate reasoning"),
        _AssistantMessage("最终标题"),
    ]

    assert tg._extract_text_from_messages(messages) == "最终标题"


@pytest.mark.asyncio
async def test_summarize_user_message_empty_input_returns_default():
    """Empty input must yield ``"新对话"`` without invoking the LLM."""
    out = await tg.summarize_user_message("", max_length=16)
    assert out == "新对话"


@pytest.mark.asyncio
async def test_summarize_user_message_whitespace_only_returns_default():
    out = await tg.summarize_user_message("   \n  \t  ", max_length=16)
    assert out == "新对话"


@pytest.mark.asyncio
async def test_summarize_user_message_falls_back_when_llm_fails(monkeypatch):
    """When ``_run_query`` raises, we must return a truncated form of the input
    rather than propagating the exception."""

    async def _boom(prompt, **kwargs):  # noqa: ARG001
        raise RuntimeError("simulated LLM failure")

    monkeypatch.setattr(tg, "_run_query", _boom)

    out = await tg.summarize_user_message("这是一条很长的用户输入用于测试摘要回退路径", max_length=8)
    # Falls back to truncated input (first 8 chars).
    assert out == "这是一条很长的用"


@pytest.mark.asyncio
async def test_summarize_user_message_uses_llm_output_when_available(monkeypatch):
    async def _ok(prompt, **kwargs):  # noqa: ARG001
        return "  网络故障排查  "

    monkeypatch.setattr(tg, "_run_query", _ok)

    out = await tg.summarize_user_message("网络一直断连，请帮我排查原因", max_length=16)
    assert out == "网络故障排查"


@pytest.mark.asyncio
async def test_summarize_user_message_rejects_reasoning_preamble(monkeypatch):
    async def _reasoning(prompt, **kwargs):  # noqa: ARG001
        return "The user wants me to create a concise title."

    monkeypatch.setattr(tg, "_run_query", _reasoning)

    out = await tg.summarize_user_message("请检查生产环境侧边栏标题异常", max_length=16)
    assert out == "请检查生产环境侧边栏标题异常"


@pytest.mark.asyncio
async def test_generate_session_title_returns_none_for_empty_pair():
    """Both user and assistant content empty → return None (caller keeps default)."""
    out = await tg.generate_session_title("", "", max_length=16)
    assert out is None


@pytest.mark.asyncio
async def test_generate_session_title_returns_none_when_llm_fails(monkeypatch):
    async def _boom(prompt, **kwargs):  # noqa: ARG001
        raise RuntimeError("simulated LLM failure")

    monkeypatch.setattr(tg, "_run_query", _boom)

    out = await tg.generate_session_title("用户提问", "助手回答", max_length=16)
    assert out is None


@pytest.mark.asyncio
async def test_generate_session_title_returns_none_when_prompt_template_missing(monkeypatch):
    """If the YAML title prompt template can't be rendered we must not crash."""

    def _boom():
        raise RuntimeError("prompt_config corrupt")

    monkeypatch.setattr(
        "app.services.prompts_config_service.get_chat_title_prompt_template", _boom
    )

    out = await tg.generate_session_title("用户提问", "助手回答", max_length=16)
    assert out is None


@pytest.mark.asyncio
async def test_generate_session_title_uses_llm_output_when_available(monkeypatch):
    async def _ok(prompt, **kwargs):  # noqa: ARG001
        return "排查网络问题"

    monkeypatch.setattr(tg, "_run_query", _ok)
    # Always return a usable template so we don't depend on prompts_config.yaml
    # contents in the test runner.
    monkeypatch.setattr(
        "app.services.prompts_config_service.get_chat_title_prompt_template",
        lambda locale=None: "U:{user_content}\nA:{ai_content}\nN:{max_length}",
    )

    out = await tg.generate_session_title("用户问网络", "助手回答", max_length=16)
    assert out == "排查网络问题"


@pytest.mark.asyncio
async def test_generate_session_title_rejects_reasoning_preamble(monkeypatch):
    async def _reasoning(prompt, **kwargs):  # noqa: ARG001
        return "The user wants me to summarize the exchange."

    monkeypatch.setattr(tg, "_run_query", _reasoning)
    monkeypatch.setattr(
        "app.services.prompts_config_service.get_chat_title_prompt_template",
        lambda locale=None: "U:{user_content}\nA:{ai_content}\nN:{max_length}",
    )

    out = await tg.generate_session_title("检查侧边栏标题", "已经检查", max_length=16)
    assert out == "检查侧边栏标题"


@pytest.mark.asyncio
async def test_generate_session_title_forwards_usage_context(monkeypatch):
    seen = {}

    async def _ok(prompt, **kwargs):  # noqa: ARG001
        seen.update(kwargs)
        return "排查网络问题"

    monkeypatch.setattr(tg, "_run_query", _ok)
    monkeypatch.setattr(
        "app.services.prompts_config_service.get_chat_title_prompt_template",
        lambda locale=None: "U:{user_content}\nA:{ai_content}\nN:{max_length}",
    )

    out = await tg.generate_session_title(
        "用户问网络",
        "助手回答",
        max_length=16,
        user_id="user-1",
        session_id="session-1",
    )

    assert out == "排查网络问题"
    assert seen["user_id"] == "user-1"
    assert seen["session_id"] == "session-1"


def test_normalize_title_strips_quotes_and_punctuation():
    """The helper that post-processes LLM output must strip surrounding noise."""
    assert tg._normalize_title('  "排查网络问题。" ', 16) == "排查网络问题"
    # Multi-line input gets flattened (whitespace joined with single space)
    # before the max_length cap is applied.
    assert tg._normalize_title("第一行\n第二行", 16) == "第一行 第二行"
    assert tg._normalize_title("非常非常非常非常长的标题文本", 4) == "非常非常"
    assert tg._normalize_title("", 16) == ""


def test_reasoning_leak_detects_language_mismatch():
    assert tg._looks_like_reasoning_leak("Review sidebar title", "检查侧边栏标题")
    assert not tg._looks_like_reasoning_leak("检查侧边栏标题", "检查侧边栏标题")
