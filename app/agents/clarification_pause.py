"""Bridge a clarification tool call to the executor's next turn."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from threading import Event
from typing import TYPE_CHECKING, Awaitable, Callable, TypeVar

if TYPE_CHECKING:
    from claude_agent_sdk import ResultMessage

PAUSED_TOOL_RESULT = (
    "Execution paused for the user's clarification. The user's answer will arrive "
    "in the next message. Continue the current task from this point after receiving it."
)

_T = TypeVar("_T")
_execution_timeout: ContextVar[asyncio.Timeout | None] = ContextVar(
    "agent_execution_timeout", default=None
)
_execution_cancel: ContextVar[Event | None] = ContextVar(
    "agent_execution_cancel", default=None
)


async def with_execution_timeout(
    run: Awaitable[_T], *, timeout: float, cancel_event: Event | None = None
) -> _T:
    async with asyncio.timeout(timeout) as deadline:
        token = _execution_timeout.set(deadline)
        cancel_token = _execution_cancel.set(cancel_event)
        try:
            return await run
        finally:
            _execution_cancel.reset(cancel_token)
            _execution_timeout.reset(token)


@dataclass
class ClarificationPause:
    # The callback owns the broker request and publishes the card only after
    # the current executor segment has stopped.
    pending: list[Callable[[], Awaitable[str | None]]] = field(default_factory=list)

    async def wait(self, *, segment_timeout: asyncio.Timeout | None = None) -> str | None:
        cancel = _execution_cancel.get()
        if cancel is not None and cancel.is_set():
            return None
        loop = asyncio.get_running_loop()
        deadlines = []
        for deadline in (_execution_timeout.get(), segment_timeout):
            if deadline is None or deadline.expired():
                continue
            when = deadline.when()
            if when is not None:
                deadlines.append((deadline, max(0, when - loop.time())))
                deadline.reschedule(None)
        try:
            answers = []
            for wait_for_answer in self.pending:
                answer = await wait_for_answer()
                if answer is None or (cancel is not None and cancel.is_set()):
                    return None
                answers.append(answer)
            return "\n\n".join(answers)
        finally:
            self.pending.clear()
            for deadline, remaining in deadlines:
                deadline.reschedule(loop.time() + remaining)


@dataclass
class SegmentUsage:
    tokens: Counter[str] = field(default_factory=Counter)
    turns: int = 0

    def add(self, result: ResultMessage | None) -> None:
        if result is None:
            return
        self.turns += result.num_turns
        for name in (
            "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"
        ):
            self.tokens[name] += (result.usage or {}).get(name) or 0

    def apply(self, result: ResultMessage) -> ResultMessage:
        if not self.turns and not self.tokens:
            return result
        usage = dict(result.usage or {})
        for name, count in self.tokens.items():
            usage[name] = (usage.get(name) or 0) + count
        return replace(result, usage=usage, num_turns=result.num_turns + self.turns)


current_pause: ContextVar[ClarificationPause | None] = ContextVar(
    "agent_clarification_pause", default=None
)


def continuation_prompt(answers: str) -> str:
    return "The user answered the pending clarification. Continue the current task from where it paused.\n\n" + answers


def cancelled_message():
    from claude_agent_sdk import SystemMessage

    # Workspace agents check their existing cancel flag between SDK messages.
    return SystemMessage(subtype="raven_clarification_cancelled", data={})
