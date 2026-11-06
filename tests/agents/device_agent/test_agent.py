"""Unit tests for app/agents/device_agent/agent.py — DeviceAgent.run_stream / run.

These tests mock ``claude_agent_sdk.query`` and ``device_link_manager.send_prompt``
so they exercise the real event-translation / workspace / broker plumbing without
the SDK or a connected device.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List
from unittest.mock import patch

import pytest

from app.agents.device_agent.agent import (
    DeviceAgent,
    DeviceAgentContext,
    _format_history_block,
    _make_queue_emitter,
    _single_user_prompt_stream,
)
from app.agents.device_agent.prompts import clarification_guidance


# ─────────────────────── Fake SDK messages ─────────────────────────


class _FakeUsage:
    def __init__(self, in_t: int = 0, out_t: int = 0):
        self.input_tokens = in_t
        self.output_tokens = out_t
        self.cache_read_input_tokens = 0


class _TextBlock:
    def __init__(self, text: str):
        self.text = text


class _AssistantMessage:
    def __init__(self, blocks):
        self.content = blocks
        self.usage = _FakeUsage(10, 20)


class _ResultMessage:
    def __init__(self, text: str):
        self.result = text
        self.num_turns = 1
        self.stop_reason = "end_turn"
        self.usage = _FakeUsage()


class _FakeDevice:
    def __init__(self, capabilities: Dict[str, Any]):
        self.capabilities = capabilities


# ─────────────────────── Helpers ───────────────────────────────────


def _fake_query_factory(messages):
    """Return an async-iterator callable matching ``claude_agent_sdk.query`` signature."""

    async def _q(*, prompt, options) -> AsyncIterator[Any]:  # noqa: ARG001
        for m in messages:
            yield m

    return _q


# ─────────────────────── _format_history_block ────────────────────


class TestFormatHistoryBlock:
    def test_empty_history(self):
        assert _format_history_block([], 10) == ""

    def test_ordering_and_role_normalization(self):
        out = _format_history_block(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "AI", "content": "more"},
            ],
            10,
        )
        assert "[user] hi" in out
        assert "[assistant] hello" in out
        assert "[assistant] more" in out
        # blocks separated by blank line
        assert out.count("\n\n") == 2

    def test_truncation_to_max_turns(self):
        history = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        out = _format_history_block(history, max_turns=2)
        # max_turns=2 → keep last 4 entries.
        assert "m19" in out and "m18" in out and "m17" in out and "m16" in out
        assert "m15" not in out

    def test_empty_content_skipped(self):
        out = _format_history_block(
            [{"role": "user", "content": "  "}, {"role": "user", "content": "ok"}],
            10,
        )
        assert out == "[user] ok"


@pytest.mark.asyncio
async def test_single_user_prompt_stream_uses_sdk_streaming_shape():
    stream = _single_user_prompt_stream("hello")
    assert hasattr(stream, "__aiter__")

    messages = [message async for message in stream]
    assert messages == [
        {
            "type": "user",
            "message": {"role": "user", "content": "hello"},
        }
    ]


@pytest.mark.asyncio
async def test_queue_emitter_wakes_consumer_from_background_thread():
    """An in-process MCP callback must wake the SSE pump across threads."""
    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
    emit = _make_queue_emitter(queue, asyncio.get_running_loop())
    waiting_consumer = asyncio.create_task(queue.get())
    await asyncio.sleep(0)

    event = {"type": "clarification_request", "request_id": "req-1"}
    await asyncio.to_thread(emit, event)

    received = await asyncio.wait_for(waiting_consumer, timeout=0.5)
    assert received is event


def test_clarification_guidance_names_product_mcp_tool_explicitly():
    guidance = clarification_guidance("zh")

    assert "`mcp__ask__AskUserQuestion`" in guidance
    assert "不要调用 Claude CLI 内置" in guidance


# ─────────────────────── DeviceAgent.run_stream ────────────────────


@pytest.mark.asyncio
async def test_run_stream_emits_run_start_thinking_and_run_complete(monkeypatch):
    """Happy path: SDK emits one assistant text + result; we get run_start →
    thinking_* → run_complete in order."""
    device = _FakeDevice({"protocol_version": 2, "mcp": {"servers": []}})

    async def _get_device(*_a, **_kw):
        return device

    fake_query = _fake_query_factory([
        _AssistantMessage([_TextBlock("hello world")]),
        _ResultMessage("hello world"),
    ])

    monkeypatch.setattr(
        "app.services.device_link_service.device_link_manager.get_device",
        _get_device,
    )
    monkeypatch.setattr("app.agents.device_agent.agent.sdk_query", fake_query, raising=False)
    # The agent imports query inside the function; patch at the SDK module level.
    monkeypatch.setattr("claude_agent_sdk.query", fake_query)

    # Force provider = anthropic so the capability gate lets us through.
    monkeypatch.setattr("app.config.settings.anthropic_provider", "anthropic")
    monkeypatch.setattr("app.config.settings.anthropic_api_key", "sk-test")
    monkeypatch.setattr("app.config.settings.anthropic_model", "claude-sonnet-4-6", raising=False)

    ctx = DeviceAgentContext(
        session_id="sess-1",
        user_message="hi",
        target_device_id="dev-1",
    )

    events: List[Dict[str, Any]] = []
    async for ev in DeviceAgent().run_stream(ctx):
        events.append(dict(ev))

    types = [e["type"] for e in events]
    assert types[0] == "run_start"
    assert types[-1] == "run_complete"
    assert "thinking_start" in types
    assert "thinking_end" in types
    # Frontend replay deduplicates by ``(run_id, seq)``.  All DeviceAgent
    # producers, including AskUserQuestion, must therefore share one strictly
    # increasing sequence space rather than restart at 1 independently.
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    # run_complete carries the final_text excerpt.
    complete = events[-1]
    assert "hello world" in complete.get("final_text", "")


@pytest.mark.asyncio
async def test_run_stream_rejects_unsupported_provider(monkeypatch):
    """When the active provider has ``supports_mcp_server_tools=False``, the agent
    emits ``error{error_kind=provider_no_mcp_support}`` without invoking the SDK."""

    # Patch the deepseek profile to NOT support MCP (its real profile does).
    from app.agents.anthropic_client import ProviderProfile

    fake_ds = ProviderProfile(
        name="deepseek",
        default_base_url="https://api.deepseek.com/anthropic",
        default_model="deepseek-v4-pro",
        default_small_fast_model="deepseek-v4-flash",
        supports_image_input=False,
        supports_document_input=False,
        supports_mcp_server_tools=False,
        thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False,
    )

    import app.agents.anthropic_client as ac

    monkeypatch.setitem(ac.PROVIDER_PROFILES, "deepseek", fake_ds)
    monkeypatch.setattr("app.config.settings.anthropic_provider", "deepseek")
    monkeypatch.setattr("app.config.settings.anthropic_api_key", "sk-test")

    called = {"n": 0}

    async def _should_not_be_called(**kwargs):  # noqa: ARG001
        called["n"] += 1
        if False:
            yield

    monkeypatch.setattr("claude_agent_sdk.query", _should_not_be_called)

    ctx = DeviceAgentContext(
        session_id="sess-deepseek",
        user_message="hello",
        target_device_id="dev-x",
    )

    events: List[Dict[str, Any]] = []
    async for ev in DeviceAgent().run_stream(ctx):
        events.append(dict(ev))

    assert called["n"] == 0
    types = [e["type"] for e in events]
    assert types[0] == "run_start"
    # The next event must be the error.
    assert "error" in types
    err = next(e for e in events if e["type"] == "error")
    assert err.get("error_kind") == "provider_no_mcp_support"
    assert "deepseek" in err.get("message", "")


@pytest.mark.asyncio
async def test_run_stream_registers_and_cleans_broker(monkeypatch):
    """``broker_registry[session_id]`` is set during the run and removed in finally."""
    device = _FakeDevice({"protocol_version": 2, "mcp": {"servers": [{"name": "probe", "tools": [
        {"name": "read", "risk": "read"}, {"name": "write", "risk": "write"},
        {"name": "erase", "risk": "destructive"}, {"name": "unknown"},
    ]}]}})

    async def _get_device(*_a, **_kw):
        return device

    seen_brokers: List[Any] = []
    seen_options: List[Any] = []

    fake_query = _fake_query_factory([_ResultMessage("done")])

    async def _capturing_query(*, prompt, options):  # noqa: ARG001
        # Inspect that broker is registered at this point.
        seen_brokers.append(dict(registry))
        seen_options.append(options)
        for m in [_ResultMessage("done")]:
            yield m

    monkeypatch.setattr(
        "app.services.device_link_service.device_link_manager.get_device",
        _get_device,
    )
    monkeypatch.setattr("claude_agent_sdk.query", _capturing_query)
    monkeypatch.setattr("app.config.settings.anthropic_provider", "anthropic")
    monkeypatch.setattr("app.config.settings.anthropic_api_key", "sk-test")

    registry: Dict[str, Any] = {}
    ctx = DeviceAgentContext(
        session_id="sess-broker",
        user_message="hi",
        target_device_id="dev-1",
        broker_registry=registry,
    )

    async for _ in DeviceAgent().run_stream(ctx):
        pass

    # Broker was registered while SDK was running...
    assert seen_brokers and "sess-broker" in seen_brokers[0]
    # The headless CLI's built-in prompt tool is unusable here; only RavenAI's
    # broker-backed MCP variant may be selected by the model.
    assert seen_options
    assert "AskUserQuestion" in (seen_options[0].disallowed_tools or [])
    assert "mcp__ask__AskUserQuestion" in (seen_options[0].allowed_tools or [])
    assert "mcp__device__probe__read" in seen_options[0].allowed_tools
    for name in ("write", "erase", "unknown"):
        assert f"mcp__device__probe__{name}" not in seen_options[0].allowed_tools
    # ...and removed after run_stream finished.
    assert "sess-broker" not in registry
