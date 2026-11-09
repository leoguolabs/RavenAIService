"""Main-chat Package Search Agent workflow.

This service keeps a persistent filesystem workspace per chat session so a
user can pick a registered project once and continue asking follow-up
questions about that project's refactor packages (and, when needed, its
Git history) against the same cloned repository.

It mirrors ``ProjectExpertChatService``:

- ``project_repo_id`` is REQUIRED for a new session; the service resolves the
  registered project and writes its non-sensitive identity into
  ``task.json.repo_info`` (``source == "user_selected_project_repo"``).
- Session-scoped persistent workspace: the first turn clones into ``repo/``
  (only if the question needs Git context); follow-up turns reuse the same
  workspace and the agent reuses ``repo/.git``.
- A ``project_repo_id`` different from the first turn does NOT switch the
  bound project — a ``system_notice`` tells the user to start a new session.

The final ``done`` event additionally carries the package-search result
contract (``recommended_package_ids`` / ``relevant_package_ids`` / ``notes``)
so the frontend can render recommended-package cards.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi.encoders import jsonable_encoder
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.package_search.agent import (
    PACKAGING_TASK_VALUE_KEYS,
    PackageSearchAgent,
)
from app.agents.package_search.workspace import WorkspaceContext, cleanup, prepare
from app.agents.clarification import (
    ClarificationBinding,
    MandatoryClarificationError,
    request_mandatory_clarification,
)
from app.agents.hitl_broker import PermissionBroker
from app.agents.log_analysis.trace import SeqCounter
from app.config import settings
from app.models.chat import ChatMessage, ImageAttachment
from app.models.user import User
from app.services import chat_image_store, ocr_service
from app.services.chat_history_service import chat_history_service
from app.utils.storage_utils import get_free_bytes

logger = logging.getLogger(__name__)


_SESSION_KEY_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_AGENT_PROGRESS_INTERVAL_SECONDS = 15
_JOB_POLL_INTERVAL_SECONDS = 0.2
# Keep finished Jobs around for late /result polling and post-disconnect reconnect.
_JOB_RETENTION_SECONDS = 30 * 60
_PACKAGE_UPLOAD_CHUNK_BYTES = 1024 * 1024
_PACKAGE_UPLOAD_MAX_FILES = 100


@dataclass
class AgentJob:
    """In-process record of a running or recently-finished package-search Agent task.

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
    # the sidebar overlay and /active-run snapshot can see package-search runs
    # alongside DeviceAgent / log-analysis / project-expert runs.
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
    # A packaging turn is distinguished by a server-staged input manifest, not
    # by words in the prompt.  This prevents a normal package-search question
    # from accidentally entering the build/publication path.
    input_manifest: Optional[Dict[str, Any]] = None
    project_catalog: List[Dict[str, Any]] = field(default_factory=list)
    confirmed_plan: Optional[Dict[str, Any]] = None
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    # Mandatory package confirmation is service-enforced and may be waiting
    # while no SDK Agent is running.  Keep its broker on the Job so cancel()
    # can settle the pending Future immediately instead of waiting for the
    # full clarification timeout.
    mandatory_broker: Optional[PermissionBroker] = field(
        default=None, repr=False, compare=False
    )
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


