"""Unit tests for app/agents/device_agent/clarification.py.

The SDK ``tool`` decorator is injected, so we pass a no-op decorator and call
the resulting proxy directly — no claude-agent-sdk required.
"""

from __future__ import annotations

import asyncio
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from app.agents.device_agent.clarification import make_ask_user_question_tool
from app.agents.device_agent.permissions import PermissionBroker
from app.agents.device_agent.trace import (
    CLARIFICATION_REQUEST,
    CLARIFICATION_RESOLVED,
    SeqCounter,
)


def _noop_decorator(_name, _desc, _schema):
    def deco(fn):
        return fn
    return deco


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _make(broker, *, timeout=1.0, on_timeout="cancel", max_rounds=5, cancel_run=None):
    events: List[dict] = []
    proxy = make_ask_user_question_tool(
        broker=broker,
        timeout_seconds=timeout,
        on_timeout=on_timeout,
        max_rounds=max_rounds,
        cancel_run=cancel_run,
        emit=events.append,
        seq_counter=SeqCounter(),
        task_id="t1",
        run_id="run-1",
        session_id="sess-1",
        tool_decorator=_noop_decorator,
    )
    return proxy, events


_QUESTION = {
    "header": "服务",
    "question": "要重启哪个服务？",
    "options": [
        {"label": "nginx", "description": "Web 服务"},
        {"label": "redis", "description": "缓存"},
    ],
}


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_answered_returns_formatted_text(self):
        broker = PermissionBroker()
        proxy, events = _make(broker)

        async def resolver():
            for _ in range(50):
                if events:
                    break
                await asyncio.sleep(0.01)
            req_id = events[0]["request_id"]
            broker.resolve(req_id, {"answers": [{"question_index": 0, "selected_labels": ["nginx"]}]})

        asyncio.create_task(resolver())
        result = await proxy({"questions": [_QUESTION]})

        types = [e["type"] for e in events]
        assert types == [CLARIFICATION_REQUEST, CLARIFICATION_RESOLVED]
        assert events[1]["outcome"] == "answered"
        text = _text(result)
        assert "要重启哪个服务？" in text
        assert "nginx" in text

    @pytest.mark.asyncio
    async def test_multi_question_and_custom_text(self):
        broker = PermissionBroker()
        proxy, events = _make(broker)
        q2 = {"header": "范围", "question": "范围？", "options": []}

        async def resolver():
            for _ in range(50):
                if events:
                    break
                await asyncio.sleep(0.01)
            req_id = events[0]["request_id"]
            broker.resolve(req_id, {"answers": [
                {"question_index": 0, "selected_labels": ["nginx"]},
                {"question_index": 1, "selected_labels": [], "custom_text": "先看日志再决定"},
            ]})

        asyncio.create_task(resolver())
        result = await proxy({"questions": [_QUESTION, q2]})
        text = _text(result)
        assert events[0]["questions"][1]["question"] == "范围？"
        assert events[0]["questions"][1]["options"] == []
        assert "先看日志再决定" in text


class TestTimeout:
    @pytest.mark.asyncio
    async def test_continue_mode_returns_continue_hint(self):
        broker = PermissionBroker()
        cancel_run = MagicMock()
        proxy, events = _make(broker, timeout=0.05, on_timeout="continue", cancel_run=cancel_run)
        result = await proxy({"questions": [_QUESTION]})
        assert events[-1]["type"] == CLARIFICATION_RESOLVED
        assert events[-1]["outcome"] == "timeout"
        cancel_run.assert_not_called()
        assert "继续" in _text(result)

    @pytest.mark.asyncio
    async def test_cancel_mode_calls_cancel_run(self):
        broker = PermissionBroker()
        cancel_run = MagicMock()
        proxy, events = _make(broker, timeout=0.05, on_timeout="cancel", cancel_run=cancel_run)
        result = await proxy({"questions": [_QUESTION]})
        assert events[-1]["outcome"] == "cancelled"
        assert events[-1]["reason"] == "timeout"
        cancel_run.assert_called_once()
        assert "取消" in _text(result)

    @pytest.mark.asyncio
    async def test_cancel_mode_without_callback_degrades_to_continue(self):
        broker = PermissionBroker()
        proxy, events = _make(broker, timeout=0.05, on_timeout="cancel", cancel_run=None)
        result = await proxy({"questions": [_QUESTION]})
        assert events[-1]["outcome"] == "timeout"
        assert "继续" in _text(result)


class TestGuards:
    @pytest.mark.asyncio
    async def test_max_rounds_cap_does_not_emit_request(self):
        broker = PermissionBroker()
        proxy, events = _make(broker, max_rounds=0)
        result = await proxy({"questions": [_QUESTION]})
        assert events == []  # no request emitted
        assert "上限" in _text(result)

    @pytest.mark.asyncio
    async def test_empty_questions_returns_hint(self):
        broker = PermissionBroker()
        proxy, events = _make(broker)
        result = await proxy({"questions": []})
        assert events == []
        assert _text(result)

    @pytest.mark.asyncio
    async def test_second_call_capped_when_max_rounds_one(self):
        broker = PermissionBroker()
        proxy, events = _make(broker, max_rounds=1)

        async def resolver():
            for _ in range(50):
                if events:
                    break
                await asyncio.sleep(0.01)
            broker.resolve(events[0]["request_id"], {"answers": [{"question_index": 0, "selected_labels": ["nginx"]}]})

        asyncio.create_task(resolver())
        await proxy({"questions": [_QUESTION]})
        # Second call exceeds the cap → no new request event, cap message returned.
        before = len(events)
        result = await proxy({"questions": [_QUESTION]})
        assert len(events) == before
        assert "上限" in _text(result)
