"""Tests for the endpoint-aware ``query()`` wrapper.

``model_router.candidates`` / ``record_outcome`` are stubbed so these exercise
the wrapper's own contract: where the commit boundary sits, what reaches the
caller before it, when failover happens and when it must not.

Two clocks are in play, deliberately. TTFT accounting is driven through the
patched ``rq._now`` so a "10 second" wait costs no test time; the first-token
deadline runs on the real event loop (``asyncio.wait_for``), so those tests use
millisecond deadlines and real ``asyncio.sleep``.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents import routed_query as rq
from app.agents.routed_query import AllEndpointsUnavailable, EndpointSwitchNotice
from app.config import settings
from app.services.model_router import EndpointChoice


# ─────────────────────────── SDK message doubles ───────────────────────────
#
# Shapes mirror what claude_agent_sdk yields; the wrapper classifies by
# attribute presence rather than isinstance, so these are enough.


class InitMessage:
    """``SystemMessage(subtype='init')`` — CLI up, model not yet contacted."""

    subtype = "init"
    data = {"session_id": "s-1"}
    content = None
    event = None


class StatusMessage:
    """``SystemMessage(subtype='status')`` — the frame that broke TTFT.

    The real CLI emits this immediately after ``init`` and again periodically
    while the model is still working. It reached production classified as model
    output, pinning every measured TTFT at 0 ms.
    """

    subtype = "status"
    data = {"message": "working"}
    content = None
    event = None


class StreamEvent:
    content = None

    def __init__(self, text: str = "hi") -> None:
        self.event = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        }


class AssistantMessage:
    event = None

    def __init__(self, text: str = "hello", *, error: str | None = None) -> None:
        self.content = [{"type": "text", "text": text}]
        self.error = error


class ResultMessage:
    """Terminal frame carrying no assistant content.

    Has ``subtype`` but no ``data`` dict — that is exactly what keeps it on the
    committing side of ``_is_pre_model_frame``.
    """

    content = None
    event = None
    subtype = "success"

    def __init__(
        self,
        result: str = "done",
        *,
        is_error: bool = False,
        api_error_status: int | None = None,
        errors: list[str] | None = None,
    ) -> None:
        self.result = result
        self.is_error = is_error
        self.api_error_status = api_error_status
        self.errors = errors


# ─────────────────────────── Harness ───────────────────────────────────────


class Clock:
    """Controllable stand-in for ``rq._now``.

    Advanced explicitly from inside the fake stream, so a test reads as a
    timeline rather than a list of ticks that must match the number of internal
    ``_now()`` calls — the old tick-list harness broke whenever that count
    changed.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Tick:
    """Advance the fake clock by ``seconds`` before the next frame.

    Yields to the loop first so the consumer has drained everything already
    queued. Without that the queue's one-slot lookahead lets the clock move
    before the consumer has read the frame it is supposed to time from.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def apply(self, clock: "Clock") -> None:
        for _ in range(4):
            await asyncio.sleep(0)
        clock.t += self.seconds


class Sleep:
    """Really await, so the first-token deadline can fire."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


def profile():
    from app.agents.anthropic_client import PROVIDER_PROFILES

    return PROVIDER_PROFILES["yinhe"]


def choice(slot: str) -> EndpointChoice:
    return EndpointChoice(
        slot=slot,
        provider="yinhe" if slot == "primary" else "deepseek",
        base_url=f"http://{slot}.test",
        api_key=f"sk-{slot}",
        model=f"{slot}-model",
        small_fast_model=None,
        profile=profile(),
    )


@pytest.fixture
def router_stub(monkeypatch):
    """Stub the router; returns a box to set candidates and read outcomes."""
    box = {"candidates": [choice("primary"), choice("backup")], "outcomes": []}

    monkeypatch.setattr(rq.model_router, "candidates", lambda **kw: box["candidates"])
    monkeypatch.setattr(
        rq.model_router,
        "record_outcome",
        lambda slot, **kw: box["outcomes"].append({"slot": slot, **kw}),
    )
    return box


