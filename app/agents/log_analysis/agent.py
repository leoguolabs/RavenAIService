"""
Claude Agent SDK 日志分析 Agent。

主入口:
  LogAnalysisAgent().run(ctx)      — async, returns dict
  LogAnalysisAgent().run_sync(ctx) — sync wrapper for Celery

两个入口都可选注入 ``trace_emitter``，在 SDK loop 内部按消息粒度向外推送
``AgentTraceEvent`` (见 ``trace.py``)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:  # avoid importing the SDK-dependent module at import time
    from app.agents.clarification import ClarificationBinding

from app.agents.log_analysis.trace import (
    AgentTraceEvent,
    ANSWER_DELTA,
    CANCELLED,
    DEFAULT_CHUNK_MAX_BYTES,
    DEFAULT_EXCERPT_MAX_BYTES,
    ERROR,
    RUN_COMPLETE,
    RUN_START,
    STEP_DELTA,
    STEP_END,
    STEP_START,
    SYSTEM_NOTICE,
    THINKING_DELTA,
    THINKING_END,
    THINKING_START,
    SeqCounter,
    build_event,
    coerce_chunk,
    coerce_excerpt,
    derive_tool_trace,
    extract_text_delta,
    mask_input,
    mask_tokens,
    new_step_id,
    safe_emit,
    summarize,
)
from app.agents.usage import accumulate_usage, new_token_usage
from app.agents.log_analysis.workspace import WorkspaceContext
from app.agents.log_analysis.result_normalization import (
    DuplicateResultField as _DuplicateResultField,
    parse_complete_analysis_result,
    unique_json_object as _unique_json_object,
)
from app.agents.log_analysis.mcp_tools import (
    PROJECT_DISCOVERY_MCP_TOOL,
    PROJECT_REPO_CLONE_MCP_TOOL,
    PROJECT_REPO_LOOKUP_MCP_TOOL,
    build_project_fit_guidance,
)

logger = logging.getLogger(__name__)


class AgentCancelled(Exception):
    """Raised inside the agent loop when an external cancel signal fires."""


PROJECT_REPO_MCP_TOOL = PROJECT_REPO_LOOKUP_MCP_TOOL

ALLOWED_TOOLS = [
    "Bash",
    "Read",
    "Grep",
    "Glob",
    "Skill",  # 允许模型调用通过 setting_sources 加载的用户自定义 Skill
    PROJECT_DISCOVERY_MCP_TOOL,
    PROJECT_REPO_MCP_TOOL,
    PROJECT_REPO_CLONE_MCP_TOOL,
]

# Agent 唯一键，与 skills_service.SUPPORTED_AGENTS 对应
AGENT_KEY = "log_analysis"


TraceEmitter = Callable[[AgentTraceEvent], None]


def _parse_partial_array(text: str, start: int, decoder: json.JSONDecoder) -> tuple[List[Any], int]:
    """Parse completed items from a possibly truncated JSON array."""
    items: List[Any] = []
    i = start + 1
    length = len(text)
    while i < length:
        while i < length and text[i].isspace():
            i += 1
        if i >= length:
            break
        if text[i] == "]":
            return items, i + 1
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        items.append(value)
        i = end
        while i < length and text[i].isspace():
            i += 1
        if i < length and text[i] == ",":
            i += 1
            continue
        if i < length and text[i] == "]":
            return items, i + 1
        break
    return items, i


_RESULT_FIELD_NAMES = (
    "status",
    "error_kind",
    "question_type",
    "answer",
    "summary",
    "severity",
    "root_cause_hypotheses",
    "recommended_actions",
    "related_keywords",
)
_RESULT_FIELD_DELIMITER_RE = re.compile(
    r'(?<!\\)"\s*,\s*"(?P<field>' + "|".join(_RESULT_FIELD_NAMES) + r')"\s*:'
)


def _is_complete_json_value_boundary(text: str, index: int) -> bool:
    """Return whether ``index`` sits after a complete top-level JSON value."""
    length = len(text)
    while index < length and text[index].isspace():
        index += 1
    return index >= length or text[index] in ",}" or text.startswith("```", index)


def _decode_lenient_json_string(raw: str) -> str:
    """Decode JSON string content while tolerating unescaped quotes.

    Grounded answers often quote source terms (for example ``"重构"``). If a
    provider forgets to escape those quotes inside the final JSON string, the
    standard decoder stops at the first one. This keeps normal JSON escapes but
    treats bare quotes as literal content.
    """
    chars: List[str] = []
    i = 0
    length = len(raw)
    while i < length:
        ch = raw[i]
        if ch != "\\":
            chars.append(ch)
            i += 1
            continue

        if i + 1 >= length:
            chars.append("\\")
            i += 1
            continue

        nxt = raw[i + 1]
        if nxt in {'"', "\\", "/"}:
            chars.append(nxt)
            i += 2
        elif nxt == "b":
            chars.append("\b")
            i += 2
        elif nxt == "f":
            chars.append("\f")
            i += 2
        elif nxt == "n":
            chars.append("\n")
            i += 2
        elif nxt == "r":
            chars.append("\r")
            i += 2
        elif nxt == "t":
            chars.append("\t")
            i += 2
        elif nxt == "u" and i + 5 < length:
            code = raw[i + 2 : i + 6]
            try:
                chars.append(chr(int(code, 16)))
                i += 6
            except ValueError:
                chars.extend(["\\", nxt])
                i += 2
        else:
            chars.extend(["\\", nxt])
            i += 2
    return "".join(chars)


def _parse_string_value_prefix(
    text: str,
    start: int,
    decoder: json.JSONDecoder,
) -> tuple[Optional[str], int]:
    """Parse a top-level JSON string value with a lenient fallback."""
    try:
        value, end = decoder.raw_decode(text, start)
        if isinstance(value, str) and _is_complete_json_value_boundary(text, end):
            return value, end
    except json.JSONDecodeError:
        pass

    # If a quoted answer contains an unescaped quote, find the closing quote by
    # looking for the next known top-level result field (", "summary":, etc.).
    for match in _RESULT_FIELD_DELIMITER_RE.finditer(text, start + 1):
        raw = text[start + 1 : match.start()]
        return _decode_lenient_json_string(raw), match.start() + 1
    return None, start


def _parse_json_object_prefix(text: str) -> Optional[Dict[str, Any]]:
    try:
        return _parse_unique_json_object_prefix(text)
    except _DuplicateResultField:
        # Retain the raw terminal response for diagnosis, but never pick an
        # earlier/later answer from a corrupt response and report success.
        return None


def _parse_unique_json_object_prefix(text: str) -> Optional[Dict[str, Any]]:
    """Parse a JSON object, keeping complete top-level members if truncated.

    Claude-compatible providers occasionally stop after emitting a large JSON
    prefix. The answer string is often already complete, while later arrays are
    cut mid-field. This helper keeps only syntactically complete members so the
    UI can still show the analysis without treating arbitrary broken JSON as
    valid.
    """
    body = (text or "").strip()
    if not body:
        return None
    try:
        parsed = json.loads(body, object_pairs_hook=_unique_json_object)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder(object_pairs_hook=_unique_json_object)
    i = 0
    length = len(body)
    while i < length and body[i].isspace():
        i += 1
    if i >= length or body[i] != "{":
        return None
    i += 1
    result: Dict[str, Any] = {}

    while i < length:
        while i < length and body[i].isspace():
            i += 1
        if i >= length:
            break
        if body[i] == "}":
            return result
        try:
            key, end = decoder.raw_decode(body, i)
        except json.JSONDecodeError:
            break
        if not isinstance(key, str):
            break
        if key in result:
            raise _DuplicateResultField(key)
        i = end
        while i < length and body[i].isspace():
            i += 1
        if i >= length or body[i] != ":":
            break
        i += 1
        while i < length and body[i].isspace():
            i += 1
        if i >= length:
            break
        if body[i] == '"':
            value, end = _parse_string_value_prefix(body, i, decoder)
            if value is None:
                break
            result[key] = value
            i = end
        else:
            try:
                value, end = decoder.raw_decode(body, i)
                result[key] = value
                i = end
            except json.JSONDecodeError:
                if body[i] == "[":
                    items, end = _parse_partial_array(body, i, decoder)
                    if items:
                        result[key] = items
                        i = end
                break
        while i < length and body[i].isspace():
            i += 1
        if i < length and body[i] == ",":
            i += 1
            continue
        if i < length and body[i] == "}":
            return result
        break

    return result or None


def _extract_fenced_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract a fenced result or a complete, bare analysis JSON object.

    If the model emitted an opening fence but was truncated before the closing
    fence, parse the completed JSON prefix as a best-effort recovery. A provider
    can omit the fence without changing the JSON payload; do not let the plain
    Skill-answer fallback wrap that payload and discard its structured fields.
    Bare objects must be complete and recognizable as an analysis result.
    """
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        return parse_complete_analysis_result(stripped)
    m = re.search(r"```json\s*", text or "")
    if not m:
        return None
    return _parse_json_object_prefix((text or "")[m.end():])


