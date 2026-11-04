"""Endpoint-aware wrapper around the Claude Agent SDK ``query()`` loop.

Every agent drives the SDK the same way::

    async for message in query(prompt=..., options=build_options(...)):

This module replaces that with a loop that first picks an endpoint, and moves
to the next one if the chosen endpoint fails *or takes too long* before
producing any model output. It also measures time-to-first-token, which is the
signal :mod:`app.services.model_router` needs to notice a degraded endpoint at
all — the main app records only whole-run wall clock today, and a 20-minute log
analysis says nothing about how fast the gateway is answering.

**Three frame classes, and why the distinction matters.**

* ``SystemMessage(subtype="init")`` — the CLI subprocess is up, *before* any
  model round trip. Not a latency signal: it restarts the TTFT clock so
  subprocess spawn and MCP setup are not billed to the endpoint.
* Any other ``SystemMessage``-shaped frame (``subtype`` string **and** ``data``
  dict) — CLI bookkeeping such as ``status``, emitted immediately after ``init``
  and periodically while the model is still thinking. Also not model output.
  ``ResultMessage`` carries ``subtype`` but no ``data`` attribute, so the dict
  check keeps a bare terminal result on the committing side where it belongs.
* Everything else — normally ``StreamEvent`` (partial streaming) or
  ``AssistantMessage``. This is the first frame that required a model round
  trip, and tools only ever run in response to a ``tool_use`` block inside an
  ``AssistantMessage``. The one exception is a narrowly identified upstream API
  rejection: compatible gateways sometimes wrap ``API Error: 402 ...`` in an
  assistant-shaped frame. Those frames are quarantined until their terminal
  result confirms whether they are an endpoint failure or legitimate prose.

Getting that middle class wrong is not academic: treating ``status`` as model
output pins TTFT at ~0 ms for every run, so the router's window fills with
"fast" samples, the breaker never trips, and — because the attempt counts as
committed — a hard failure on the actual model call is re-raised instead of
failing over. Both failover paths are dead. Enumerating the *committing* shapes
instead would fail the other way (an unrecognised frame stalls the run), so the
classification is anchored on the SDK's own ``SystemMessage`` shape.

**Pre-model frames are forwarded immediately, not buffered.** Failover safety
only requires that no tool has run and no model output has reached the user;
a CLI status notice is neither. Holding them back would leave the browser with
a dead screen for the whole first-token wait, which is exactly the symptom this
module exists to prevent. The cost is that a preempted attempt has already
shown its ``init``/``status`` notices — so a switch emits
:class:`EndpointSwitchNotice` to explain the second set that follows.

**The first-token deadline.** ``model_router_slow_ttft_ms`` only labels a
sample after the token has already arrived; it cannot bound what the user
waits. ``model_router_first_token_deadline_ms`` does: once it expires the
attempt is abandoned and the next candidate starts immediately. It applies only
while uncommitted **and** only when another candidate exists — preempting the
last endpoint would leave the user with nothing, which is worse than slow.

Once a genuine model frame arrives the attempt is **committed**: the deadline is
disarmed, the rest of the stream passes through untouched, and no further
failover happens. Re-running a committed attempt would replay tool side effects
and pay for the tokens twice.

**Why the SDK generator is owned by a task.** Abandoning an attempt has to stop
the generator, and calling ``aclose()`` on it from the consumer while it is
suspended inside ``__anext__`` raises ``RuntimeError: aclose(): asynchronous
generator is already running`` — observed in production. Iterating inside a
dedicated task instead means cancellation is delivered at the generator's own
await point, so ``process_query``'s ``finally: await inner.aclose()`` →
``query.close()`` → ``transport.close()`` unwinds to completion *inside that
task* and the ``claude`` subprocess is reaped. Cleanup is then detached from
the switch itself, because ``transport.close()`` costs up to 10s (5s graceful +
5s SIGTERM) and the user must not wait for a corpse — but it is awaited before
this generator returns, since the workspace agents run under ``asyncio.run`` in
a Celery worker and the loop closes the instant the agent finishes.

The task also owns the SDK one frame at a time, pulling only when the consumer
grants a credit. Reading ahead would be observable: the agents poll
``cancel_event`` between frames, so a frame the SDK had already produced would
be dropped from the trace by a cancel that arrived after it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, AsyncIterator, Callable, List, Optional, Set, Tuple

from app.services import model_router
from app.services.model_router import (
    AllEndpointsUnavailable,
    EndpointChoice,
    OUTCOME_HARD_FAILURE,
    OUTCOME_OK,
    OUTCOME_TIMEOUT,
)

logger = logging.getLogger(__name__)

__all__ = [
    "routed_query",
    "AllEndpointsUnavailable",
    "EndpointChoice",
    "EndpointSwitchNotice",
    "UpstreamAPIError",
    "drain_cleanup_tasks",
]


def _now() -> float:
    """Monotonic clock, indirected so tests can drive TTFT deterministically.

    Patching ``time.monotonic`` itself would also retime the event loop.
    """
    return time.monotonic()


# Upper bound on how long an abandoned attempt may take to unwind. Only needs
# headroom over transport.close()'s 5s + 5s; it never delays the user because
# the wait happens in a detached task.
_CLEANUP_TIMEOUT_S = 30.0

# Strong references to detached cleanup tasks. asyncio holds only weak
# references to running tasks, so without this a cleanup can be garbage
# collected mid-unwind — orphaning the very subprocess it was reaping.
_CLEANUP_TASKS: Set["asyncio.Task[None]"] = set()

# Queue protocol between the pump task and the consumer.
_MSG = "msg"
_ERR = "err"
_END = "end"


# Exception *names* that mean "never reached the model" rather than "the model
# said no". Matched by name so this module does not import httpx/anyio just to
# classify; substring matching keeps it robust across SDK transport wrappers.
_HARD_FAILURE_MARKERS = (
    "connect",
    "connection",
    "timeout",
    "dns",
    "unauthorized",
    "authentication",
)

_UPSTREAM_ERROR_KINDS = frozenset(
    {
        "authentication_failed",
        "billing_error",
        "rate_limit",
        "invalid_request",
        "server_error",
        "unknown",
    }
)
_API_ERROR_TEXT_RE = re.compile(
    r"\A\s*API\s+Error:\s*(?P<status>[1-5]\d{2})\s+(?P<detail>\S(?:.*\S)?)\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
_TOKEN_URL_RE = re.compile(r"https?://[^@\s/]+@", re.IGNORECASE)
_NAMED_SECRET_RE = re.compile(
    r"(?i)\b(authorization|x-api-key|api[_-]?key)\b\s*[:=]\s*"
    r"(?:bearer\s+)?[^\s,;]+"
)
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_MAX_UPSTREAM_DETAIL_CHARS = 300


def _sanitize_upstream_detail(value: Any) -> str:
    """Return a single-line, credential-masked provider detail for diagnostics."""
    detail = " ".join(str(value or "").split())
    detail = _TOKEN_URL_RE.sub("https://***@", detail)
    detail = _NAMED_SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", detail)
    detail = _SK_TOKEN_RE.sub("[REDACTED]", detail)
    if len(detail) > _MAX_UPSTREAM_DETAIL_CHARS:
        detail = detail[: _MAX_UPSTREAM_DETAIL_CHARS - 3].rstrip() + "..."
    return detail


def _kind_for_status(status_code: Optional[int]) -> str:
    if status_code in {401, 403}:
        return "authentication_failed"
    if status_code == 402:
        return "billing_error"
    if status_code == 429:
        return "rate_limit"
    if status_code is not None and 400 <= status_code < 500:
        return "invalid_request"
    if status_code is not None and status_code >= 500:
        return "server_error"
    return "unknown"


class UpstreamAPIError(RuntimeError):
    """An endpoint rejected the request before genuine model output.

    Only normalized and sanitized fields are retained so aggregate router errors
    remain actionable without turning provider payloads into an unbounded or
    credential-bearing log surface.
    """

    def __init__(
        self,
        *,
        error_kind: str,
        status_code: Optional[int] = None,
        detail: str = "",
    ) -> None:
        normalized_kind = str(error_kind or "unknown").strip().lower()
        self.error_kind = (
            normalized_kind if normalized_kind in _UPSTREAM_ERROR_KINDS else "unknown"
        )
        self.status_code = status_code if isinstance(status_code, int) else None
        self.detail = _sanitize_upstream_detail(detail) or self.error_kind.replace(
            "_", " "
        )
        status = f", status={self.status_code}" if self.status_code is not None else ""
        super().__init__(
            f"upstream API rejected request (kind={self.error_kind}{status}, "
            f"detail={self.detail})"
        )


def _only_assistant_text(message: Any) -> Optional[str]:
    """Return text only for a one-block, text-only assistant-shaped frame."""
    content = getattr(message, "content", None)
    if not isinstance(content, (list, tuple)) or len(content) != 1:
        return None
    block = content[0]
    if isinstance(block, dict):
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            return None
        return block["text"]
    text = getattr(block, "text", None)
    return text if isinstance(text, str) else None


def _match_api_error_text(value: Any) -> Optional[Tuple[int, str]]:
    if not isinstance(value, str):
        return None
    matched = _API_ERROR_TEXT_RE.fullmatch(value)
    if matched is None:
        return None
    return int(matched.group("status")), matched.group("detail")


def _upstream_error_candidate(
    message: Any,
) -> Optional[Tuple[UpstreamAPIError, bool]]:
    """Extract an upstream rejection and whether the frame confirms it.

    Structured SDK fields are authoritative. The legacy assistant-text form is
    only a candidate until a terminal/error frame follows; that quarantine lets
    a model legitimately output the exact sentinel without being discarded.
    """
    raw_kind = getattr(message, "error", None)
    kind = (
        str(raw_kind).strip().lower()
        if isinstance(raw_kind, str) and raw_kind.strip()
        else None
    )
    raw_status = getattr(message, "api_error_status", None)
    structured_status = raw_status if isinstance(raw_status, int) else None
    status = structured_status

    text = _only_assistant_text(message)
    match = _match_api_error_text(text)

    result = getattr(message, "result", None)
    result_match = _match_api_error_text(result)
    result_is_error = getattr(message, "is_error", None)
    if (
        result_match is not None
        and result_is_error is False
        and kind is None
        and status is None
    ):
        # A successful terminal result can legitimately contain this exact prose.
        # It disproves a quarantined legacy candidate instead of confirming it.
        result_match = None
    if match is None and result_match is not None:
        match = result_match
        text = result

    errors = getattr(message, "errors", None)
    if match is None and isinstance(errors, (list, tuple)):
        for item in errors:
            error_match = _match_api_error_text(item)
            if error_match is not None:
                match = error_match
                text = item
                break

    if match is not None:
        matched_status, matched_detail = match
        status = status or matched_status
        detail = matched_detail
    else:
        detail = text or (result if isinstance(result, str) else "")

    if kind is not None or structured_status is not None:
        return (
            UpstreamAPIError(
                error_kind=kind or _kind_for_status(status),
                status_code=status,
                detail=detail,
            ),
            True,
        )

    if match is None:
        return None

    # A terminal result or errors[] entry confirms the legacy assistant-shaped
    # candidate. Assistant text alone remains quarantined but unconfirmed.
    confirmed = result_is_error is True or bool(errors)
    return (
        UpstreamAPIError(
            error_kind=_kind_for_status(status),
            status_code=status,
            detail=detail,
        ),
        confirmed,
    )


def _classify(exc: BaseException) -> str:
    if isinstance(exc, UpstreamAPIError):
        return OUTCOME_HARD_FAILURE
    name = f"{type(exc).__name__} {exc}".lower()
    if any(marker in name for marker in _HARD_FAILURE_MARKERS):
        return OUTCOME_HARD_FAILURE
    return OUTCOME_HARD_FAILURE if isinstance(exc, OSError) else "error"


def _is_init_frame(message: Any) -> bool:
    """``SystemMessage(subtype='init')`` — CLI is up, model not yet contacted."""
    return getattr(message, "subtype", None) == "init" and isinstance(
        getattr(message, "data", None), dict
    )


def _is_pre_model_frame(message: Any) -> bool:
    """Any ``SystemMessage``-shaped frame: CLI bookkeeping, never model output.

    ``ResultMessage`` also carries ``subtype`` but has no ``data`` attribute,
    so the dict check leaves a bare terminal result on the committing side.
    """
    return isinstance(getattr(message, "subtype", None), str) and isinstance(
        getattr(message, "data", None), dict
    )


class EndpointSwitchNotice:
    """Synthetic frame announcing a failover, shaped like a ``SystemMessage``.

    Agents dispatch on duck-typed attributes (``subtype`` plus a ``data`` dict),
    so this renders as an ordinary ``system_notice`` with no agent-side change.
    ``content`` and ``event`` are ``None`` so it can never be mistaken for model
    output, and it carries no ``result`` so it cannot terminate a run.

    It exists because pre-model frames are forwarded rather than buffered: the
    browser has already seen the abandoned attempt's ``init``/``status``, and a
    second set arriving unexplained would read as a glitch.
    """

    content = None
    event = None
    subtype = "endpoint_switch"

    def __init__(
        self,
        *,
        from_slot: str,
        to_slot: str,
        reason: str,
        waited_ms: int,
        from_key_id: Optional[str] = None,
        to_key_id: Optional[str] = None,
    ) -> None:
        self.data = {
            "from_slot": from_slot,
            "to_slot": to_slot,
            "reason": reason,
            "waited_ms": waited_ms,
            "message": (
                f"主力端点 {waited_ms} ms 未返回首个 token，已切换到备用端点继续"
                if reason == "first_token_deadline"
                else (
                    "主力 API Key 达到 RPM 限制，已轮换另一个 Key 继续"
                    if reason == "rate_limit" and from_slot == to_slot
                    else f"端点 {from_slot} 不可用（{reason}），已切换到 {to_slot} 继续"
                )
            ),
        }
        if from_key_id:
            self.data["from_key_id"] = from_key_id
        if to_key_id:
            self.data["to_key_id"] = to_key_id


def _deadline_seconds() -> float:
    """First-token deadline in seconds; ``0`` disables preemption."""
    from app.config import settings

    try:
        raw = int(settings.model_router_first_token_deadline_ms)
    except Exception:  # noqa: BLE001 — a bad knob must not break routing
        return 0.0
    return raw / 1000.0 if raw > 0 else 0.0


async def _pump(
    agen: Any,
    queue: "asyncio.Queue[Tuple[str, Any]]",
    credits: "asyncio.Queue[None]",
) -> None:
    """Own the SDK generator; the consumer only ever reads the queue.

    Keeping iteration inside a task is what makes preemption safe — see the
    module docstring on ``aclose()`` racing a suspended frame.

    One frame is pulled per credit, and the consumer grants a credit only when
    it wants the next one. That reproduces a plain ``async for`` exactly: the
    SDK stays suspended for as long as the consumer is working. Letting the
    pump read ahead instead is observable — the agents poll ``cancel_event``
    between frames, so a frame the SDK had already produced would be dropped
    from the trace by a cancel that arrived after it.
    """
    iterator = agen.__aiter__()
    try:
        while True:
            await credits.get()
            try:
                message = await iterator.__anext__()
            except StopAsyncIteration:
                break
            await queue.put((_MSG, message))
    except asyncio.CancelledError:
        # Abandonment. Let it unwind the generator's finally chain here, in the
        # task that owns the frame, rather than converting it into a queued
        # error the consumer would mistake for an endpoint failure.
        raise
    except BaseException as exc:  # noqa: BLE001
        await queue.put((_ERR, exc))
    else:
        await queue.put((_END, None))


async def _close_attempt(
    agen: Any,
    pump: "asyncio.Task[None]",
    *,
    slot: str,
    agent_kind: str,
    reason: str,
) -> None:
    """Tear down an abandoned attempt. Never raises.

    Cancels the pump exactly once — a second cancel would interrupt the
    generator's ``finally`` mid-teardown and strand the subprocess — then waits
    for the unwind before touching ``aclose()``, by which point the frame is no
    longer running and the call is a safe no-op.
    """
    if not pump.done():
        pump.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(pump, return_exceptions=True),
            timeout=_CLEANUP_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.error(
            "routed_query: abandoned attempt slot=%s (agent_kind=%s, reason=%s) did "
            "not unwind within %.0fs — the claude subprocess may be orphaned",
            slot,
            agent_kind,
            reason,
            _CLEANUP_TIMEOUT_S,
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("routed_query: cleanup wait failed slot=%s: %s", slot, exc)

    aclose = getattr(agen, "aclose", None)
    if aclose is None:
        return
    try:
        await asyncio.wait_for(aclose(), timeout=_CLEANUP_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "routed_query: aclose failed slot=%s (agent_kind=%s): %s",
            slot,
            agent_kind,
            exc,
        )


def _detach_cleanup(
    agen: Any,
    pump: "asyncio.Task[None]",
    *,
    slot: str,
    agent_kind: str,
    reason: str,
) -> "asyncio.Task[None]":
    """Reap an abandoned attempt off the request path."""
    task = asyncio.ensure_future(
        _close_attempt(agen, pump, slot=slot, agent_kind=agent_kind, reason=reason)
    )
    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_CLEANUP_TASKS.discard)
    return task


async def drain_cleanup_tasks(timeout: float = _CLEANUP_TIMEOUT_S) -> None:
    """Wait for detached attempt teardowns to finish. Never raises.

    Preempted attempts are reaped off the request path, so at any moment there
    may be a ``claude`` subprocess still being torn down. Call this from a
    graceful-shutdown hook to reap them before the loop closes; tests use it to
    make abandonment deterministic.
    """
    pending = [task for task in list(_CLEANUP_TASKS) if not task.done()]
    if not pending:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            "routed_query: %d abandoned attempt(s) still tearing down after %.0fs",
            len(pending),
            timeout,
        )


async def routed_query(
    *,
    prompt: Any,
    make_options: Callable[[Optional[EndpointChoice]], Any],
    agent_kind: str = "",
    require_mcp: bool = False,
    require_image: bool = False,
    require_document: bool = False,
    require_small_fast: bool = False,
    on_endpoint: Optional[Callable[[EndpointChoice], None]] = None,
    sdk_query: Optional[Callable[..., AsyncIterator[Any]]] = None,
    candidates: Optional[List[EndpointChoice]] = None,
) -> AsyncIterator[Any]:
    """Drive ``query()`` against the best available endpoint, failing over on
    error or on a first-token deadline.

    ``make_options`` is called once per candidate — options must be rebuilt, not
    reused, because capability gating (MCP tools, system prompt selection) is
    baked into them per provider. ``on_endpoint`` fires as soon as a candidate is
    chosen and before any message is yielded, so callers can record which
    endpoint actually served the run in their own telemetry; on a switch it
    fires again with the new endpoint.

    ``candidates`` accepts an already-resolved list from
    :func:`app.services.model_router.candidates`. Agents that emit a ``run_start``
    trace event naming the model must resolve the endpoint *before* that event
    goes out — it is already on the wire to the browser by the time the first
    message arrives — so they select first and hand the result in here.
    """
    if sdk_query is None:
        from app.agents.provider_query import query as sdk_query  # type: ignore[no-redef]

    choices = (
        candidates
        if candidates is not None
        else model_router.candidates(
            agent_kind=agent_kind,
            require_mcp=require_mcp,
            require_image=require_image,
            require_document=require_document,
            require_small_fast=require_small_fast,
        )
    )
    if not choices:
        # No routed endpoint is usable (routing off, or nothing configured).
        # Fall through to the legacy path so behaviour is unchanged.
        choices = [None]  # type: ignore[list-item]

    failures: List[Tuple[str, BaseException]] = []
    rate_limit_key_retry_used = False
    deadline = _deadline_seconds()
    # Teardowns this call started. Reaped before we hand control back, because
    # the workspace agents run under ``asyncio.run`` in a Celery worker — the
    # loop is closed the moment the agent returns, and a still-pending cleanup
    # would be cancelled with the `claude` subprocess left behind.
    detached: List["asyncio.Task[None]"] = []

    try:
        for index, choice in enumerate(choices):
            slot = choice.slot if choice is not None else "settings"
            has_fallback = index < len(choices) - 1
            # Preempting the last candidate would leave the user with nothing —
            # slow output beats no output, so the deadline only arms when there is
            # somewhere to go.
            attempt_deadline = deadline if (has_fallback and deadline > 0) else 0.0

            options = make_options(choice)
            if choice is not None and on_endpoint is not None:
                on_endpoint(choice)

            queue: "asyncio.Queue[Tuple[str, Any]]" = asyncio.Queue(maxsize=1)
            credits: "asyncio.Queue[None]" = asyncio.Queue()
            agen = sdk_query(prompt=prompt, options=options)
            pump: "asyncio.Task[None]" = asyncio.ensure_future(
                _pump(agen, queue, credits)
            )

            started = _now()
            committed = False
            preempted = False
            abandoned_failure = False
            stream_error: Optional[BaseException] = None
            pending_api_error: Optional[UpstreamAPIError] = None
            pending_api_error_confirmed = False
            pending_error_frames: List[Any] = []
            pending_ttft_ms: Optional[int] = None
            live = True  # pump still owns an un-drained generator

            try:
                while True:
                    # Ask for exactly one frame, then wait for it. Granting the
                    # credit here rather than eagerly is what keeps the SDK from
                    # reading ahead of the consumer.
                    credits.put_nowait(None)

                    timeout: Optional[float] = None
                    if not committed and attempt_deadline > 0:
                        timeout = attempt_deadline - (_now() - started)
                        if timeout <= 0:
                            if pending_api_error is not None:
                                stream_error = pending_api_error
                                abandoned_failure = has_fallback
                            else:
                                preempted = True
                            break
                    try:
                        if timeout is None:
                            kind, payload = await queue.get()
                        else:
                            kind, payload = await asyncio.wait_for(queue.get(), timeout)
                    except asyncio.TimeoutError:
                        if pending_api_error is not None:
                            # A legacy assistant-shaped rejection arrived, then
                            # its terminal confirmation stalled. It is still not
                            # genuine output; preserve the useful captured cause.
                            stream_error = pending_api_error
                            abandoned_failure = has_fallback
                        else:
                            preempted = True
                        break

                    if kind == _ERR:
                        # The SDK often follows a useful `API Error: 402 ...`
                        # frame with `error result: success`. Keep the former.
                        stream_error = pending_api_error or payload
                        live = False
                        break
                    if kind == _END:
                        if pending_api_error is not None:
                            stream_error = pending_api_error
                        live = False
                        break

                    message = payload
                    if not committed:
                        if _is_init_frame(message):
                            # CLI startup, not model latency — restart both the TTFT
                            # clock and the deadline so process spawn and MCP setup
                            # are not charged to the endpoint.
                            started = _now()
                            yield message
                            continue
                        if _is_pre_model_frame(message):
                            # Bookkeeping (``status`` and friends). Forward it so the
                            # browser keeps ticking, but do not touch the clock: a
                            # keepalive arriving mid-wait must not reset the deadline.
                            yield message
                            continue

                        candidate = _upstream_error_candidate(message)
                        if candidate is not None:
                            api_error, confirmed = candidate
                            if pending_api_error is None:
                                pending_ttft_ms = int((_now() - started) * 1000)
                            # Prefer a later structured/terminal confirmation over
                            # an earlier text-only candidate.
                            if pending_api_error is None or confirmed:
                                pending_api_error = api_error
                            pending_api_error_confirmed = (
                                pending_api_error_confirmed or confirmed
                            )
                            pending_error_frames.append(message)
                            if pending_api_error_confirmed:
                                stream_error = pending_api_error
                                abandoned_failure = has_fallback
                                break
                            continue

                        if pending_api_error is not None:
                            if pending_api_error_confirmed:
                                # Structured metadata is authoritative even if a
                                # malformed gateway sends another frame afterward.
                                stream_error = pending_api_error
                                abandoned_failure = has_fallback
                                break

                            # The next normal frame disproves a legacy text-only
                            # candidate. Commit at the candidate's original TTFT
                            # and release every quarantined frame in order.
                            committed = True
                            ttft_ms = pending_ttft_ms or 0
                            if choice is not None:
                                model_router.record_outcome(
                                    slot, outcome=OUTCOME_OK, ttft_ms=ttft_ms
                                )
                            logger.info(
                                "routed_query: committed slot=%s key_id=%s pool_size=%d "
                                "agent_kind=%s ttft_ms=%d",
                                slot,
                                choice.api_key_id if choice is not None else "settings",
                                choice.api_key_count if choice is not None else 1,
                                agent_kind,
                                ttft_ms,
                            )
                            for pending_message in pending_error_frames:
                                yield pending_message
                            pending_error_frames.clear()
                            pending_api_error = None
                            yield message
                            continue

                        committed = True
                        ttft_ms = int((_now() - started) * 1000)
                        if choice is not None:
                            model_router.record_outcome(
                                slot, outcome=OUTCOME_OK, ttft_ms=ttft_ms
                            )
                        logger.info(
                            "routed_query: committed slot=%s key_id=%s pool_size=%d "
                            "agent_kind=%s ttft_ms=%d",
                            slot,
                            choice.api_key_id if choice is not None else "settings",
                            choice.api_key_count if choice is not None else 1,
                            agent_kind,
                            ttft_ms,
                        )
                    yield message
            finally:
                # Reached on every exit from the loop, including the caller closing
                # us mid-stream (cancel, break, throw). ``live`` is already False on
                # the error/end paths, where the generator finalised itself.
                if live and not pump.done():
                    if preempted or abandoned_failure:
                        # Someone is waiting on the next endpoint — reap in the
                        # background so the switch is instant, and remember the
                        # task so this call does not return before it is done.
                        detached.append(
                            _detach_cleanup(
                                agen,
                                pump,
                                slot=slot,
                                agent_kind=agent_kind,
                                reason=(
                                    "upstream_api_error"
                                    if abandoned_failure
                                    else "preempt"
                                ),
                            )
                        )
                    else:
                        # Caller walked away. Await inline: a Celery worker's loop
                        # is often torn down right after, and ``asyncio.run`` would
                        # cancel a detached task before it could reap the
                        # subprocess. If our own task is being cancelled the await
                        # cannot finish — fall back to detaching, best effort.
                        try:
                            await _close_attempt(
                                agen,
                                pump,
                                slot=slot,
                                agent_kind=agent_kind,
                                reason="caller_closed",
                            )
                        except asyncio.CancelledError:
                            _detach_cleanup(
                                agen,
                                pump,
                                slot=slot,
                                agent_kind=agent_kind,
                                reason="caller_cancelled",
                            )
                            raise
                    live = False

            if preempted:
                waited_ms = int((_now() - started) * 1000)
                if choice is not None:
                    model_router.record_outcome(slot, outcome=OUTCOME_TIMEOUT)
                failures.append(
                    (slot, asyncio.TimeoutError(f"no first token in {waited_ms}ms"))
                )
                logger.warning(
                    "routed_query: slot=%s no first token within %dms (agent_kind=%s) — "
                    "abandoning and switching to the next endpoint",
                    slot,
                    waited_ms,
                    agent_kind,
                )
                next_slot = choices[index + 1]
                yield EndpointSwitchNotice(
                    from_slot=slot,
                    to_slot=next_slot.slot if next_slot is not None else "settings",
                    reason="first_token_deadline",
                    waited_ms=waited_ms,
                )
                continue

            if stream_error is not None:
                if committed:
                    # Mid-stream failure: the run is already partly done and the
                    # caller owns the error. Retrying would replay tool side
                    # effects and re-bill the tokens.
                    raise stream_error
                outcome = _classify(stream_error)
                alternate_choice: Optional[EndpointChoice] = None
                if (
                    choice is not None
                    and isinstance(stream_error, UpstreamAPIError)
                    and stream_error.error_kind == "rate_limit"
                    and not rate_limit_key_retry_used
                ):
                    alternate_choice = model_router.alternate_api_key(choice)
                    if alternate_choice is not None:
                        choices.insert(index + 1, alternate_choice)
                        rate_limit_key_retry_used = True
                        logger.warning(
                            "routed_query: key-local rate limit slot=%s key_id=%s; "
                            "retrying once with key_id=%s before backup",
                            slot,
                            choice.api_key_id,
                            alternate_choice.api_key_id,
                        )
                if choice is not None and alternate_choice is None:
                    model_router.record_outcome(slot, outcome=outcome)
                failures.append((slot, stream_error))
                remaining = len(choices) - index - 1
                logger.warning(
                    "routed_query: slot=%s failed before first token (agent_kind=%s, "
                    "outcome=%s): %s%s",
                    slot,
                    agent_kind,
                    outcome,
                    stream_error,
                    f" — trying {remaining} more endpoint(s)" if remaining else "",
                )
                if remaining:
                    next_slot = choices[index + 1]
                    reason = (
                        stream_error.error_kind
                        if isinstance(stream_error, UpstreamAPIError)
                        else outcome
                    )
                    yield EndpointSwitchNotice(
                        from_slot=slot,
                        to_slot=next_slot.slot if next_slot is not None else "settings",
                        reason=reason,
                        waited_ms=int((_now() - started) * 1000),
                        from_key_id=(choice.api_key_id if choice is not None else None),
                        to_key_id=(
                            next_slot.api_key_id if next_slot is not None else None
                        ),
                    )
                continue

            if not committed:
                # The stream ended without any model output — a refusal or a bare
                # terminal result. The upstream did answer (the generator finished
                # cleanly), so this is not an endpoint failure.
                if choice is not None:
                    model_router.record_outcome(slot, outcome=OUTCOME_OK)
                logger.info(
                    "routed_query: slot=%s produced no assistant message (agent_kind=%s)",
                    slot,
                    agent_kind,
                )
            return

        raise AllEndpointsUnavailable(failures)
    finally:
        # Reap teardowns this call started. The workspace agents run under
        # ``asyncio.run`` inside a Celery worker, so the loop is closed the
        # moment the agent returns — a still-pending cleanup would be
        # cancelled with the ``claude`` subprocess left behind. Only runs
        # that actually preempted pay for this, and by then the answer has
        # already been streamed to the user.
        if detached:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*detached, return_exceptions=True),
                    timeout=_CLEANUP_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "routed_query: %d abandoned attempt(s) still tearing down",
                    len(detached),
                )
            except asyncio.CancelledError:
                # Our own task is going away; the detached tasks stay
                # registered in _CLEANUP_TASKS for drain_cleanup_tasks().
                raise