@pytest.fixture(autouse=True)
def no_deadline(monkeypatch):
    """Preemption off unless a test asks for it.

    Most tests assert on the commit boundary, where a real-time deadline would
    only add flakiness.
    """
    monkeypatch.setattr(settings, "model_router_first_token_deadline_ms", 0)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(rq, "_now", c)
    return c


@pytest.fixture(autouse=True)
async def reap_cleanups():
    """Detached teardowns must not leak into the next test."""
    yield
    await rq.drain_cleanup_tasks(timeout=5)


def sdk_yielding(*scripts, clock: Clock | None = None):
    """Build a fake ``query`` that plays one script per successive call.

    Script items are messages, ``Tick``/``Sleep`` markers, or an exception
    instance to raise. ``closed`` records generators that were finalised, which
    is how abandonment is verified.
    """
    calls = {"n": 0, "options": [], "closed": [], "started": []}

    async def fake_query(*, prompt, options):
        index = min(calls["n"], len(scripts) - 1)
        calls["n"] += 1
        calls["options"].append(options)
        calls["started"].append(index)
        try:
            for item in scripts[index]:
                if isinstance(item, Tick):
                    assert clock is not None, "Tick requires a clock"
                    await item.apply(clock)
                    continue
                if isinstance(item, Sleep):
                    await asyncio.sleep(item.seconds)
                    continue
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            calls["closed"].append(index)

    return fake_query, calls


async def drain(**kwargs):
    return [m async for m in rq.routed_query(**kwargs)]


OPTS = lambda c: {"slot": c.slot if c else None}  # noqa: E731


def kinds(messages) -> list[str]:
    return [type(m).__name__ for m in messages]


# ─────────────────────────── Commit boundary ───────────────────────────────


async def test_status_frame_does_not_commit(router_stub, clock):
    """Regression: the ``status`` frame is CLI bookkeeping, not model output.

    Production shipped with commit defined as "anything that is not ``init``".
    The CLI emits ``status`` microseconds after ``init``, so every run measured
    ttft_ms=0, the router's window filled with "fast" samples, and the breaker
    could never trip. This is the test that would have caught it.
    """
    fake_query, _ = sdk_yielding(
        [InitMessage(), StatusMessage(), Tick(12.0), StreamEvent()],
        clock=clock,
    )
    await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert router_stub["outcomes"] == [
        {"slot": "primary", "outcome": "ok", "ttft_ms": 12_000}
    ]


async def test_empty_token_progress_still_reaches_consumer_before_commit(router_stub, clock):
    from app.agents.log_analysis.agent import _RunState, _emit_for_message

    progress = StatusMessage()
    progress.subtype = "thinking_tokens"
    progress.data = {"thinking_tokens": 1}
    fake_query, _ = sdk_yielding(
        [InitMessage(), progress, Tick(12.0), StreamEvent()], clock=clock,
    )
    messages = await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)
    assert any(message is progress for message in messages)
    assert router_stub["outcomes"] == [
        {"slot": "primary", "outcome": "ok", "ttft_ms": 12_000}
    ]
    state = _RunState("progress", None)
    _emit_for_message(progress, state=state)
    assert state.trace_events == []


async def test_ttft_excludes_cli_startup(router_stub, clock):
    """The init frame resets the clock — it precedes any model round trip.

    Measuring from generator creation would fold subprocess spawn and MCP setup
    into "endpoint latency" and trip the breaker on a healthy endpoint.
    """
    fake_query, _ = sdk_yielding(
        [Tick(10.0), InitMessage(), Tick(0.25), StreamEvent()],
        clock=clock,
    )
    await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert router_stub["outcomes"] == [
        {"slot": "primary", "outcome": "ok", "ttft_ms": 250}
    ]


async def test_status_frames_do_not_reset_the_ttft_clock(router_stub, clock):
    """Only ``init`` restarts the clock.

    The CLI keeps emitting ``status`` while the model thinks. If those reset the
    clock the way ``init`` does, a slow endpoint would measure as the gap
    between the last keepalive and the token — near zero again.
    """
    fake_query, _ = sdk_yielding(
        [
            InitMessage(),
            Tick(9.0),
            StatusMessage(),
            Tick(9.0),
            StatusMessage(),
            Tick(9.0),
            StreamEvent(),
        ],
        clock=clock,
    )
    await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert router_stub["outcomes"][0]["ttft_ms"] == 27_000


