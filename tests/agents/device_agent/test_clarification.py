"""Unit tests for app/agents/device_agent/clarification.py.

The SDK ``tool`` decorator is injected, so we pass a no-op decorator and call
the resulting proxy directly — no claude-agent-sdk required.
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from app.agents.clarification_pause import ClarificationPause, current_pause, with_execution_timeout
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


def _make(broker, *, max_rounds=5):
    events: List[dict] = []
    proxy = make_ask_user_question_tool(
        broker=broker,
        max_rounds=max_rounds,
        emit=events.append,
        seq_counter=SeqCounter(),
        task_id="t1",
        run_id="run-1",
        session_id="sess-1",
        tool_decorator=_noop_decorator,
    )
    return proxy, events


@pytest.fixture(autouse=True)
def pause():
    pause = ClarificationPause()
    token = current_pause.set(pause)
    try:
        yield pause
    finally:
        current_pause.reset(token)


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
    async def test_answered_returns_formatted_text(self, pause):
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
        await proxy({"questions": [_QUESTION]})
        assert events == []
        text = await pause.wait()

        types = [e["type"] for e in events]
        assert types == [CLARIFICATION_REQUEST, CLARIFICATION_RESOLVED]
        assert events[1]["outcome"] == "answered"
        assert "要重启哪个服务？" in text
        assert "nginx" in text

    @pytest.mark.asyncio
    async def test_multi_question_and_custom_text(self, pause):
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
        await proxy({"questions": [_QUESTION, q2]})
        text = await pause.wait()
        assert events[0]["questions"][1]["question"] == "范围？"
        assert events[0]["questions"][1]["options"] == []
        assert "先看日志再决定" in text


class TestPause:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cancel", [False, True])
    async def test_wait_outlives_execution_deadline(self, pause, cancel):
        broker = PermissionBroker()
        proxy, events = _make(broker)

        async def run():
            await proxy({"questions": [_QUESTION]})
            return await pause.wait()

        task = asyncio.create_task(with_execution_timeout(run(), timeout=0.05))
        await asyncio.sleep(0.10)
        assert not task.done()
        assert len(events) == 1 and events[0]["paused"] is True
        request_id = events[0]["request_id"]

        if cancel:
            broker.close()
            assert await task is None
            assert events[-1]["outcome"] == "cancelled"
        else:
            decision = {"answers": [{"question_index": 0, "selected_labels": ["nginx"]}]}
            assert broker.resolve(request_id, decision)
            assert not broker.resolve(request_id, decision)
            assert "nginx" in await task
            assert events[-1]["outcome"] == "answered"


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
    async def test_second_call_capped_when_max_rounds_one(self, pause):
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
        await pause.wait()
        # Second call exceeds the cap → no new request event, cap message returned.
        before = len(events)
        result = await proxy({"questions": [_QUESTION]})
        assert len(events) == before
        assert "上限" in _text(result)
