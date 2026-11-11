"""Agent trace event protocol.

Defines the schema and helper utilities for streaming Claude Agent SDK
internal messages (assistant text, thinking, tool_use, tool_result,
system) out to the UI as a unified ``AgentTraceEvent`` stream.

Two transports consume this protocol:

- chat: ``LogAnalysisChatService`` pushes events into an in-process
  ``AgentJob.events`` buffer which the SSE ``_subscribe`` loop replays;
- celery / log detail: ``app/tasks/ai_analysis.py`` writes events into a
  Redis bounded list which a separate SSE endpoint reads.

Both transports MUST emit identical field names and semantics so the
frontend can render either source with the same component.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, TypedDict

logger = logging.getLogger(__name__)


# Regex to scrub token-injected URLs (e.g. https://abc123@host/repo.git)
# from any text that may flow into a trace event.
_TOKEN_URL_RE = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)
_GITLAB_TOKEN_RE = re.compile(r"\bglpat-[A-Za-z0-9_-]{8,}\b")
_MOONSHOT_KEY_RE = re.compile(r"<ak-[A-Za-z0-9_-]+>")


EventType = Literal[
    "run_start",
    "run_complete",
    "cancelled",
    "step_start",
    "step_delta",
    "step_end",
    "thinking_start",
    "thinking_delta",
    "thinking_end",
    "answer_delta",
    "system_notice",
    "error",
    "clarification_request",
    "clarification_resolved",
]


# Event type constants (importable so callers don't repeat string literals).
RUN_START = "run_start"
RUN_COMPLETE = "run_complete"
CANCELLED = "cancelled"
STEP_START = "step_start"
STEP_DELTA = "step_delta"
STEP_END = "step_end"
THINKING_START = "thinking_start"
THINKING_DELTA = "thinking_delta"
THINKING_END = "thinking_end"
ANSWER_DELTA = "answer_delta"
SYSTEM_NOTICE = "system_notice"
ERROR = "error"

# Clarification (AskUserQuestion) events. Declared here — not under a single
# agent — because clarification is cross-cutting: every chat-facing agent may
# pause and ask the user, so every transport must be able to carry these.
CLARIFICATION_REQUEST = "clarification_request"
CLARIFICATION_RESOLVED = "clarification_resolved"


# Default size cap for a single delta chunk (bytes of UTF-8 encoded text).
DEFAULT_CHUNK_MAX_BYTES = 4096

# Hard cap for output_excerpt on step_end / final text payloads.
DEFAULT_EXCERPT_MAX_BYTES = 4096


class AgentTraceEvent(TypedDict, total=False):
    """Unified trace event schema shared by all transports."""

    type: EventType
    task_id: str
    seq: int
    timestamp: float

    # step_* events
    step_id: str
    tool_name: str
    tool_input: Dict[str, Any]
    output_chunk: str
    output_excerpt: str
    status: Literal["ok", "error"]
    duration_seconds: float

    # thinking_* events (text_chunk also carries answer_delta increments)
    text_chunk: str
    text: str

    # system_notice events
    kind: str
    subtype: str
    detail: str

    # lifecycle: full enabled Agent/project catalog (not materialization proof)
    model: str
    provider: str
    executor: str
    requested_model: str
    agent_key: str
    available_skills: List[str]
    # Deprecated lifecycle alias for the filtered materialized subset.
    loaded_skills: List[str]

    # lifecycle: run_complete / cancelled / error
    trace_summary: Dict[str, Any]
    final_text: str
    error_kind: str
    message: str

    # clarification_request / clarification_resolved
    request_id: str
    questions: List[Dict[str, Any]]
    paused: bool
    publication_review: Dict[str, Any]
    outcome: str
    reason: str
    run_id: str
    session_id: str


class SeqCounter:
    """Thread-safe monotonic counter starting at 1.

    The agent loop is single-threaded but the emitter callback may be
    invoked from background threads (Celery worker, asyncio.to_thread
    bridge) — keep a lock so concurrent emit calls cannot duplicate seq
    values.
    """

    __slots__ = ("_value", "_lock")

    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    @property
    def value(self) -> int:
        return self._value


def mask_tokens(text: str) -> str:
    """Mask credentials in URLs, GitLab tokens and Moonshot error details."""
    text = _TOKEN_URL_RE.sub(r"\1***@", text)
    return _MOONSHOT_KEY_RE.sub("<***>", _GITLAB_TOKEN_RE.sub("***", text))


def runtime_metadata(events: Iterable[dict]) -> dict:
    """Read authoritative runtime notices from live or persisted traces."""
    metadata: dict = {}
    routes = []
    for event in events:
        if event.get("type") != SYSTEM_NOTICE or event.get("kind") not in {"raven_executor", "raven_route"}:
            continue
        for key in ("executor", "provider", "model", "requested_model"):
            if event.get(key):
                metadata[key] = event[key]
        if event.get("kind") == "raven_route":
            route = {key: event[key] for key in ("provider", "model") if event.get(key)}
            if route and (not routes or routes[-1] != route):
                routes.append(route)
    if metadata:
        metadata["engine"] = {"claude_sdk": "claude-agent-sdk", "antigravity_cli": "antigravity-cli",
                              "openai_agents": "openai-agents-sdk"}.get(metadata.get("executor"), "claude-agent-sdk")
        metadata["routes"] = routes
    return metadata


def mask_input(value: Any) -> Any:
    """Recursively mask token URLs in a tool input payload.

    Returns a JSON-serialisable structure (dict / list / str / number /
    bool / None). Non-serialisable values fall back to ``str(value)``.
    """
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return mask_tokens(value)
    if isinstance(value, dict):
        return {str(key): mask_input(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_input(item) for item in value]
    try:
        return mask_tokens(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return mask_tokens(str(value))


def coerce_chunk(text: str, max_bytes: int = DEFAULT_CHUNK_MAX_BYTES) -> List[str]:
    """Split a possibly large text into <= ``max_bytes`` UTF-8 chunks.

    Splits on UTF-8 codepoint boundaries so we never produce invalid
    sequences. Used to slice ``thinking_delta`` / ``step_delta`` payloads
    before they are pushed onto an SSE frame.
    """
    if not text:
        return []
    if max_bytes <= 0:
        return [text]

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return [text]

    chunks: List[str] = []
    start = 0
    n = len(encoded)
    while start < n:
        end = min(start + max_bytes, n)
        # Backtrack to a UTF-8 codepoint boundary. A continuation byte
        # has the top two bits set to 10, so walk back until we find a
        # byte that does NOT match 0b10xxxxxx (or we hit the start).
        while end < n and (encoded[end] & 0xC0) == 0x80 and end > start:
            end -= 1
        # If we ended up empty (extremely narrow max_bytes vs. multibyte
        # codepoint), force progress by taking one whole codepoint.
        if end == start:
            # Find next codepoint boundary going forward instead.
            end = start + 1
            while end < n and (encoded[end] & 0xC0) == 0x80:
                end += 1
        chunks.append(encoded[start:end].decode("utf-8", errors="replace"))
        start = end
    return chunks


def coerce_excerpt(text: str, max_bytes: int = DEFAULT_EXCERPT_MAX_BYTES) -> str:
    """Truncate ``text`` to at most ``max_bytes`` UTF-8 bytes.

    Used for ``step_end.output_excerpt`` and similar one-shot payloads.
    """
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    end = max_bytes
    # Backtrack to codepoint boundary.
    while end > 0 and (encoded[end] & 0xC0) == 0x80:
        end -= 1
    return encoded[:end].decode("utf-8", errors="replace")


def new_step_id() -> str:
    """Mint a UUIDv4 string used to correlate start/delta/end events."""
    return str(uuid.uuid4())


def extract_text_delta(event: Any) -> Optional[str]:
    """Extract an assistant answer-text increment from a raw SDK ``StreamEvent``.

    ``StreamEvent.event`` carries the native Anthropic stream event dict. The
    final answer body is streamed as ``content_block_delta`` events whose
    ``delta.type == "text_delta"``. We translate only those into
    ``answer_delta``; thinking increments (``thinking_delta``), tool-input
    increments (``input_json_delta``) and signature deltas are ignored here so
    ``answer_delta`` strictly mirrors the user-facing answer body.

    Returns the increment text, or ``None`` when ``event`` is not a text delta.
    """
    if not isinstance(event, dict):
        return None
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None
    if delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    if not isinstance(text, str) or not text:
        return None
    return text


def build_event(
    type: EventType,
    *,
    task_id: str,
    seq_counter: SeqCounter,
    timestamp: Optional[float] = None,
    **fields: Any,
) -> AgentTraceEvent:
    """Construct a trace event with bookkeeping fields auto-populated.

    Any value passed in ``fields`` that is a string is *not* masked
    automatically — callers are responsible for sanitising before they
    hand the data to ``build_event``, because the masking strategy
    depends on the field (tool_input vs. text_chunk vs. detail).
    """
    if timestamp is None:
        timestamp = round(time.time(), 6)
    event: AgentTraceEvent = {
        "type": type,
        "task_id": task_id,
        "seq": seq_counter.next(),
        "timestamp": timestamp,
    }
    # Drop None values to keep payloads small.
    for key, value in fields.items():
        if value is None:
            continue
        event[key] = value  # type: ignore[literal-required]
    return event


def safe_emit(
    emitter: Optional[Callable[[AgentTraceEvent], None]],
    event: AgentTraceEvent,
) -> None:
    """Invoke ``emitter`` with ``event``, swallowing any exception.

    Failure of a single emit MUST NOT bring down the agent loop. We log
    once at warning level so operators can still diagnose problems.

    Also bumps the ``ai_analysis_trace_events_emitted_total`` Prometheus
    counter — this is the single chokepoint through which every emitted
    event passes, regardless of transport (chat in-process buffer vs.
    Celery Redis buffer).
    """
    # Record the kind even if no emitter is wired — it's still a tracked
    # event in the agent's internal accumulator.
    try:
        from app.utils.metrics import record_trace_event_emitted

        record_trace_event_emitted(str(event.get("type") or "unknown"))
    except Exception:  # noqa: BLE001
        # Metrics MUST never break the agent loop.
        pass
    if emitter is None:
        return
    try:
        emitter(event)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "AgentTraceEvent emit failed: type=%s seq=%s err=%s",
            event.get("type"),
            event.get("seq"),
            exc,
        )


def derive_tool_trace(events: Iterable[AgentTraceEvent]) -> List[Dict[str, str]]:
    """Reconstruct the legacy ``tool_trace`` list from a stream of events.

    The old shape is ``[{name, input, output_excerpt}, ...]`` — one entry
    per tool invocation, in the order they started. We build it by
    indexing on ``step_id``: a ``step_start`` creates an entry,
    subsequent ``step_end`` updates the excerpt.
    """
    pending: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        step_id = event.get("step_id")
        if not isinstance(step_id, str):
            continue
        if etype == STEP_START:
            if step_id in pending:
                continue
            pending[step_id] = {
                "name": str(event.get("tool_name") or ""),
                "input": _legacy_input_string(event.get("tool_input")),
                "output_excerpt": "",
            }
            order.append(step_id)
        elif etype == STEP_END and step_id in pending:
            excerpt = event.get("output_excerpt") or ""
            if not isinstance(excerpt, str):
                excerpt = str(excerpt)
            pending[step_id]["output_excerpt"] = excerpt
    return [pending[sid] for sid in order if sid in pending]


def _legacy_input_string(value: Any) -> str:
    """Format a tool_input back into the legacy stringified shape."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def summarize(events: Iterable[AgentTraceEvent]) -> Dict[str, Any]:
    """Compute ``trace_summary`` from an event list.

    Counts every ``step_end`` as a completed tool call (regardless of
    status), sums every ``thinking_delta.text_chunk`` length toward
    ``thinking_chars``, and computes ``thought_duration_seconds`` as the
    span between the first and last event.
    """
    tool_calls = 0
    thinking_chars = 0
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    for event in events:
        if not isinstance(event, dict):
            continue
        ts = event.get("timestamp")
        if isinstance(ts, (int, float)):
            if first_ts is None or ts < first_ts:
                first_ts = float(ts)
            if last_ts is None or ts > last_ts:
                last_ts = float(ts)
        etype = event.get("type")
        if etype == STEP_END:
            tool_calls += 1
        elif etype == THINKING_DELTA:
            chunk = event.get("text_chunk") or ""
            if isinstance(chunk, str):
                thinking_chars += len(chunk)
        elif etype == THINKING_END:
            # If the agent emits a complete `text` on end but no deltas,
            # fall back to length of the final text.
            if thinking_chars == 0:
                text = event.get("text") or ""
                if isinstance(text, str):
                    thinking_chars += len(text)
    duration = 0.0
    if first_ts is not None and last_ts is not None and last_ts > first_ts:
        duration = round(last_ts - first_ts, 3)
    return {
        "thought_duration_seconds": duration,
        "tool_call_count": tool_calls,
        "thinking_chars": thinking_chars,
    }
