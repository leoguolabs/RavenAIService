"""DeviceAgent —— 基于 Claude Agent SDK 的设备联动对话智能体主入口。

驱动 ``claude_agent_sdk.query()`` agent loop，将 SDK 消息流翻译为 :class:`AgentTraceEvent`
异步推给调用方（chat SSE / 非流式调用）。设计要点：

- 复用 ``app.agents.log_analysis.agent`` 的 ``_RunState`` / ``_emit_for_message`` 同款翻译
  机制（直接 import，不重复实现），保持与 LogAnalysisAgent 同构。
- 远端设备 MCP 工具 → in-process SDK 工具映射在 :mod:`mcp_tools` 实现，本类只组装。
- HITL ``can_use_tool`` 与 ``PostToolUse`` hook 分别在 :mod:`permissions` 与
  :mod:`post_tool_hook` 实现，本类负责创建 :class:`PermissionBroker` 并把它注册到外部
  ``broker_registry``（一般是 ``AIChatService.permission_broker_registry``）。
- workspace 仅在请求开始时按 session_id 准备临时目录，结束时幂等清理。

可调用入口：

- :py:meth:`DeviceAgent.run_stream` —— async generator，按 SDK 消息粒度 yield trace 事件
- :py:meth:`DeviceAgent.run`        —— 同步包装，聚合所有事件后返回 ``(events, final_text, model)``
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from app.agents.device_agent.mcp_tools import build_device_mcp_server
from app.agents.device_agent.permissions import PermissionBroker, classify_risk, make_can_use_tool
from app.agents.device_agent.post_tool_hook import build_post_tool_use_hook
from app.agents.device_agent.prompts import (
    get_prompts,
    get_risk_rules,
    render_user_prompt,
)
from app.agents.device_agent.trace import (
    AgentTraceEvent,
    SeqCounter,
    build_event,
    coerce_excerpt,
    mask_tokens,
    summarize,
)
from app.agents.device_agent import workspace as workspace_mod
from app.agents.log_analysis.agent import (
    _RunState,
    _close_any_active_steps,
    _emit_for_message,
)
from app.agents.log_analysis.trace import (
    DEFAULT_EXCERPT_MAX_BYTES,
    ERROR,
    RUN_COMPLETE,
    RUN_START,
    SYSTEM_NOTICE,
)

logger = logging.getLogger(__name__)

AGENT_KEY = "device_agent"

# Sentinel pushed into the event queue to signal "no more events".
_SENTINEL: Any = object()


def _make_queue_emitter(
    queue: "asyncio.Queue[AgentTraceEvent]",
    loop: asyncio.AbstractEventLoop,
) -> Callable[[AgentTraceEvent], None]:
    """Return an emitter that wakes the queue's owning loop safely.

    SDK MCP tool handlers may invoke their callbacks from a background thread.
    ``asyncio.Queue`` is not thread-safe: calling ``put_nowait`` there can add
    an item without waking a consumer blocked in ``queue.get()``, deadlocking
    HITL requests because the browser never sees the question it must answer.
    """

    def emit(event: AgentTraceEvent) -> None:
        try:
            if loop.is_closed():
                logger.warning("DeviceAgent: event loop closed before queue emit")
                return
            try:
                caller_loop = asyncio.get_running_loop()
            except RuntimeError:
                caller_loop = None
            # Thread identity is insufficient here: SDK callbacks can run from
            # a portal/nested async context on the owner's thread without the
            # queue's loop being current.  Only write directly when we are
            # actually executing on the queue's owning loop.
            if caller_loop is loop:
                queue.put_nowait(event)
            else:
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DeviceAgent: queue put failed: %s", exc)

    return emit


# ─────────────────────── Run Context ───────────────────────────────


@dataclass
class DeviceAgentContext:
    """单次 ``DeviceAgent.run_stream`` 输入。"""

    session_id: str
    user_message: str
    target_device_id: str
    images_json: Optional[str] = None
    target_device_name: Optional[str] = None
    # 最近 N 轮历史，按时间顺序，每条 ``{"role": "user"|"assistant", "content": str}``。
    # 由 ``AIChatService`` 从 ``chat_history_service`` 拉取后传入，已含本次用户消息之前
    # 的全部上下文；不要重复追加当前 user_message。
    history: List[Dict[str, str]] = field(default_factory=list)
    # 调用方可传入额外的 system prompt 段，会拼到 yaml ``system_prompt`` 之后。
    system_prompt_override: Optional[str] = None
    scene_hint: Optional[str] = None
    # 本轮 ``ChatAgentRun.id``；trace ``task_id`` 与 workspace 目录都基于它，保证不同 run
    # 的事件序列互不串扰。未提供时按 session_id 兜底（兼容单元测试 / 旧调用方）。
    run_id: Optional[str] = None
    # 归属作用域（user:<id> 或 anon:<token>）。 与 ``ChatRunJob.owner_scope`` 一致，
    # 用于工作区物理隔离，确保不同用户即使提交相同 session_id 也不共享 ``.claude/skills``。
    owner_scope: Optional[str] = None
    # broker 注册回调：``register(run_id, broker)`` / ``unregister(run_id)``。由
    # :class:`ChatRunService` 注入，使 HITL 端点可以按 ``run_id`` 找到 broker。
    # 为空时退化为本地 broker_registry 字典。
    broker_register: Optional[Callable[[str, PermissionBroker], None]] = None
    broker_unregister: Optional[Callable[[str], None]] = None
    # 共享 broker 注册表（旧字典风格）：``{session_id_or_run_id: PermissionBroker}``。
    # 当 ``broker_register`` 未提供时使用；保留兼容旧 ``AIChatService`` 调用方。
    broker_registry: Optional[Dict[str, PermissionBroker]] = None
    # 本轮活动语言（驱动每语言提示词选择 + 回复语言指令）。由调用方
    # （``ChatRunService`` / ``AIChatService``）从请求/用户偏好解析后传入；
    # 缺省时回退到系统默认语言，旧调用方无需改动即可工作。
    locale: Optional[str] = None
    # ---- AskUserQuestion 澄清提问（用户偏好 + run 级取消回调）----
    # 是否向 Agent 暴露 AskUserQuestion 工具（用户「全局禁用澄清」开关，默认开）。
    clarification_enabled: bool = True
    # 单轮 run 内最多发起 AskUserQuestion 的次数（用户偏好，默认 5）。
    clarification_max_rounds: int = 5
    # 澄清超时（固定 5 分钟）后的行为：``cancel`` 取消本轮 / ``continue`` 基于已知信息继续。
    clarification_on_timeout: str = "cancel"
    # run 级取消回调，澄清超时 ``cancel`` 模式时调用；由 ``ChatRunService`` 注入。
    # 缺省（如 legacy ``ai_chat_service`` 路径）时澄清超时降级为 ``continue`` 语义。
    cancel_run: Optional[Callable[[], None]] = None


# ─────────────────────── Helpers ───────────────────────────────────


def _format_history_block(history: List[Dict[str, str]], max_turns: int) -> str:
    """把历史消息拼成 ``[role] content`` 行格式。

    保留最近 ``max_turns`` 轮（一个 user/assistant 对算一轮，按数量 *2 截取）。
    """
    if not history:
        return ""
    limit = max(0, int(max_turns)) * 2 if max_turns else len(history)
    if limit and len(history) > limit:
        history = history[-limit:]
    lines: List[str] = []
    for entry in history:
        role = str(entry.get("role") or "user").strip().lower()
        if role in ("ai", "assistant"):
            role = "assistant"
        elif role == "system":
            role = "system"
        else:
            role = "user"
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{role}] {content}")
    return "\n\n".join(lines)


def _compose_system_prompt(base: str, override: Optional[str]) -> str:
    parts = [s for s in (base.strip() if base else "", override.strip() if override else "") if s]
    return "\n\n".join(parts) if parts else ""


async def _single_user_prompt_stream(prompt: str) -> AsyncIterator[Dict[str, Any]]:
    """Yield one SDK streaming user message for callbacks such as ``can_use_tool``."""
    yield {
        "type": "user",
        "message": {"role": "user", "content": prompt},
    }


def _resolve_device(target_device_id: str) -> Any:
    """同步入口：拉取 ``DeviceInfo``。失败时返回 ``None`` 让调用方走 graceful path。"""
    from app.services.device_link_service import device_link_manager

    try:
        # device_link_manager.get_device 是 async；调用方在 async 上下文中 await
        return device_link_manager.get_device(target_device_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DeviceAgent: failed to load device %s: %s", target_device_id, exc)
        return None


# ─────────────────────── Agent ─────────────────────────────────────


class DeviceAgent:
    """基于 Claude Agent SDK 的设备联动对话 agent。"""

    async def run_stream(self, ctx: DeviceAgentContext) -> AsyncIterator[AgentTraceEvent]:
        """驱动一次对话 ``query()`` 调用，按事件粒度 yield trace。

        生命周期：
        1. provider 检查（DeepSeek 拒绝）
        2. 创建 workspace + 物化 skills
        3. 拉取设备 + 构建 device MCP server + tool_meta_map
        4. 创建 PermissionBroker，注册到 broker_registry
        5. 构造 can_use_tool / post_tool_hook
        6. ``build_options(...)`` + ``async for message in query(...)``
        7. ``finally``: close broker, cleanup workspace
        """
        # Lazy imports keep CLI tools (alembic, etc.) loadable without the SDK.
        from app.agents.anthropic_client import (
            AnthropicConfigurationError,
            PROVIDER_PROFILES,
            build_options,
        )
        from app.config import settings

        try:
            from claude_agent_sdk import query  # noqa: F401
        except ImportError as exc:  # pragma: no cover - guarded at import time elsewhere
            raise RuntimeError(
                "claude-agent-sdk is required. Install with: pip install claude-agent-sdk>=0.1"
            ) from exc

        session_id = ctx.session_id or ""
        # ``task_id`` is what every trace event carries; using ``run_id`` keeps
        # the seq spaces of concurrent runs in the same session isolated. Fall
        # back to ``session_id`` to remain backward-compatible with callers that
        # do not (yet) provide a run_id (unit tests, legacy chat endpoint).
        run_id = ctx.run_id or session_id or "device-agent"
        task_id = run_id
        broker_key = ctx.run_id or session_id

        # Event queue: producer = _drive_loop coroutine, consumer = this generator.
        queue: "asyncio.Queue[AgentTraceEvent]" = asyncio.Queue()
        event_loop = asyncio.get_running_loop()
        emit = _make_queue_emitter(queue, event_loop)

        state = _RunState(task_id=task_id, emitter=emit)
        # Every event in one DeviceAgent stream must share a single sequence
        # space.  Clarification / permission helpers use ``seq_counter`` while
        # SDK message translation uses ``state.seq_counter``; separate counters
        # produce duplicate ``(run_id, seq)`` keys, causing the frontend replay
        # deduper to discard AskUserQuestion cards as already seen events.
        seq_counter = state.seq_counter

        # Endpoint selection must precede RUN_START below: that event names the
        # model and is emitted straight onto the SSE stream, before the capability
        # gate and before query() ever runs, so it cannot be corrected afterwards.
        from app.agents.routed_query import routed_query
        from app.services import model_router

        endpoints = model_router.candidates(agent_kind="device_agent", require_mcp=True,
            require_image=settings.anthropic_provider == "openai" and bool(ctx.images_json))
        chosen = endpoints[0] if endpoints else None
        if chosen is not None:
            provider = chosen.provider
            profile = chosen.profile
            effective_model = chosen.model
        else:
            provider = settings.anthropic_provider
            profile = PROVIDER_PROFILES.get(provider)
            effective_model = (
                settings.anthropic_model
                or (profile.default_model if profile else "unknown")
            )
        served: Dict[str, str] = {"model": effective_model, "provider": str(provider)}

        # Emit run_start immediately so the UI sees the agent starting.
        emit(
            build_event(
                RUN_START,
                task_id=task_id,
                seq_counter=seq_counter,
                model=effective_model,
                provider=str(provider),
            )
        )

        # --- Provider capability gate ---------------------------------------
        if profile is None or not profile.supports_mcp_server_tools:
            emit(
                build_event(
                    ERROR,
                    task_id=task_id,
                    seq_counter=seq_counter,
                    error_kind="provider_no_mcp_support",
                    message=(
                        f"Active Anthropic provider '{provider}' does not support MCP server "
                        "tools; DeviceAgent cannot start. Switch to a provider with "
                        "supports_mcp_server_tools=True (e.g. 'anthropic')."
                    ),
                )
            )
            # Drain immediately and return.
            while not queue.empty():
                yield queue.get_nowait()
            return

        # --- Workspace + skills materialization -----------------------------
        workspace_path = None
        broker: Optional[PermissionBroker] = None
        runner: Optional[asyncio.Task] = None
        start_ts = time.monotonic()
        try:
            workspace_path = workspace_mod.prepare_session(
                session_id, run_id=ctx.run_id, owner_scope=ctx.owner_scope
            )
            from app.services import chat_image_store
            chat_image_store.materialize_session_images(session_id, ctx.images_json, workspace_path)

            available_skills: List[str] = []
            materialized: List[str] = []
            try:
                from app.agents.skill_prompting import build_skill_relevance_query
                from app.services import skills_service

                available_skills = [
                    item["name"]
                    for item in skills_service.enabled_skill_overviews(AGENT_KEY)
                ]
                skill_query = build_skill_relevance_query(
                    question=ctx.user_message,
                    extra_text=[
                        item.get("content", "")
                        for item in ctx.history[-4:]
                        if isinstance(item, dict)
                    ],
                )
                materialized = skills_service.materialize_relevant_enabled_skills(
                    AGENT_KEY,
                    str(workspace_path),
                    query_text=skill_query,
                )
                if materialized:
                    logger.info(
                        "DeviceAgent: loaded %d skill(s): %s",
                        len(materialized),
                        ", ".join(materialized),
                    )
                if available_skills:
                    emit(
                        build_event(
                            SYSTEM_NOTICE,
                            task_id=task_id,
                            seq_counter=seq_counter,
                            kind="skills_available",
                            detail=", ".join(available_skills),
                            available_skills=list(available_skills),
                            loaded_skills=list(materialized),
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("DeviceAgent: failed to materialize skills: %s", exc)

            setting_sources = ["project"] if materialized else None

            # --- Resolve device + build MCP server --------------------------
            from app.services.device_link_service import device_link_manager

            try:
                device = await device_link_manager.get_device(ctx.target_device_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "DeviceAgent: failed to resolve device %s: %s",
                    ctx.target_device_id,
                    exc,
                )
                device = None

            mcp_server, allowed_tools, tool_meta_map = build_device_mcp_server(
                device,
                session_id=session_id,
                target_device_id=ctx.target_device_id,
                emit=emit,
                seq_counter=seq_counter,
                task_id=task_id,
            )

            # Skill tool is always allowed so user-uploaded skills are discoverable.
            full_allowed = list(allowed_tools) + ["Skill"]

            # --- HITL broker + can_use_tool ---------------------------------
            broker = PermissionBroker()
            if ctx.broker_register is not None:
                try:
                    ctx.broker_register(run_id, broker)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("DeviceAgent: broker_register failed: %s", exc)
            elif ctx.broker_registry is not None and broker_key:
                ctx.broker_registry[broker_key] = broker

            risk_rules = get_risk_rules(ctx.scene_hint)
            # Claude Code's allowed_tools auto-approves before can_use_tool. Keep
            # write/destructive/unknown device tools out so Raven's broker is consulted.
            full_allowed = [name for name in full_allowed if name not in tool_meta_map
                            or classify_risk(name, tool_meta_map[name], risk_rules) == "read"]
            timeout_s = float(
                getattr(settings, "device_agent_permission_timeout_seconds", 120)
            )
            can_use_tool = make_can_use_tool(
                broker,
                tool_meta_map,
                risk_rules,
                timeout_seconds=timeout_s,
                emit=emit,
                seq_counter=seq_counter,
                task_id=task_id,
                run_id=run_id,
                session_id=session_id,
            )

            # --- PostToolUse hook -------------------------------------------
            excerpt_bytes = int(
                getattr(settings, "device_agent_result_excerpt_bytes", 16 * 1024)
            )
            max_bytes = int(
                getattr(settings, "device_agent_result_max_bytes", 256 * 1024)
            )
            post_hook = build_post_tool_use_hook(
                tool_meta_map,
                excerpt_bytes=excerpt_bytes,
                max_bytes=max_bytes,
                emit=emit,
                seq_counter=seq_counter,
                task_id=task_id,
            )

            # --- AskUserQuestion clarification tool (optional) ---------------
            # Only exposed when the user's ``clarification_enabled`` preference is
            # on; disabling it removes the tool entirely so the model cannot ask.
            # The wiring itself lives in the shared module so all chat agents get
            # identical behaviour from one code path.
            from app.agents.clarification import ClarificationPrefs, setup_clarification

            mcp_servers: Dict[str, Any] = {}
            if mcp_server is not None:
                mcp_servers["device"] = mcp_server
            clarification = setup_clarification(
                ClarificationPrefs(
                    enabled=bool(ctx.clarification_enabled),
                    max_rounds=int(ctx.clarification_max_rounds),
                    on_timeout=str(ctx.clarification_on_timeout),
                    timeout_seconds=ClarificationPrefs._timeout_from_settings(),
                ),
                emit=emit,
                seq_counter=seq_counter,
                task_id=task_id,
                run_id=run_id,
                session_id=session_id,
                locale=ctx.locale,
                cancel_run=ctx.cancel_run,
                # DeviceAgent shares one broker between tool approval and
                # clarification, and registers it itself above.
                broker=broker,
            )
            if clarification is not None:
                mcp_servers, full_allowed = clarification.apply(mcp_servers, full_allowed)

            # --- Compose prompts --------------------------------------------
            base_system, user_template = get_prompts(ctx.scene_hint, locale=ctx.locale)
            system_prompt = _compose_system_prompt(base_system, ctx.system_prompt_override)
            # 末尾追加直白的回复语言指令，使回复语言与（可能为中文的）设备
            # 数据/历史解耦。
            from app.i18n.prompts import response_language_directive

            system_prompt = _compose_system_prompt(
                system_prompt, response_language_directive(ctx.locale)
            )
            # 仅当本轮暴露了 AskUserQuestion 工具时，追加其使用指引。
            if clarification is not None:
                system_prompt = _compose_system_prompt(
                    system_prompt, clarification.prompt_addendum
                )

            max_history_turns = int(
                getattr(settings, "anthropic_max_history_turns", 10)
            )
            history_block = _format_history_block(ctx.history, max_history_turns)
            user_prompt = render_user_prompt(
                user_template,
                user_message=ctx.user_message,
                history_block=history_block,
                target_device_id=ctx.target_device_id or "",
                target_device_name=ctx.target_device_name or "",
                session_id=session_id,
            )

            # --- Build options ----------------------------------------------
            def _make_options(endpoint: Optional[Any]) -> Any:
                return build_options(
                    system_prompt=system_prompt,
                    allowed_tools=full_allowed,
                    # Claude Code also ships a built-in AskUserQuestion tool.
                    # It is not connected to RavenAI's clarification broker or
                    # SSE card, so using it leaves headless SDK runs waiting on
                    # a terminal UI that does not exist.  Only the qualified
                    # in-process MCP tool below is valid for this product flow.
                    disallowed_tools=["AskUserQuestion"],
                    cwd=str(workspace_path),
                    permission_mode="default",
                    mcp_servers=mcp_servers or None,
                    setting_sources=setting_sources,
                    can_use_tool=can_use_tool,
                    hooks={"PostToolUse": [post_hook]},
                    endpoint=endpoint,
                )

            def _on_endpoint(endpoint: Any) -> None:
                served["model"] = endpoint.model
                served["provider"] = endpoint.provider

            try:
                # Validate the chosen endpoint up front so a misconfiguration is
                # still reported before the workspace-bound run begins.
                _make_options(chosen)
            except AnthropicConfigurationError as exc:
                emit(
                    build_event(
                        ERROR,
                        task_id=task_id,
                        seq_counter=seq_counter,
                        error_kind="anthropic_misconfigured",
                        message=str(exc),
                    )
                )
                # Drain whatever was queued and stop.
                while not queue.empty():
                    yield queue.get_nowait()
                return

            # --- Drive query() in a background task -------------------------

            async def _drive() -> None:
                try:
                    prompt_stream = _single_user_prompt_stream(user_prompt)
                    async for message in routed_query(
                        prompt=prompt_stream,
                        make_options=_make_options,
                        agent_kind="device_agent",
                        candidates=endpoints,
                        on_endpoint=_on_endpoint,
                    ):
                        _emit_for_message(message, state=state)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("DeviceAgent: SDK query failed: %s", exc)
                    _close_any_active_steps(state, reason="error")
                    emit(
                        build_event(
                            ERROR,
                            task_id=task_id,
                            seq_counter=seq_counter,
                            error_kind=type(exc).__name__,
                            message=str(exc),
                        )
                    )
                finally:
                    # Preserve ordering with events scheduled by the
                    # thread-safe emitter immediately before completion.
                    event_loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

            runner = asyncio.create_task(_drive())

            # --- Pump events to consumer ------------------------------------
            while True:
                event = await queue.get()
                if event is _SENTINEL:
                    break
                yield event

            # After SDK loop ends, decide whether to emit run_complete.
            final_text = state.final_text or ""
            # Don't emit run_complete if an error event was already emitted.
            had_error = any(
                isinstance(ev, dict) and ev.get("type") == ERROR for ev in state.trace_events
            )
            if not had_error:
                trace_summary = summarize(state.trace_events)
                complete = build_event(
                    RUN_COMPLETE,
                    task_id=task_id,
                    seq_counter=seq_counter,
                    trace_summary=trace_summary,
                    model=served["model"],
                    provider=served["provider"],
                    token_usage=dict(state.token_usage),
                    duration_seconds=round(time.monotonic() - start_ts, 3),
                    final_text=coerce_excerpt(
                        mask_tokens(final_text),
                        DEFAULT_EXCERPT_MAX_BYTES * 4,
                    ),
                )
                state.trace_events.append(complete)
                yield complete

            duration = time.monotonic() - start_ts
            logger.info(
                "DeviceAgent run_complete: session=%s model=%s duration_s=%.2f "
                "tokens_in=%d tokens_out=%d tools=%d",
                session_id,
                served["model"],
                duration,
                state.token_usage["input_tokens"],
                state.token_usage["output_tokens"],
                len(tool_meta_map),
            )
        except asyncio.CancelledError:
            # Consumer cancelled (e.g. SSE client disconnected) — propagate after cleanup.
            logger.info("DeviceAgent run_stream cancelled: session=%s", session_id)
            raise
        finally:
            if runner is not None and not runner.done():
                runner.cancel()
                try:
                    await runner
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if broker is not None:
                broker.close()
                if ctx.broker_unregister is not None:
                    try:
                        ctx.broker_unregister(run_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("DeviceAgent: broker_unregister failed: %s", exc)
                elif ctx.broker_registry is not None and broker_key:
                    ctx.broker_registry.pop(broker_key, None)
            if workspace_path is not None:
                workspace_mod.cleanup(workspace_path)

    async def run(self, ctx: DeviceAgentContext) -> Tuple[List[AgentTraceEvent], str, str]:
        """Non-streaming wrapper: drain all events, return ``(events, final_text, model)``.

        Used by the non-streaming ``POST /chat`` endpoint when the front-end only needs
        the final text. The same trace events are still produced (and persisted in the
        returned list) so callers can render or log them after the fact.
        """
        events: List[AgentTraceEvent] = []
        final_text = ""
        model = ""
        async for ev in self.run_stream(ctx):
            events.append(ev)
            if isinstance(ev, dict):
                if ev.get("type") == RUN_START and ev.get("model"):
                    model = str(ev.get("model") or "")
                if ev.get("type") == RUN_COMPLETE and ev.get("final_text"):
                    final_text = str(ev.get("final_text") or "")
        return events, final_text, model


__all__ = ["DeviceAgent", "DeviceAgentContext", "AGENT_KEY"]
