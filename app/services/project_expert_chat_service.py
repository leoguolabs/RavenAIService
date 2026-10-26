"""Main-chat Project Expert Agent workflow.

This service keeps a persistent filesystem workspace per chat session so a
user can pick a registered project once and continue asking follow-up
questions against the same cloned repository.

It is modelled on ``LogAnalysisChatService`` but with the attached-log
analysis path removed entirely:

- No file upload, no archive extraction, no ``logs/`` directory, no
  ``metadata.json`` lookup.
- ``project_repo_id`` is REQUIRED for a new session; the service resolves the
  registered project and writes its non-sensitive identity into
  ``task.json.repo_info`` (``source == "user_selected_project_repo"``).
- Session-scoped persistent workspace: follow-ups reuse the checkout until the
  user selects another project, which gets a fresh workspace in the same chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.project_expert.agent import (
    ProjectExpertAgent,
    extract_recoverable_result_fields,
)
from app.agents.project_expert.workspace import WorkspaceContext, cleanup, prepare
from app.agents.clarification import ClarificationBinding
from app.config import settings
from app.models.chat import ChatMessage, ImageAttachment
from app.models.user import User
from app.services import chat_image_store, ocr_service
from app.services.agent_session_title import PendingSessionTitle, generate_after_commit
from app.services.chat_history_service import chat_history_service

logger = logging.getLogger(__name__)


_SESSION_KEY_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_AGENT_PROGRESS_INTERVAL_SECONDS = 15
_JOB_POLL_INTERVAL_SECONDS = 0.2
# Keep finished Jobs around for late /result polling and post-disconnect reconnect.
_JOB_RETENTION_SECONDS = 30 * 60


@dataclass
class AgentJob:
    """In-process record of a running or recently-finished project-expert Agent task.

    The Agent runs as a long-lived asyncio task; SSE streams are just views
    that subscribe to ``events``. Cancellation is best-effort and signaled via
    ``cancel_event`` (checked between SDK messages inside the Agent loop).
    """

    session_id: str
    task_id: str
    context_meta: Dict[str, Any]
    question: str
    user_id: Optional[Any]
    remember: bool
    started_at: float
    # run_id projects this job into the unified chat_agent_runs lifecycle so
    # the sidebar overlay and /active-run snapshot can see project-expert runs
    # alongside DeviceAgent / log-analysis runs.
    run_id: str = ""
    owner_scope: str = ""
    project_repo_id: Optional[int] = None
    # Metadata for images attached to this turn; persisted with the user
    # message so history reloads can re-render the thumbnails.
    images_json: Optional[str] = None
    # AskUserQuestion wiring for this run (user preference + broker registry +
    # cancel hook). ``None`` when the user turned clarification off, which is
    # exactly what makes the agent unable to ask.
    clarification: Optional[Any] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    # Raw AgentTraceEvent payloads (no SSE wrapper) for late-subscriber
    # full-history replay and for the final `done` frame to carry the
    # complete trace.
    trace_events: List[Dict[str, Any]] = field(default_factory=list)
    done: bool = False
    cancel_requested: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    finished_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    answer: Optional[str] = None
    error: Optional[str] = None
    task: Optional[asyncio.Task] = None


class ProjectExpertChatService:
    """Run ProjectExpertAgent from the main chat composer."""

    def __init__(self) -> None:
        self.registry_dir = (
            Path(settings.code_repo_clone_base_dir) / "chat_project_expert_sessions"
        )
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        # In-process Job registry. Single-process uvicorn deployment is assumed.
        # If we move to multi-worker, swap this for a Redis-backed registry.
        self._jobs: Dict[str, AgentJob] = {}

    # ───────────────────────── Public helpers ──────────────────────────

    def session_has_workspace(self, session_id: Optional[str]) -> bool:
        """Whether a persistent workspace context already exists for the session.

        Used by the API layer to decide whether ``project_repo_id`` is required
        (only required when starting a brand-new session).
        """
        if not session_id:
            return False
        if session_id in self._jobs:
            return True
        return self._context_file(session_id).exists()

    # ───────────────────────────── Stream ──────────────────────────────

    async def stream(
        self,
        *,
        message: str,
        session_id: Optional[str],
        history_json: Optional[str],
        remember: bool,
        project_repo_id: Optional[int],
        db: Optional[AsyncSession],
        user: Optional[User],
        owner_scope: Optional[str] = None,
        images: Optional[List[ImageAttachment]] = None,
        locale: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """SSE stream for one project-expert turn.

        Behavior:
        - If an in-flight Job exists for ``session_id``: subscribe and replay
          buffered events.
        - New session: ``project_repo_id`` is required; the selected project is
          resolved and a workspace is prepared (``repo/`` placeholder only).
        - Follow-up turn: reuse the workspace for the same project, or create
          a fresh workspace for a newly selected project in the same session.

        SSE client disconnects do NOT cancel the underlying Agent Job.
        """
        effective_session_id = session_id or str(uuid.uuid4())
        question = (message or "").strip()

        self._evict_old_jobs()

        yield self._sse_event({"event": "session", "session_id": effective_session_id})

        logger.info(
            "project-expert chat: stream started session_id=%s project_repo_id=%s",
            effective_session_id,
            project_repo_id,
        )

        existing_job = self._jobs.get(effective_session_id)

        # Subscribe path: in-flight Job already exists for this session.
        if existing_job is not None and not existing_job.done:
            if question and project_repo_id is not None and (
                existing_job.project_repo_id is None
                or int(project_repo_id) != int(existing_job.project_repo_id)
            ):
                yield self._sse_event(
                    {
                        "event": "error",
                        "reason": "project_switch_while_running",
                        "message": "当前项目专家任务仍在运行，请待其结束后重试切换项目的这条消息。",
                    }
                )
                return
            yield self._sse_event(
                {
                    "event": "project_expert_status",
                    "message": "已重新连接到正在运行的分析任务，继续推送已积累的进度...",
                    "reattached": True,
                }
            )
            async for chunk in self._subscribe(existing_job):
                yield chunk
            return

        # Re-subscribe path: Job already done and still cached; replay terminal events.
        if existing_job is not None and existing_job.done and not question:
            async for chunk in self._subscribe(existing_job):
                yield chunk
            return

        try:
            loaded = self._load_context(effective_session_id, user=user)

            if loaded is None:
                # New session: project_repo_id is required.
                if project_repo_id is None:
                    yield self._sse_event(
                        {
                            "event": "error",
                            "reason": "project_repo_required",
                            "message": "请先选择一个关联项目，再开始向项目专家提问。",
                        }
                    )
                    return

                repo = await self._resolve_project_repo(db, project_repo_id)
                if repo is None:
                    yield self._sse_event(
                        {
                            "event": "error",
                            "message": "所选项目不存在、已禁用或未启用项目专家 Agent，请重新选择。",
                        }
                    )
                    return

                if not question:
                    question = "请基于这个项目的源码，介绍它的整体结构和关键模块。"

                yield self._sse_event(
                    {
                        "event": "project_expert_status",
                        "message": (
                            f"已选定项目 `{getattr(repo, 'project_name', None) or getattr(repo, 'project_code', '')}`，"
                            "正在建立项目专家工作区..."
                        ),
                    }
                )
                ctx, context_meta = self._create_context_for_project(
                    session_id=effective_session_id,
                    question=question,
                    repo=repo,
                    user=user,
                )
                yield self._sse_event(
                    {
                        "event": "project_expert_context",
                        "session_id": effective_session_id,
                        "project_repo_id": context_meta.get("project_repo_id"),
                        "project_code": context_meta.get("project_code"),
                    }
                )
            else:
                ctx, context_meta = loaded
                bound_repo_id = context_meta.get("project_repo_id")
                if (
                    project_repo_id is not None
                    and (
                        bound_repo_id is None
                        or int(project_repo_id) != int(bound_repo_id)
                    )
                ):
                    repo = await self._resolve_project_repo(db, project_repo_id)
                    if repo is None:
                        yield self._sse_event(
                            {
                                "event": "error",
                                "reason": "project_repo_invalid",
                                "message": "所选项目不存在、已禁用或未启用项目专家 Agent，请重新选择。",
                            }
                        )
                        return
                    previous_project = (
                        context_meta.get("project_name")
                        or context_meta.get("project_code")
                    )
                    ctx, context_meta = self._create_context_for_project(
                        session_id=effective_session_id,
                        question=question,
                        repo=repo,
                        user=user,
                    )
                    yield self._sse_event(
                        {
                            "event": "agent_trace",
                            "type": "system_notice",
                            "kind": "project_switched",
                            "task_id": ctx.task_id,
                            "message": (
                                f"已从项目 `{previous_project}` 切换到 "
                                f"`{context_meta.get('project_name') or context_meta.get('project_code')}`，"
                                "已建立独立代码工作区，并保留本会话的对话历史。"
                            ),
                            "timestamp": time.time(),
                        }
                    )
                    yield self._sse_event(
                        {
                            "event": "project_expert_context",
                            "session_id": effective_session_id,
                            "project_repo_id": context_meta.get("project_repo_id"),
                            "project_code": context_meta.get("project_code"),
                        }
                    )
                if not question:
                    question = "请继续基于这个项目的源码回答我的问题。"
                yield self._sse_event(
                    {
                        "event": "project_expert_status",
                        "message": "已准备当前选择项目的工作区，正在基于该项目代码继续分析...",
                    }
                )

            # Minted before OCR so the OCR usage event carries the same run_id as
            # the agent run it preprocesses for — that pairing is what lets the
            # admin audit feed show them as one invocation.
            run_id = str(uuid.uuid4())

            # OCR-merge any attached images into the question before the agent
            # runs; degrade to text-only when OCR is unconfigured/failed.
            # Persist the originals so history reloads can re-render the
            # thumbnails, and materialize them into the agent workspace as
            # groundwork for a future multimodal path (no-op unless the
            # provider supports image input — see chat_image_store).
            stored_images = chat_image_store.save_turn_images(
                images, session_id=effective_session_id
            )
            images_json = chat_image_store.to_meta_json(stored_images)
            chat_image_store.materialize_into_workspace(stored_images, ctx.temp_dir)

            question, ocr_meta = await ocr_service.enrich_message(
                question,
                images,
                user_id=str(getattr(user, "id", None)) if getattr(user, "id", None) else None,
                session_id=effective_session_id,
                run_id=run_id,
                locale=locale,
                project_repo_id=str(context_meta.get("project_repo_id"))
                if context_meta.get("project_repo_id") is not None
                else None,
            )
            if ocr_meta.image_count > 0 and ocr_meta.status in ("unconfigured", "failed"):
                yield self._sse_event(
                    {
                        "event": "ocr_status",
                        "status": ocr_meta.status,
                        "image_count": ocr_meta.image_count,
                        "error_kind": ocr_meta.error_kind,
                    }
                )
            elif (
                ocr_meta.image_count > 0
                and ocr_meta.status == "succeeded"
                and ocr_meta.text
            ):
                yield self._sse_event(
                    {
                        "event": "ocr_result",
                        "status": ocr_meta.status,
                        "image_count": ocr_meta.image_count,
                        "text": ocr_meta.text,
                    }
                )

            history_hint = await self._build_history_hint(
                db=db,
                user=user,
                session_id=effective_session_id,
                history_json=history_json,
                workspace_dir=ctx.temp_dir,
            )
            # Drive prompt selection + the response-language directive for this
            # run. Resolved from the request/owner at the API layer; falls back
            # to the default when absent.
            if locale:
                from app.i18n import normalize

                ctx.locale = normalize(locale)
            self._bind_question_and_hints(ctx, question=question, hints=history_hint)

            yield self._sse_event(
                {
                    "event": "project_expert_status",
                    "message": "Project Expert Agent 正在读取项目代码上下文...",
                }
            )

            effective_owner_scope = owner_scope or (
                f"user:{user.id}" if getattr(user, "id", None) else "anon:legacy"
            )
            job = AgentJob(
                session_id=effective_session_id,
                task_id=ctx.task_id,
                context_meta=context_meta,
                question=question,
                user_id=getattr(user, "id", None),
                remember=bool(remember),
                images_json=images_json,
                started_at=time.monotonic(),
                run_id=run_id,
                owner_scope=effective_owner_scope,
                project_repo_id=context_meta.get("project_repo_id"),
                # Honour the user's global "let the agent ask me when the
                # instruction is unclear" preference on this agent too, not just
                # DeviceAgent. Returns None when the preference is off.
                clarification=ClarificationBinding.for_chat_run(
                    user=user,
                    run_id=run_id,
                    session_id=effective_session_id,
                    cancel_run=lambda sid=effective_session_id: self.cancel(sid),
                ),
            )
            self._jobs[effective_session_id] = job
            # Announce run_id to subscribers (replayed by _subscribe) so the
            # frontend can latch it and target the unified cancel endpoint.
            job.events.append(
                {
                    "event": "session",
                    "session_id": effective_session_id,
                    "run_id": run_id,
                }
            )
            await self._register_chat_run(job, ctx)
            job.task = asyncio.create_task(self._run_job_async(job, ctx))

            logger.info(
                "project-expert chat: agent job scheduled session_id=%s task_id=%s temp_dir=%s",
                effective_session_id,
                ctx.task_id,
                ctx.temp_dir,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "project-expert chat stream failed to start job: %s", exc, exc_info=True
            )
            yield self._sse_event({"event": "error", "message": str(exc)})
            return

        try:
            async for chunk in self._subscribe(job):
                yield chunk
        except asyncio.CancelledError:
            # SSE was cancelled by client disconnect. The Job keeps running
            # in the background; the client (or another) can reattach.
            logger.warning(
                "project-expert chat stream cancelled (client disconnect): session_id=%s job still running",
                effective_session_id,
            )
            raise

    # ───────────────────────── Background task ─────────────────────────

    async def _run_job_async(self, job: AgentJob, ctx: WorkspaceContext) -> None:
        """Background Agent task. Survives SSE disconnects; persists to DB on completion."""
        pending_title = None
        try:
            def _emit_trace(event: Dict[str, Any]) -> None:
                job.trace_events.append(event)
                job.events.append({"event": "agent_trace", **event})

            result = await asyncio.to_thread(
                ProjectExpertAgent().run_sync,
                ctx,
                job.cancel_event,
                _emit_trace,
                job.clarification,
            )
            job.result = result
            answer_text = self._format_agent_result(
                result, question=job.question, context_meta=job.context_meta
            )
            job.answer = answer_text

            pending_title = await self._persist_job_result(job, result, answer_text)
            self._touch_context(job.session_id, result=result, answer=answer_text)

            job.events.append(
                {
                    "event": "done",
                    "session_id": job.session_id,
                    "answer": answer_text,
                    "model": result.get("model"),
                    "result": result,
                    "trace_summary": result.get("trace_summary"),
                    "trace_events": result.get("trace_events"),
                }
            )
            logger.info(
                "project-expert chat: agent job completed session_id=%s task_id=%s status=%s error_kind=%s duration=%ss",
                job.session_id,
                job.task_id,
                result.get("status"),
                result.get("error_kind"),
                int(time.monotonic() - job.started_at),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "project-expert chat: agent job failed session_id=%s: %s",
                job.session_id,
                exc,
                exc_info=True,
            )
            job.error = str(exc)
            job.events.append({"event": "error", "message": str(exc)})
        finally:
            job.done = True
            job.finished_at = time.monotonic()
            await self._finalize_chat_run(job)

        # Keep optional metadata work outside the answer's completion path.
        if pending_title is not None:
            from app.models.database import db_manager

            if db_manager.session_factory is not None:
                await generate_after_commit(pending_title, db_manager.session_factory)

    async def _register_chat_run(self, job: AgentJob, ctx: WorkspaceContext) -> None:
        """Create a ``chat_agent_runs`` row and register a ChatRunJob shadow."""
        from app.models.database import db_manager
        from app.models.user import ChatAgentRun
        from app.services.chat_run_service import (
            RUN_STATUS_RUNNING,
            chat_run_service,
        )

        request_payload: Dict[str, Any] = {
            "project_repo_id": job.context_meta.get("project_repo_id"),
            "project_code": job.context_meta.get("project_code"),
        }

        try:
            chat_run_service.register_external_job(
                run_id=job.run_id,
                session_id=job.session_id,
                user_id=str(job.user_id) if job.user_id is not None else None,
                owner_scope=job.owner_scope,
                agent_kind="project_expert",
                user_message=job.question,
                request_payload=request_payload,
                events_ref=job.events,
                trace_events_ref=job.trace_events,
                cancel_callback=lambda sid=job.session_id: self.cancel(sid),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "project-expert chat: chat_run_service register failed run_id=%s: %s",
                job.run_id,
                exc,
            )

        if db_manager.session_factory is None:
            return
        try:
            async with db_manager.session_factory() as db:
                try:
                    if job.remember and job.user_id is not None:
                        await chat_history_service.ensure_session_summary(
                            db,
                            user_id=str(job.user_id),
                            session_id=job.session_id,
                            title_hint=job.question,
                        )
                    row = ChatAgentRun(
                        id=job.run_id,
                        session_id=job.session_id,
                        user_id=(
                            str(job.user_id) if job.user_id is not None else None
                        ),
                        owner_scope=job.owner_scope or "anon:legacy",
                        agent_kind="project_expert",
                        status=RUN_STATUS_RUNNING,
                        user_message=job.question or "",
                        request_json=json.dumps(request_payload, ensure_ascii=False),
                        workspace_path=ctx.temp_dir,
                        started_at=datetime.utcnow(),
                    )
                    db.add(row)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "project-expert chat: chat_agent_runs row insert failed run_id=%s: %s",
                job.run_id,
                exc,
            )

    async def _finalize_chat_run(self, job: AgentJob) -> None:
        """Update ``chat_agent_runs`` terminal state and clear active pointer."""
        if not job.run_id:
            return

        from app.models.database import db_manager
        from app.models.user import ChatAgentRun
        from app.services.chat_run_service import (
            RUN_STATUS_CANCELLED,
            RUN_STATUS_FAILED,
            RUN_STATUS_SUCCEEDED,
            chat_run_service,
        )

        agent_status = ""
        if isinstance(job.result, dict):
            agent_status = str(job.result.get("status") or "")
        if job.error:
            terminal_status = RUN_STATUS_FAILED
        elif agent_status == "cancelled" or (
            job.cancel_requested and not job.answer
        ):
            terminal_status = RUN_STATUS_CANCELLED
        else:
            terminal_status = RUN_STATUS_SUCCEEDED

        result_model = ""
        if isinstance(job.result, dict):
            model_value = job.result.get("model")
            if isinstance(model_value, str):
                result_model = model_value

        try:
            chat_run_service.mark_external_terminal(
                job.run_id,
                terminal_status,
                answer=job.answer or "",
                model=result_model,
                error=job.error,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "project-expert chat: chat_run_service finalize failed run_id=%s: %s",
                job.run_id,
                exc,
            )

        # Best-effort AI usage metrics, idempotent on run_id. Project repository
        # metadata is attached when available (project_code is allowlisted).
        try:
            from app.services import metrics_service

            project_repo_id = job.context_meta.get("project_repo_id")
            project_code = job.context_meta.get("project_code")
            extra_metadata = (
                {"project_code": str(project_code)} if project_code else None
            )
            await metrics_service.record_agent_run_usage(
                source="project_expert_agent",
                agent_kind="project_expert",
                run_id=job.run_id,
                result=job.result,
                terminal_status=terminal_status,
                provider=settings.anthropic_provider,
                user_id=str(job.user_id) if job.user_id is not None else None,
                owner_scope=job.owner_scope or None,
                session_id=job.session_id,
                task_id=job.task_id,
                project_repo_id=str(project_repo_id) if project_repo_id is not None else None,
                extra_metadata=extra_metadata,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "project-expert chat: metrics record skipped run_id=%s: %s",
                job.run_id,
                exc,
            )

        if db_manager.session_factory is None:
            return
        try:
            async with db_manager.session_factory() as db:
                try:
                    from sqlalchemy import select

                    result = await db.execute(
                        select(ChatAgentRun).where(ChatAgentRun.id == job.run_id)
                    )
                    row = result.scalar_one_or_none()
                    if row is None:
                        return
                    row.status = terminal_status
                    row.answer = job.answer or None
                    row.model = result_model or None
                    row.error = job.error
                    row.finished_at = datetime.utcnow()
                    try:
                        row.trace_events_json = json.dumps(
                            list(job.trace_events), ensure_ascii=False, default=str
                        )
                    except Exception:  # noqa: BLE001
                        row.trace_events_json = None
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "project-expert chat: chat_agent_runs finalize failed run_id=%s: %s",
                job.run_id,
                exc,
            )

    async def _persist_job_result(
        self,
        job: AgentJob,
        result: Dict[str, Any],
        answer_text: str,
    ) -> Optional[PendingSessionTitle]:
        """Commit the exchange and return optional title work for after completion."""
        from app.models.database import db_manager

        if db_manager.session_factory is None:
            logger.warning("project-expert chat: db not initialized; skipping persistence")
            return

        if not (job.remember and job.user_id is not None):
            return

        try:
            async with db_manager.session_factory() as db:
                try:
                    pending_title = await self._persist_exchange(
                        db=db,
                        user_id=job.user_id,
                        session_id=job.session_id,
                        question=job.question,
                        answer=answer_text,
                        images_json=job.images_json,
                        project_code=job.context_meta.get("project_code"),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
            return pending_title
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "project-expert chat: failed to persist job result session_id=%s: %s",
                job.session_id,
                exc,
                exc_info=True,
            )

    # ─────────────────────────── Subscribe ─────────────────────────────

    async def _subscribe(self, job: AgentJob) -> AsyncIterator[str]:
        """Yield SSE chunks for this Job. Replays the full event buffer to
        late subscribers, then polls for new events until the Job is done.
        """
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
            if job.done:
                return
            await asyncio.sleep(_JOB_POLL_INTERVAL_SECONDS)
            now = time.monotonic()
            if now - last_activity >= _AGENT_PROGRESS_INTERVAL_SECONDS:
                elapsed = int(now - job.started_at)
                yield self._sse_event(
                    {
                        "event": "agent_trace",
                        "type": "system_notice",
                        "task_id": job.task_id,
                        "kind": "heartbeat",
                        "elapsed_seconds": elapsed,
                        "timestamp": time.time(),
                    }
                )
                last_activity = now

    # ───────────────────────── Cancel / status ─────────────────────────

    def cancel(self, session_id: str, user: Optional[User] = None) -> bool:
        """Request cancellation of the in-flight Agent Job for ``session_id``."""
        job = self._jobs.get(session_id or "")
        if job is None or job.done:
            return False
        if user is not None and job.user_id is not None and getattr(user, "id", None) != job.user_id:
            raise PermissionError("当前用户无权取消这个分析任务")
        if job.cancel_requested:
            return True
        job.cancel_requested = True
        job.cancel_event.set()
        job.events.append(
            {
                "event": "project_expert_status",
                "message": "已收到取消请求，正在等待 Agent 退出（最多一条 SDK 消息的延迟）...",
                "cancel_requested": True,
            }
        )
        logger.info("project-expert chat: cancel requested session_id=%s", session_id)
        return True

    def get_status(self, session_id: str, user: Optional[User] = None) -> Dict[str, Any]:
        """Return the current state snapshot of the Job for polling clients."""
        job = self._jobs.get(session_id or "")
        if job is None:
            return {"session_id": session_id, "status": "not_found"}
        if user is not None and job.user_id is not None and getattr(user, "id", None) != job.user_id:
            raise PermissionError("当前用户无权查看这个分析任务")
        now = time.monotonic()
        elapsed = int((job.finished_at or now) - job.started_at)
        return {
            "session_id": session_id,
            "status": "done" if job.done else "running",
            "cancel_requested": job.cancel_requested,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "elapsed_seconds": elapsed,
            "project_repo_id": job.project_repo_id,
            "events": list(job.events),
            "result": job.result,
            "answer": job.answer or "",
            "error": job.error,
        }

    def _evict_old_jobs(self) -> None:
        """Lazy cleanup of Jobs that finished more than _JOB_RETENTION_SECONDS ago."""
        if not self._jobs:
            return
        cutoff = time.monotonic() - _JOB_RETENTION_SECONDS
        stale = [
            sid
            for sid, job in self._jobs.items()
            if job.done and (job.finished_at or 0) < cutoff
        ]
        for sid in stale:
            self._jobs.pop(sid, None)
            logger.debug("project-expert chat: evicted finished job session_id=%s", sid)

    # ───────────────────────── Context handling ────────────────────────

    async def _resolve_project_repo(
        self, db: Optional[AsyncSession], project_repo_id: int
    ) -> Optional[Any]:
        if db is None:
            return None
        try:
            from app.services import project_repo_service

            repo = await project_repo_service.get_by_id(db, project_repo_id)
            if repo and not await project_repo_service.supports_agent(
                db, repo, "project_expert"
            ):
                logger.info(
                    "project-expert chat: project_repo_id=%s does not enable project_expert",
                    project_repo_id,
                )
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("project-expert chat: 校验 project_repo_id 失败: %s", exc)
            return None
        if not repo or not getattr(repo, "enabled", True):
            return None
        return repo

    def _create_context_for_project(
        self,
        *,
        session_id: str,
        question: str,
        repo: Any,
        user: Optional[User],
    ) -> Tuple[WorkspaceContext, Dict[str, Any]]:
        old_context = self._load_context(session_id, user=user)
        old_ctx = old_context[0] if old_context else None

        ctx = prepare(
            project_repo=repo,
            question=question,
            hints="",
            session_id=session_id,
        )

        context_meta = {
            "session_id": session_id,
            "owner_user_id": getattr(user, "id", None),
            "task_id": ctx.task_id,
            "temp_dir": ctx.temp_dir,
            "repo_dir": ctx.repo_dir,
            "task_json_path": ctx.task_json_path,
            "project_repo_id": getattr(repo, "id", None),
            "project_code": getattr(repo, "project_code", None),
            "project_name": getattr(repo, "project_name", None),
            "project_card": getattr(repo, "project_card", None),
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        try:
            self._save_context(session_id, context_meta)
        except Exception:
            cleanup(ctx)
            raise
        if old_ctx and old_ctx.temp_dir != ctx.temp_dir:
            cleanup(old_ctx)
        return ctx, context_meta

    def _bind_question_and_hints(
        self, ctx: WorkspaceContext, *, question: str, hints: str
    ) -> None:
        task_path = Path(ctx.task_json_path)
        try:
            task_data = json.loads(task_path.read_text(encoding="utf-8"))
            if not isinstance(task_data, dict):
                task_data = {}
        except Exception:
            task_data = {}

        task_data["question"] = question
        task_data["hints"] = hints
        task_data["conversation_context"] = hints
        if (Path(ctx.temp_dir) / "conversation_history.json").is_file():
            task_data["conversation_history_file"] = "conversation_history.json"
        else:
            task_data.pop("conversation_history_file", None)
        task_path.write_text(
            json.dumps(task_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ctx.metadata["question"] = question
        ctx.metadata["hints"] = hints

    async def _build_history_hint(
        self,
        *,
        db: Optional[AsyncSession],
        user: Optional[User],
        session_id: str,
        history_json: Optional[str],
        workspace_dir: Optional[str] = None,
    ) -> str:
        messages: List[ChatMessage] = []
        if user and db:
            try:
                records = await chat_history_service.fetch_messages(
                    db,
                    user_id=user.id,
                    session_id=session_id,
                )
                messages = chat_history_service.to_chat_messages(records)
            except Exception:
                messages = []

        if not messages:
            messages = self._parse_client_history(history_json)

        if not messages:
            if workspace_dir:
                (Path(workspace_dir) / "conversation_history.json").unlink(missing_ok=True)
            return ""

        if workspace_dir:
            transcript = [
                {"role": msg.role, "content": str(msg.content or "")}
                for msg in messages
            ]
            (Path(workspace_dir) / "conversation_history.json").write_text(
                json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        recent = messages[-12:]
        lines = [
            "以下是同一对话中最近的上下文；完整历史在 conversation_history.json。"
            "历史中旧项目的回答只用于理解指代，不能作为当前项目的源码证据："
        ]
        for msg in recent:
            role = "用户" if msg.role == "user" else "助手"
            content = " ".join(str(msg.content or "").split())
            if len(content) > 1600:
                content = content[:1600] + "..."
            if content:
                lines.append(f"- {role}: {content}")
        return "\n".join(lines)

    def _parse_client_history(self, history_json: Optional[str]) -> List[ChatMessage]:
        if not history_json:
            return []
        try:
            raw = json.loads(history_json)
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        messages: List[ChatMessage] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            if role not in {"user", "ai", "assistant", "system"}:
                role = "user"
            content = str(item.get("content") or "")
            if content:
                messages.append(ChatMessage(role=role, content=content))
        return messages

    async def _persist_exchange(
        self,
        *,
        db: AsyncSession,
        user_id: Any,
        session_id: str,
        question: str,
        answer: str,
        project_code: Optional[str],
        images_json: Optional[str] = None,
    ) -> Optional[PendingSessionTitle]:
        """Persist exchange; caller commits before generating the optional title."""
        user_content = question
        if project_code:
            user_content = f"{question}\n\n[项目专家] 项目: {project_code}"
        session = await chat_history_service.save_exchange(
            db,
            user_id=user_id,
            session_id=session_id,
            user_content=user_content,
            ai_content=answer,
            title_hint=question,
            user_images_json=images_json,
        )
        return PendingSessionTitle.from_exchange(session, user_content, answer)

    def _load_context(
        self,
        session_id: str,
        *,
        user: Optional[User],
        ignore_owner: bool = False,
    ) -> Optional[Tuple[WorkspaceContext, Dict[str, Any]]]:
        path = self._context_file(session_id)
        if not path.exists():
            return None
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

        owner_user_id = meta.get("owner_user_id")
        if not ignore_owner and owner_user_id and getattr(user, "id", None) != owner_user_id:
            raise PermissionError("当前用户无权访问这个项目专家上下文")

        required_paths = ["temp_dir", "repo_dir", "task_json_path"]
        if any(not meta.get(key) for key in required_paths):
            return None
        if not Path(meta["temp_dir"]).exists() or not Path(meta["task_json_path"]).exists():
            return None

        ctx = WorkspaceContext(
            task_id=meta.get("task_id") or Path(meta["temp_dir"]).name,
            temp_dir=meta["temp_dir"],
            repo_dir=meta["repo_dir"],
            task_json_path=meta["task_json_path"],
            metadata={
                "question": "",
                "hints": "",
                "repo_info": {
                    "project_code": meta.get("project_code"),
                    "project_name": meta.get("project_name"),
                    "project_card": meta.get("project_card"),
                    "source": "user_selected_project_repo",
                },
            },
        )
        return ctx, meta

    def _save_context(self, session_id: str, meta: Dict[str, Any]) -> None:
        path = self._context_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _touch_context(self, session_id: str, *, result: Dict[str, Any], answer: str) -> None:
        path = self._context_file(session_id)
        if not path.exists():
            return
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            meta["updated_at"] = datetime.utcnow().isoformat()
            meta["last_result_status"] = result.get("status")
            meta["last_error_kind"] = result.get("error_kind")
            meta["last_answer_excerpt"] = answer[:1000]
            self._save_context(session_id, meta)
        except Exception:
            logger.debug("project-expert chat: failed to touch context", exc_info=True)

    def _context_file(self, session_id: str) -> Path:
        safe = _SESSION_KEY_RE.sub("_", session_id or "")
        if not safe:
            safe = str(uuid.uuid4())
        return self.registry_dir / f"{safe}.json"

    # ─────────────────────────── Formatting ────────────────────────────

    @staticmethod
    def _format_agent_result(
        result: Dict[str, Any],
        *,
        question: str,
        context_meta: Dict[str, Any],
    ) -> str:
        status = result.get("status") or "unknown"
        project = (
            context_meta.get("project_name")
            or context_meta.get("project_code")
            or "当前项目"
        )
        model = result.get("model") or "unknown"
        duration = result.get("duration_seconds")
        answer = str(result.get("answer") or result.get("summary") or "").strip()
        summary = str(result.get("summary") or "").strip()
        raw = result.get("raw")
        recovered = {}
        if not answer and status == "schema_mismatch" and isinstance(raw, str) and raw.strip():
            recovered = extract_recoverable_result_fields(raw)
            answer = str(recovered.get("answer") or recovered.get("summary") or "").strip()
            summary = str(recovered.get("summary") or summary or "").strip()
        display_status = "ok" if recovered and answer else status

        lines: List[str] = [
            "**项目专家 Agent** 已完成本轮分析。",
            "",
            f"- 项目：`{project}`",
            f"- 问题：{question}",
            f"- 状态：`{display_status}`",
            f"- 模型：`{model}`" + (f"，耗时：{duration}s" if duration is not None else ""),
            "- 上下文：已保留本次克隆的代码工作区，可在当前对话继续追问。",
        ]

        if result.get("error_kind"):
            lines.append(f"- 错误类型：`{result.get('error_kind')}`")

        if answer:
            lines.extend(["", "## 回答", answer])
        elif status == "schema_mismatch":
            lines.extend(["", "## 回答", "模型返回内容不完整，且未能提取出可展示的回答。"])

        if summary and summary != answer:
            lines.extend(["", "## 摘要", summary])

        keywords = result.get("related_keywords")
        if isinstance(keywords, list) and keywords:
            rendered = " ".join(
                f"`{str(keyword)}`" for keyword in keywords if str(keyword).strip()
            )
            if rendered:
                lines.extend(["", "## 关键词", rendered])

        return "\n".join(lines).strip()

    @staticmethod
    def _sse_event(payload: Dict[str, object]) -> str:
        safe_payload = jsonable_encoder(payload)
        return f"data: {json.dumps(safe_payload, ensure_ascii=False)}\n\n"


project_expert_chat_service = ProjectExpertChatService()
