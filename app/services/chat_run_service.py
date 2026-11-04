"""Chat Agent Run service —— 把 Agent loop 的生命周期从 SSE 请求里抽出来。

设计要点（详见 ``openspec/changes/support-concurrent-chat-agent-sessions/design.md``）：

- 每一轮用户输入对应一个 :class:`ChatRunJob`：后台 ``asyncio.Task`` 驱动 DeviceAgent 或
  LogAnalysisAgent，事件先写入内存 buffer 再被订阅者消费；SSE 断开 / 切会话
  不会 cancel job。
- 同一 ``session_id`` 同一时间只允许一个 active run；不同 session 可以并发。
- finished job 在内存 retention 期内可被晚到订阅者重放完整 trace；超期后
  仍可从 ``chat_agent_runs.trace_events_json`` 取得终态回放。
- HITL ``PermissionBroker`` 按 ``run_id`` 注册；resolve API 优先用 ``run_id``
  定位，再 fallback 到 ``session_id``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.device_agent.permissions import PermissionBroker
from app.agents.log_analysis.trace import (
    CLARIFICATION_REQUEST,
    CLARIFICATION_RESOLVED,
)
from app.models.user import ChatAgentRun, User

logger = logging.getLogger(__name__)


# Active runs poll cadence for subscribers waiting on new events.
_SUBSCRIBE_POLL_INTERVAL_SECONDS = 0.2
# Heartbeat cadence: emit ``system_notice{kind:"heartbeat"}`` when idle.
_HEARTBEAT_INTERVAL_SECONDS = 15
# Keep finished jobs in memory for this long so late subscribers can replay
# without hitting the DB. Beyond this, snapshots fall back to ``trace_events_json``.
_JOB_RETENTION_SECONDS = 30 * 60
# In-memory event buffer hard cap. Beyond this we drop the oldest events and
# include ``trace_truncated=true`` in the terminal frame.
_EVENT_BUFFER_LIMIT = 2000

# Run status constants — shared with ``ChatAgentRun.status``.
RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_STALE = "stale"

TERMINAL_RUN_STATUSES = frozenset(
    {RUN_STATUS_SUCCEEDED, RUN_STATUS_FAILED, RUN_STATUS_CANCELLED, RUN_STATUS_STALE}
)


# ──────────────────────────── ChatRunJob ────────────────────────────


@dataclass
class ChatRunJob:
    """In-memory record of a chat agent run.

    The DB ``ChatAgentRun`` row mirrors the persistent subset for snapshot /
    sidebar overlay; this in-memory object is authoritative while running.
    """

    run_id: str
    session_id: str
    user_id: Optional[str]
    # Owner scope: ``user:<id>`` for authenticated users, ``anon:<token>``
    # for anonymous browsers. Two users MUST never share the same scope —
    # all active-run registry lookups key on ``(owner_scope, session_id)``.
    owner_scope: str
    agent_kind: str  # "device" | "log_analysis"
    status: str
    started_at: float
    user_message: str
    request_payload: Dict[str, Any]
    # Buffered SSE-shaped frames (``{"event": ..., ...}``). Subscribers replay
    # the whole list, then poll for new tail entries.
    events: List[Dict[str, Any]] = field(default_factory=list)
    # Raw ``AgentTraceEvent`` payloads (no SSE wrapper) for late-subscriber
    # full-history replay; what gets persisted to ``trace_events_json``.
    trace_events: List[Dict[str, Any]] = field(default_factory=list)
    # Pending permission requests by ``request_id``; restored to subscribers
    # via the active-run / run-snapshot endpoints.
    pending_permissions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Pending AskUserQuestion clarification requests by ``request_id``; restored
    # to subscribers the same way as ``pending_permissions``.
    pending_clarifications: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    task: Optional[asyncio.Task] = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # External-driver runs (log_analysis / project_expert / package_search)
    # have ``task is None``; cancelling them must go through the owning
    # service's own job (its threading.Event), which this callback triggers.
    cancel_callback: Optional[Callable[[], None]] = None
    answer: str = ""
    model: str = ""
    error: Optional[str] = None
    workspace_path: Optional[str] = None
    # AI usage captured from the agent's terminal ``run_complete`` event, used
    # by ``_persist_terminal`` to record an idempotent metric event. Defaults
    # leave token counts at 0 so error/cancel terminals still record an
    # invocation with status metadata (tasks 3.4 / 3.9).
    token_usage: Optional[Dict[str, int]] = None
    provider: Optional[str] = None
    duration_seconds: Optional[float] = None
    trace_summary: Optional[Dict[str, Any]] = None
    error_kind: Optional[str] = None
    # GeneralAgent 的结构化路由建议（device|log_analysis|package_search|
    # project_expert），无建议时为 None。其它 agent_kind 不设置。
    suggested_agent_type: Optional[str] = None
    updated_at: float = field(default_factory=time.monotonic)
    finished_at: Optional[float] = None
    trace_truncated: bool = False

    # ---- buffer helpers ---------------------------------------------------

    def append_event(self, event: Dict[str, Any]) -> None:
        """Append an SSE-shaped frame, enforcing the buffer cap."""
        self.events.append(event)
        if len(self.events) > _EVENT_BUFFER_LIMIT:
            # Drop the oldest non-essential frames; keep the most recent.
            overflow = len(self.events) - _EVENT_BUFFER_LIMIT
            del self.events[:overflow]
            self.trace_truncated = True
        self.updated_at = time.monotonic()

    def append_trace(self, event: Dict[str, Any]) -> None:
        """Append a raw trace event (without SSE ``event:`` wrapper)."""
        self.trace_events.append(event)
        if len(self.trace_events) > _EVENT_BUFFER_LIMIT:
            overflow = len(self.trace_events) - _EVENT_BUFFER_LIMIT
            del self.trace_events[:overflow]
            self.trace_truncated = True

    def mark_status(self, new_status: str, *, error: Optional[str] = None) -> None:
        self.status = new_status
        if error is not None:
            self.error = error
        self.updated_at = time.monotonic()
        if new_status in TERMINAL_RUN_STATUSES and self.finished_at is None:
            self.finished_at = time.monotonic()


# ──────────────────────────── Service ───────────────────────────────


class ChatRunService:
    """Owns the in-memory ChatRunJob registry plus permission brokers."""

    def __init__(self) -> None:
        self._jobs: Dict[str, ChatRunJob] = {}
        # Active-run pointer is keyed on ``(owner_scope, session_id)`` so two
        # users using identical ``session_id`` values never collide. Plain
        # ``session_id`` lookups are explicitly forbidden — every accessor
        # below requires both pieces.
        self._active_by_owner_session: Dict[Tuple[str, str], str] = {}
        self._brokers: Dict[str, PermissionBroker] = {}

    # ---- registry primitives ---------------------------------------------

    def _evict_finished(self) -> None:
        """Lazy cleanup: drop terminal jobs whose finished_at is older than
        the retention window."""
        if not self._jobs:
            return
        cutoff = time.monotonic() - _JOB_RETENTION_SECONDS
        stale = [
            rid
            for rid, job in self._jobs.items()
            if job.status in TERMINAL_RUN_STATUSES and (job.finished_at or 0) < cutoff
        ]
        for rid in stale:
            job = self._jobs.pop(rid, None)
            if job is not None:
                key = (job.owner_scope, job.session_id)
                if self._active_by_owner_session.get(key) == rid:
                    self._active_by_owner_session.pop(key, None)
            self._brokers.pop(rid, None)
            logger.debug("chat_run: evicted finished run_id=%s", rid)

    # ---- broker helpers --------------------------------------------------

    def register_broker(self, run_id: str, broker: PermissionBroker) -> None:
        self._brokers[run_id] = broker

    def unregister_broker(self, run_id: str) -> None:
        self._brokers.pop(run_id, None)

    def get_broker_by_run_id(self, run_id: str) -> Optional[PermissionBroker]:
        return self._brokers.get(run_id)

    def get_broker_by_owner_session(
        self, owner_scope: str, session_id: str
    ) -> Optional[PermissionBroker]:
        run_id = self._active_by_owner_session.get((owner_scope, session_id))
        if not run_id:
            return None
        return self._brokers.get(run_id)

    # ---- job lookup ------------------------------------------------------

    def get_job(self, run_id: str) -> Optional[ChatRunJob]:
        return self._jobs.get(run_id)

    def get_active_job_for_session(
        self, owner_scope: str, session_id: str
    ) -> Optional[ChatRunJob]:
        """Active-run lookup for a specific ``(owner_scope, session_id)``.

        Never accepts a bare ``session_id`` to prevent cross-user collisions
        when two users happen to send the same UUID.
        """
        run_id = self._active_by_owner_session.get((owner_scope, session_id))
        if not run_id:
            return None
        job = self._jobs.get(run_id)
        if job is None or job.status in TERMINAL_RUN_STATUSES:
            return None
        return job

    # ---- snapshots -------------------------------------------------------

    @staticmethod
    def _check_owner_scope(job: ChatRunJob, owner_scope: Optional[str]) -> None:
        """Reject access by anyone whose ``owner_scope`` doesn't match the job's.

        Returns 404 (not 403) for mismatches so we don't leak whether a given
        ``run_id`` exists for another user.
        """
        if owner_scope is None or owner_scope != job.owner_scope:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="未找到该 run",
            )

    def get_snapshot(
        self, run_id: str, owner_scope: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        job = self._jobs.get(run_id)
        if job is None:
            return None
        self._check_owner_scope(job, owner_scope)
        return self._snapshot_payload(job)

    def get_active_run_snapshot(
        self, owner_scope: str, session_id: str
    ) -> Optional[Dict[str, Any]]:
        job = self.get_active_job_for_session(owner_scope, session_id)
        if job is None:
            return None
        return self._snapshot_payload(job)

    @staticmethod
    def _derive_pending_clarifications(
        trace_events: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Replay ``clarification_request`` / ``_resolved`` to find open questions.

        ``_run_device_job`` maintains ``job.pending_clarifications`` as it drives
        the stream, but externally-driven runs (log_analysis / project_expert /
        package_search) only share their raw ``trace_events`` list — without this
        replay, reloading the page mid-question would lose the card. Applying the
        events in order gives the same answer the dict would, so it is also a
        safe fallback for device runs with nothing pending.
        """
        pending: Dict[str, Dict[str, Any]] = {}
        for ev in trace_events:
            if not isinstance(ev, dict):
                continue
            ev_type = ev.get("type")
            request_id = ev.get("request_id")
            if not request_id:
                continue
            if ev_type == CLARIFICATION_REQUEST:
                pending[str(request_id)] = dict(ev)
            elif ev_type == CLARIFICATION_RESOLVED:
                pending.pop(str(request_id), None)
        return list(pending.values())

    @classmethod
    def _snapshot_payload(cls, job: ChatRunJob) -> Dict[str, Any]:
        return {
            "run_id": job.run_id,
            "session_id": job.session_id,
            "agent_kind": job.agent_kind,
            "status": job.status,
            "answer_so_far": job.answer,
            "model": job.model,
            "error": job.error,
            "trace_events": list(job.trace_events),
            "events": list(job.events),
            "pending_permissions": list(job.pending_permissions.values()),
            "pending_clarifications": (
                list(job.pending_clarifications.values())
                or cls._derive_pending_clarifications(job.trace_events)
            ),
            "started_at": job.started_at,
            "updated_at": job.updated_at,
            "finished_at": job.finished_at,
            "workspace_path": job.workspace_path,
            "trace_truncated": job.trace_truncated,
            "user_message": job.user_message,
            "suggested_agent_type": job.suggested_agent_type,
        }

    # ---- usage capture ---------------------------------------------------

    @staticmethod
    def _capture_run_usage(job: ChatRunJob, event: Dict[str, Any]) -> None:
        """Pull AI usage fields off an agent trace event onto the job.

        Called for ``run_start`` (provider/model), ``run_complete`` (tokens,
        duration, trace summary) and ``error`` (error_kind) events. Best-effort:
        each field is only overwritten when the event actually carries it, so a
        later terminal event never clobbers an earlier provider/model.
        """
        usage = event.get("token_usage")
        if isinstance(usage, dict):
            job.token_usage = {k: int(v or 0) for k, v in usage.items()}
        model = event.get("model")
        if isinstance(model, str) and model:
            job.model = model
        provider = event.get("provider")
        if isinstance(provider, str) and provider:
            job.provider = provider
        duration = event.get("duration_seconds")
        if isinstance(duration, (int, float)):
            job.duration_seconds = float(duration)
        trace_summary = event.get("trace_summary")
        if isinstance(trace_summary, dict):
            job.trace_summary = trace_summary
        error_kind = event.get("error_kind")
        if isinstance(error_kind, str) and error_kind:
            job.error_kind = error_kind

    async def _record_terminal_metrics(
        self, job: ChatRunJob, terminal_status: str
    ) -> None:
        """Best-effort, idempotent AI-usage metric for a chat agent run.

        Covers both ``general`` and ``device`` agent kinds (task 3.4) and every
        terminal status — succeeded / failed / cancelled / stale all record an
        invocation with whatever tokens were captured (task 3.9). Never raises.
        """
        try:
            from app.services import metrics_service

            agent_kind = job.agent_kind or "general"
            result = {
                "token_usage": job.token_usage,
                "model": job.model or None,
                "provider": job.provider,
                "duration_seconds": job.duration_seconds,
                "trace_summary": job.trace_summary,
                "trace_events": job.trace_events,
                "error_kind": job.error_kind,
            }
            await metrics_service.record_agent_run_usage(
                source=f"{agent_kind}_agent",
                agent_kind=agent_kind,
                run_id=job.run_id,
                result=result,
                terminal_status=terminal_status,
                provider=job.provider,
                user_id=str(job.user_id) if job.user_id is not None else None,
                owner_scope=job.owner_scope or None,
                session_id=job.session_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "chat_run: metrics record skipped run_id=%s: %s", job.run_id, exc
            )

    # ---- persistence helpers --------------------------------------------

    @staticmethod
    async def load_terminal_snapshot_from_db(
        db: AsyncSession, run_id: str, owner_scope: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Reload a run snapshot from ``chat_agent_runs`` for evicted jobs.

        Returns ``None`` (not raises) when the row's owner_scope doesn't
        match — the API layer turns that into a 404 to avoid leaking the
        existence of other users' runs.
        """
        result = await db.execute(select(ChatAgentRun).where(ChatAgentRun.id == run_id))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        if owner_scope is None or row.owner_scope != owner_scope:
            return None
        trace_events: List[Dict[str, Any]] = []
        if row.trace_events_json:
            try:
                parsed = json.loads(row.trace_events_json)
                if isinstance(parsed, list):
                    trace_events = parsed
            except Exception:  # noqa: BLE001
                trace_events = []
        started_ts = (
            row.started_at.timestamp() if isinstance(row.started_at, datetime) else None
        )
        finished_ts = (
            row.finished_at.timestamp() if isinstance(row.finished_at, datetime) else None
        )
        return {
            "run_id": row.id,
            "session_id": row.session_id,
            "agent_kind": row.agent_kind,
            "status": row.status,
            "answer_so_far": row.answer or "",
            "model": row.model or "",
            "error": row.error,
            "trace_events": trace_events,
            "events": [],
            "pending_permissions": [],
            "started_at": started_ts,
            "updated_at": finished_ts or started_ts,
            "finished_at": finished_ts,
            "workspace_path": row.workspace_path,
            "trace_truncated": False,
            "user_message": row.user_message or "",
        }


    # ---- start / drive DeviceAgent runs ---------------------------------

    async def start_device_run(
        self,
        *,
        db: AsyncSession,
        user: Optional[User],
        owner_scope: str,
        session_id: str,
        user_message: str,
        target_device_id: str,
        target_device_name: Optional[str],
        history: List[Dict[str, str]],
        system_prompt_override: Optional[str] = None,
        remember: bool = True,
        locale: Optional[str] = None,
        images_json: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> ChatRunJob:
        """Create a new DeviceAgent run and start its background task.

        - Enforces single-active-run-per-session: raises HTTPException(409) if
          ``session_id`` already has a non-terminal run.
        - Persists the user message immediately (if ``remember`` and ``user``).
        - Creates the ``chat_agent_runs`` row in ``status=running`` so the
          sidebar overlay can show a spinner.
        - Accepts a caller-supplied ``run_id`` so pre-run work (image OCR) can
          be metered under the same run; mints one when omitted.
        - Returns the in-memory ``ChatRunJob`` ready for subscription.
        """
        from app.services.chat_history_service import chat_history_service

        # Evict any retention-window-expired finished jobs before checking.
        self._evict_finished()

        existing = self.get_active_job_for_session(owner_scope, session_id)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "该会话已有运行中的 agent run",
                    "active_run_id": existing.run_id,
                },
            )

        run_id = run_id or str(uuid.uuid4())
        request_payload = {
            "target_device_id": target_device_id,
            "target_device_name": target_device_name,
            "system_prompt_override": system_prompt_override,
            "remember": remember,
        }

        # Persist user message + ChatAgentRun row using the caller's DB session.
        if remember and user is not None:
            try:
                await chat_history_service.append_message(
                    db,
                    user_id=user.id,
                    session_id=session_id,
                    role="user",
                    content=user_message,
                    images_json=images_json,
                )
                row = ChatAgentRun(
                    id=run_id,
                    session_id=session_id,
                    user_id=user.id,
                    owner_scope=owner_scope,
                    agent_kind="device",
                    status=RUN_STATUS_RUNNING,
                    user_message=user_message,
                    request_json=json.dumps(request_payload, ensure_ascii=False),
                    started_at=datetime.utcnow(),
                )
                db.add(row)
                await db.flush()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "chat_run: failed to persist initial run row run_id=%s: %s",
                    run_id,
                    exc,
                )

        # Precompute the deterministic workspace path so it can appear in
        # snapshots before the agent task actually starts. ``prepare_session``
        # creates the directory; ``DeviceAgent.run_stream`` will reuse the same
        # path because mkdir is idempotent.
        workspace_path_str: Optional[str] = None
        try:
            from app.agents.device_agent import workspace as workspace_mod

            workspace_path_str = str(
                workspace_mod.prepare_session(
                    session_id, run_id=run_id, owner_scope=owner_scope
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "chat_run: failed to precompute workspace path run_id=%s: %s",
                run_id,
                exc,
            )

        # Persist workspace_path early so the snapshot endpoint can return it
        # for the in-flight run.
        if workspace_path_str and remember and user is not None:
            try:
                result = await db.execute(
                    select(ChatAgentRun).where(ChatAgentRun.id == run_id)
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    row.workspace_path = workspace_path_str
                    await db.flush()
            except Exception as exc:  # noqa: BLE001
                logger.debug("chat_run: workspace_path write skipped: %s", exc)

        job = ChatRunJob(
            run_id=run_id,
            session_id=session_id,
            user_id=getattr(user, "id", None),
            owner_scope=owner_scope,
            agent_kind="device",
            status=RUN_STATUS_RUNNING,
            started_at=time.monotonic(),
            user_message=user_message,
            request_payload=request_payload,
            workspace_path=workspace_path_str,
        )
        self._jobs[run_id] = job
        self._active_by_owner_session[(owner_scope, session_id)] = run_id

        ctx_kwargs = {
            "session_id": session_id,
            "user_message": user_message,
            "images_json": images_json,
            "target_device_id": target_device_id or "",
            "target_device_name": target_device_name,
            "history": history,
            "system_prompt_override": system_prompt_override,
            "run_id": run_id,
            "owner_scope": owner_scope,
            "remember": remember,
            "locale": locale,
            # AskUserQuestion 澄清偏好：来自用户 profile（匿名用户用默认值）。
            "clarification_enabled": bool(getattr(user, "clarification_enabled", True)),
            "clarification_max_rounds": int(getattr(user, "clarification_max_rounds", 5)),
            "clarification_on_timeout": str(getattr(user, "clarification_on_timeout", "cancel")),
        }
        job.task = asyncio.create_task(self._run_device_job(job, ctx_kwargs))
        # Surface a synthetic ``run_start`` SSE frame so subscribers can pick up
        # the run before the agent actually emits its own ``run_start``.
        job.append_event(
            {
                "event": "run_start_pending",
                "run_id": run_id,
                "session_id": session_id,
                "agent_kind": "device",
            }
        )
        return job

    async def _run_device_job(
        self, job: ChatRunJob, ctx_kwargs: Dict[str, Any]
    ) -> None:
        """Background task: drive ``DeviceAgent.run_stream`` to completion.

        On terminal, opens a fresh DB session to persist the assistant message
        and the run terminal state. SSE subscribers consume from ``job.events``.
        """
        from app.agents.device_agent.agent import DeviceAgent, DeviceAgentContext
        from app.agents.log_analysis.trace import RUN_COMPLETE, RUN_START, ERROR

        run_id = job.run_id
        session_id = job.session_id
        owner_scope = job.owner_scope
        remember = bool(ctx_kwargs.pop("remember", True))

        def _cancel_run() -> None:
            """Run-level cancel used by AskUserQuestion clarification timeout
            (``on_timeout == "cancel"``). Cancels this run's task so the agent
            loop finalises as ``cancelled`` via DeviceAgent's CancelledError path."""
            try:
                self.cancel(run_id, owner_scope)
            except Exception as exc:  # noqa: BLE001
                logger.warning("chat_run: clarification cancel_run failed run_id=%s: %s", run_id, exc)

        ctx = DeviceAgentContext(
            broker_register=self.register_broker,
            broker_unregister=self.unregister_broker,
            cancel_run=_cancel_run,
            **ctx_kwargs,
        )

        last_event: Optional[Dict[str, Any]] = None
        try:
            async for ev in DeviceAgent().run_stream(ctx):
                if not isinstance(ev, dict):
                    continue
                ev_type = ev.get("type")
                if not ev_type:
                    continue
                # Save raw trace event for replay / persistence.
                job.append_trace(ev)
                last_event = ev

                if ev_type == RUN_START:
                    model_value = ev.get("model")
                    if model_value:
                        job.model = str(model_value)
                    self._capture_run_usage(job, ev)
                    if not job.workspace_path:
                        # The agent does not currently emit workspace_path; the
                        # prepare_session helper builds a deterministic path
                        # under <base>/device_agent/<session>/<run_id>/.
                        pass
                if ev_type == RUN_COMPLETE:
                    self._capture_run_usage(job, ev)
                    final_text = ev.get("final_text")
                    if isinstance(final_text, str):
                        job.answer = final_text
                    elif isinstance(final_text, dict):
                        text_val = final_text.get("text")
                        if isinstance(text_val, str):
                            job.answer = text_val
                if ev_type == ERROR:
                    self._capture_run_usage(job, ev)
                if ev_type == "tool_permission_request":
                    rid = str(ev.get("request_id") or "")
                    if rid:
                        job.pending_permissions[rid] = dict(ev)
                if ev_type == "tool_permission_resolved":
                    rid = str(ev.get("request_id") or "")
                    if rid:
                        job.pending_permissions.pop(rid, None)
                if ev_type == "clarification_request":
                    rid = str(ev.get("request_id") or "")
                    if rid:
                        job.pending_clarifications[rid] = dict(ev)
                if ev_type == "clarification_resolved":
                    rid = str(ev.get("request_id") or "")
                    if rid:
                        job.pending_clarifications.pop(rid, None)

                # SSE-shaped frame: drop ``type`` and re-emit as ``event``.
                payload_out: Dict[str, Any] = {
                    k: v for k, v in ev.items() if k != "type"
                }
                payload_out["event"] = ev_type
                payload_out["run_id"] = run_id
                payload_out["session_id"] = session_id
                job.append_event(payload_out)
            terminal_status = RUN_STATUS_SUCCEEDED
            terminal_error: Optional[str] = None
            # If the last raw event was ``error``, treat as failed.
            if isinstance(last_event, dict) and last_event.get("type") == ERROR:
                terminal_status = RUN_STATUS_FAILED
                terminal_error = str(last_event.get("message") or "agent error")
        except asyncio.CancelledError:
            logger.info("chat_run: run cancelled run_id=%s", run_id)
            terminal_status = RUN_STATUS_CANCELLED
            terminal_error = "用户取消"
            job.append_event(
                {
                    "event": "cancelled",
                    "run_id": run_id,
                    "session_id": session_id,
                    "message": terminal_error,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("chat_run: run failed run_id=%s", run_id)
            terminal_status = RUN_STATUS_FAILED
            terminal_error = str(exc)
            job.append_event(
                {
                    "event": "error",
                    "run_id": run_id,
                    "session_id": session_id,
                    "message": terminal_error,
                }
            )

        job.mark_status(terminal_status, error=terminal_error)
        # Persist assistant message + terminal run state in a fresh session.
        await self._persist_terminal(job, terminal_status, remember=remember)
        # Build the aggregated ``messages`` list expected by legacy clients of
        # the streaming endpoint: original history + user_message + final
        # assistant answer (only when we actually have one). This mirrors the
        # contract the pre-ChatRunService AIChatService.chat_stream() observed.
        history = ctx_kwargs.get("history") or []
        composed_messages: List[Dict[str, Any]] = [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in history
            if isinstance(m, dict)
        ]
        if job.user_message:
            composed_messages.append({"role": "user", "content": job.user_message})
        if job.answer:
            composed_messages.append({"role": "ai", "content": job.answer})
        # Emit final ``done`` frame so subscribers can exit their loop.
        job.append_event(
            {
                "event": "done",
                "run_id": run_id,
                "session_id": session_id,
                "status": terminal_status,
                "answer": job.answer,
                "model": job.model,
                "error": job.error,
                "trace_truncated": job.trace_truncated,
                "messages": composed_messages,
            }
        )
        # Clear the active-session pointer if it still points at us so a new
        # run for the same (owner_scope, session_id) can start immediately.
        key = (job.owner_scope, session_id)
        if self._active_by_owner_session.get(key) == run_id:
            self._active_by_owner_session.pop(key, None)

    async def _persist_terminal(
        self, job: ChatRunJob, terminal_status: str, *, remember: bool
    ) -> None:
        """Write assistant message + finalize ``chat_agent_runs`` row.

        Uses ``db_manager.session_factory()`` so the run can outlive the
        original HTTP request's DB session.
        """
        from app.models.database import db_manager
        from app.services.chat_history_service import chat_history_service

        # Record AI usage first; it opens its own short transaction and must run
        # independent of (and never block) the assistant-message persistence.
        await self._record_terminal_metrics(job, terminal_status)

        if db_manager.session_factory is None:
            logger.warning("chat_run: db not initialized; skipping terminal persistence")
            return

        try:
            async with db_manager.session_factory() as db:
                try:
                    # Update ChatAgentRun row.
                    result = await db.execute(
                        select(ChatAgentRun).where(ChatAgentRun.id == job.run_id)
                    )
                    row = result.scalar_one_or_none()
                    if row is None:
                        # Row may not exist for anonymous runs where we skipped
                        # the initial insert; create it now best-effort.
                        row = ChatAgentRun(
                            id=job.run_id,
                            session_id=job.session_id,
                            user_id=job.user_id,
                            owner_scope=job.owner_scope,
                            agent_kind=job.agent_kind,
                            status=terminal_status,
                            user_message=job.user_message,
                            request_json=json.dumps(
                                job.request_payload, ensure_ascii=False
                            ),
                            started_at=datetime.utcfromtimestamp(
                                time.time() - (time.monotonic() - job.started_at)
                            ),
                        )
                        db.add(row)
                    row.status = terminal_status
                    row.answer = job.answer or None
                    row.model = job.model or None
                    row.error = job.error
                    row.workspace_path = job.workspace_path
                    row.finished_at = datetime.utcnow()
                    try:
                        row.trace_events_json = json.dumps(
                            job.trace_events, ensure_ascii=False, default=str
                        )
                    except Exception:  # noqa: BLE001
                        row.trace_events_json = None

                    # Persist assistant message + maybe generate title.
                    if remember and job.user_id and job.answer:
                        await chat_history_service.append_message(
                            db,
                            user_id=job.user_id,
                            session_id=job.session_id,
                            role="ai",
                            content=job.answer,
                        )
                        try:
                            from app.services.ai_chat_service import ai_chat_service

                            await ai_chat_service._try_generate_and_update_session_title(  # noqa: SLF001
                                db,
                                user=type("U", (), {"id": job.user_id})(),
                                session_id=job.session_id,
                                user_content=job.user_message,
                                answer_text=job.answer,
                            )
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "chat_run: title generation skipped", exc_info=True
                            )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "chat_run: failed to persist terminal run_id=%s: %s",
                job.run_id,
                exc,
                exc_info=True,
            )

    # ---- start / drive GeneralAgent runs ---------------------------------

    async def start_general_run(
        self,
        *,
        db: AsyncSession,
        user: Optional[User],
        owner_scope: str,
        session_id: str,
        user_message: str,
        history: List[Dict[str, str]],
        system_prompt_override: Optional[str] = None,
        remember: bool = True,
        locale: Optional[str] = None,
        images_json: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> ChatRunJob:
        """Create a new GeneralAgent run and start its background task.

        ``run_id`` lets the caller pre-mint the id so work done *before* the run
        starts (image OCR, for one) can be metered under the same run and shown
        as a single invocation in the admin audit feed. Omitted, one is minted
        here as before.
        """
        from app.services.chat_history_service import chat_history_service

        self._evict_finished()

        existing = self.get_active_job_for_session(owner_scope, session_id)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "该会话已有运行中的 agent run",
                    "active_run_id": existing.run_id,
                },
            )

        run_id = run_id or str(uuid.uuid4())
        request_payload = {
            "system_prompt_override": system_prompt_override,
            "remember": remember,
        }

        if remember and user is not None:
            try:
                await chat_history_service.append_message(
                    db,
                    user_id=user.id,
                    session_id=session_id,
                    role="user",
                    content=user_message,
                    images_json=images_json,
                )
                row = ChatAgentRun(
                    id=run_id,
                    session_id=session_id,
                    user_id=user.id,
                    owner_scope=owner_scope,
                    agent_kind="general",
                    status=RUN_STATUS_RUNNING,
                    user_message=user_message,
                    request_json=json.dumps(request_payload, ensure_ascii=False),
                    started_at=datetime.utcnow(),
                )
                db.add(row)
                await db.flush()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "chat_run: failed to persist initial general run row run_id=%s: %s",
                    run_id,
                    exc,
                )

        job = ChatRunJob(
            run_id=run_id,
            session_id=session_id,
            user_id=getattr(user, "id", None),
            owner_scope=owner_scope,
            agent_kind="general",
            status=RUN_STATUS_RUNNING,
            started_at=time.monotonic(),
            user_message=user_message,
            request_payload=request_payload,
        )
        self._jobs[run_id] = job
        self._active_by_owner_session[(owner_scope, session_id)] = run_id

        ctx_kwargs = {
            "session_id": session_id,
            "user_message": user_message,
            "images_json": images_json,
            "history": history,
            "system_prompt_override": system_prompt_override,
            "run_id": run_id,
            "owner_scope": owner_scope,
            "remember": remember,
            "locale": locale,
        }
        job.task = asyncio.create_task(self._run_general_job(job, ctx_kwargs))
        job.append_event(
            {
                "event": "run_start_pending",
                "run_id": run_id,
                "session_id": session_id,
                "agent_kind": "general",
            }
        )
        return job

    async def _run_general_job(
        self, job: ChatRunJob, ctx_kwargs: Dict[str, Any]
    ) -> None:
        """Background task: drive GeneralAgent.run_stream to completion."""
        from app.agents.general_agent.agent import GeneralAgent, GeneralAgentContext

        run_id = job.run_id
        session_id = job.session_id
        remember = bool(ctx_kwargs.pop("remember", True))
        ctx = GeneralAgentContext(**ctx_kwargs)

        last_event: Optional[Dict[str, Any]] = None
        try:
            async for ev in GeneralAgent().run_stream(ctx):
                if not isinstance(ev, dict):
                    continue
                ev_type = ev.get("type")
                if not ev_type:
                    continue
                job.append_trace(ev)
                last_event = ev

                if ev_type == "run_start":
                    model_value = ev.get("model")
                    if model_value:
                        job.model = str(model_value)
                    self._capture_run_usage(job, ev)
                if ev_type == "run_complete":
                    self._capture_run_usage(job, ev)
                    final_text = ev.get("final_text")
                    if isinstance(final_text, str):
                        job.answer = final_text
                    suggested = ev.get("suggested_agent_type")
                    if isinstance(suggested, str) and suggested:
                        job.suggested_agent_type = suggested
                if ev_type == "error":
                    self._capture_run_usage(job, ev)

                payload_out: Dict[str, Any] = {
                    k: v for k, v in ev.items() if k != "type"
                }
                payload_out["event"] = ev_type
                payload_out["run_id"] = run_id
                payload_out["session_id"] = session_id
                job.append_event(payload_out)
            terminal_status = RUN_STATUS_SUCCEEDED
            terminal_error: Optional[str] = None
            if isinstance(last_event, dict) and last_event.get("type") == "error":
                terminal_status = RUN_STATUS_FAILED
                terminal_error = str(last_event.get("message") or "agent error")
        except asyncio.CancelledError:
            logger.info("chat_run: general run cancelled run_id=%s", run_id)
            terminal_status = RUN_STATUS_CANCELLED
            terminal_error = "用户取消"
            job.append_event(
                {
                    "event": "cancelled",
                    "run_id": run_id,
                    "session_id": session_id,
                    "message": terminal_error,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("chat_run: general run failed run_id=%s", run_id)
            terminal_status = RUN_STATUS_FAILED
            terminal_error = str(exc)
            job.append_event(
                {
                    "event": "error",
                    "run_id": run_id,
                    "session_id": session_id,
                    "message": terminal_error,
                }
            )

        job.mark_status(terminal_status, error=terminal_error)
        await self._persist_terminal(job, terminal_status, remember=remember)
        history = ctx_kwargs.get("history") or []
        composed_messages: List[Dict[str, Any]] = [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in history
            if isinstance(m, dict)
        ]
        if job.user_message:
            composed_messages.append({"role": "user", "content": job.user_message})
        if job.answer:
            composed_messages.append({"role": "ai", "content": job.answer})
        job.append_event(
            {
                "event": "done",
                "run_id": run_id,
                "session_id": session_id,
                "status": terminal_status,
                "answer": job.answer,
                "model": job.model,
                "error": job.error,
                "trace_truncated": job.trace_truncated,
                "messages": composed_messages,
                "suggested_agent_type": job.suggested_agent_type,
            }
        )
        key = (job.owner_scope, session_id)
        if self._active_by_owner_session.get(key) == run_id:
            self._active_by_owner_session.pop(key, None)

    # ---- subscribe -------------------------------------------------------

    async def subscribe(
        self, run_id: str, owner_scope: Optional[str] = None
    ) -> AsyncIterator[str]:
        """Replay buffered SSE events for ``run_id`` then poll until terminal.

        Late subscribers see the full history because the buffer is the source
        of truth. Heartbeats fire every 15s of inactivity to keep proxies open.
        Cancellation of the subscription does NOT cancel the underlying job.
        """
        job = self._jobs.get(run_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="未找到该 run",
            )
        self._check_owner_scope(job, owner_scope)

        sent = 0
        last_activity = time.monotonic()
        while True:
            saw_new = False
            while sent < len(job.events):
                yield self._sse_event(job.events[sent])
                sent += 1
                saw_new = True
            if saw_new:
                last_activity = time.monotonic()
            if job.status in TERMINAL_RUN_STATUSES and sent >= len(job.events):
                return
            await asyncio.sleep(_SUBSCRIBE_POLL_INTERVAL_SECONDS)
            now = time.monotonic()
            if now - last_activity >= _HEARTBEAT_INTERVAL_SECONDS:
                elapsed = int(now - job.started_at)
                yield self._sse_event(
                    {
                        "event": "system_notice",
                        "kind": "heartbeat",
                        "run_id": job.run_id,
                        "session_id": job.session_id,
                        "elapsed_seconds": elapsed,
                        "timestamp": time.time(),
                    }
                )
                last_activity = now

    # ---- external (log_analysis) integration ----------------------------

    def register_external_job(
        self,
        *,
        run_id: str,
        session_id: str,
        user_id: Optional[str],
        owner_scope: str,
        agent_kind: str,
        user_message: str,
        request_payload: Dict[str, Any],
        events_ref: List[Dict[str, Any]],
        trace_events_ref: List[Dict[str, Any]],
        cancel_callback: Optional[Callable[[], None]] = None,
    ) -> ChatRunJob:
        """Register a run whose execution is driven by another service.

        Used by :class:`LogAnalysisChatService` to project its in-memory
        ``AgentJob`` lifecycle into the unified ChatRunJob registry so:

        - ``GET /chat/sessions/{session_id}/active-run`` returns the
          running log-analysis snapshot (events/trace_events/answer).
        - ``list_chat_sessions`` sidebar overlay shows a spinner for the
          session.
        - ``cross-session HITL`` / restore code paths see the same run.

        ``events_ref`` / ``trace_events_ref`` are shared list references —
        appends made by the external driver are immediately visible through
        the snapshot. The external driver is responsible for calling
        :meth:`mark_external_terminal` when the run finishes/cancels.
        """
        self._evict_finished()
        existing = self.get_active_job_for_session(owner_scope, session_id)
        if existing is not None and existing.status not in TERMINAL_RUN_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "该会话已有运行中的 agent run",
                    "active_run_id": existing.run_id,
                },
            )
        job = ChatRunJob(
            run_id=run_id,
            session_id=session_id,
            user_id=user_id,
            owner_scope=owner_scope,
            agent_kind=agent_kind,
            status=RUN_STATUS_RUNNING,
            started_at=time.monotonic(),
            user_message=user_message,
            request_payload=request_payload,
        )
        job.cancel_callback = cancel_callback
        # Share buffer references with the external driver so live subscribers
        # of either side see the same event stream.
        job.events = events_ref
        job.trace_events = trace_events_ref
        self._jobs[run_id] = job
        self._active_by_owner_session[(owner_scope, session_id)] = run_id
        return job

    def mark_external_terminal(
        self,
        run_id: str,
        terminal_status: str,
        *,
        answer: str = "",
        model: str = "",
        error: Optional[str] = None,
    ) -> None:
        """Mark an externally-driven run as terminal in the registry.

        Counterpart to :meth:`register_external_job`. Idempotent. Clears the
        active-session pointer so a new run can start immediately on the
        same ``(owner_scope, session_id)``.
        """
        job = self._jobs.get(run_id)
        if job is None:
            return
        if answer:
            job.answer = answer
        if model:
            job.model = model
        job.mark_status(terminal_status, error=error)
        key = (job.owner_scope, job.session_id)
        if self._active_by_owner_session.get(key) == run_id:
            self._active_by_owner_session.pop(key, None)

    # ---- cancel ----------------------------------------------------------

    def cancel(self, run_id: str, owner_scope: Optional[str] = None) -> bool:
        """Request cancellation of a running job. Returns True if a cancel
        signal was sent, False if the run is already terminal or unknown."""
        job = self._jobs.get(run_id)
        if job is None:
            return False
        self._check_owner_scope(job, owner_scope)
        if job.status in TERMINAL_RUN_STATUSES:
            return False
        if job.task is not None and not job.task.done():
            job.task.cancel()
        job.cancel_event.set()
        # External-driver runs have no asyncio.Task here — propagate the
        # cancel into the owning service (sets its threading.Event so the
        # agent loop exits between SDK messages).
        if job.cancel_callback is not None:
            try:
                job.cancel_callback()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "chat_run: external cancel callback failed run_id=%s: %s",
                    run_id,
                    exc,
                )
        return True

    def evict_finished_jobs(self) -> None:
        """Public hook for tests / periodic sweepers."""
        self._evict_finished()

    # ---- SSE helper ------------------------------------------------------

    @staticmethod
    def _sse_event(payload: Dict[str, Any]) -> str:
        safe_payload = jsonable_encoder(payload)
        return f"data: {json.dumps(safe_payload, ensure_ascii=False)}\n\n"


# Module-level singleton — imported by API routes and AIChatService.
chat_run_service = ChatRunService()


__all__ = [
    "ChatRunJob",
    "ChatRunService",
    "chat_run_service",
    "RUN_STATUS_QUEUED",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_SUCCEEDED",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_CANCELLED",
    "RUN_STATUS_STALE",
    "TERMINAL_RUN_STATUSES",
]
