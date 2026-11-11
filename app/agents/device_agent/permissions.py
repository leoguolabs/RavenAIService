"""Human-in-the-loop 工具审核 —— ``can_use_tool`` callback 与 PermissionBroker。

设计要点（详见 openspec design.md Decision 3）：

- ``PermissionBroker`` 每次 ``DeviceAgent.run_stream`` 创建一个；内部维护
  ``Dict[request_id, asyncio.Future]``。当 ``can_use_tool`` 需要用户裁决时，
  broker 发出 ``tool_permission_request`` trace 事件，并创建一个 Future 等待
  HTTP 端点 ``POST /chat/permissions/{request_id}/resolve`` 写入结果。
- ``classify_risk(tool_name, meta, rules)`` 把工具调用归类到 ``read|write|destructive``：
  优先取 capability 上报的 ``risk`` 字段，否则按 yaml 中 ``risk_rules`` 的 glob
  匹配，最后兜底为 ``write`` —— 凡是没有显式标 read 的工具都要走 HITL。
- ``make_can_use_tool(...)`` 工厂返回 SDK 所需的 async callable，对外签名为
  ``(tool_name, tool_input, context) -> dict``：``read`` 短路 allow；其它走 broker。
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

from app.agents.device_agent.mcp_tools import ToolMeta
from app.agents.device_agent.trace import (
    AgentTraceEvent,
    RESULT_VALIDATION,  # noqa: F401  re-export convenience
    SeqCounter,
    TOOL_PERMISSION_REQUEST,
    TOOL_PERMISSION_RESOLVED,
    build_event,
    mask_input,
    safe_emit,
)

logger = logging.getLogger(__name__)


RiskLevel = Literal["read", "write", "destructive"]

EmitFn = Callable[[AgentTraceEvent], None]


# ─────────────────────── PermissionBroker ──────────────────────────


# PermissionBroker 已上移到 app.agents.hitl_broker：它同时服务工具审批与跨 agent
# 的澄清提问，不再是 DeviceAgent 专属。此处 re-export 保持既有 import 路径可用。
from app.agents.hitl_broker import PermissionBroker  # noqa: E402,F401


# ─────────────────────── Risk classification ───────────────────────


def _match_glob(value: str, pattern: str) -> bool:
    """大小写无关的 glob 匹配。``*`` 默认通配全部。"""
    if not pattern or pattern == "*":
        return True
    return fnmatch.fnmatchcase(value.lower(), pattern.lower())


def classify_risk(
    tool_name: str,
    tool_meta: Optional[ToolMeta],
    risk_rules: Optional[List[Dict[str, str]]] = None,
) -> RiskLevel:
    """决定一次工具调用的风险等级。

    优先级：
    1. ``tool_meta.risk`` 若已声明（capability 上报）。
    2. ``risk_rules`` 中按声明顺序匹配第一条命中的规则。
    3. 兜底：``write``（必须人工 review）。

    ``risk_rules`` 条目形如 ``{"server":"<glob>", "tool":"<glob>", "risk":"..."}``。
    """
    if tool_meta is not None:
        meta_risk = tool_meta.risk
        if meta_risk in {"read", "write", "destructive"}:
            return meta_risk  # type: ignore[return-value]

    if risk_rules:
        # 优先按 ToolMeta 的 server/tool 字段匹配；否则根据完整 SDK 名拆分。
        if tool_meta is not None:
            server_part = tool_meta.server or ""
            tool_part = tool_meta.tool or ""
        else:
            # SDK 名形如 ``mcp__device__<server>__<tool>``。
            server_part = ""
            tool_part = tool_name
            if tool_name.startswith("mcp__device__"):
                tail = tool_name[len("mcp__device__"):]
                # 找最后一个 "__"：server / tool 都可能含下划线。
                idx = tail.rfind("__")
                if idx > 0:
                    server_part = tail[:idx]
                    tool_part = tail[idx + 2:]
                else:
                    tool_part = tail

        for rule in risk_rules:
            srv = str(rule.get("server") or "*")
            tl = str(rule.get("tool") or "*")
            risk = str(rule.get("risk") or "").lower()
            if risk not in {"read", "write", "destructive"}:
                continue
            if _match_glob(server_part, srv) and _match_glob(tool_part, tl):
                return risk  # type: ignore[return-value]

    return "write"


# ─────────────────────── can_use_tool factory ──────────────────────


def make_can_use_tool(
    broker: PermissionBroker,
    tool_meta_map: Dict[str, ToolMeta],
    risk_rules: Optional[List[Dict[str, str]]] = None,
    *,
    timeout_seconds: float,
    emit: Optional[EmitFn] = None,
    seq_counter: Optional[SeqCounter] = None,
    task_id: str = "",
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Callable[..., Awaitable[Dict[str, Any]]]:
    """构造 SDK ``can_use_tool`` callback。

    返回 async 函数 ``async def can_use_tool(tool_name, tool_input, context) -> dict``。
    返回值遵循 Claude Agent SDK 约定：
    - ``{"behavior": "allow"}``
    - ``{"behavior": "allow", "updatedInput": {...}}``
    - ``{"behavior": "deny", "message": "..."}``

    Args:
        broker: 与端点 ``POST /chat/permissions/{request_id}/resolve`` 共享的 broker。
        tool_meta_map: ``mcp_tools.build_device_mcp_server`` 返回的工具元数据。
        risk_rules: yaml ``claude_agent_device.risk_rules`` 列表；为空时只依赖
            ``ToolMeta.risk`` + 默认 write。
        timeout_seconds: 用户裁决等待时长；超时返回 deny + ``reason="timeout"``。
        emit: trace 事件 emitter（一般是 ``_RunState.append`` 之类）。
        seq_counter: 与 emit 配套；只要 emit 不为 None 就应同时提供。
        task_id: trace event 的 task_id 字段（一般等于 session_id）。
    """

    rules = risk_rules or []

    async def can_use_tool(
        tool_name: str,
        tool_input: Any,
        context: Any = None,  # noqa: ARG001  SDK 透传上下文，本期未使用
    ) -> Dict[str, Any]:
        from app.agents.clarification import ASK_SDK_NAME

        # The clarification MCP tool only asks a question. Some executors
        # still call this broker for tools the Agent has already allowed.
        if tool_name == ASK_SDK_NAME:
            return {"behavior": "allow"}
        meta = tool_meta_map.get(tool_name)
        risk = classify_risk(tool_name, meta, rules)

        # Read-level：直接放行，不打扰用户。
        if risk == "read":
            logger.debug(
                "can_use_tool short-circuit allow: tool=%s risk=read", tool_name,
            )
            return {"behavior": "allow"}

        request_id = str(uuid.uuid4())
        rationale = _build_rationale(meta, risk)
        masked_args = mask_input(tool_input)

        # 发出 request 事件（SSE 推到前端）
        if emit is not None and seq_counter is not None:
            safe_emit(
                emit,
                build_event(
                    TOOL_PERMISSION_REQUEST,
                    task_id=task_id,
                    seq_counter=seq_counter,
                    request_id=request_id,
                    tool_name=tool_name,
                    tool_input=masked_args if isinstance(masked_args, dict) else {"value": masked_args},
                    risk=risk,
                    rationale=rationale,
                    run_id=run_id,
                    session_id=session_id,
                ),
            )

        try:
            future = broker.open(request_id, tool_name=tool_name, risk=risk)
        except RuntimeError as exc:
            logger.warning("PermissionBroker closed while opening request: %s", exc)
            _emit_resolved(
                emit, seq_counter, task_id, request_id, "deny",
                reason="broker_closed", run_id=run_id, session_id=session_id,
            )
            return {"behavior": "deny", "message": "permission broker unavailable"}

        try:
            decision = await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            broker.cancel(request_id, reason="timeout")
            _emit_resolved(
                emit, seq_counter, task_id, request_id, "deny",
                reason="timeout", run_id=run_id, session_id=session_id,
            )
            return {"behavior": "deny", "message": "permission timeout"}
        except asyncio.CancelledError:
            broker.cancel(request_id, reason="cancelled")
            _emit_resolved(
                emit, seq_counter, task_id, request_id, "deny",
                reason="cancelled", run_id=run_id, session_id=session_id,
            )
            raise

        # decision 形如 {decision, updated_args?, message?}
        action = str(decision.get("decision") or "").strip().lower()
        updated_args = decision.get("updated_args") or decision.get("updatedInput")
        message = decision.get("message")
        if isinstance(message, str):
            message = message.strip() or None

        if action == "allow":
            _emit_resolved(
                emit, seq_counter, task_id, request_id, "allow",
                updated_args=updated_args if isinstance(updated_args, dict) else None,
                run_id=run_id,
                session_id=session_id,
            )
            result: Dict[str, Any] = {"behavior": "allow"}
            if isinstance(updated_args, dict):
                result["updatedInput"] = updated_args
            return result

        # 其余一律按 deny
        reason = decision.get("reason") or "user_denied"
        _emit_resolved(
            emit, seq_counter, task_id, request_id, "deny",
            reason=str(reason), message=message,
            run_id=run_id, session_id=session_id,
        )
        deny: Dict[str, Any] = {"behavior": "deny"}
        if message:
            deny["message"] = message
        else:
            deny["message"] = "user denied tool execution"
        return deny

    return can_use_tool


def _emit_resolved(
    emit: Optional[EmitFn],
    seq_counter: Optional[SeqCounter],
    task_id: str,
    request_id: str,
    decision: str,
    *,
    reason: Optional[str] = None,
    updated_args: Optional[Dict[str, Any]] = None,
    message: Optional[str] = None,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None:
    if emit is None or seq_counter is None:
        return
    safe_emit(
        emit,
        build_event(
            TOOL_PERMISSION_RESOLVED,
            task_id=task_id,
            seq_counter=seq_counter,
            request_id=request_id,
            decision=decision,
            reason=reason,
            updated_args=updated_args,
            message=message,
            run_id=run_id,
            session_id=session_id,
        ),
    )


def _build_rationale(meta: Optional[ToolMeta], risk: RiskLevel) -> str:
    if meta is None:
        return f"Tool risk classified as {risk}; user approval required."
    desc = (meta.description or "").strip()
    if desc:
        return f"[{risk}] {meta.server}.{meta.tool}: {desc}"
    return f"[{risk}] {meta.server}.{meta.tool}"


__all__ = [
    "RiskLevel",
    "PermissionBroker",
    "classify_risk",
    "make_can_use_tool",
]
