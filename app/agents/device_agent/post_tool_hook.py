"""客户端结果审查 —— Claude Agent SDK ``PostToolUse`` hook。

设计要点（详见 openspec design.md Decision 4）：

- matcher 串为 ``"mcp__device__*"``，仅拦截远端设备代理工具，跳过 Skill / 内置工具。
- 解析 ``tool_response`` 为 Decision 10 的 envelope（``status`` / ``result`` /
  ``evidence`` / ``error_kind`` / ``error_message``）。
- 校验：若工具上报了 ``outputSchema``（最小子集：``required``/``properties``/``type``），
  按"必填字段存在 + 顶层类型对"做轻量校验，**不**引入 ``jsonschema`` 依赖。
- 裁剪：单条 ``evidence`` 超 ``excerpt_bytes`` 截断并加 ``truncated=True``；
  整体超 ``max_bytes`` 替换为 ``{"error_kind":"result_too_large"}``。
- 脱敏：复用 ``mask_tokens`` / ``mask_input`` 对 token URL 做正则处理。
- 已知 ``error_kind``（``device_offline`` / ``tool_not_found`` / ``tool_timeout``
  / ``permission_denied`` / ``internal_error``）原样喂回，不当 schema_mismatch。
- emit ``result_validation{status, reason?}`` trace 事件。

**SDK 行为说明**：本期使用的 ``claude-agent-sdk`` ``PostToolUseHookSpecificOutput``
字段为 ``hookEventName`` + 可选 ``additionalContext`` / ``updatedMCPToolOutput``，
本 hook 通过 ``updatedMCPToolOutput`` 替换 / 改写工具输出，达到"把不合契约结果
替换为 error_kind 精简对象再喂回模型"的目的。OpenSpec spec.md 中以
``modifiedContent`` 描述该字段，二者语义一致，由 hook 层透明转换。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.agents.device_agent.mcp_tools import ToolMeta
from app.agents.device_agent.trace import (
    AgentTraceEvent,
    RESULT_VALIDATION,
    SeqCounter,
    build_event,
    mask_input,
    mask_tokens,
    safe_emit,
)

logger = logging.getLogger(__name__)


EmitFn = Callable[[AgentTraceEvent], None]


# Known error_kind values from the device-side envelope. Anything outside this
# set gets normalized to "internal_error" before being fed back to the model.
KNOWN_ERROR_KINDS = frozenset({
    "device_offline",
    "tool_not_found",
    "tool_timeout",
    "permission_denied",
    "internal_error",
    "schema_mismatch",
    "result_too_large",
})


def _utf8_len(value: Any) -> int:
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
        return len(text.encode("utf-8"))
    except Exception:
        return 0


def _truncate_text(text: str, max_bytes: int) -> Tuple[str, bool]:
    if not isinstance(text, str):
        text = str(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    end = max_bytes
    while end > 0 and (encoded[end] & 0xC0) == 0x80:
        end -= 1
    return encoded[:end].decode("utf-8", errors="replace"), True


def _validate_against_output_schema(result: Any, schema: Optional[Dict[str, Any]]) -> Optional[str]:
    """轻量级 outputSchema 校验。

    仅做：
    - schema 缺失 / 非 dict → 跳过（返回 None）。
    - 顶层 ``type`` 校验（object/array/string/number/integer/boolean/null）。
    - 顶层 ``required`` 字段必须在 result（dict）中出现。

    返回 None 表示通过；返回字符串表示失败原因。
    """
    if not isinstance(schema, dict):
        return None

    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        type_ok = True
        if expected_type == "object" and not isinstance(result, dict):
            type_ok = False
        elif expected_type == "array" and not isinstance(result, list):
            type_ok = False
        elif expected_type == "string" and not isinstance(result, str):
            type_ok = False
        elif expected_type == "integer" and not (isinstance(result, int) and not isinstance(result, bool)):
            type_ok = False
        elif expected_type == "number" and not isinstance(result, (int, float)):
            type_ok = False
        elif expected_type == "boolean" and not isinstance(result, bool):
            type_ok = False
        elif expected_type == "null" and result is not None:
            type_ok = False
        if not type_ok:
            return f"expected type={expected_type}, got {type(result).__name__}"

    required = schema.get("required")
    if isinstance(required, list) and isinstance(result, dict):
        missing = [field for field in required if isinstance(field, str) and field not in result]
        if missing:
            return f"missing required fields: {missing}"

    return None


def _extract_envelope(tool_response: Any) -> Dict[str, Any]:
    """把 SDK 透传的 ``tool_response`` 解析为 Decision 10 envelope dict。

    SDK 工具返回 ``{"content": [{"type":"text","text":"<json>"}]}``；这里把它
    剥回去拿到 dispatcher 写入的 JSON。失败时构造一个 ``schema_mismatch``
    占位 envelope（保留原 raw 文本截断后传递）。
    """
    # Claude Code passes MCP content blocks directly; Antigravity's bridge
    # passes the complete CallToolResult. Normalize both before validation.
    if isinstance(tool_response, list):
        tool_response = {"content": tool_response}
    if isinstance(tool_response, dict):
        # 优先解 content[0].text 为 JSON
        content = tool_response.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict):
                text = first.get("text")
                if isinstance(text, str):
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            return parsed
                    except (ValueError, TypeError):
                        return {"status": "error", "error_kind": "schema_mismatch", "raw_excerpt": text}
        # 已是 envelope 形式
        if {"status", "result"} & set(tool_response.keys()) or "error_kind" in tool_response:
            return tool_response
        # 兜底：把整个 dict 当 result
        return {"status": "ok", "result": tool_response}

    if isinstance(tool_response, str):
        try:
            parsed = json.loads(tool_response)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
        return {"status": "error", "error_kind": "schema_mismatch", "raw_excerpt": tool_response}

    return {"status": "error", "error_kind": "schema_mismatch", "raw_excerpt": str(tool_response)}


def _normalize_evidence(
    evidence: Any,
    *,
    excerpt_bytes: int,
) -> List[Dict[str, Any]]:
    """对 ``evidence`` 列表做截断/脱敏处理，单条 > excerpt_bytes 标 truncated。"""
    if not isinstance(evidence, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for entry in evidence:
        if not isinstance(entry, dict):
            entry = {"text": str(entry)}
        label = entry.get("label")
        text = entry.get("text")
        if not isinstance(text, str):
            try:
                text = json.dumps(text, ensure_ascii=False, default=str)
            except Exception:
                text = str(text)
        text = mask_tokens(text)
        text, truncated = _truncate_text(text, excerpt_bytes)
        item: Dict[str, Any] = {"label": label if isinstance(label, str) else "", "text": text}
        if truncated:
            item["truncated"] = True
        normalized.append(item)
    return normalized


def _normalize_envelope(
    envelope: Dict[str, Any],
    *,
    tool_meta: Optional[ToolMeta],
    excerpt_bytes: int,
    max_bytes: int,
) -> Tuple[Dict[str, Any], str, Optional[str]]:
    """规范化 envelope。

    Returns:
        ``(modified, status, reason)``：
        - modified: 喂回模型的最终结构（已脱敏、裁剪）。
        - status: ``"ok"``/``"schema_mismatch"``/``"truncated"``/``"error"``。
        - reason: 若非 ok，失败原因字符串。
    """
    status = "ok"
    reason: Optional[str] = None

    # 1) 总长检查（裸 envelope，未脱敏前）：超 max_bytes 直接替换。
    if _utf8_len(envelope) > max_bytes:
        return (
            {"error_kind": "result_too_large", "raw_excerpt": ""},
            "error",
            "raw payload exceeds device_agent_result_max_bytes",
        )

    raw_status = envelope.get("status")
    error_kind = envelope.get("error_kind")
    if isinstance(error_kind, str):
        if error_kind not in KNOWN_ERROR_KINDS:
            error_kind = "internal_error"

    # 2) 设备返回了错误 → 直接喂回（仅做脱敏 + evidence 裁剪）
    if (isinstance(raw_status, str) and raw_status.lower() == "error") or error_kind:
        out: Dict[str, Any] = {"status": "error"}
        out["error_kind"] = error_kind or "internal_error"
        msg = envelope.get("error_message")
        if isinstance(msg, str):
            out["error_message"] = mask_tokens(msg)
        ev = _normalize_evidence(envelope.get("evidence"), excerpt_bytes=excerpt_bytes)
        if ev:
            out["evidence"] = ev
        topic_id = envelope.get("topic_id")
        if isinstance(topic_id, str):
            out["topic_id"] = topic_id
        return out, "ok", None  # 已知 error 是合法回包，status ok

    # 3) 校验 outputSchema（若有）
    result = envelope.get("result")
    schema_err: Optional[str] = None
    if tool_meta is not None and tool_meta.output_schema:
        schema_err = _validate_against_output_schema(result, tool_meta.output_schema)

    if schema_err:
        # schema_mismatch：替换 result，保留 raw_excerpt
        raw_excerpt_src = result if result is not None else envelope
        try:
            raw_text = json.dumps(raw_excerpt_src, ensure_ascii=False, default=str)
        except Exception:
            raw_text = str(raw_excerpt_src)
        raw_text = mask_tokens(raw_text)
        raw_text, _ = _truncate_text(raw_text, excerpt_bytes)
        return (
            {
                "error_kind": "schema_mismatch",
                "raw_excerpt": raw_text,
                "reason": schema_err,
            },
            "schema_mismatch",
            schema_err,
        )

    # 4) 正常通过：脱敏 result + evidence
    safe_result = mask_input(result)
    normalized: Dict[str, Any] = {"status": "ok", "result": safe_result}
    ev = _normalize_evidence(envelope.get("evidence"), excerpt_bytes=excerpt_bytes)
    if ev:
        normalized["evidence"] = ev
        # 标 truncated 仅记录在 evidence 自身，整体 status 仍 ok。
        if any(e.get("truncated") for e in ev):
            status = "truncated"
    topic_id = envelope.get("topic_id")
    if isinstance(topic_id, str):
        normalized["topic_id"] = topic_id
    request_id = envelope.get("request_id")
    if isinstance(request_id, str):
        normalized["request_id"] = request_id

    # 二次大小检查（脱敏后）
    if _utf8_len(normalized) > max_bytes:
        return (
            {"error_kind": "result_too_large", "raw_excerpt": ""},
            "error",
            "normalized payload exceeds device_agent_result_max_bytes",
        )

    return normalized, status, reason


def build_post_tool_use_hook(
    tool_meta_map: Dict[str, ToolMeta],
    *,
    excerpt_bytes: int,
    max_bytes: int,
    emit: Optional[EmitFn] = None,
    seq_counter: Optional[SeqCounter] = None,
    task_id: str = "",
) -> Any:
    """构造 ``HookMatcher(matcher="mcp__device__*", hooks=[validator])``。

    validator 异步函数签名：``async (input_data, tool_use_id, context) -> dict``，
    返回值符合 ``SyncHookJSONOutput`` schema：
    ``{"hookSpecificOutput": {"hookEventName": "PostToolUse",
       "updatedMCPToolOutput": <new_content>}}``。

    ``new_content`` 形态对齐 SDK in-process tool 返回值：
    ``{"content": [{"type": "text", "text": "<json>"}]}``，由 PostToolUse hook
    透明替换。当 validator 失败或不需要修改时，仍返回带 ``updatedMCPToolOutput``
    的对象（保留 status_event 可见性 + 一致性）。
    """

    try:
        from claude_agent_sdk import HookMatcher
    except ImportError as exc:  # pragma: no cover - import guarded at runtime
        raise RuntimeError(
            "claude-agent-sdk is required. Install with: pip install claude-agent-sdk>=0.1"
        ) from exc

    async def validator(
        input_data: Dict[str, Any],
        tool_use_id: Optional[str],
        context: Any,  # noqa: ARG001
    ) -> Dict[str, Any]:
        tool_name = ""
        tool_response: Any = None
        try:
            tool_name = str(input_data.get("tool_name") or "")
            tool_response = input_data.get("tool_response")
        except AttributeError:
            pass

        meta = tool_meta_map.get(tool_name)
        envelope = _extract_envelope(tool_response)
        modified, status, reason = _normalize_envelope(
            envelope,
            tool_meta=meta,
            excerpt_bytes=excerpt_bytes,
            max_bytes=max_bytes,
        )

        # emit result_validation 事件
        if emit is not None and seq_counter is not None:
            safe_emit(
                emit,
                build_event(
                    RESULT_VALIDATION,
                    task_id=task_id,
                    seq_counter=seq_counter,
                    step_id=tool_use_id or "",
                    tool_name=tool_name,
                    status=status,
                    reason=reason,
                ),
            )

        # 构造 SDK 期望的 in-process tool 输出格式
        try:
            text_payload = json.dumps(modified, ensure_ascii=False, default=str)
        except Exception:
            text_payload = str(modified)

        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedMCPToolOutput": {
                    "content": [{"type": "text", "text": text_payload}],
                },
            },
        }

    return HookMatcher(matcher="mcp__device__*", hooks=[validator])


__all__ = [
    "KNOWN_ERROR_KINDS",
    "build_post_tool_use_hook",
]