def _validate_result_schema(data: Dict[str, Any]) -> bool:
    # `answer` and `question_type` are part of the v3 schema. They are
    # checked softly (the agent is instructed to emit them, but legacy
    # responses without them are still accepted so we can fall back to
    # `summary`).
    if not isinstance(data.get("status"), str):
        return False
    if not isinstance(data.get("severity"), str):
        return False
    return bool(data.get("answer") or data.get("summary"))


def extract_recoverable_result_fields(text: str) -> Dict[str, Any]:
    """Best-effort extraction for model outputs with a truncated JSON tail.

    The model contract is still fenced JSON, but some providers occasionally
    end a turn after emitting a valid prefix. When the important user-facing
    fields are already complete, keep them instead of surfacing raw broken JSON.
    """
    parsed = _extract_fenced_json(text)
    if not isinstance(parsed, dict):
        return {}

    answer = parsed.get("answer")
    summary = parsed.get("summary")
    if not isinstance(answer, str) or not answer.strip():
        answer = summary if isinstance(summary, str) else ""
    if not isinstance(summary, str):
        summary = ""
    if not str(answer or summary).strip():
        return {}

    recovered: Dict[str, Any] = {
        "status": parsed.get("status") if isinstance(parsed.get("status"), str) else "ok",
        "question_type": _normalize_question_type(parsed.get("question_type")),
        "answer": str(answer or "").strip(),
        "summary": summary.strip(),
        "severity": parsed.get("severity") if isinstance(parsed.get("severity"), str) else "info",
        "root_cause_hypotheses": parsed.get("root_cause_hypotheses")
        if isinstance(parsed.get("root_cause_hypotheses"), list)
        else [],
        "recommended_actions": parsed.get("recommended_actions")
        if isinstance(parsed.get("recommended_actions"), list)
        else [],
        "related_keywords": parsed.get("related_keywords")
        if isinstance(parsed.get("related_keywords"), list)
        else [],
    }
    if isinstance(parsed.get("error_kind"), str) and parsed.get("error_kind"):
        recovered["error_kind"] = parsed["error_kind"]
    requires_code_fix, proposed_fixes = _normalize_code_fix_signal(parsed)
    recovered["requires_code_fix"] = requires_code_fix
    recovered["proposed_fixes"] = proposed_fixes
    return recovered


_VALID_QUESTION_TYPES = {"root_cause", "qa", "search", "stats", "meta", "other"}