async def test_pre_model_frames_reach_the_caller_immediately(router_stub):
    """No silent period: bookkeeping frames are forwarded, not buffered.

    Holding them until the first token leaves the browser with a dead screen
    for the whole wait, which is the symptom the deadline exists to bound.
    """
    seen: list[str] = []

    async def collect():
        async for message in rq.routed_query(
            prompt="p", make_options=OPTS, sdk_query=fake_query
        ):
            seen.append(type(message).__name__)

    started = asyncio.Event()

    async def fake_query(*, prompt, options):
        yield InitMessage()
        yield StatusMessage()
        started.set()
        await asyncio.sleep(30)  # model still thinking
        yield StreamEvent()

    task = asyncio.create_task(collect())
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(0)  # let the consumer drain the queue

    assert seen == ["InitMessage", "StatusMessage"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_sdk_is_not_read_ahead_of_the_consumer(router_stub):
    """The SDK must stay suspended while the consumer works on a frame.

    Regression: routing the stream through a task made it tempting to let the
    pump buffer one frame ahead. That is observable — agents poll
    ``cancel_event`` between frames, so a cancel raised by frame N's handler
    would drop frame N+1 that the SDK had already produced. Here the generator
    records how far it has run each time the consumer sees a frame.
    """
    produced: list[int] = []
    observed: list[tuple[int, int]] = []

    async def fake_query(*, prompt, options):
        for index in range(4):
            produced.append(index)
            yield AssistantMessage(f"m{index}")

    seen = 0
    async for _ in rq.routed_query(prompt="p", make_options=OPTS, sdk_query=fake_query):
        seen += 1
        observed.append((seen, len(produced)))

    # Exactly as many frames produced as consumed — never one more.
    assert observed == [(1, 1), (2, 2), (3, 3), (4, 4)]


async def test_result_message_still_commits(router_stub):
    """``ResultMessage`` has a ``subtype`` but no ``data`` — not a system frame."""
    fake_query, _ = sdk_yielding([InitMessage(), ResultMessage()])
    out = await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert kinds(out) == ["InitMessage", "ResultMessage"]
    assert router_stub["outcomes"][0]["outcome"] == "ok"
    assert "ttft_ms" in router_stub["outcomes"][0]


async def test_assistant_message_commits_when_no_partial_streaming(router_stub):
    fake_query, _ = sdk_yielding([InitMessage(), AssistantMessage(), ResultMessage()])
    out = await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert len(out) == 3
    assert router_stub["outcomes"][0]["outcome"] == "ok"


async def test_unrecognised_message_shape_still_commits(router_stub):
    """Commit is defined by exclusion, so an unknown shape is never swallowed.

    Regression: an earlier version committed only on messages carrying
    ``content``/``event``. A message shaped differently (agents' own doubles
    carry ``tool_uses``) stalled the run.
    """

    class ToolUseOnly:
        tool_uses = [{"name": "Read"}]

    fake_query, _ = sdk_yielding([InitMessage(), ToolUseOnly(), ResultMessage()])
    out = await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert kinds(out) == ["InitMessage", "ToolUseOnly", "ResultMessage"]
    assert router_stub["outcomes"][0]["outcome"] == "ok"


async def test_init_only_stream_is_not_an_endpoint_failure(router_stub):
    """A stream that never gets past startup still answered — do not fail over."""
    fake_query, calls = sdk_yielding([InitMessage(), StatusMessage()])
    out = await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert kinds(out) == ["InitMessage", "StatusMessage"]
    assert router_stub["outcomes"] == [{"slot": "primary", "outcome": "ok"}]
    assert calls["n"] == 1


# ─────────────────────────── First-token deadline ──────────────────────────


async def test_deadline_preempts_and_switches_to_backup(router_stub, monkeypatch):
    """The user's wait is bounded: switch endpoints and keep going.

    This is the whole point of the deadline — ``slow_ttft_ms`` only labels a
    sample after the token finally arrives, so it can never shorten what the
    user sits through.
    """
    monkeypatch.setattr(settings, "model_router_first_token_deadline_ms", 50)
    fake_query, calls = sdk_yielding(
        [InitMessage(), StatusMessage(), Sleep(30), StreamEvent("never")],
        [InitMessage(), StreamEvent("from backup")],
    )
    seen: list[str] = []

    out = await asyncio.wait_for(
        drain(
            prompt="p",
            make_options=OPTS,
            sdk_query=fake_query,
            on_endpoint=lambda c: seen.append(c.slot),
        ),
        timeout=10,
    )

    assert seen == ["primary", "backup"]
    assert kinds(out) == [
        "InitMessage",
        "StatusMessage",
        "EndpointSwitchNotice",
        "InitMessage",
        "StreamEvent",
    ]
    assert router_stub["outcomes"][0] == {"slot": "primary", "outcome": "timeout"}
    assert router_stub["outcomes"][1]["slot"] == "backup"


async def test_preempted_attempt_is_torn_down(router_stub, monkeypatch):
    """The abandoned generator must be finalised, not left running.

    Two reasons it cannot simply be dropped: the ``claude`` subprocess would be
    orphaned, and a still-live attempt would keep driving tools in the *same*
    workspace the retry is using.
    """
    monkeypatch.setattr(settings, "model_router_first_token_deadline_ms", 50)
    fake_query, calls = sdk_yielding(
        [InitMessage(), Sleep(30), StreamEvent("never")],
        [InitMessage(), StreamEvent("from backup")],
    )

    await asyncio.wait_for(
        drain(prompt="p", make_options=OPTS, sdk_query=fake_query), timeout=10
    )

    # Already reaped by the time we get control back — not merely eventually.
    # The workspace agents run under ``asyncio.run`` in a Celery worker, so the
    # loop is closed the instant the agent returns; a cleanup still pending
    # then would be cancelled and the subprocess left behind.
    assert 0 in calls["closed"], "preempted attempt was never finalised"


async def test_switch_notice_is_shaped_like_a_system_frame():
    """Agents dispatch on ``subtype`` + ``data``; it must not look like output."""
    notice = EndpointSwitchNotice(
        from_slot="primary",
        to_slot="backup",
        reason="first_token_deadline",
        waited_ms=20_000,
    )

    assert notice.content is None and notice.event is None
    assert isinstance(notice.data, dict) and notice.data["message"]
    assert not hasattr(notice, "result"), "a result field would terminate the run"
    assert rq._is_pre_model_frame(notice)


async def test_switch_notice_renders_as_a_trace_event():
    """Cross-module contract: agents must translate it without a code change.

    All five agents share ``_emit_for_message``, which dispatches on duck-typed
    attributes. Asserting the shape alone would not catch a notice that silently
    produces no event — or worse, one that trips the ``result`` branch and ends
    the run early — so this drives the real translator.
    """
    from app.agents.log_analysis.agent import _RunState, _emit_for_message

    state = _RunState("task-1", None)
    before = state.final_text
    _emit_for_message(
        EndpointSwitchNotice(
            from_slot="primary",
            to_slot="backup",
            reason="first_token_deadline",
            waited_ms=20_000,
        ),
        state=state,
    )

    assert [e["type"] for e in state.trace_events] == ["system_notice"]
    event = state.trace_events[0]
    assert event["subtype"] == "endpoint_switch"
    assert "备用端点" in event["detail"]
    assert state.final_text == before, "a switch must not set the run's answer"


async def test_no_preemption_without_a_fallback(router_stub, monkeypatch):
    """Killing the only endpoint leaves the user with nothing — slow beats dead."""
    monkeypatch.setattr(settings, "model_router_first_token_deadline_ms", 50)
    router_stub["candidates"] = [choice("primary")]
    fake_query, calls = sdk_yielding([InitMessage(), Sleep(0.3), StreamEvent("late")])

    out = await asyncio.wait_for(
        drain(prompt="p", make_options=OPTS, sdk_query=fake_query), timeout=10
    )

    assert kinds(out) == ["InitMessage", "StreamEvent"]
    assert calls["n"] == 1


async def test_no_preemption_after_commit(router_stub, monkeypatch):
    """Once model output exists the deadline is disarmed.

    A long tool-running turn is normal; cutting it would replay side effects and
    re-bill the tokens.
    """
    monkeypatch.setattr(settings, "model_router_first_token_deadline_ms", 50)
    fake_query, calls = sdk_yielding(
        [InitMessage(), StreamEvent("first"), Sleep(0.3), AssistantMessage("second")],
        [StreamEvent("backup must not run")],
    )

    out = await asyncio.wait_for(
        drain(prompt="p", make_options=OPTS, sdk_query=fake_query), timeout=10
    )

    assert kinds(out) == ["InitMessage", "StreamEvent", "AssistantMessage"]
    assert calls["n"] == 1


async def test_deadline_is_measured_from_init_not_from_spawn(router_stub, monkeypatch):
    """Subprocess spawn and MCP setup must not eat the budget.

    Cloning skills and booting in-process MCP servers happens before ``init``;
    charging that to the endpoint would preempt healthy runs under load.
    """
    monkeypatch.setattr(settings, "model_router_first_token_deadline_ms", 300)
    fake_query, calls = sdk_yielding(
        [Sleep(0.2), InitMessage(), Sleep(0.2), StreamEvent("in time")],
        [StreamEvent("backup must not run")],
    )

    out = await asyncio.wait_for(
        drain(prompt="p", make_options=OPTS, sdk_query=fake_query), timeout=10
    )

    assert kinds(out) == ["InitMessage", "StreamEvent"]
    assert calls["n"] == 1


async def test_deadline_default_is_consistent_with_the_slow_threshold():
    """A deadline below the "slow" label would preempt before a sample is bad.

    Reads the field defaults rather than the live settings — the autouse
    fixture disables preemption for every other test in this module.
    """
    fields = type(settings).model_fields
    assert (
        fields["model_router_first_token_deadline_ms"].default
        >= fields["model_router_slow_ttft_ms"].default
    )


# ─────────────────────────── Failover ──────────────────────────────────────


async def test_fails_over_before_first_token(router_stub):
    fake_query, calls = sdk_yielding(
        [InitMessage(), ConnectionError("connection refused")],
        [InitMessage(), AssistantMessage("from backup")],
    )
    seen: list[str] = []

    out = await drain(
        prompt="p",
        make_options=OPTS,
        sdk_query=fake_query,
        on_endpoint=lambda c: seen.append(c.slot),
    )

    assert seen == ["primary", "backup"]
    assert kinds(out) == [
        "InitMessage",
        "EndpointSwitchNotice",
        "InitMessage",
        "AssistantMessage",
    ]
    assert router_stub["outcomes"][0] == {"slot": "primary", "outcome": "hard_failure"}
    assert router_stub["outcomes"][1]["slot"] == "backup"


async def test_fails_over_when_the_error_lands_after_the_status_frame(router_stub):
    """The second path the ``status`` misclassification killed.

    A 401/502 on the real model call arrives *after* ``init``+``status``. With
    those counted as commit the attempt was already committed, so the error was
    re-raised to the user instead of failing over.
    """
    fake_query, calls = sdk_yielding(
        [InitMessage(), StatusMessage(), ConnectionError("502 from gateway")],
        [InitMessage(), AssistantMessage("from backup")],
    )

    out = await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert calls["n"] == 2
    assert kinds(out)[-1] == "AssistantMessage"
    assert router_stub["outcomes"][0] == {"slot": "primary", "outcome": "hard_failure"}


async def test_options_are_rebuilt_per_candidate(router_stub):
    """Capability gating is baked into options, so they cannot be reused."""
    fake_query, calls = sdk_yielding(
        [ConnectionError("nope")],
        [AssistantMessage()],
    )
    await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert calls["options"] == [{"slot": "primary"}, {"slot": "backup"}]


async def test_no_failover_after_commit(router_stub):
    """Once tools may have run, retrying would replay side effects."""
    fake_query, calls = sdk_yielding(
        [InitMessage(), AssistantMessage(), RuntimeError("upstream died mid-stream")],
        [AssistantMessage("backup should never run")],
    )

    with pytest.raises(RuntimeError, match="mid-stream"):
        await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert calls["n"] == 1


async def test_pre_output_429_rotates_one_primary_key_before_backup(
    router_stub, monkeypatch
):
    first = choice("primary")
    first = EndpointChoice(
        **{
            **first.__dict__,
            "api_key": "sk-first",
            "api_key_id": "key-first",
            "api_key_index": 0,
            "api_key_count": 2,
        }
    )
    second = EndpointChoice(
        **{
            **first.__dict__,
            "api_key": "sk-second",
            "api_key_id": "key-second",
            "api_key_index": 1,
        }
    )
    router_stub["candidates"] = [first, choice("backup")]
    monkeypatch.setattr(
        rq.model_router,
        "alternate_api_key",
        lambda endpoint: second if endpoint.api_key_id == "key-first" else None,
    )
    fake_query, calls = sdk_yielding(
        [AssistantMessage("API Error: 429 RPM exceeded", error="rate_limit")],
        [AssistantMessage("from rotated key")],
        [AssistantMessage("backup must not run")],
    )
    seen: list[str] = []

    out = await drain(
        prompt="p",
        make_options=lambda endpoint: endpoint.api_key_id,
        sdk_query=fake_query,
        on_endpoint=lambda endpoint: seen.append(endpoint.api_key_id),
    )

    assert calls["options"] == ["key-first", "key-second"]
    assert seen == ["key-first", "key-second"]
    assert kinds(out) == ["EndpointSwitchNotice", "AssistantMessage"]
    assert out[0].data == {
        "from_slot": "primary",
        "to_slot": "primary",
        "reason": "rate_limit",
        "waited_ms": 0,
        "message": "主力 API Key 达到 RPM 限制，已轮换另一个 Key 继续",
        "from_key_id": "key-first",
        "to_key_id": "key-second",
    }
    # A key-local 429 does not poison the endpoint breaker; the successful
    # rotated attempt supplies the endpoint outcome.
    assert router_stub["outcomes"] == [
        {"slot": "primary", "outcome": "ok", "ttft_ms": 0}
    ]


# ────────────────────── Upstream API error frames ──────────────────────────


async def test_structured_billing_error_fails_over_without_leaking_text(router_stub):
    """SDK 0.2.82 exposes AssistantMessage.error; it is authoritative."""
    fake_query, calls = sdk_yielding(
        [
            InitMessage(),
            AssistantMessage(
                "API Error: 402 Insufficient Balance", error="billing_error"
            ),
            RuntimeError("backup should be selected before this wrapper error"),
        ],
        [InitMessage(), AssistantMessage("from backup"), ResultMessage()],
    )

    out = await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert calls["n"] == 2
    assert kinds(out) == [
        "InitMessage",
        "EndpointSwitchNotice",
        "InitMessage",
        "AssistantMessage",
        "ResultMessage",
    ]
    assert out[1].data["reason"] == "billing_error"
    assert out[3].content[0]["text"] == "from backup"
    assert router_stub["outcomes"][0] == {
        "slot": "primary",
        "outcome": "hard_failure",
    }


async def test_production_legacy_402_preserves_real_cause(router_stub):
    """Captured production sequence must not become `error result: success`."""
    router_stub["candidates"] = [choice("backup")]
    fake_query, calls = sdk_yielding(
        [
            InitMessage(),
            StatusMessage(),
            AssistantMessage("API Error: 402 Insufficient Balance"),
            ResultMessage(
                "API Error: 402 Insufficient Balance",
                is_error=True,
            ),
            RuntimeError("Claude Code returned an error result: success"),
        ]
    )

    with pytest.raises(AllEndpointsUnavailable) as excinfo:
        await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert calls["n"] == 1
    assert len(excinfo.value.failures) == 1
    slot, cause = excinfo.value.failures[0]
    assert slot == "backup"
    assert isinstance(cause, rq.UpstreamAPIError)
    assert cause.error_kind == "billing_error"
    assert cause.status_code == 402
    assert cause.detail == "Insufficient Balance"
    assert "error result: success" not in str(excinfo.value)


async def test_timeout_then_backup_402_reports_both_causes(router_stub, monkeypatch):
    monkeypatch.setattr(settings, "model_router_first_token_deadline_ms", 25)
    fake_query, calls = sdk_yielding(
        [InitMessage(), StatusMessage(), Sleep(1), AssistantMessage("too late")],
        [
            InitMessage(),
            AssistantMessage("API Error: 402 Insufficient Balance"),
            ResultMessage(
                "API Error: 402 Insufficient Balance",
                is_error=True,
                api_error_status=402,
            ),
        ],
    )

    with pytest.raises(AllEndpointsUnavailable) as excinfo:
        await asyncio.wait_for(
            drain(prompt="p", make_options=OPTS, sdk_query=fake_query), timeout=5
        )

    assert calls["n"] == 2
    assert [slot for slot, _ in excinfo.value.failures] == ["primary", "backup"]
    message = str(excinfo.value)
    assert "no first token" in message
    assert "status=402" in message
    assert "Insufficient Balance" in message


async def test_successful_result_disproves_legacy_text_candidate(router_stub, clock):
    fake_query, calls = sdk_yielding(
        [
            InitMessage(),
            Tick(0.3),
            AssistantMessage("API Error: 402 Insufficient Balance"),
            ResultMessage("done", is_error=False),
        ],
        clock=clock,
    )

    out = await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert calls["n"] == 1
    assert kinds(out) == ["InitMessage", "AssistantMessage", "ResultMessage"]
    assert out[1].content[0]["text"] == "API Error: 402 Insufficient Balance"
    assert router_stub["outcomes"] == [
        {"slot": "primary", "outcome": "ok", "ttft_ms": 300}
    ]


async def test_error_like_diagnostic_prose_commits_normally(router_stub):
    fake_query, calls = sdk_yielding(
        [
            InitMessage(),
            AssistantMessage("The log contains API Error: 402, so check billing."),
            RuntimeError("upstream died after the answer"),
        ],
        [AssistantMessage("must not retry")],
    )

    with pytest.raises(RuntimeError, match="after the answer"):
        await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert calls["n"] == 1


async def test_confirmed_error_after_stream_commit_never_retries(router_stub):
    fake_query, calls = sdk_yielding(
        [
            InitMessage(),
            StreamEvent("partial"),
            AssistantMessage(
                "API Error: 402 Insufficient Balance", error="billing_error"
            ),
            RuntimeError("mid-stream wrapper failure"),
        ],
        [AssistantMessage("must not retry")],
    )

    with pytest.raises(RuntimeError, match="mid-stream"):
        await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert calls["n"] == 1


def test_upstream_error_diagnostic_is_bounded_and_masks_credentials():
    secret = "sk-1234567890abcdef"
    exc = rq.UpstreamAPIError(
        error_kind="billing_error",
        status_code=402,
        detail=(
            f"line one\napi_key={secret} https://token-value@example.test/path "
            + "x" * 500
        ),
    )

    assert "\n" not in exc.detail
    assert secret not in str(exc)
    assert "token-value" not in str(exc)
    assert "[REDACTED]" in str(exc)
    assert len(exc.detail) <= rq._MAX_UPSTREAM_DETAIL_CHARS


async def test_all_endpoints_failing_raises_typed_error(router_stub):
    fake_query, _ = sdk_yielding(
        [ConnectionError("primary down")],
        [ConnectionError("backup down")],
    )

    with pytest.raises(AllEndpointsUnavailable) as excinfo:
        await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    # Both causes must survive — with everything down, why is all that is left.
    assert "primary down" in str(excinfo.value)
    assert "backup down" in str(excinfo.value)
    assert [slot for slot, _ in excinfo.value.failures] == ["primary", "backup"]


async def test_falls_back_to_legacy_path_when_no_candidates(router_stub):
    """Routing off / nothing configured must behave exactly as before."""
    router_stub["candidates"] = []
    fake_query, calls = sdk_yielding([AssistantMessage()])

    out = await drain(prompt="p", make_options=OPTS, sdk_query=fake_query)

    assert len(out) == 1
    assert calls["options"] == [
        {"slot": None}
    ]  # build_options called with endpoint=None
    assert router_stub["outcomes"] == []  # nothing to attribute the call to