class PackageSearchChatService:
    """Run PackageSearchAgent from the main chat composer."""

    def __init__(self) -> None:
        self.registry_dir = (
            Path(settings.code_repo_clone_base_dir) / "chat_package_search_sessions"
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

    def assert_session_access(
        self,
        session_id: Optional[str],
        *,
        owner_scope: str,
        user: Optional[User],
    ) -> None:
        """Fail before StreamingResponse starts when a cached Job has another owner."""
        if not session_id:
            return
        job = self._jobs.get(session_id)
        if job is None:
            return
        if job.owner_scope and job.owner_scope != owner_scope:
            raise PermissionError("当前用户无权访问这个配置管理员任务")
        if (
            not job.owner_scope
            and job.user_id is not None
            and getattr(user, "id", None) != job.user_id
        ):
            raise PermissionError("当前用户无权访问这个配置管理员任务")

    @staticmethod
    async def _close_uploads(files: List[UploadFile]) -> None:
        for upload in files:
            try:
                await upload.close()
            except Exception:
                pass

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
        files: Optional[List[UploadFile]] = None,
        locale: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """SSE stream for one package-search turn.

        Behavior:
        - If an in-flight Job exists for ``session_id``: subscribe and replay
          buffered events.
        - New session: ``project_repo_id`` is required; the selected project is
          resolved and a workspace is prepared (``repo/`` placeholder only).
        - Follow-up turn: reuse the existing session workspace. A
          ``project_repo_id`` different from the first turn does NOT switch the
          project — a ``system_notice`` tells the user to start a new session.

        SSE client disconnects do NOT cancel the underlying Agent Job.
        """
        effective_session_id = session_id or str(uuid.uuid4())
        question = (message or "").strip()
        uploaded_files = [item for item in (files or []) if item is not None]
        packaging_requested = bool(uploaded_files)
        effective_owner_scope = owner_scope or (
            f"user:{user.id}" if getattr(user, "id", None) else "anon:legacy"
        )

        self._evict_old_jobs()

        # Reconnect lookup must be owner-scoped before replaying even the first
        # cached frame.  Clarification questions contain uploaded basenames and
        # the terminal frame contains the download link.
        try:
            self.assert_session_access(
                effective_session_id,
                owner_scope=effective_owner_scope,
                user=user,
            )
        except PermissionError:
            await self._close_uploads(uploaded_files)
            raise

        yield self._sse_event({"event": "session", "session_id": effective_session_id})

        logger.info(
            "package-search chat: stream started session_id=%s project_repo_id=%s",
            effective_session_id,
            project_repo_id,
        )

        existing_job = self._jobs.get(effective_session_id)

        # Subscribe path: in-flight Job already exists for this session.
        if existing_job is not None and not existing_job.done:
            # A reconnect never consumes newly submitted attachments.
            await self._close_uploads(uploaded_files)
            if db is not None:
                await db.close()
            yield self._sse_event(
                {
                    "event": "package_search_status",
                    "message": "已重新连接到正在运行的检索任务，继续推送已积累的进度...",
                    "reattached": True,
                }
            )
            async for chunk in self._subscribe(existing_job):
                yield chunk
            return

        # Re-subscribe path: Job already done and still cached; replay terminal events.
        if existing_job is not None and existing_job.done and not question:
            await self._close_uploads(uploaded_files)
            if db is not None:
                await db.close()
            async for chunk in self._subscribe(existing_job):
                yield chunk
            return

        try:
            loaded = self._load_context(effective_session_id, user=user)

            if loaded is None:
                # Pure search remains project-scoped.  A packaging turn may
                # start unbound because project identity is one of the fields
                # the server must infer and then force the human to confirm.
                if project_repo_id is None and not packaging_requested:
                    yield self._sse_event(
                        {
                            "event": "error",
                            "reason": "project_repo_required",
                            "message": "请先选择一个项目再检索；上传组件打包时可由配置管理员初判项目。",
                        }
                    )
                    return

                repo = (
                    await self._resolve_project_repo(db, project_repo_id)
                    if project_repo_id is not None
                    else None
                )
                if project_repo_id is not None and repo is None:
                    yield self._sse_event(
                        {
                            "event": "error",
                            "message": "所选项目不存在、已禁用或未启用配置管理员 Agent，请重新选择。",
                        }
                    )
                    return

                if not question:
                    question = (
                        "请识别这些组件文件，制作软件升级整包并上传到重构包仓库。"
                        if packaging_requested
                        else "请介绍这个项目下重构包的整体情况。"
                    )

                yield self._sse_event(
                    {
                        "event": "package_search_status",
                        "message": (
                            f"已把项目 `{getattr(repo, 'project_name', None) or getattr(repo, 'project_code', '')}` 作为初判候选，"
                            "正在建立配置管理员工作区..."
                            if repo is not None
                            else "正在建立配置管理员工作区，随后会初判并强制确认目标项目..."
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
                        "event": "package_search_context",
                        "session_id": effective_session_id,
                        "project_repo_id": context_meta.get("project_repo_id"),
                        "project_code": context_meta.get("project_code"),
                    }
                )
            else:
                ctx, context_meta = loaded
                # Switching project mid-session is not silently honored.
                bound_repo_id = context_meta.get("project_repo_id")
                if (
                    project_repo_id is not None
                    and bound_repo_id is not None
                    and int(project_repo_id) != int(bound_repo_id)
                ):
                    yield self._sse_event(
                        {
                            "event": "agent_trace",
                            "type": "system_notice",
                            "kind": "project_switch_ignored",
                            "task_id": ctx.task_id,
                            "message": (
                                "本会话已绑定到首轮选择的项目，无法在追问中切换项目。"
                                "如需就另一个项目检索，请新开一个会话。"
                            ),
                            "timestamp": time.time(),
                        }
                    )
                if not question:
                    question = "请继续基于这个项目回答我的重构包问题。"
                yield self._sse_event(
                    {
                        "event": "package_search_status",
                        "message": "已复用当前配置管理员对话工作区，正在继续分析...",
                    }
                )

            # Minted before file staging/OCR so every audit event carries the
            # same run_id as the Agent run it preprocesses for.
            # the agent run it preprocesses for — that pairing is what lets the
            # admin audit feed show them as one invocation.
            run_id = str(uuid.uuid4())

            input_manifest: Optional[Dict[str, Any]] = None
            project_catalog: List[Dict[str, Any]] = []
            if not uploaded_files and context_meta.get("last_inputs_manifest") and context_meta.get("last_result_status") != "ok":
                from app.services.agent_package_service import workspace_file

                # A failed/rejected plan can be corrected in the same chat
                # without uploading the release again. Authority is always new.
                manifest_path = workspace_file(Path(ctx.temp_dir), context_meta["last_inputs_manifest"])
                input_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                packaging_requested = bool(input_manifest.get("inputs"))
                task = json.loads(Path(ctx.task_json_path).read_text(encoding="utf-8"))
                task["inputs_manifest"] = str(manifest_path)
                self._write_json_atomic(Path(ctx.task_json_path), task)
            if packaging_requested:
                if uploaded_files:
                    input_manifest = await self._stage_uploaded_inputs(
                        ctx, uploaded_files, run_id=run_id
                    )
                ctx.metadata["inputs_manifest"] = input_manifest
                context_meta["last_inputs_manifest"] = input_manifest.get(
                    "manifest_path"
                )
                context_meta["updated_at"] = datetime.utcnow().isoformat()
                self._save_context(effective_session_id, context_meta)
                project_catalog = await self._discover_package_projects(db)
                yield self._sse_event(
                    {
                        "event": "package_search_status",
                        "message": f"已安全接收 {len(input_manifest.get('inputs') or [])} 个组件文件，正在初判项目和组件...",
                        "uploaded_file_count": len(input_manifest.get("inputs") or []),
                    }
                )

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
                user_id=str(getattr(user, "id", None))
                if getattr(user, "id", None)
                else None,
                session_id=effective_session_id,
                run_id=run_id,
                locale=locale,
                project_repo_id=str(context_meta.get("project_repo_id"))
                if context_meta.get("project_repo_id") is not None
                else None,
            )
            if ocr_meta.image_count > 0 and ocr_meta.status in (
                "unconfigured",
                "failed",
            ):
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
            )
            # Drive prompt selection + the response-language directive for this
            # run. Resolved from the request/owner at the API layer; falls back
            # to the default when absent.
            if locale:
                from app.i18n import normalize

                ctx.locale = normalize(locale)
            self._bind_question_and_hints(
                ctx,
                question=question,
                hints=history_hint,
                packaging_requested=packaging_requested,
            )

            yield self._sse_event(
                {
                    "event": "package_search_status",
                    "message": (
                        "配置管理员正在检查文件并生成待确认的整包计划..."
                        if packaging_requested
                        else "配置管理员正在检索项目的重构包..."
                    ),
                }
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
                input_manifest=input_manifest,
                project_catalog=project_catalog,
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
            # Auth/history/project lookups use this request session. A streaming
            # dependency otherwise retains its connection until SSE disconnects,
            # starving history/reconnect requests across multiple browser tabs.
            # All writes below use independent background-job sessions.
            if db is not None:
                await db.close()
            job.task = asyncio.create_task(self._run_job_async(job, ctx))

            logger.info(
                "package-search chat: agent job scheduled session_id=%s task_id=%s temp_dir=%s",
                effective_session_id,
                ctx.task_id,
                ctx.temp_dir,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "package-search chat stream failed to start job: %s", exc, exc_info=True
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
                "package-search chat stream cancelled (client disconnect): session_id=%s job still running",
                effective_session_id,
            )
            raise

    # ───────────────────────── Background task ─────────────────────────

    async def _run_job_async(self, job: AgentJob, ctx: WorkspaceContext) -> None:
        """Background Agent task. Survives SSE disconnects; persists to DB on completion."""
        packaging_requested = bool(job.input_manifest)
        persisted = False
        try:

            def _emit_trace(event: Dict[str, Any]) -> None:
                job.trace_events.append(event)
                job.events.append({"event": "agent_trace", **event})

            if packaging_requested:
                ctx.metadata["trace_seq_counter"] = SeqCounter()
                loop = asyncio.get_running_loop()
                publication_lock = asyncio.Lock()

                async def publish_on_service_loop(plan_path: str, artifact_path: str):
                    async with publication_lock:
                        return await self._publish_agent_package(
                            job, ctx, plan_path, artifact_path, _emit_trace
                        )

                async def publish(plan_path: str, artifact_path: str):
                    # The SDK owns a worker-thread loop; confirmation and job
                    # state stay on the service loop, as do cancel requests.
                    future = asyncio.run_coroutine_threadsafe(
                        publish_on_service_loop(plan_path, artifact_path), loop
                    )
                    return await asyncio.wrap_future(future)

                ctx.metadata["packaging_publisher"] = publish
                task = json.loads(Path(ctx.task_json_path).read_text(encoding="utf-8"))
                task["packaging_requested"] = True
                task["projects"] = job.project_catalog
                task["run_id"] = job.run_id
                self._write_json_atomic(Path(ctx.task_json_path), task)

            result = await asyncio.to_thread(
                PackageSearchAgent().run_sync,
                ctx,
                job.cancel_event,
                _emit_trace,
                job.clarification,
            )
            if packaging_requested and isinstance(result, dict):
                result["packaging"] = True
                if job.artifacts:
                    result["artifacts"] = list(job.artifacts)
                    result["recommended_package_ids"] = [a["package_id"] for a in job.artifacts]
                    result["relevant_package_ids"] = result["recommended_package_ids"]
                    # Publication is authoritative even if the final prose/JSON
                    # or the provider connection fails after the tool succeeds.
                    result["status"] = "ok"
                    result["error_kind"] = None
                    result["answer"] = result.get("answer") or "整包已发布 / Package published."
                elif result.get("status") not in {"cancelled", "error"}:
                    result["status"] = "error"
                    result["error_kind"] = "package_not_published"
                    result["answer"] = (result.get("answer") or "") + (
                        "\n\n本轮未发布整包，可继续补充信息后重试。 / No package was published; continue this conversation to retry."
                    )
                if result.get("status") == "error":
                    job.error = str(result.get("error_kind") or "agent_error")

            if not isinstance(result, dict):
                result = {
                    "engine": "configuration-manager",
                    "model": None,
                    "status": "error",
                    "error_kind": "invalid_result",
                    "answer": "配置管理员未返回有效结果。",
                    "recommended_package_ids": [],
                    "relevant_package_ids": [],
                    "notes": "invalid agent result",
                    "trace_summary": {},
                }

            result["trace_events"] = list(job.trace_events)
            job.result = result
            answer_text = self._format_agent_result(
                result, question=job.question, context_meta=job.context_meta
            )
            job.answer = answer_text

            await self._persist_job_result(job, result, answer_text)
            persisted = True
            self._touch_context(job.session_id, result=result, answer=answer_text)

            job.events.append(
                {
                    "event": "done",
                    "session_id": job.session_id,
                    "answer": answer_text,
                    "model": result.get("model"),
                    "result": result,
                    # Package-search result contract surfaced at the top level
                    # so the frontend can render recommended-package cards
                    # without digging into ``result``.
                    "recommended_package_ids": result.get("recommended_package_ids")
                    or [],
                    "relevant_package_ids": result.get("relevant_package_ids") or [],
                    "notes": result.get("notes"),
                    "artifacts": result.get("artifacts") or [],
                    "trace_summary": result.get("trace_summary"),
                    "trace_events": result.get("trace_events"),
                }
            )
            logger.info(
                "package-search chat: agent job completed session_id=%s task_id=%s status=%s error_kind=%s duration=%ss",
                job.session_id,
                job.task_id,
                result.get("status"),
                result.get("error_kind"),
                int(time.monotonic() - job.started_at),
            )
        except MandatoryClarificationError as exc:
            cancelled = job.cancel_requested or exc.code in {"cancelled", "timeout"}
            if cancelled:
                job.result = {
                    "engine": "configuration-manager-agent",
                    "model": None,
                    "status": "cancelled",
                    "error_kind": exc.code,
                    "answer": str(exc),
                    "artifacts": [],
                    "trace_events": list(job.trace_events),
                }
                job.events.append(
                    {
                        "event": "cancelled",
                        "message": str(exc),
                        "reason": exc.code,
                    }
                )
            else:
                job.error = str(exc)
                job.events.append({"event": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "package-search chat: agent job failed session_id=%s: %s",
                job.session_id,
                exc,
                exc_info=True,
            )
            job.error = str(exc)
            job.events.append({"event": "error", "message": str(exc)})
        finally:
            try:
                if not persisted:
                    answer = (job.result or {}).get("answer") or (
                        "配置管理员任务失败 / Configuration Manager failed: " + str(job.error or "unknown error")
                    )
                    job.answer = answer
                    job.result = {
                        **(job.result or {}), "answer": answer,
                        "status": "cancelled" if job.cancel_requested or (job.result or {}).get("status") == "cancelled" else "error",
                        "trace_events": list(job.trace_events), "artifacts": list(job.artifacts),
                    }
                    await self._persist_job_result(job, job.result, answer)
                    self._touch_context(job.session_id, result=job.result, answer=answer)
            except Exception:
                logger.exception("Failed to save package conversation session_id=%s", job.session_id)
            finally:
                ctx.metadata.pop("packaging_publisher", None)
                try:
                    if packaging_requested:
                        self._revoke_packaging_authority(ctx, run_id=job.run_id)
                except Exception:
                    logger.exception("Failed to clear package workspace task_id=%s", job.task_id)
                # Disk/history failures must never leave a running conversation
                # forever. No live publisher capability survives this point.
                job.done = True
                job.finished_at = time.monotonic()
                await self._finalize_chat_run(job)

    async def _publish_agent_package(
        self, job: AgentJob, ctx: WorkspaceContext,
        plan_path: str, artifact_path: str, emit_trace: Any,
    ) -> Dict[str, Any]:
        """Review and publish Agent-built bytes; no business-rule builder fallback."""
        from app.services.agent_package_service import validate_agent_package, review_questions
        from app.services.package_confirmation_service import sign_confirmed_plan
        from app.services.raven_package_service import raven_package_service
        from app.services.chat_run_service import chat_run_service
        from app.services.full_package_service import canonical_json

        if job.cancel_requested or job.cancel_event.is_set():
            raise MandatoryClarificationError("cancelled", "发布已取消 / Publication cancelled")
        if job.artifacts:
            return {"status": "published", "artifact": job.artifacts[0], "reused": True}
        plan = await asyncio.to_thread(
            validate_agent_package, ctx.temp_dir, plan_path, artifact_path,
            inputs=list((job.input_manifest or {}).get("inputs") or []),
            projects=job.project_catalog,
        )
        answers = await self._request_packaging_confirmation(
            job, ctx, questions=review_questions(plan), draft=plan,
            inputs=plan["inputs"], materialized=["full-package-build"],
            emit_trace=emit_trace, chat_run_service=chat_run_service,
        )
        if any(a.get("selected_labels") != ["确认 / Confirm"] for a in answers):
            self.cancel(job.session_id)
            raise MandatoryClarificationError("cancelled", "用户取消发布 / Publication cancelled")
        if job.cancel_requested or job.cancel_event.is_set():
            raise MandatoryClarificationError("cancelled", "发布已取消 / Publication cancelled")
        # Validate fresh bytes after the potentially long review. Keep the exact
        # original snapshot, not a mutable file the Agent could replace.
        current = await asyncio.to_thread(
            validate_agent_package, ctx.temp_dir, plan_path, artifact_path,
            inputs=list((job.input_manifest or {}).get("inputs") or []),
            projects=job.project_catalog,
        )
        if current["plan_hash"] != plan["plan_hash"]:
            raise ValueError("确认期间计划或产物改变，请重新确认 / Package changed during review")
        plan.update({
            "run_id": job.run_id, "session_id": job.session_id,
            "user_id": str(job.user_id) if job.user_id is not None else job.owner_scope,
            "confirmation_hash": hashlib.sha256(canonical_json(answers).encode()).hexdigest(),
        })
        signed = sign_confirmed_plan(plan)
        if job.cancel_requested or job.cancel_event.is_set():
            raise MandatoryClarificationError("cancelled", "发布已取消 / Publication cancelled")
        publication_components = [{
            "name": c["component_key"], "version": c["version"],
            "fileAttr": c["file_attr"], "sha256": c["sha256"],
        } for c in plan["components"]]
        package = await asyncio.to_thread(
            raven_package_service.publish_built_package, Path(plan["artifact_path"]),
            confirmed_plan=signed, components=publication_components,
            expected_sha256=plan["artifact_sha256"],
            cancel_event=job.cancel_event,
        )
        artifact = {
            "package_id": str(package["id"]), "name": package["name"],
            "download_url": f"/raven/api/download/{package['id']}",
            "size": package["size"], "sha256": package["metadata"]["sha256"],
            "project_code": plan["project_code"], "version": plan["version"],
            "components": publication_components,
        }
        # Record success before ancillary workspace persistence can fail.
        job.confirmed_plan = signed
        job.artifacts = [artifact]
        task = json.loads(Path(ctx.task_json_path).read_text(encoding="utf-8"))
        registry_project = next(p for p in job.project_catalog if p["project_code"] == plan["project_code"])
        self._bind_confirmed_project(job, ctx, task, registry_project=registry_project, confirmed_plan=signed)
        self._write_json_atomic(Path(ctx.task_json_path), task)
        self._write_json_atomic(Path(ctx.temp_dir) / "output" / "published-package.json", {
            "plan": plan, "artifact": artifact,
        })
        return {"status": "published", "artifact": artifact}

    async def _request_packaging_confirmation(
        self,
        job: AgentJob,
        ctx: WorkspaceContext,
        *,
        questions: List[Dict[str, Any]],
        draft: Dict[str, Any],
        inputs: List[Dict[str, Any]],
        materialized: List[str],
        emit_trace: Any,
        chat_run_service: Any,
    ) -> List[Dict[str, Any]]:
        """Ask through the shared broker and expose it to immediate cancellation."""
        if job.cancel_requested or job.cancel_event.is_set():
            raise MandatoryClarificationError(
                "cancelled", "打包确认已取消，未执行构建和发布。"
            )
        seq_counter = ctx.metadata.get("trace_seq_counter")
        if not isinstance(seq_counter, SeqCounter):
            seq_counter = SeqCounter()
            ctx.metadata["trace_seq_counter"] = seq_counter
        broker = PermissionBroker()
        job.mandatory_broker = broker
        chat_run_service.register_broker(job.run_id, broker)
        try:
            return await request_mandatory_clarification(
                questions,
                broker=broker,
                emit=emit_trace,
                seq_counter=seq_counter,
                task_id=ctx.task_id,
                run_id=job.run_id,
                session_id=job.session_id,
                cancel_run=lambda sid=job.session_id: self.cancel(sid),
                event_fields={
                    "plan_hash": draft.get("plan_hash"),
                    "input_count": len(inputs),
                    "loaded_skills": list(materialized),
                },
            )
        finally:
            chat_run_service.unregister_broker(job.run_id)
            if job.mandatory_broker is broker:
                job.mandatory_broker = None
            broker.close()

    def _bind_confirmed_project(
        self,
        job: AgentJob,
        ctx: WorkspaceContext,
        task_data: Dict[str, Any],
        *,
        registry_project: Dict[str, Any],
        confirmed_plan: Dict[str, Any],
    ) -> None:
        """Replace candidate identity with the authoritative confirmed project."""
        project_code = str(confirmed_plan.get("project_code") or "")
        project_name = str(
            registry_project.get("project_name")
            or confirmed_plan.get("project_name")
            or project_code
        )
        existing = task_data.get("repo_info")
        if (
            not isinstance(existing, dict)
            or str(existing.get("project_code") or "").casefold()
            != project_code.casefold()
        ):
            existing = {}
        task_data["repo_info"] = {
            **existing,
            "project_code": project_code,
            "project_name": project_name,
            "source": "mandatory_packaging_confirmation",
        }
        ctx.project_code = project_code
        ctx.metadata["repo_info"] = dict(task_data["repo_info"])
        job.project_repo_id = registry_project.get("id")
        job.context_meta.update(
            {
                "project_repo_id": registry_project.get("id"),
                "project_code": project_code,
                "project_name": project_name,
                "updated_at": datetime.utcnow().isoformat(),
            }
        )
        self._save_context(job.session_id, job.context_meta)

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
            "packaging_requested": bool(job.input_manifest),
            "input_count": len((job.input_manifest or {}).get("inputs") or []),
            "input_names": [
                str(item.get("original_name") or "")
                for item in (job.input_manifest or {}).get("inputs") or []
                if isinstance(item, dict)
            ],
        }

        try:
            chat_run_service.register_external_job(
                run_id=job.run_id,
                session_id=job.session_id,
                user_id=str(job.user_id) if job.user_id is not None else None,
                owner_scope=job.owner_scope,
                agent_kind="package_search",
                user_message=job.question,
                request_payload=request_payload,
                events_ref=job.events,
                trace_events_ref=job.trace_events,
                cancel_callback=lambda sid=job.session_id: self.cancel(sid),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "package-search chat: chat_run_service register failed run_id=%s: %s",
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
                        user_id=(str(job.user_id) if job.user_id is not None else None),
                        owner_scope=job.owner_scope or "anon:legacy",
                        agent_kind="package_search",
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
                "package-search chat: chat_agent_runs row insert failed run_id=%s: %s",
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
        if agent_status == "cancelled" or (job.cancel_requested and not job.answer):
            terminal_status = RUN_STATUS_CANCELLED
        elif job.error or agent_status in {"error", "failed"}:
            terminal_status = RUN_STATUS_FAILED
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
                "package-search chat: chat_run_service finalize failed run_id=%s: %s",
                job.run_id,
                exc,
            )

        # Best-effort AI usage metrics, idempotent on run_id. Project repository
        # metadata is attached when available (project_code is allowlisted).
        try:
            from app.services import metrics_service

            project_repo_id = job.context_meta.get("project_repo_id")
            project_code = job.context_meta.get("project_code")
            extra_metadata: Dict[str, Any] = {}
            if project_code:
                extra_metadata["project_code"] = str(project_code)
            if isinstance(job.result, dict):
                recommended = job.result.get("recommended_package_ids")
                if isinstance(recommended, list):
                    extra_metadata["result_count"] = len(recommended)
            await metrics_service.record_agent_run_usage(
                source="package_search_agent",
                agent_kind="package_search",
                run_id=job.run_id,
                result=job.result,
                terminal_status=terminal_status,
                provider=settings.anthropic_provider,
                user_id=str(job.user_id) if job.user_id is not None else None,
                owner_scope=job.owner_scope or None,
                session_id=job.session_id,
                task_id=job.task_id,
                project_repo_id=str(project_repo_id)
                if project_repo_id is not None
                else None,
                extra_metadata=extra_metadata or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "package-search chat: metrics record skipped run_id=%s: %s",
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
                "package-search chat: chat_agent_runs finalize failed run_id=%s: %s",
                job.run_id,
                exc,
            )

    async def _persist_job_result(
        self,
        job: AgentJob,
        result: Dict[str, Any],
        answer_text: str,
    ) -> None:
        """Open a fresh DB session and persist the chat exchange."""
        from app.models.database import db_manager

        if db_manager.session_factory is None:
            logger.warning(
                "package-search chat: db not initialized; skipping persistence"
            )
            return

        if not (job.remember and job.user_id is not None):
            return

        try:
            async with db_manager.session_factory() as db:
                try:
                    await self._persist_exchange(
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
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "package-search chat: failed to persist job result session_id=%s: %s",
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
        if (
            user is not None
            and job.user_id is not None
            and getattr(user, "id", None) != job.user_id
        ):
            raise PermissionError("当前用户无权取消这个检索任务")
        if job.cancel_requested:
            return True
        job.cancel_requested = True
        job.cancel_event.set()
        mandatory_broker = job.mandatory_broker
        if mandatory_broker is not None:
            # close() settles every pending clarification Future with a deny
            # decision and is safe across event loops/threads.
            mandatory_broker.close()
        job.events.append(
            {
                "event": "package_search_status",
                "message": (
                    "已取消强制打包确认，不会构建或发布整包。"
                    if mandatory_broker is not None
                    else "已收到取消请求，正在等待 Agent 退出..."
                ),
                "cancel_requested": True,
            }
        )
        logger.info("package-search chat: cancel requested session_id=%s", session_id)
        return True

    def get_status(
        self, session_id: str, user: Optional[User] = None
    ) -> Dict[str, Any]:
        """Return the current state snapshot of the Job for polling clients."""
        job = self._jobs.get(session_id or "")
        if job is None:
            return {"session_id": session_id, "status": "not_found"}
        if (
            user is not None
            and job.user_id is not None
            and getattr(user, "id", None) != job.user_id
        ):
            raise PermissionError("当前用户无权查看这个检索任务")
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
            "artifacts": list(job.artifacts),
            "confirmed_plan_hash": (
                (job.confirmed_plan or {}).get("plan_hash")
                if job.confirmed_plan
                else None
            ),
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
            logger.debug("package-search chat: evicted finished job session_id=%s", sid)

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
                db, repo, "package_search"
            ):
                logger.info(
                    "package-search chat: project_repo_id=%s does not enable package_search",
                    project_repo_id,
                )
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("package-search chat: 校验 project_repo_id 失败: %s", exc)
            return None
        if not repo or not getattr(repo, "enabled", True):
            return None
        return repo

    async def _discover_package_projects(
        self, db: Optional[AsyncSession]
    ) -> List[Dict[str, Any]]:
        """Return the bounded, credential-free project catalog for planning."""
        if db is None:
            return []
        from app.services import project_repo_service

        payload = await project_repo_service.discover_projects(db)
        projects = payload.get("projects") if isinstance(payload, dict) else []
        return [
            dict(project)
            for project in (projects or [])
            if isinstance(project, dict)
            and "package_search" in (project.get("enabled_agent_keys") or [])
        ]

    @staticmethod
    def _safe_upload_basename(filename: Optional[str]) -> str:
        raw = str(filename or "").replace("\\", "/")
        name = raw.rsplit("/", 1)[-1].strip()
        if not name or name in {".", ".."} or "\x00" in name:
            raise ValueError("上传文件缺少安全的文件名")
        if len(name.encode("utf-8")) > 255:
            raise ValueError(f"上传文件名过长：{name[:80]}")
        return name

    @staticmethod
    def _detected_upload_type(name: str, header: bytes) -> str:
        lower = name.lower()
        if header.startswith(b"PK\x03\x04") or header.startswith(b"PK\x05\x06"):
            return "zip"
        if header.startswith(b"\x1f\x8b"):
            return "tar.gz" if lower.endswith((".tar.gz", ".tgz")) else "gzip"
        if header.startswith(b"Rar!\x1a\x07"):
            return "rar"
        if header.startswith(b"7z\xbc\xaf\x27\x1c"):
            return "7z"
        if len(header) > 262 and header[257:262] == b"ustar":
            return "tar"
        if lower.endswith(".tar.gz"):
            return "tar.gz"
        suffix = Path(lower).suffix.lstrip(".")
        return suffix or "binary"

    @staticmethod
    def _write_json_atomic(path: Path, value: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def _stage_uploaded_inputs(
        self,
        ctx: WorkspaceContext,
        files: List[UploadFile],
        *,
        run_id: str,
    ) -> Dict[str, Any]:
        """Chunk-copy one packaging turn into its workspace and hash every input."""
        if not files:
            raise ValueError("整包制作至少需要一个组件文件")
        if len(files) > _PACKAGE_UPLOAD_MAX_FILES:
            raise ValueError(f"一次最多上传 {_PACKAGE_UPLOAD_MAX_FILES} 个组件文件")

        per_file_limit = int(settings.max_file_size)
        total_limit = int(settings.max_file_size)
        turn_dir = Path(ctx.temp_dir) / "inputs" / run_id
        turn_dir.mkdir(parents=True, exist_ok=False)
        records: List[Dict[str, Any]] = []
        total_bytes = 0
        try:
            for index, upload in enumerate(files):
                original_name = self._safe_upload_basename(upload.filename)
                upload_id = f"input-{index + 1:03d}-{uuid.uuid4().hex[:12]}"
                safe_tail = re.sub(r"[^a-zA-Z0-9._-]+", "_", original_name).strip("._")
                safe_tail = (safe_tail or "component.bin")[-140:]
                destination = turn_dir / f"{upload_id}-{safe_tail}"
                digest = hashlib.sha256()
                size = 0
                header = bytearray()
                try:
                    with destination.open("xb") as handle:
                        while True:
                            chunk = await upload.read(_PACKAGE_UPLOAD_CHUNK_BYTES)
                            if not chunk:
                                break
                            size += len(chunk)
                            total_bytes += len(chunk)
                            if size > per_file_limit:
                                raise ValueError(
                                    f"文件 {original_name} 超过 {per_file_limit // (1024 * 1024)}MiB 限制"
                                )
                            if total_bytes > total_limit:
                                raise ValueError(
                                    f"本次上传总量超过 {total_limit // (1024 * 1024)}MiB 限制"
                                )
                            if get_free_bytes(turn_dir) - int(
                                settings.disk_reserve_bytes
                            ) < len(chunk):
                                raise OSError("磁盘空间不足，无法接收全部组件文件")
                            if len(header) < 512:
                                header.extend(chunk[: 512 - len(header)])
                            handle.write(chunk)
                            digest.update(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    await upload.close()
                if size <= 0:
                    raise ValueError(f"上传文件为空：{original_name}")
                records.append(
                    {
                        "upload_id": upload_id,
                        "index": index,
                        "original_name": original_name,
                        "stored_name": destination.name,
                        "path": str(destination),
                        "relative_path": str(destination.relative_to(ctx.temp_dir)),
                        "size": size,
                        "sha256": digest.hexdigest(),
                        "detected_type": self._detected_upload_type(
                            original_name, bytes(header)
                        ),
                    }
                )
        except Exception:
            # Starlette owns every UploadFile; close any not yet visited before
            # deleting the partial turn so request temp files are not leaked.
            for upload in files:
                try:
                    await upload.close()
                except Exception:
                    pass
            shutil.rmtree(turn_dir, ignore_errors=True)
            raise

        manifest_path = turn_dir / "input-manifest.json"
        manifest: Dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "task_id": ctx.task_id,
            "created_at": datetime.utcnow().isoformat(),
            "total_size": total_bytes,
            "inputs": records,
            "manifest_path": str(manifest_path),
        }
        self._write_json_atomic(manifest_path, manifest)

        try:
            task_path = Path(ctx.task_json_path)
            task_data = json.loads(task_path.read_text(encoding="utf-8"))
            if not isinstance(task_data, dict):
                task_data = {}
        except Exception:
            task_data = {}
        task_data["inputs_manifest"] = str(manifest_path)
        task_data["input_count"] = len(records)
        self._write_json_atomic(Path(ctx.task_json_path), task_data)
        return manifest

    def _create_context_for_project(
        self,
        *,
        session_id: str,
        question: str,
        repo: Optional[Any],
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
            "project_repo_id": getattr(repo, "id", None) if repo is not None else None,
            "project_code": getattr(repo, "project_code", None)
            if repo is not None
            else None,
            "project_name": getattr(repo, "project_name", None)
            if repo is not None
            else None,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        self._save_context(session_id, context_meta)
        if old_ctx and old_ctx.temp_dir != ctx.temp_dir:
            cleanup(old_ctx)
        return ctx, context_meta

    def _bind_question_and_hints(
        self,
        ctx: WorkspaceContext,
        *,
        question: str,
        hints: str,
        packaging_requested: bool,
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
        task_data["packaging_requested"] = bool(packaging_requested)
        task_data["package_mode"] = "packaging" if packaging_requested else "search"
        if not packaging_requested:
            # A persistent chat workspace may contain a previous turn's signed
            # plan. Pure search must not inherit that authority or re-expose
            # the package-builder MCP on a later turn.
            self._clear_packaging_task_fields(task_data)
            ctx.metadata.pop("confirmed_plan", None)
            ctx.metadata.pop("inputs_manifest", None)
            shutil.rmtree(Path(ctx.temp_dir) / "package_plan", ignore_errors=True)
        self._write_json_atomic(task_path, task_data)
        ctx.metadata["question"] = question
        ctx.metadata["hints"] = hints

    @staticmethod
    def _clear_packaging_task_fields(task_data: Dict[str, Any]) -> None:
        """Remove every value that can make PackageSearchAgent expose builder authority."""
        for key in set(PACKAGING_TASK_VALUE_KEYS) | {
            "input_count",
            "package_catalog_path",
            "package_plan_hash",
        }:
            task_data.pop(key, None)

    def _revoke_packaging_authority(
        self, ctx: WorkspaceContext, *, run_id: Optional[str] = None
    ) -> None:
        """Consume every signed plan after success/failure so no later turn can replay it."""
        task_path = Path(ctx.task_json_path)
        try:
            task_data = json.loads(task_path.read_text(encoding="utf-8"))
            if not isinstance(task_data, dict):
                task_data = {}
        except Exception:
            task_data = {}
        self._clear_packaging_task_fields(task_data)
        task_data["packaging_requested"] = False
        task_data["package_mode"] = "completed"
        self._write_json_atomic(task_path, task_data)
        ctx.metadata.pop("confirmed_plan", None)
        ctx.metadata.pop("inputs_manifest", None)
        plan_root = Path(ctx.temp_dir) / "package_plan"
        # There is at most one active job per persistent session. Remove stale
        # directories from older/deployed builds as well as the current run;
        # otherwise a pure-search turn with Bash could discover and replay a
        # still-valid legacy confirmation token.
        shutil.rmtree(plan_root, ignore_errors=True)

    async def _build_history_hint(
        self,
        *,
        db: Optional[AsyncSession],
        user: Optional[User],
        session_id: str,
        history_json: Optional[str],
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
            return ""

        recent = messages[-12:]
        lines = [
            "以下是同一对话中此前的上下文。用户可能会用“刚才/这个/继续”等指代，请结合这些内容理解当前问题："
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
    ) -> None:
        """Persist the user / assistant exchange. Caller owns commit/rollback."""
        user_content = question
        if project_code:
            user_content = f"{question}\n\n[配置管理员] 项目: {project_code}"
        session = await chat_history_service.save_exchange(
            db,
            user_id=user_id,
            session_id=session_id,
            user_content=user_content,
            ai_content=answer,
            title_hint=question,
            user_images_json=images_json,
        )
        if (session.message_count or 0) <= 2:
            try:
                from app.services.ai_chat_service import ai_chat_service

                title = await asyncio.wait_for(
                    ai_chat_service.generate_session_title(
                        user_content,
                        answer,
                        user_id=str(user_id) if user_id is not None else None,
                        session_id=session_id,
                    ),
                    timeout=8,
                )
                if title:
                    await chat_history_service.update_session_title(
                        db,
                        user_id=user_id,
                        session_id=session_id,
                        title=title,
                    )
            except Exception:
                pass

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
        if (
            not ignore_owner
            and owner_user_id
            and getattr(user, "id", None) != owner_user_id
        ):
            raise PermissionError("当前用户无权访问这个配置管理员上下文")

        required_paths = ["temp_dir", "repo_dir", "task_json_path"]
        if any(not meta.get(key) for key in required_paths):
            return None
        # Legacy contexts stored paths relative to the service working directory.
        # Resolve once before staging so the builder never prefixes them again.
        for key in required_paths:
            meta[key] = str(Path(meta[key]).expanduser().resolve())
        if (
            not Path(meta["temp_dir"]).exists()
            or not Path(meta["task_json_path"]).exists()
        ):
            return None

        ctx = WorkspaceContext(
            task_id=meta.get("task_id") or Path(meta["temp_dir"]).name,
            temp_dir=meta["temp_dir"],
            repo_dir=meta["repo_dir"],
            task_json_path=meta["task_json_path"],
            project_code=str(meta.get("project_code") or ""),
            metadata={
                "question": "",
                "hints": "",
                "repo_info": {
                    "project_code": meta.get("project_code"),
                    "project_name": meta.get("project_name"),
                    "source": "user_selected_project_repo",
                },
            },
        )
        return ctx, meta

    def _save_context(self, session_id: str, meta: Dict[str, Any]) -> None:
        path = self._context_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _touch_context(
        self, session_id: str, *, result: Dict[str, Any], answer: str
    ) -> None:
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
            logger.debug("package-search chat: failed to touch context", exc_info=True)

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
        answer = str(result.get("answer") or "").strip()
        notes = result.get("notes")
        recommended = result.get("recommended_package_ids") or []
        relevant = result.get("relevant_package_ids") or []
        artifacts = [
            artifact
            for artifact in (result.get("artifacts") or [])
            if isinstance(artifact, dict)
        ]
        is_packaging = bool(artifacts or result.get("packaging"))
        if status == "cancelled":
            notice = (
                "打包已取消，未发布整包。上传件已保留，可在当前对话继续重试。 / "
                "Packaging cancelled; nothing was published. Continue this conversation to retry with the retained uploads."
                if is_packaging
                else "任务已取消，可在当前对话继续提问。 / Task cancelled; you can continue this conversation."
            )
            answer = notice + ("\n\n" + answer if answer else "")
        elif not answer and status != "ok":
            answer = (
                "配置管理员任务失败，可在当前对话补充信息后重试。 / "
                "Configuration Manager failed; continue this conversation to retry. "
                + str(result.get("error_kind") or status)
            )

        lines: List[str] = [
            (
                "**配置管理员 Agent** 已完成本轮处理。"
                if status != "ok"
                else "**配置管理员 Agent** 已完成本轮打包。"
                if is_packaging else "**配置管理员 Agent** 已完成本轮检索。"
            ),
            "",
            f"- 项目：`{project}`",
            f"- 问题：{question}",
            f"- 状态：`{status}`",
            f"- 模型：`{model}`"
            + (f"，耗时：{duration}s" if duration is not None else ""),
            "- 上下文：已保留本次配置管理员工作区，可在当前对话继续追问。",
        ]

        if result.get("error_kind"):
            lines.append(f"- 错误类型：`{result.get('error_kind')}`")

        if recommended:
            lines.append(
                "- 推荐包：" + " ".join(f"`{str(pid)}`" for pid in recommended)
            )
        if relevant:
            lines.append("- 相关包：" + " ".join(f"`{str(pid)}`" for pid in relevant))

        if artifacts:
            lines.extend(["", "## 整包产物"])
            for artifact in artifacts:
                name = str(artifact.get("name") or "软件升级整包")
                url = str(artifact.get("download_url") or "")
                size = int(artifact.get("size") or 0)
                sha256 = str(artifact.get("sha256") or "")
                lines.append(
                    f"- [{name}]({url})"
                    + (f"（{size / (1024 * 1024):.2f} MiB）" if size else "")
                )
                if sha256:
                    lines.append(f"  - SHA-256：`{sha256}`")
                component_names = [
                    str(component.get("name") or "")
                    for component in artifact.get("components") or []
                    if isinstance(component, dict) and component.get("name")
                ]
                if component_names:
                    lines.append(
                        "  - 已确认组件："
                        + "、".join(f"`{name}`" for name in component_names)
                    )
            if len(artifacts) == 1:
                lines.extend(
                    [
                        "",
                        f"[下载整包]({artifacts[0].get('download_url')})",
                    ]
                )

        if answer:
            lines.extend(["", "## 配置管理员说明", answer])
        elif status == "ok":
            lines.extend(["", "## 配置管理员说明", "本轮没有额外说明。"])

        if isinstance(notes, str) and notes.strip() and notes.strip() != answer:
            lines.extend(["", "## 备注", notes.strip()])

        return "\n".join(lines).strip()

    @staticmethod
    def _sse_event(payload: Dict[str, object]) -> str:
        safe_payload = jsonable_encoder(payload)
        return f"data: {json.dumps(safe_payload, ensure_ascii=False)}\n\n"


package_search_chat_service = PackageSearchChatService()