def _normalize_question_type(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() in _VALID_QUESTION_TYPES:
        return value.strip().lower()
    return "other"


def _normalize_proposed_fixes(value: Any) -> List[Dict[str, Any]]:
    """Validate and normalize the ``proposed_fixes`` array from model output.

    Keeps only well-formed entries (each needs non-empty ``title``,
    ``description`` and ``rationale`` strings). Optional ``suspected_files`` /
    ``suspected_symbols`` are coerced to lists of strings when present. Anything
    malformed is dropped rather than raising, so a partially-broken array still
    yields the usable entries.
    """
    if not isinstance(value, list):
        return []
    fixes: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        description = item.get("description")
        rationale = item.get("rationale")
        if not (isinstance(title, str) and title.strip()):
            continue
        if not (isinstance(description, str) and description.strip()):
            continue
        if not (isinstance(rationale, str) and rationale.strip()):
            continue
        fix: Dict[str, Any] = {
            "title": title.strip(),
            "description": description.strip(),
            "rationale": rationale.strip(),
        }
        for key in ("suspected_files", "suspected_symbols"):
            raw = item.get(key)
            if isinstance(raw, list):
                cleaned = [str(x).strip() for x in raw if str(x).strip()]
                if cleaned:
                    fix[key] = cleaned
        fixes.append(fix)
    return fixes


def _normalize_code_fix_signal(parsed: Dict[str, Any]) -> tuple[bool, List[Dict[str, Any]]]:
    """Derive the (requires_code_fix, proposed_fixes) pair with safe defaults.

    Legacy responses omit both fields; they default to ``False`` / ``[]`` so no
    bug fix task is dispatched. ``requires_code_fix`` is only honored as ``True``
    when there is at least one well-formed proposed fix, so a stray boolean
    cannot trigger dispatch with nothing to act on.
    """
    proposed_fixes = _normalize_proposed_fixes(parsed.get("proposed_fixes"))
    requires_code_fix = bool(parsed.get("requires_code_fix")) and bool(proposed_fixes)
    return requires_code_fix, proposed_fixes


def _strip_confidence_fields(value: Any) -> Any:
    """Remove confidence scores from model output before persisting or returning it."""
    if isinstance(value, list):
        return [_strip_confidence_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_confidence_fields(item)
            for key, item in value.items()
            if key != "confidence"
        }
    return value


_WORKFLOW_LOG_LIMIT = 600


def _truncate_for_log(text: str, limit: int = _WORKFLOW_LOG_LIMIT) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "..."


def _tool_result_to_text(content: Any) -> str:
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif hasattr(item, "text"):
                parts.append(str(getattr(item, "text", "")))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _accumulate_token_usage(usage: Any, token_usage: Dict[str, int]) -> None:
    accumulate_usage(usage, token_usage)


def _log_workflow(task_id: str, event: str, **fields: Any) -> None:
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    logger.info("LogAnalysisAgent workflow task_id=%s %s", task_id, " ".join(parts))


# ───────────────────────── trace state machine ─────────────────────────


class _RunState:
    """Mutable state shared across all SDK message handlers for one run.

    Owns the seq counter, the accumulated ``trace_events`` list, in-flight
    step bookkeeping, token usage accumulator, and the final result text.
    """

    __slots__ = (
        "task_id",
        "emitter",
        "seq_counter",
        "trace_events",
        "active_step_ids",
        "tool_use_id_to_step",
        "step_started_at",
        "token_usage",
        "final_text",
        "cancel_notice_sent",
    )

    def __init__(self, task_id: str, emitter: Optional[TraceEmitter]) -> None:
        self.task_id = task_id
        self.emitter = emitter
        self.seq_counter = SeqCounter()
        self.trace_events: List[AgentTraceEvent] = []
        # FIFO of step_ids that have step_start but no step_end yet.
        # Used as the positional fallback when a tool_result lacks tool_use_id.
        self.active_step_ids: List[str] = []
        # Map SDK tool_use id → trace step_id so subsequent tool_result blocks
        # can correlate without relying on positional ordering.
        self.tool_use_id_to_step: Dict[str, str] = {}
        # step_id → monotonic start time, popped on step_end for duration calc.
        self.step_started_at: Dict[str, float] = {}
        self.token_usage: Dict[str, int] = new_token_usage()
        self.final_text: str = ""
        self.cancel_notice_sent = False

    def emit(self, event: AgentTraceEvent) -> None:
        """Append to internal buffer and notify external emitter (if any)."""
        self.trace_events.append(event)
        safe_emit(self.emitter, event)


def _emit_step_start(
    state: _RunState,
    *,
    tool_name: str,
    tool_input: Any,
    tool_use_id: Optional[str] = None,
) -> str:
    step_id = new_step_id()
    state.active_step_ids.append(step_id)
    state.step_started_at[step_id] = time.monotonic()
    if isinstance(tool_use_id, str) and tool_use_id:
        state.tool_use_id_to_step[tool_use_id] = step_id

    if isinstance(tool_input, dict):
        masked_input = mask_input(tool_input)
    elif tool_input is None:
        masked_input = {}
    else:
        masked_input = {"value": mask_input(tool_input)}

    state.emit(
        build_event(
            STEP_START,
            task_id=state.task_id,
            seq_counter=state.seq_counter,
            step_id=step_id,
            tool_name=str(tool_name or ""),
            tool_input=masked_input,
        )
    )
    return step_id


def _emit_step_end(
    state: _RunState,
    *,
    step_id: str,
    output_text: str,
    is_error: bool,
) -> None:
    started = state.step_started_at.pop(step_id, None)
    duration = max(0.0, time.monotonic() - started) if started is not None else 0.0
    masked = mask_tokens(output_text or "")

    for chunk in coerce_chunk(masked, DEFAULT_CHUNK_MAX_BYTES):
        state.emit(
            build_event(
                STEP_DELTA,
                task_id=state.task_id,
                seq_counter=state.seq_counter,
                step_id=step_id,
                output_chunk=chunk,
            )
        )

    state.emit(
        build_event(
            STEP_END,
            task_id=state.task_id,
            seq_counter=state.seq_counter,
            step_id=step_id,
            status="error" if is_error else "ok",
            output_excerpt=coerce_excerpt(masked, DEFAULT_EXCERPT_MAX_BYTES),
            duration_seconds=round(duration, 3),
        )
    )
    try:
        state.active_step_ids.remove(step_id)
    except ValueError:
        pass


def _emit_thinking_or_text(state: _RunState, text: str) -> None:
    """Emit one logical "thinking" unit (start → deltas → end) for a block.

    Per spec, both ``thinking`` blocks and assistant ``text`` blocks map to
    the ``thinking_*`` event family on the wire — the UI does not need to
    distinguish them.
    """
    if not text:
        return
    masked = mask_tokens(str(text))
    step_id = new_step_id()
    state.emit(
        build_event(
            THINKING_START,
            task_id=state.task_id,
            seq_counter=state.seq_counter,
            step_id=step_id,
        )
    )
    for chunk in coerce_chunk(masked, DEFAULT_CHUNK_MAX_BYTES):
        state.emit(
            build_event(
                THINKING_DELTA,
                task_id=state.task_id,
                seq_counter=state.seq_counter,
                step_id=step_id,
                text_chunk=chunk,
            )
        )
    state.emit(
        build_event(
            THINKING_END,
            task_id=state.task_id,
            seq_counter=state.seq_counter,
            step_id=step_id,
            text=coerce_excerpt(masked, DEFAULT_EXCERPT_MAX_BYTES * 4),
        )
    )


def _emit_answer_delta_from_stream(state: _RunState, message: Any) -> bool:
    """Translate a partial-streaming ``StreamEvent`` into ``answer_delta``.

    Returns ``True`` when ``message`` was a ``StreamEvent`` (handled here),
    so the caller can short-circuit the regular message dispatch. Only
    ``content_block_delta`` events with a ``text_delta`` produce output;
    thinking / tool-input increments on the same channel are ignored.
    """
    event = getattr(message, "event", None)
    if not isinstance(event, dict):
        return False
    text = extract_text_delta(event)
    if text is None:
        return True  # was a StreamEvent, just not a text increment
    masked = mask_tokens(text)
    for chunk in coerce_chunk(masked, DEFAULT_CHUNK_MAX_BYTES):
        state.emit(
            build_event(
                ANSWER_DELTA,
                task_id=state.task_id,
                seq_counter=state.seq_counter,
                text_chunk=chunk,
            )
        )
    return True


def _resolve_step_id_for_result(
    state: _RunState,
    tool_use_id: Optional[str],
) -> Optional[str]:
    """Find the step_id that this tool_result should close out.

    Prefer matching by ``tool_use_id`` (carried by the Anthropic SDK
    `ToolResultBlock`), fall back to the oldest unfinished step so order
    is preserved when the SDK does not propagate ids.
    """
    if isinstance(tool_use_id, str) and tool_use_id in state.tool_use_id_to_step:
        return state.tool_use_id_to_step.pop(tool_use_id)
    if state.active_step_ids:
        return state.active_step_ids[0]
    return None


def _emit_for_content_block(state: _RunState, block: Any) -> None:
    """Translate one assistant content block into trace events.

    Mirrors the dispatch order of the legacy ``_handle_content_block``:

    1. thinking block (has non-empty ``thinking`` attr)
    2. tool_result block (has ``tool_use_id``)
    3. tool_use block (has ``name`` + ``input``)
    4. assistant text block (has ``text``)
    """
    thinking = getattr(block, "thinking", None)
    if thinking:
        _log_workflow(
            state.task_id,
            "thinking",
            content=_truncate_for_log(mask_tokens(str(thinking))),
        )
        _emit_thinking_or_text(state, str(thinking))
        return

    tool_use_id = getattr(block, "tool_use_id", None)
    if tool_use_id is not None:
        content = getattr(block, "content", None)
        is_error = bool(getattr(block, "is_error", False))
        output_text = _tool_result_to_text(content)
        step_id = _resolve_step_id_for_result(state, tool_use_id)
        # Map step_id to a logging-friendly tool name (legacy expectation).
        tool_name = ""
        for past in reversed(state.trace_events):
            if past.get("type") == STEP_START and past.get("step_id") == step_id:
                tool_name = str(past.get("tool_name") or "")
                break
        _log_workflow(
            state.task_id,
            "tool_result",
            tool=tool_name or str(tool_use_id),
            status="error" if is_error else "ok",
            output=_truncate_for_log(mask_tokens(output_text)),
        )
        if step_id is None:
            state.emit(
                build_event(
                    SYSTEM_NOTICE,
                    task_id=state.task_id,
                    seq_counter=state.seq_counter,
                    kind="orphan_tool_result",
                    detail=coerce_excerpt(mask_tokens(output_text), 256),
                )
            )
            return
        _emit_step_end(
            state,
            step_id=step_id,
            output_text=output_text,
            is_error=is_error,
        )
        return

    name = getattr(block, "name", None)
    tool_input = getattr(block, "input", None)
    if name and tool_input is not None:
        block_id = getattr(block, "id", None)
        _log_workflow(
            state.task_id,
            "tool_call",
            tool=str(name),
            input=_truncate_for_log(
                mask_tokens(json.dumps(mask_input(tool_input), ensure_ascii=False))
                if isinstance(tool_input, (dict, list))
                else mask_tokens(str(tool_input))
            ),
        )
        _emit_step_start(
            state,
            tool_name=str(name),
            tool_input=tool_input,
            tool_use_id=block_id if isinstance(block_id, str) else None,
        )
        return

    text = getattr(block, "text", None)
    if text:
        _log_workflow(
            state.task_id,
            "assistant_text",
            content=_truncate_for_log(mask_tokens(str(text))),
        )
        _emit_thinking_or_text(state, str(text))


def _emit_for_message(message: Any, *, state: _RunState) -> None:
    """Top-level dispatch — one SDK message → 0..N trace events.

    Side-effects:
      - emits events through ``state.emit`` (which appends + notifies emitter)
      - accumulates token usage on ``state.token_usage``
      - sets ``state.final_text`` on terminal ResultMessage
      - writes structured workflow logs (legacy behaviour preserved)
    """
    # StreamEvent (include_partial_messages): native content_block_delta
    # increments. Has an ``event`` dict and no ``content`` list — translate
    # answer text increments into answer_delta and short-circuit.
    if getattr(message, "content", None) is None and isinstance(
        getattr(message, "event", None), dict
    ):
        _emit_answer_delta_from_stream(state, message)
        return

    content = getattr(message, "content", None)
    if isinstance(content, list) and content:
        for block in content:
            _emit_for_content_block(state, block)
        _accumulate_token_usage(getattr(message, "usage", None), state.token_usage)
        return

    # Older SDK shape: `tool_uses` / `tool_results` attributes on the message.
    if hasattr(message, "tool_uses") and message.tool_uses:
        for tool_use in message.tool_uses or []:
            name = getattr(tool_use, "name", "")
            tool_input = getattr(tool_use, "input", {})
            block_id = getattr(tool_use, "id", None)
            _log_workflow(
                state.task_id,
                "tool_call",
                tool=str(name),
                input=_truncate_for_log(
                    mask_tokens(json.dumps(mask_input(tool_input), ensure_ascii=False))
                    if isinstance(tool_input, (dict, list))
                    else mask_tokens(str(tool_input))
                ),
            )
            _emit_step_start(
                state,
                tool_name=str(name),
                tool_input=tool_input,
                tool_use_id=block_id if isinstance(block_id, str) else None,
            )

    if hasattr(message, "usage"):
        _accumulate_token_usage(message.usage, state.token_usage)

    if hasattr(message, "tool_results") and message.tool_results:
        for tool_result in message.tool_results or []:
            content_text = _tool_result_to_text(getattr(tool_result, "content", None))
            tool_use_id = getattr(tool_result, "tool_use_id", None)
            is_error = bool(getattr(tool_result, "is_error", False))
            step_id = _resolve_step_id_for_result(state, tool_use_id)
            tool_name = ""
            if step_id is not None:
                for past in reversed(state.trace_events):
                    if past.get("type") == STEP_START and past.get("step_id") == step_id:
                        tool_name = str(past.get("tool_name") or "")
                        break
            _log_workflow(
                state.task_id,
                "tool_result",
                tool=tool_name or str(tool_use_id or ""),
                status="error" if is_error else "ok",
                output=_truncate_for_log(mask_tokens(content_text)),
            )
            if step_id is None:
                state.emit(
                    build_event(
                        SYSTEM_NOTICE,
                        task_id=state.task_id,
                        seq_counter=state.seq_counter,
                        kind="orphan_tool_result",
                        detail=coerce_excerpt(mask_tokens(content_text), 256),
                    )
                )
                continue
            _emit_step_end(
                state,
                step_id=step_id,
                output_text=content_text,
                is_error=is_error,
            )

    data = getattr(message, "data", None)
    subtype = getattr(message, "subtype", None)
    if isinstance(data, dict) and subtype:
        summary_text = data.get("summary") or data.get("description") or data.get("message")
        detail = coerce_excerpt(mask_tokens(str(summary_text or "")), 512) or None
        if str(subtype) != "thinking_tokens":
            _log_workflow(
                state.task_id,
                "system",
                subtype=str(subtype),
                detail=_truncate_for_log(str(summary_text)) if summary_text else None,
            )
        # SDK token-progress messages can arrive once per reasoning token.
        # An empty notice carries no text or counter on our wire protocol;
        # retaining thousands of these only inflates SSE, replay and storage.
        # Keep descriptive notices and actual ThinkingBlock/text deltas. The
        # caller still observes every SDK message for cancellation/routing.
        if str(subtype) != "thinking_tokens" or detail:
            state.emit(
                build_event(
                    SYSTEM_NOTICE,
                    task_id=state.task_id,
                    seq_counter=state.seq_counter,
                    subtype=str(subtype),
                    detail=detail,
                )
            )

    raw_result = getattr(message, "result", None)
    if isinstance(raw_result, str) and raw_result:
        state.final_text = raw_result
        _log_workflow(
            state.task_id,
            "result",
            turns=getattr(message, "num_turns", None),
            stop_reason=getattr(message, "stop_reason", None),
            excerpt=_truncate_for_log(mask_tokens(raw_result)),
        )


def _emit_cancel_requested(state: _RunState) -> None:
    """Send the first half of the two-phase cancel signal, exactly once."""
    if state.cancel_notice_sent:
        return
    state.cancel_notice_sent = True
    state.emit(
        build_event(
            SYSTEM_NOTICE,
            task_id=state.task_id,
            seq_counter=state.seq_counter,
            kind="cancel_requested",
        )
    )


def _close_any_active_steps(state: _RunState, *, reason: str) -> None:
    """If the run ended with steps still in flight (cancel/error), close
    them with a synthetic step_end so the UI does not see orphan cards."""
    for step_id in list(state.active_step_ids):
        _emit_step_end(
            state,
            step_id=step_id,
            output_text=f"[interrupted: {reason}]",
            is_error=True,
        )


def _cancelled_result(
    *,
    model: str,
    duration: float,
    tool_trace: List[Dict[str, str]],
    token_usage: Dict[str, int],
    trace_events: List[AgentTraceEvent],
    trace_summary: Dict[str, Any],
    raw: str = "Agent cancelled by user",
) -> Dict[str, Any]:
    return {
        "engine": "claude-agent-sdk",
        "model": model,
        "schema_version": 3,
        "status": "cancelled",
        "error_kind": "cancelled",
        "question_type": "other",
        "answer": "本轮分析已被用户取消。",
        "summary": "本轮分析已被用户取消。",
        "severity": "info",
        "root_cause_hypotheses": [],
        "recommended_actions": [],
        "related_keywords": [],
        "requires_code_fix": False,
        "proposed_fixes": [],
        "tool_trace": tool_trace,
        "trace_events": trace_events,
        "trace_summary": trace_summary,
        "raw": raw,
        "duration_seconds": round(duration, 2),
        "token_usage": token_usage,
    }


class LogAnalysisAgent:
    """Wraps Claude Agent SDK query() loop for log analysis tasks."""

    async def run(
        self,
        ctx: WorkspaceContext,
        cancel_event: Optional[threading.Event] = None,
        trace_emitter: Optional[TraceEmitter] = None,
        clarification_binding: Optional["ClarificationBinding"] = None,
    ) -> Dict[str, Any]:
        """Run the agent loop and return the structured result dict.

        Args:
            ctx: workspace context (paths + metadata).
            cancel_event: optional ``threading.Event`` checked between SDK
                messages. When set, the agent emits a ``cancel_requested``
                ``system_notice`` then raises ``AgentCancelled`` internally
                and finally emits a ``cancelled`` terminal event.
            trace_emitter: optional **synchronous** callback invoked once
                per ``AgentTraceEvent``. Exceptions inside the callback are
                caught and logged at warning level. Pass ``None`` (the
                default) for the legacy behaviour where events are still
                accumulated internally but no external sink is notified.
            clarification_binding: optional AskUserQuestion wiring supplied by
                the chat service (user preference + run_id + cancel hook).
                ``None`` — the default, and what the Celery entry point passes —
                means the agent cannot pause to ask, because no one is watching
                an SSE stream to answer.
        """
        from app.agents.anthropic_client import PROVIDER_PROFILES, build_options
        from app.agents.clarification import BUILTIN_ASK_TOOL_NAME
        from app.agents.log_analysis.prompts import get_prompts, render_user_prompt
        from app.config import settings

        try:
            from claude_agent_sdk import AssistantMessage, ResultMessage  # noqa: F401
            from app.agents.provider_query import query
        except ImportError as exc:
            raise RuntimeError(
                "claude-agent-sdk is required. Install with: pip install claude-agent-sdk>=0.1"
            ) from exc

        from app.i18n.prompts import response_language_directive

        language_directive = response_language_directive(ctx.locale)
        system_prompt, user_prompt_template = get_prompts(locale=ctx.locale)
        system_prompt += (
            "\n\n## 当前运行工作区\n"
            f"本次运行的初始工作目录是 `{ctx.temp_dir}`。"
            f"`task.json` 的真实路径是 `{ctx.task_json_path}`，"
            f"日志目录是 `{ctx.logs_dir}`，源码目录是 `{ctx.repo_dir}`。"
            "读取文件和搜索时只使用这些路径或它们的相对路径 "
            "(`task.json`、`logs/...`、`repo/...`)。"
            "第一次 Read 调用请使用 `{\"file_path\":\"task.json\"}`。"
            "不要尝试 `/data/task/task.json`、`/data/task/logs`，"
            "也不要尝试 `temp/code_repos/<task_id>/task.json` 这种相对路径；"
            "初始工作目录已经是任务目录。"
            "Bash 调用可能保留前次 cd 后的目录；Bash 读取和检索使用上述绝对路径，"
            "Git 使用 git -C 指定仓库。独立调用不得依赖另一个调用改变目录。"
            "如果路径不确定，先用 `pwd` / `ls -la` 确认当前目录。\n"
        )

        task_data: Dict[str, Any] = {}
        try:
            import json as _json
            from pathlib import Path
            task_data = _json.loads(Path(ctx.task_json_path).read_text(encoding="utf-8"))
        except Exception:
            pass

        # When the user explicitly selected a project repository on the
        # frontend, the backend writes repo_info into task.json with
        # source="user_selected_project_repo". The base system prompt
        # already makes cloning mandatory for every run; this block only
        # adds the extra signal that metadata.json discovery can be
        # skipped because repo_info is already authoritative.
        repo_info = task_data.get("repo_info") if isinstance(task_data, dict) else None
        user_selected_repo = (
            isinstance(repo_info, dict)
            and repo_info.get("source") == "user_selected_project_repo"
        )
        if user_selected_repo:
            system_prompt += (
                "\n\n## 用户已选定项目仓库\n"
                "用户已为本次运行显式选择了项目仓库。`task.json` 中已经"
                "包含完整解析过的 `repo_info`（`clone_url`、`repo_url`、"
                "`default_branch`、`project_card`）。先执行后文的项目适配性"
                "检查；仅在项目匹配时，才将其视为权威仓库来源，跳过对 "
                "metadata.json 的项目发现并进入克隆与调查。\n"
            )

        # Resolve effective model before run for logging & result.
        # Endpoint selection happens here, not at build_options time: RUN_START
        # below names the model and is already streamed to the browser by the
        # time the first SDK message arrives, so it cannot be corrected later.
        from app.agents.routed_query import routed_query
        from app.services import model_router

        from app.services.chat_image_store import native_image_input_required
        endpoints = model_router.candidates(agent_kind=AGENT_KEY, require_mcp=True,
            require_image=native_image_input_required(ctx.temp_dir))
        chosen = endpoints[0] if endpoints else None
        if chosen is not None:
            provider = chosen.provider
            profile = chosen.profile
            effective_model = chosen.model
        else:
            provider = settings.anthropic_provider
            profile = PROVIDER_PROFILES.get(provider)
            effective_model = settings.anthropic_model or (profile.default_model if profile else "unknown")
        supports_mcp = bool(profile and profile.supports_mcp_server_tools)
        # Every candidate is MCP-capable (require_mcp above), so the tool set and
        # the system prompt below are identical across slots and safe to build once.
        served: Dict[str, str] = {"model": effective_model, "provider": str(provider)}

        allowed_tools = list(ALLOWED_TOOLS)
        mcp_servers = None
        if supports_mcp:
            from app.agents.log_analysis.mcp_tools import get_mcp_server
            primary_project_code = (
                repo_info.get("project_code")
                if isinstance(repo_info, dict)
                else None
            )
            mcp_servers = {
                "project_repo": get_mcp_server(
                    workspace_dir=ctx.temp_dir,
                    primary_project_code=primary_project_code,
                    agent_key=AGENT_KEY,
                )
            }
        else:
            allowed_tools = [
                name for name in allowed_tools if not name.startswith("mcp__")
            ]
            system_prompt += (
                "\n\n## 运行时约束\n"
                f"当前 provider `{provider}` 不支持 MCP server 工具。"
                "本次运行中项目目录、仓库查询和工作区克隆 MCP 工具不可用，"
                "因此不能在当前任务追加其他项目仓库。"
                "请使用 `task.json` / `metadata.json` 中的显式仓库字段或 "
                "`repo_info` 来解析仓库。按基础工作流的规定，源代码侧的"
                "查证依然是强制要求。如果任何地方都不存在显式仓库信息，"
                "请以 "
                '`"status": "error", "error_kind": "project_repo_not_registered"` 结束。'
            )
            logger.info(
                "LogAnalysisAgent: provider=%s does not support MCP; using repo_info fallback only",
                provider,
            )

        evidence_snapshot = None
        if settings.agent_evidence_efficiency_enabled is True and supports_mcp:
            from app.agents.evidence_policy import prepare_snapshot
            from app.agents.evidence_tools import TOOL_NAMES, get_evidence_mcp_server

            evidence_snapshot = await prepare_snapshot(task_data)
            if evidence_snapshot is not None:
                statistics = settings.agent_evidence_statistics_enabled is True
                mcp_servers["evidence"] = get_evidence_mcp_server(ctx.temp_dir, statistics=statistics)
                allowed_tools.extend(TOOL_NAMES)
                if statistics:
                    from app.agents.evidence_statistics import TOOL_NAME

                    allowed_tools.append(TOOL_NAME)
                    system_prompt += (
                        "\nFull-file evidence statistics are available through mcp__evidence__summarize_evidence. "
                        "Batch independent counts, literal field values and numeric ranges instead of repeated sample reads. "
                        "Before making an all/always/never/range claim, verify the relevant complete scan and its extrema "
                        "or keep the claim explicitly limited to observed samples. Regex coverage is not incident coverage; "
                        "preserve original tools and uncertainty.\n" if ctx.locale == "en" else
                        "\n可用 mcp__evidence__summarize_evidence 批量统计全文匹配次数、字段取值和数值范围，"
                        "将互不依赖的统计合并，避免反复抽样读取。声称全程、始终、从未或数值范围前，"
                        "先核对相关完整扫描及极值；未完成核验时将结论限定为已观察到的样本。"
                        "正则覆盖不等于事件覆盖，原有工具和不确定性要求均保留。\n"
                    )
                from app.agents.evidence_policy import use_snapshot_in_templates

                system_prompt, user_prompt_template = use_snapshot_in_templates(system_prompt, user_prompt_template)

        user_prompt = render_user_prompt(
            user_prompt_template,
            task_id=ctx.task_id,
            workspace_dir=ctx.temp_dir,
            question=ctx.metadata.get("question") or task_data.get("question", ""),
            project_code=(task_data.get("repo_info") or {}).get("project_code") if isinstance(task_data.get("repo_info"), dict) else None,
            hints=task_data.get("hints", ""),
        )

        # 先按用户问题、提示和附件名筛选候选 Skill，再物化到工作区供 SDK
        # 按需调用。未命中时保持为空，不能回退为全部启用 Skill。
        project_code: Optional[str] = None
        project_name: Optional[str] = None
        project_card: Optional[str] = None
        if isinstance(repo_info, dict):
            project_code = repo_info.get("project_code") or None
            project_name = repo_info.get("project_name") or None
            project_card = repo_info.get("project_card") or None

        # 项目级附加系统提示词：像 Skill 一样分级处理——在通用（Agent 级）系统
        # 提示词之后叠加该项目的专属约束。无配置时返回空串。无论 project_code 来
        # 自用户显式选择还是 metadata.json 自动发现，只要它落到 repo_info 即统一生效。
        try:
            from app.services import project_prompt_service

            project_prompt_addendum = project_prompt_service.build_project_prompt_addendum(
                project_code, AGENT_KEY, project_name=project_name
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LogAnalysisAgent: failed to load project prompt: %s", exc)
            project_prompt_addendum = ""
        if project_prompt_addendum:
            system_prompt += project_prompt_addendum
            logger.info(
                "LogAnalysisAgent: applied project-level system prompt project_code=%s chars=%d",
                project_code,
                len(project_prompt_addendum),
            )

        available_skills: List[str] = []
        materialized_skills: List[str] = []
        skill_overviews: List[Dict[str, str]] = []
        try:
            from app.agents.skill_prompting import build_skill_relevance_query
            from app.services import skills_service

            all_skill_overviews = skills_service.enabled_skill_overviews(
                AGENT_KEY,
                project_code=project_code,
            )
            available_skills = [item["name"] for item in all_skill_overviews]
            issue_info = ctx.metadata.get("issue_info")
            issue_extra = []
            if isinstance(issue_info, dict):
                issue_extra = [
                    issue_info.get("issue_description"),
                    issue_info.get("service_name"),
                    issue_info.get("environment_info"),
                ]
            skill_query = build_skill_relevance_query(
                question=ctx.metadata.get("question") or task_data.get("question", ""),
                hints=ctx.metadata.get("hints") or task_data.get("hints", ""),
                attachments=(
                    ctx.metadata.get("attachments")
                    or task_data.get("attachments")
                ),
                extra_text=issue_extra,
            )
            materialized_skills = skills_service.materialize_relevant_enabled_skills(
                AGENT_KEY,
                ctx.temp_dir,
                query_text=skill_query,
                project_code=project_code,
            )
            if materialized_skills:
                selected_names = set(materialized_skills)
                skill_overviews = [
                    item
                    for item in all_skill_overviews
                    if item["name"] in selected_names
                ]
                logger.info(
                    "LogAnalysisAgent: materialized %d skill(s): %s",
                    len(materialized_skills),
                    ", ".join(materialized_skills),
                )
        except Exception as exc:
            logger.warning("LogAnalysisAgent: failed to materialize skills: %s", exc)

        # Required response rules must survive optional-Skill failures and
        # execute on every invocation, including resumed/follow-up workspaces.
        from app.agents.required_skill_policy import (
            load_required_skill_prompt,
            required_skill_names,
        )

        required_names = required_skill_names(AGENT_KEY)
        required_skill_prompt, required_skill_evidence = load_required_skill_prompt(
            AGENT_KEY, ctx.temp_dir,
        )
        available_skills = list(dict.fromkeys([*available_skills, *required_names]))
        materialized_skills = list(dict.fromkeys([*materialized_skills, *required_names]))
        optional_skills = [name for name in materialized_skills if name not in required_names]
        optional_overviews = [item for item in skill_overviews if item["name"] not in required_names]

        if optional_skills:
            from app.agents.skill_prompting import build_skill_availability_prompt

            skill_availability_prompt = build_skill_availability_prompt(
                optional_overviews or optional_skills,
                final_output_contract="第 6 步的围栏 JSON schema",
            )
            system_prompt += skill_availability_prompt
            user_prompt += skill_availability_prompt

        system_prompt += build_project_fit_guidance(
            workflow_name="日志分析",
            project_name=project_name,
            project_code=project_code,
            project_card=project_card,
            catalog_available=supports_mcp,
            catalog_preloaded=evidence_snapshot is not None,
            locale=ctx.locale,
        )

        user_prompt += "\n\nOutput language requirement:\n" + language_directive

        setting_sources = ["project"] if materialized_skills else None

        # Response-language directive last: an explicit final instruction is the
        # most reliable lever on output language, decoupling the answer language
        # from the (possibly mixed-language) log/source input.
        system_prompt += "\n\n" + language_directive

        # ``state`` must exist before the clarification tool is built: the ask
        # tool emits onto the *same* seq space as every other trace event, or the
        # frontend's (run_id, seq) deduper drops the question card as a replay.
        state = _RunState(task_id=ctx.task_id, emitter=trace_emitter)

        # --- AskUserQuestion clarification (optional) -----------------------
        # Exposed whenever the caller supplied a binding and the user's global
        # "let the agent ask me when instructions are unclear" preference is on.
        # ``workflow_agent=True``: this agent's system prompt mandates a 6-step
        # workflow, so the guidance must state that clarifying outranks it.
        clarification = (
            clarification_binding.setup(
                emit=state.emit,
                seq_counter=state.seq_counter,
                task_id=ctx.task_id,
                locale=ctx.locale,
                workflow_agent=True,
            )
            if clarification_binding is not None
            else None
        )
        if clarification is not None:
            mcp_servers, allowed_tools = clarification.apply(mcp_servers, allowed_tools)
            system_prompt += "\n\n" + clarification.prompt_addendum

        system_prompt += required_skill_prompt
        evidence_hooks = None
        evidence_review = None
        if evidence_snapshot is not None:
            from app.agents.evidence_policy import execution_hooks, snapshot_prompt
            from app.agents.evidence_review import FinalEvidenceReview

            system_prompt += snapshot_prompt(
                evidence_snapshot, ctx.locale,
                batch_plan=settings.agent_evidence_batch_plan_enabled is True,
            )
            if settings.agent_evidence_log_overview_enabled is True:
                from app.agents.log_overview import collect_log_overview, log_overview_prompt

                try:
                    overview = await asyncio.to_thread(collect_log_overview, ctx.temp_dir, ctx.logs_dir)
                    system_prompt += log_overview_prompt(overview, ctx.locale)
                except Exception:
                    logger.warning("Log overview unavailable; retaining native evidence access")
            if settings.agent_evidence_final_review_enabled is True:
                evidence_review = FinalEvidenceReview(ctx.locale, ctx.task_id, settings.anthropic_max_turns)
            evidence_hooks = execution_hooks(
                ctx.locale, ctx.task_id, ctx.temp_dir,
                source_context=settings.agent_evidence_source_context_enabled is True,
                read_window=settings.agent_evidence_read_window_enabled is True,
                topic_context=settings.agent_evidence_topic_context_enabled is True,
                search_endpoints=settings.agent_evidence_search_endpoints_enabled is True,
                final_review=evidence_review,
            )

        def _make_options(endpoint: Optional[Any]) -> Any:
            return build_options(
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                cwd=ctx.temp_dir,
                # Claude Code ships a built-in AskUserQuestion that is not wired
                # to RavenAI's broker or SSE card; under bypassPermissions it
                # would be invoked freely and block on a terminal UI that does
                # not exist. Only the qualified MCP tool above is valid here.
                disallowed_tools=[BUILTIN_ASK_TOOL_NAME],
                permission_mode="bypassPermissions",
                mcp_servers=mcp_servers,
                setting_sources=setting_sources,
                endpoint=endpoint,
                use_extended_context=settings.agent_evidence_extended_context_enabled is True,
                **({"hooks": evidence_hooks} if evidence_hooks else {}),
            )

        def _on_endpoint(endpoint: Any) -> None:
            served["model"] = endpoint.model
            served["provider"] = endpoint.provider

        collecting_stage = False

        def _check_cancel() -> None:
            if cancel_event is not None and cancel_event.is_set():
                _log_workflow(ctx.task_id, "cancelled", reason="cancel_event_set")
                _emit_cancel_requested(state)
                raise AgentCancelled()

        def _on_stage(phase: str) -> None:
            nonlocal collecting_stage
            collecting_stage = phase == "collection"
            if phase == "original":
                return
            detail = (
                ("Collecting evidence" if collecting_stage else "Preparing the answer")
                if ctx.locale == "en" else
                ("正在调查证据" if collecting_stage else "正在整理结论")
            )
            state.emit(build_event(
                SYSTEM_NOTICE, task_id=ctx.task_id, seq_counter=state.seq_counter,
                kind="evidence_stage", detail=detail, phase=phase,
            ))

        def _analysis_error(text: str) -> bool:
            parsed = _extract_fenced_json(text)
            return isinstance(parsed, dict) and parsed.get("status") == "error"

        query_overrides: Dict[str, Any] = {}
        if (
            settings.agent_evidence_staged_effort_enabled is True
            and evidence_snapshot is not None
            and settings.agent_evidence_extended_context_enabled is True
            and settings.agent_evidence_final_review_enabled is not True
        ):
            from app.agents.staged_evidence import make_staged_query

            query_overrides["sdk_query"] = make_staged_query(
                query, locale=ctx.locale, check_cancel=_check_cancel,
                on_phase=_on_stage, is_analysis_error=_analysis_error,
            )

        start = time.monotonic()

        # run_start lifecycle event (and legacy log line).
        _log_workflow(ctx.task_id, "run_start", model=effective_model)
        state.emit(
            build_event(
                RUN_START,
                task_id=ctx.task_id,
                seq_counter=state.seq_counter,
                model=effective_model,
                provider=str(provider),
                available_skills=list(available_skills),
                loaded_skills=list(materialized_skills),
            )
        )
        if available_skills:
            state.emit(
                build_event(
                    SYSTEM_NOTICE,
                    task_id=ctx.task_id,
                    seq_counter=state.seq_counter,
                    kind="skills_available",
                    detail=", ".join(available_skills),
                    available_skills=list(available_skills),
                    loaded_skills=list(materialized_skills),
                )
            )

        for evidence in required_skill_evidence:
            state.emit(
                build_event(
                    SYSTEM_NOTICE,
                    task_id=ctx.task_id,
                    seq_counter=state.seq_counter,
                    kind="required_skill_loaded",
                    detail=f"{evidence['name']}: complete rules loaded for this response",
                    **evidence,
                )
            )

        try:
            async for message in routed_query(
                prompt=user_prompt,
                make_options=_make_options,
                agent_kind=AGENT_KEY,
                candidates=endpoints,
                on_endpoint=_on_endpoint,
                **query_overrides,
            ):
                _check_cancel()
                if evidence_review is not None:
                    evidence_review.observe(message)
                # Collection prose stays in the full assistant trace, never in
                # the final-answer delta channel. Routing sees every SDK frame.
                if collecting_stage and isinstance(getattr(message, "event", None), dict):
                    continue
                _emit_for_message(message, state=state)

        except AgentCancelled:
            duration = time.monotonic() - start
            _log_workflow(
                ctx.task_id,
                "run_complete",
                status="cancelled",
                tool_calls=len(state.active_step_ids) + sum(
                    1 for ev in state.trace_events if ev.get("type") == STEP_END
                ),
                duration_s=round(duration, 2),
                tokens_in=state.token_usage["input_tokens"],
                tokens_out=state.token_usage["output_tokens"],
            )
            _close_any_active_steps(state, reason="cancelled")
            trace_summary = summarize(state.trace_events)
            state.emit(
                build_event(
                    CANCELLED,
                    task_id=ctx.task_id,
                    seq_counter=state.seq_counter,
                    trace_summary=trace_summary,
                )
            )
            return _cancelled_result(
                model=served["model"],
                duration=duration,
                tool_trace=derive_tool_trace(state.trace_events),
                token_usage=state.token_usage,
                trace_events=list(state.trace_events),
                trace_summary=trace_summary,
            )
        except asyncio.TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            duration = time.monotonic() - start
            _close_any_active_steps(state, reason="error")
            trace_summary = summarize(state.trace_events)
            state.emit(
                build_event(
                    ERROR,
                    task_id=ctx.task_id,
                    seq_counter=state.seq_counter,
                    error_kind=type(exc).__name__,
                    message=str(exc),
                    trace_summary=trace_summary,
                )
            )
            logger.exception("LogAnalysisAgent: run failed: %s", exc)
            raise
        finally:
            # The ask tool can only fire inside the loop above; once it exits
            # (normally, cancelled or failed) the broker must stop holding a
            # registry slot and must deny anything still pending.
            if clarification is not None:
                clarification.close()

        final_text = state.final_text
        duration = time.monotonic() - start
        _log_workflow(
            ctx.task_id,
            "run_complete",
            status="finished",
            tool_calls=sum(1 for ev in state.trace_events if ev.get("type") == STEP_END),
            duration_s=round(duration, 2),
            tokens_in=state.token_usage["input_tokens"],
            tokens_out=state.token_usage["output_tokens"],
        )

        trace_summary = summarize(state.trace_events)
        state.emit(
            build_event(
                RUN_COMPLETE,
                task_id=ctx.task_id,
                seq_counter=state.seq_counter,
                trace_summary=trace_summary,
                final_text=coerce_excerpt(mask_tokens(final_text), DEFAULT_EXCERPT_MAX_BYTES * 4),
            )
        )

        parsed = _extract_fenced_json(final_text)
        tool_trace = derive_tool_trace(state.trace_events)
        common_extra = {
            "tool_trace": tool_trace,
            "trace_events": list(state.trace_events),
            "trace_summary": trace_summary,
            "raw": final_text,
            "duration_seconds": round(duration, 2),
            "token_usage": state.token_usage,
            "loaded_skills": list(materialized_skills),
            # Safe defaults for the bug-fix dispatch signal; overridden below
            # for the success/recovered paths. Error/mismatch paths keep these
            # defaults so no bug fix task is ever dispatched off a bad result.
            "requires_code_fix": False,
            "proposed_fixes": [],
        }

        if parsed is None:
            from app.agents.skill_prompting import build_plain_text_skill_answer_fields

            wrapped_skill_answer = build_plain_text_skill_answer_fields(
                final_text,
                optional_skills,
            )
            if wrapped_skill_answer:
                logger.warning(
                    "LogAnalysisAgent: no fenced JSON in skill result; wrapped plain text answer"
                )
                return {
                    "engine": "claude-agent-sdk",
                    "model": served["model"],
                    "provider": served["provider"],
                    "schema_version": 3,
                    **wrapped_skill_answer,
                    **common_extra,
                }
            logger.warning("LogAnalysisAgent: no fenced JSON in result, schema_mismatch")
            return {
                "engine": "claude-agent-sdk",
                "model": served["model"],
                "provider": served["provider"],
                "schema_version": 3,
                "status": "schema_mismatch",
                "question_type": "other",
                "answer": "",
                "summary": "",
                "severity": "info",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
                **common_extra,
            }

        if not _validate_result_schema(parsed):
            recovered = extract_recoverable_result_fields(final_text)
            if recovered:
                logger.warning(
                    "LogAnalysisAgent: result JSON incomplete; recovered user-facing answer"
                )
                recovered_status = recovered.get("status", "ok")
                recovered_error_kind = recovered.get("error_kind")
                if recovered_error_kind:
                    recovered_status = "error"
                return {
                    "engine": "claude-agent-sdk",
                    "model": served["model"],
                    "provider": served["provider"],
                    "schema_version": 3,
                    "status": recovered_status,
                    "error_kind": recovered_error_kind,
                    "question_type": recovered.get("question_type", "other"),
                    "answer": recovered.get("answer", ""),
                    "summary": recovered.get("summary", ""),
                    "severity": recovered.get("severity", "info"),
                    "root_cause_hypotheses": _strip_confidence_fields(
                        recovered.get("root_cause_hypotheses", [])
                    ),
                    "recommended_actions": recovered.get("recommended_actions", []),
                    "related_keywords": recovered.get("related_keywords", []),
                    "parse_warning": "incomplete_json_recovered",
                    **common_extra,
                    "requires_code_fix": recovered.get("requires_code_fix", False),
                    "proposed_fixes": recovered.get("proposed_fixes", []),
                }

            logger.warning("LogAnalysisAgent: result JSON missing required fields, schema_mismatch")
            return {
                "engine": "claude-agent-sdk",
                "model": served["model"],
                "provider": served["provider"],
                "schema_version": 3,
                "status": "schema_mismatch",
                "question_type": "other",
                "answer": "",
                "summary": "",
                "severity": "info",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
                **common_extra,
            }

        status = parsed.get("status", "ok")
        error_kind = parsed.get("error_kind")

        if error_kind:
            status = "error"

        question_type = _normalize_question_type(parsed.get("question_type"))
        answer = parsed.get("answer", "") or ""
        summary = parsed.get("summary", "") or ""
        # Backward-compat fallback: if the model omitted `answer` (older
        # response shape) but did fill `summary`, expose summary as the
        # answer so the UI still has a question-facing response.
        if not answer and summary:
            answer = summary

        requires_code_fix, proposed_fixes = _normalize_code_fix_signal(parsed)

        return {
            "engine": "claude-agent-sdk",
            "model": served["model"],
            "provider": served["provider"],
            "schema_version": 3,
            "status": status,
            "error_kind": error_kind,
            "question_type": question_type,
            "answer": answer,
            "summary": summary,
            "severity": parsed.get("severity", "info"),
            "root_cause_hypotheses": _strip_confidence_fields(
                parsed.get("root_cause_hypotheses", [])
            ),
            "recommended_actions": parsed.get("recommended_actions", []),
            "related_keywords": parsed.get("related_keywords", []),
            **common_extra,
            "requires_code_fix": requires_code_fix,
            "proposed_fixes": proposed_fixes,
        }

    def run_sync(
        self,
        ctx: WorkspaceContext,
        cancel_event: Optional[threading.Event] = None,
        trace_emitter: Optional[TraceEmitter] = None,
        clarification_binding: Optional["ClarificationBinding"] = None,
    ) -> Dict[str, Any]:
        """Synchronous wrapper for Celery tasks. Applies request timeout.

        Note this runs the agent on a **fresh event loop in the calling thread**,
        which is not the FastAPI loop that serves the clarification-resolve
        endpoint. ``PermissionBroker`` settles its futures across loops, so a
        ``clarification_binding`` still works from here.
        """
        from app.config import settings

        timeout = settings.anthropic_request_timeout_seconds

        try:
            return asyncio.run(
                asyncio.wait_for(
                    self.run(
                        ctx,
                        cancel_event=cancel_event,
                        trace_emitter=trace_emitter,
                        clarification_binding=clarification_binding,
                    ),
                    timeout=float(timeout),
                )
            )
        except asyncio.TimeoutError:
            logger.error("LogAnalysisAgent: timed out after %ds", timeout)
            return {
                "engine": "claude-agent-sdk",
                "model": "unknown",
                "schema_version": 3,
                "status": "error",
                "error_kind": "timeout",
                "question_type": "other",
                "answer": "",
                "summary": "",
                "severity": "error",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
                "requires_code_fix": False,
                "proposed_fixes": [],
                "tool_trace": [],
                "trace_events": [],
                "trace_summary": {
                    "thought_duration_seconds": float(timeout),
                    "tool_call_count": 0,
                    "thinking_chars": 0,
                },
                "raw": f"Agent timed out after {timeout} seconds",
                "duration_seconds": float(timeout),
                "token_usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
            }
