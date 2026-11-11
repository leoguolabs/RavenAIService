"""Main-chat Log Analysis Agent workflow.

This service keeps a persistent filesystem workspace per chat session so a
user can upload a log archive once and continue asking follow-up questions
against the same extracted logs and cloned repository.
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

from fastapi import UploadFile
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.log_analysis.agent import LogAnalysisAgent, extract_recoverable_result_fields
from app.agents.log_analysis.workspace import (
    MissingArchiveError,
    MissingMetadataJsonError,
    UnsupportedUploadFormatError,
    WorkspaceContext,
    WorkspaceError,
    WorkspaceExtractTooLarge,
    cleanup,
    prepare,
    prepare_many,
)
from app.config import settings
from app.exceptions import FileSizeExceededError, UnsupportedFileTypeError, ValidationError
from app.i18n import DEFAULT as I18N_DEFAULT, normalize as normalize_locale
from app.agents.clarification import ClarificationBinding
from app.i18n.prompts import response_language_directive
from app.models.chat import ChatMessage, ImageAttachment
from app.models.log import LogLevel, LogMetadata, LogStatus, LogUploadRequest
from app.models.user import User
from app.services import chat_image_store, ocr_service
from app.services.agent_session_title import PendingSessionTitle, generate_after_commit
from app.services.chat_history_service import chat_history_service
from app.services.log_service import log_service
from app.utils.validation import file_validator

logger = logging.getLogger(__name__)


_SESSION_KEY_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_AGENT_PROGRESS_INTERVAL_SECONDS = 15
_JOB_POLL_INTERVAL_SECONDS = 0.2
# Keep finished Jobs around for late /result polling and post-disconnect reconnect.
_JOB_RETENTION_SECONDS = 30 * 60


@dataclass
class AgentJob:
    """In-process record of a running or recently-finished log-analysis Agent task.

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
    filename: Optional[str]
    started_at: float
    # Active locale for this run; drives prompt selection, the response-language
    # directive, and the language of the generated session title.
    locale: str = I18N_DEFAULT
    started_at_utc: str = ""
    user_snapshot: Dict[str, Any] = field(default_factory=dict)
    # run_id projects this job into the unified chat_agent_runs lifecycle so
    # the sidebar overlay and /active-run snapshot can see log-analysis runs
    # alongside DeviceAgent runs.
    run_id: str = ""
    owner_scope: str = ""
    # Metadata for images attached to this turn; persisted with the user
    # message so history reloads can re-render the thumbnails.
    images_json: Optional[str] = None
    source_context: Optional[Dict[str, Any]] = None
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


class LogAnalysisChatService:
    """Run LogAnalysisAgent from the main chat composer."""

    def __init__(self) -> None:
        self.registry_dir = Path(settings.code_repo_clone_base_dir) / "chat_log_analysis_sessions"
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        # In-process Job registry. Single-process uvicorn deployment is assumed.
        # If we move to multi-worker, swap this for a Redis-backed registry.
        self._jobs: Dict[str, AgentJob] = {}

    async def stream(
        self,
        *,
        message: str,
        session_id: Optional[str],
        history_json: Optional[str],
        file: Optional[UploadFile],
        files: Optional[List[UploadFile]] = None,
        remember: bool,
        db: Optional[AsyncSession],
        user: Optional[User],
        owner_scope: Optional[str] = None,
        project_repo_id: Optional[int] = None,
        images: Optional[List[ImageAttachment]] = None,
        locale: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """SSE stream for one log-analysis turn.

        Behavior:
        - If an in-flight Job exists for ``session_id``: subscribe and replay
          buffered events. A new file upload while a Job is running is rejected.
        - Else: start a new Job (with file upload, or follow-up against the
          previously-saved workspace context).

        SSE client disconnects do NOT cancel the underlying Agent Job — the
        Job runs to completion or until explicit cancel/timeout, and its
        result is persisted to DB so it can be retrieved later via the
        ``/result`` endpoint or by reconnecting.
        """
        effective_session_id = session_id or str(uuid.uuid4())
        question = (message or "").strip()
        uploaded_files = self._uploaded_files(files, legacy_file=file)
        uploaded_filenames = [
            filename
            for filename in (
                self._uploaded_filename(upload_file)
                for upload_file in uploaded_files
            )
            if filename
        ]

        self._evict_old_jobs()

        yield self._sse_event({"event": "session", "session_id": effective_session_id})

        logger.info(
            "log-analysis chat: stream started session_id=%s "
            "attachment_count=%d filenames=%s",
            effective_session_id,
            len(uploaded_filenames),
            uploaded_filenames or "-",
        )

        existing_job = self._jobs.get(effective_session_id)

        # Subscribe path: in-flight Job already exists for this session.
        if existing_job is not None and not existing_job.done:
            if uploaded_filenames:
                yield self._sse_event(
                    {
                        "event": "error",
                        "message": "本会话已有正在进行的分析任务，请先取消或等待完成后再上传新日志附件。",
                    }
                )
                return
            yield self._sse_event(
                {
                    "event": "log_analysis_status",
                    "message": "已重新连接到正在运行的分析任务，继续推送已积累的进度...",
                    "reattached": True,
                }
            )
            async for chunk in self._subscribe(existing_job):
                yield chunk
            return

        # Re-subscribe path: Job already done and still cached; replay terminal events.
        if (
            existing_job is not None
            and existing_job.done
            and not uploaded_filenames
            and not question
        ):
            async for chunk in self._subscribe(existing_job):
                yield chunk
            return

        # Validate explicit project_repo_id up-front so we fail fast before
        # writing any file or running heavy work.
        if project_repo_id is not None and db is not None:
            try:
                from app.services import project_repo_service

                repo = await project_repo_service.get_by_id(db, project_repo_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("log-analysis chat: 校验 project_repo_id 失败: %s", exc)
                repo = None
            if not repo or not repo.enabled:
                yield self._sse_event(
                    {
                        "event": "error",
                        "message": "所选项目仓库不存在或已禁用，请重新选择。",
                    }
                )
                return
            if not await project_repo_service.supports_agent(
                db, repo, "log_analysis"
            ):
                yield self._sse_event(
                    {
                        "event": "error",
                        "message": "该项目未启用日志分析 Agent，请重新选择项目或联系管理员配置。",
                    }
                )
                return

        # Start a new Job.
        try:
            if uploaded_filenames:
                # Validate the complete batch before any upload is persisted.
                # Preserve typed errors instead of losing them in FileUploadError.
                upload_locale = normalize_locale(locale or getattr(user, "language", None))
                for upload_file in uploaded_files:
                    try:
                        await file_validator.validate_upload_file_or_raise(upload_file)
                    except (UnsupportedFileTypeError, FileSizeExceededError, ValidationError) as exc:
                        error = self._upload_validation_error(upload_file, exc, upload_locale)
                        logger.info(
                            "log-analysis chat: upload rejected session_id=%s filename=%s reason=%s: %s",
                            effective_session_id, upload_file.filename, error["reason"], exc,
                        )
                        yield self._sse_event(error)
                        return
                attachment_summary = "、".join(
                    f"`{filename}`" for filename in uploaded_filenames
                )
                yield self._sse_event(
                    {
                        "event": "log_analysis_status",
                        "message": (
                            f"已接收 {len(uploaded_filenames)} 份日志附件："
                            f"{attachment_summary}，正在建立分析工作区..."
                        ),
                    }
                )
                ctx, context_meta = await self._create_context_from_upload(
                    db=db,
                    session_id=effective_session_id,
                    question=question
                    or "请分析这些日志附件，给出概览、可疑异常和下一步建议。",
                    files=uploaded_files,
                    user=user,
                    project_repo_id=project_repo_id,
                )
                yield self._sse_event(
                    {
                        "event": "log_analysis_context",
                        "session_id": effective_session_id,
                        "log_id": context_meta.get("log_id"),
                        "log_ids": context_meta.get("log_ids", []),
                        "filename": context_meta.get("filename"),
                        "filenames": context_meta.get("filenames", []),
                        "attachments": context_meta.get("attachments", []),
                    }
                )
            else:
                loaded = self._load_context(effective_session_id, user=user)
                if loaded is None:
                    yield self._sse_event(
                        {
                            "event": "error",
                            "message": "请先在“日志分析”模式下上传一个包含 metadata.json 的日志包，再继续追问。",
                        }
                    )
                    return
                ctx, context_meta = loaded
                yield self._sse_event(
                    {
                        "event": "log_analysis_status",
                        "message": "已复用当前对话的日志分析工作区，正在基于已解压日志和已克隆代码继续分析...",
                    }
                )

            if not question:
                question = "请分析这些日志附件，给出概览、可疑异常和下一步建议。"

            # Minted before OCR so the OCR usage event carries the same run_id as
            # the agent run it preprocesses for — that pairing is what lets the
            # admin audit feed show them as one invocation.
            run_id = str(uuid.uuid4())

            # OCR-merge any attached images into the question before the agent
            # runs; degrade to text-only when OCR is unconfigured/failed. The
            # merged text is what gets bound to the agent and persisted.
            # Persist the originals so history reloads can re-render the
            # thumbnails, and materialize them into the agent workspace as
            # groundwork for a future multimodal path (no-op unless the
            # provider supports image input — see chat_image_store).
            stored_images = chat_image_store.save_turn_images(
                images, session_id=effective_session_id
            )
            images_json = chat_image_store.to_meta_json(stored_images)
            chat_image_store.materialize_into_workspace(stored_images, ctx.temp_dir)

            _ocr_project_repo_id = context_meta.get("project_repo_id") or context_meta.get(
                "project_id"
            )
            question, ocr_meta = await ocr_service.enrich_message(
                question,
                images,
                user_id=str(getattr(user, "id", None)) if getattr(user, "id", None) else None,
                session_id=effective_session_id,
                run_id=run_id,
                locale=locale,
                project_repo_id=str(_ocr_project_repo_id)
                if _ocr_project_repo_id is not None
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

            source_images = list(stored_images)
            history_hint = await self._build_history_hint(
                db=db,
                user=user,
                session_id=effective_session_id,
                history_json=history_json,
                source_images=source_images,
            )
            self._bind_question_and_hints(ctx, question=question, hints=history_hint)

            # Resolve the active locale (explicit choice → user preference →
            # default) and bind it to the workspace so the agent run selects
            # localized prompts and appends the response-language directive.
            effective_locale = normalize_locale(
                locale or getattr(user, "language", None)
            )
            ctx.locale = effective_locale
            self._bind_locale_to_task(ctx, effective_locale)

            yield self._sse_event(
                {
                    "event": "log_analysis_status",
                    "message": "Log Analysis Agent 正在读取日志与代码上下文...",
                }
            )

            effective_owner_scope = owner_scope or (
                f"user:{user.id}" if getattr(user, "id", None) else "anon:legacy"
            )
            from app.services.bug_fix_context import capture_source

            job = AgentJob(
                session_id=effective_session_id,
                task_id=ctx.task_id,
                context_meta=context_meta,
                question=question,
                user_id=getattr(user, "id", None),
                user_snapshot=self._user_snapshot(user),
                remember=bool(remember),
                images_json=images_json,
                source_context=capture_source(ctx, images=source_images),
                filename=self._display_filenames(context_meta),
                started_at=time.monotonic(),
                started_at_utc=datetime.utcnow().isoformat(),
                run_id=run_id,
                owner_scope=effective_owner_scope,
                locale=effective_locale,
                # Honour the user's global "let the agent ask me when the
                # instruction is unclear" preference on this agent too, not just
                # DeviceAgent. Returns None when the preference is off.
                clarification=ClarificationBinding.for_chat_run(
                    user=user,
                    run_id=run_id,
                    session_id=effective_session_id,
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
            # Project the job into the unified chat_agent_runs lifecycle so
            # the active-run endpoint and sidebar overlay can show it.
            await self._register_chat_run(job, ctx)
            job.task = asyncio.create_task(self._run_job_async(job, ctx))

            logger.info(
                "log-analysis chat: agent job scheduled session_id=%s task_id=%s temp_dir=%s",
                effective_session_id,
                ctx.task_id,
                ctx.temp_dir,
            )
        except MissingMetadataJsonError as exc:
            logger.info(
                "log-analysis chat: archive missing metadata.json session_id=%s: %s",
                effective_session_id,
                exc,
            )
            yield self._sse_event(
                {
                    "event": "error",
                    "reason": "missing_metadata",
                    "message": (
                        "这个附件里没有找到 metadata.json，我无法自动识别它属于哪个项目。"
                        "请在输入框上方的「关联项目」下拉菜单中手动选择对应项目后重新发送，"
                        "或改用一个包含 metadata.json 的日志压缩包。"
                        "（纯文本日志不含 metadata.json，必须手动选择关联项目。）"
                    ),
                }
            )
            return
        except MissingArchiveError as exc:
            logger.warning(
                "log-analysis chat: archive unreadable session_id=%s: %s",
                effective_session_id,
                exc,
            )
            yield self._sse_event(
                {
                    "event": "error",
                    "reason": "missing_archive",
                    "message": (
                        "这个日志包好像读取不到内容，可能上传未完成或文件已损坏。"
                        "请重新上传日志包后再试一次。"
                    ),
                }
            )
            return
        except WorkspaceExtractTooLarge as exc:
            logger.warning(
                "log-analysis chat: archive too large session_id=%s: %s",
                effective_session_id,
                exc,
            )
            yield self._sse_event(
                {
                    "event": "error",
                    "reason": "archive_too_large",
                    "message": (
                        "这个附件解压后体积超出了分析上限。"
                        "请精简日志内容或拆分后再上传。"
                    ),
                }
            )
            return
        except UnsupportedUploadFormatError as exc:
            logger.info(
                "log-analysis chat: unsupported upload format session_id=%s: %s",
                effective_session_id,
                exc,
            )
            yield self._sse_event(
                {
                    "event": "error",
                    "reason": "unsupported_format",
                    "message": (
                        "这个附件既不是受支持的日志压缩包，也不是可识别的纯文本日志或 Excel 表格。"
                        "请上传 .zip/.tar.gz/.7z/.rar 等压缩包，"
                        "或 .log/.txt/.json/.xml/.csv/.xlsx/.xlsm 等文件后再试。"
                    ),
                }
            )
            return
        except WorkspaceError as exc:
            logger.warning(
                "log-analysis chat: workspace preparation failed session_id=%s: %s",
                effective_session_id,
                exc,
            )
            yield self._sse_event(
                {
                    "event": "error",
                    "reason": "archive_extract_failed",
                    "message": (
                        "这个附件保存成功了，但解压日志包时失败。"
                        "请确认压缩包完整、未加密、不是缺少分卷的 RAR；"
                        "也可以重新打包为 .zip 后再上传。"
                    ),
                }
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("log-analysis chat stream failed to start job: %s", exc, exc_info=True)
            yield self._sse_event(
                {
                    "event": "error",
                    "message": (
                        "抱歉，准备日志分析工作区时遇到了问题，暂时无法完成分析。"
                        "请稍后重试；若反复出现，请联系管理员。"
                    ),
                }
            )
            return

        try:
            async for chunk in self._subscribe(job):
                yield chunk
        except asyncio.CancelledError:
            # SSE was cancelled by client disconnect. The Job keeps running
            # in the background; the client (or another) can reattach.
            logger.warning(
                "log-analysis chat stream cancelled (client disconnect): session_id=%s job still running",
                effective_session_id,
            )
            raise

    @staticmethod
    def _upload_validation_error(upload_file: UploadFile, exc: Exception, locale: str) -> Dict[str, Any]:
        """Only expose known validation failures; never return internal exceptions."""
        filename = upload_file.filename or ("未命名文件" if locale == "zh" else "unnamed file")
        if isinstance(exc, UnsupportedFileTypeError):
            supported = ", ".join(file_validator.supported_extensions)
            file_type = exc.file_type
            if file_type == "unknown":
                file_type = "无扩展名" if locale == "zh" else "no extension"
            message = (
                f"无法上传附件「{filename}」：不支持的文件类型（{file_type}）。"
                f"支持的文件扩展名：{supported}。请移除该附件或转换为支持的格式后重新上传；"
                "本批附件尚未保存，分析未启动。"
                if locale == "zh" else
                f'Cannot upload "{filename}": unsupported file type ({file_type}). '
                f"Supported file extensions: {supported}. Remove this attachment or convert it "
                "to a supported format and upload again. No files in this batch were saved; analysis has not started."
            )
            reason = "unsupported_format"
        elif isinstance(exc, FileSizeExceededError):
            message = (
                f"无法上传附件「{filename}」：文件大小 {exc.file_size / 1024 / 1024:.1f} MB，"
                f"超过单文件上限 {exc.max_size / 1024 / 1024:.1f} MB。请拆分或精简后重新上传。"
                if locale == "zh" else
                f'Cannot upload "{filename}": {exc.file_size / 1024 / 1024:.1f} MB exceeds '
                f"the per-file limit of {exc.max_size / 1024 / 1024:.1f} MB. Split or reduce the file and upload again."
            )
            reason = "file_too_large"
        else:
            message = (
                f"无法上传附件「{filename}」：{exc}。请修改文件名后重新上传。"
                if locale == "zh" else
                f'Cannot upload "{filename}": invalid filename. Use a name of at most 255 characters '
                "with letters, numbers, spaces, dots, hyphens or brackets; avoid hidden names and path separators."
            )
            reason = "invalid_filename"
        return {"event": "error", "reason": reason, "filename": filename, "message": message}

    async def _run_job_async(self, job: AgentJob, ctx: WorkspaceContext) -> None:
        """Background Agent task. Survives SSE disconnects; persists to DB on completion."""
        pending_title = None
        try:
            # Sync emitter: append the raw AgentTraceEvent to job.trace_events
            # (for late-subscriber replay + final `done` payload) AND push an
            # SSE-shaped frame onto job.events so live subscribers see the
            # event right away. The lambda is invoked from the Agent's thread
            # (via asyncio.to_thread → run_sync → asyncio.run); list.append
            # is thread-safe under the GIL.
            def _emit_trace(event: Dict[str, Any]) -> None:
                job.trace_events.append(event)
                job.events.append({"event": "agent_trace", **event})

            result = await asyncio.to_thread(
                LogAnalysisAgent().run_sync,
                ctx,
                job.cancel_event,
                _emit_trace,
                job.clarification,
            )
            result = self._attach_trigger_context(job, result)
            job.result = result
            answer_text = self._format_agent_result(
                result, question=job.question, context_meta=job.context_meta
            )
            job.answer = answer_text

            pending_title = await self._persist_job_result(job, result, answer_text)
            self._touch_context(job.session_id, result=result, answer=answer_text)

            # Best-effort: 分析判定需要代码修复时自动派发 Bug 修复任务。结果已在
            # 上方持久化；派发完全包在 try/except 中，绝不影响本轮回答与持久化。
            await self._maybe_dispatch_bug_fix(job)

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
                "log-analysis chat: agent job completed session_id=%s task_id=%s status=%s error_kind=%s duration=%ss",
                job.session_id,
                job.task_id,
                result.get("status"),
                result.get("error_kind"),
                int(time.monotonic() - job.started_at),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "log-analysis chat: agent job failed session_id=%s: %s",
                job.session_id,
                exc,
                exc_info=True,
            )
            job.error = str(exc)
            job.events.append({"event": "error", "message": str(exc)})
        finally:
            job.done = True
            job.finished_at = time.monotonic()
            # Project terminal state into chat_agent_runs + chat_run_service so
            # snapshot endpoints and sidebar overlay reflect completion.
            await self._finalize_chat_run(job)

        # The subscriber can now finish with the durable answer. Optional title
        # generation (including SDK timeout cleanup) must not delay completion.
        if pending_title is not None:
            from app.models.database import db_manager

            if db_manager.session_factory is not None:
                await generate_after_commit(pending_title, db_manager.session_factory)

    async def _maybe_dispatch_bug_fix(self, job: AgentJob) -> None:
        """Dispatch a Bug-fix task when this turn's analysis carries a code-fix signal.

        The main-chat log-analysis path runs the same agent as the Celery
        analysis task but historically never wired the auto-dispatch hook, so a
        ``requires_code_fix`` result here was silently dropped. This mirrors the
        Celery path (``ai_analysis._maybe_dispatch_bug_fix``): gated by the
        global flag + structured signal + a resolvable ``project_repo``.

        The actual dispatch is synchronous (sync SQLAlchemy session +
        ``run_bug_fix_task.delay``), so it runs in a worker thread to keep the
        event loop free. All failures are swallowed — the answer is already
        persisted and must not be affected.
        """
        if job.context_meta.get("forked_from_log_id"):
            return
        if not isinstance(job.result, dict):
            return
        # Cheap pre-check on the hot path: only touch the DB / Celery broker when
        # the global flag is on AND the structured signal is present. The
        # authoritative guard still runs inside ai_analysis._maybe_dispatch_bug_fix.
        if not settings.bug_fix_auto_dispatch:
            return
        from app.services import bug_fix_service

        if not bug_fix_service.should_dispatch(job.result):
            return
        try:
            await asyncio.to_thread(self._dispatch_bug_fix_sync, job)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "log-analysis chat: bug_fix dispatch failed (non-fatal) session_id=%s: %s",
                job.session_id,
                exc,
            )

    @staticmethod
    def _dispatch_bug_fix_sync(job: AgentJob) -> None:
        """Resolve the source log + project_repo and delegate to the shared
        dispatch policy on a synchronous session (runs in a worker thread)."""
        from app.models.log import LogRecord
        from app.tasks.ai_analysis import SessionLocal, _maybe_dispatch_bug_fix

        session = SessionLocal()
        try:
            log_id = job.context_meta.get("log_id")
            # context_meta["project_id"] is the user-selected project_repo.id.
            project_repo_id = job.context_meta.get("project_id")
            log_record = session.get(LogRecord, log_id) if log_id else None
            _maybe_dispatch_bug_fix(
                session,
                analysis_result=job.result or {},
                source_context=job.source_context,
                log_record=log_record,
                analysis_task_id=job.task_id,
                project_repo_id=project_repo_id,
            )
        finally:
            session.close()

    async def _register_chat_run(self, job: AgentJob, ctx: WorkspaceContext) -> None:
        """Create a ``chat_agent_runs`` row and register a ChatRunJob shadow.

        Best-effort: persistence and registry failures are logged but do not
        block the actual agent execution (the LogAnalysisChatService remains
        the source of truth for execution; chat_run_service mirrors state).
        """
        from app.models.database import db_manager
        from app.models.user import ChatAgentRun
        from app.services.chat_run_service import (
            RUN_STATUS_RUNNING,
            chat_run_service,
        )

        request_payload: Dict[str, Any] = {
            "filename": job.filename,
            "filenames": job.context_meta.get("filenames", []),
            "log_id": job.context_meta.get("log_id"),
            "log_ids": job.context_meta.get("log_ids", []),
            "attachments": job.context_meta.get("attachments", []),
            "project_id": job.context_meta.get("project_id"),
            "project_repo_id": job.context_meta.get("project_repo_id")
            or job.context_meta.get("project_id"),
        }
        if job.context_meta.get("forked_from_log_id"):
            request_payload["forked_from_log_id"] = job.context_meta["forked_from_log_id"]

        # Register the in-memory ChatRunJob first so even if DB write fails
        # the sidebar/active-run endpoint still surfaces the running task.
        try:
            chat_run_service.register_external_job(
                run_id=job.run_id,
                session_id=job.session_id,
                user_id=str(job.user_id) if job.user_id is not None else None,
                owner_scope=job.owner_scope,
                agent_kind="log_analysis",
                user_message=job.question,
                request_payload=request_payload,
                events_ref=job.events,
                trace_events_ref=job.trace_events,
                cancel_callback=lambda sid=job.session_id: self.cancel(sid),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "log-analysis chat: chat_run_service register failed run_id=%s: %s",
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
                        agent_kind="log_analysis",
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
                "log-analysis chat: chat_agent_runs row insert failed run_id=%s: %s",
                job.run_id,
                exc,
            )

    async def _finalize_chat_run(self, job: AgentJob) -> None:
        """Update ``chat_agent_runs`` terminal state and clear active pointer.

        Status mapping:
        - ``cancel_requested`` → ``cancelled``
        - ``job.error`` → ``failed``
        - otherwise → ``succeeded``
        """
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

        # Prefer the agent's own status when available — cancel_requested may
        # have been set after the agent already produced an answer.
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
                "log-analysis chat: chat_run_service finalize failed run_id=%s: %s",
                job.run_id,
                exc,
            )

        # Best-effort AI usage metrics, idempotent on run_id. Covers terminal
        # success as well as failed/cancelled states (the result still carries
        # any partial token_usage and the error_kind).
        try:
            from app.services import metrics_service

            log_id = job.context_meta.get("log_id")
            # context_meta["project_id"] is the user-selected project_repo.id
            # (see _create_context_from_upload); older saved contexts only
            # carry this key, so fall back to it.
            project_repo_id = job.context_meta.get(
                "project_repo_id"
            ) or job.context_meta.get("project_id")
            await metrics_service.record_agent_run_usage(
                source="log_analysis_agent",
                agent_kind="log_analysis",
                run_id=job.run_id,
                result=job.result,
                terminal_status=terminal_status,
                provider=settings.anthropic_provider,
                user_id=str(job.user_id) if job.user_id is not None else None,
                owner_scope=job.owner_scope or None,
                session_id=job.session_id,
                task_id=job.task_id,
                log_id=str(log_id) if log_id is not None else None,
                project_repo_id=str(project_repo_id) if project_repo_id is not None else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "log-analysis chat: metrics record skipped run_id=%s: %s",
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
                "log-analysis chat: chat_agent_runs finalize failed run_id=%s: %s",
                job.run_id,
                exc,
            )

    async def _persist_job_result(
        self,
        job: AgentJob,
        result: Dict[str, Any],
        answer_text: str,
    ) -> Optional[PendingSessionTitle]:
        """Open a fresh DB session and persist analysis result + chat exchange.

        Uses its own session because the original request DB session is already
        closed by the time this method runs (Job outlives the HTTP request).
        Return title work only after commit; the caller runs it after completion.
        """
        from app.models.database import db_manager

        if db_manager.session_factory is None:
            logger.warning("log-analysis chat: db not initialized; skipping persistence")
            return

        pending_title = None
        try:
            async with db_manager.session_factory() as db:
                try:
                    await self._save_analysis_result(
                        db=db,
                        context_meta=job.context_meta,
                        result=result,
                        question=job.question,
                    )
                    if job.remember and job.user_id is not None:
                        pending_title = await self._persist_exchange(
                            db=db,
                            user_id=job.user_id,
                            session_id=job.session_id,
                            question=job.question,
                            answer=answer_text,
                            images_json=job.images_json,
                            filename=job.filename,
                            locale=job.locale,
                        )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
            return pending_title
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "log-analysis chat: failed to persist job result session_id=%s: %s",
                job.session_id,
                exc,
                exc_info=True,
            )

    async def _subscribe(self, job: AgentJob) -> AsyncIterator[str]:
        """Yield SSE chunks for this Job. Replays the full event buffer to
        late subscribers, then polls for new events until the Job is done.

        Late subscribers automatically get the full historical ``agent_trace``
        stream because those frames live in ``job.events`` next to the
        coarse ``log_analysis_status`` frames — the ``sent`` cursor walks
        every appended event in order.

        Heartbeats: if no event has been buffered for
        ``_AGENT_PROGRESS_INTERVAL_SECONDS`` (15s) while the Job is still
        running, yield a ``system_notice{kind: heartbeat}`` frame to keep
        proxies / browsers from closing the SSE stream as idle.
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

    def cancel(self, session_id: str, user: Optional[User] = None) -> bool:
        """Request cancellation of the in-flight Agent Job for ``session_id``.

        Returns True if a cancel signal was sent, False if no in-flight job.
        Raises PermissionError if the caller is not the Job owner.
        """
        job = self._jobs.get(session_id or "")
        if job is None or job.done:
            return False
        if user is not None and job.user_id is not None and getattr(user, "id", None) != job.user_id:
            raise PermissionError("当前用户无权取消这个分析任务")
        if job.cancel_requested:
            return True
        job.cancel_requested = True
        job.cancel_event.set()
        from app.services.chat_run_service import chat_run_service

        broker = chat_run_service.get_broker_by_run_id(job.run_id)
        if broker is not None:
            broker.close()
        job.events.append(
            {
                "event": "log_analysis_status",
                "message": "已收到取消请求，正在等待 Agent 退出（最多一条 SDK 消息的延迟）...",
                "cancel_requested": True,
            }
        )
        logger.info("log-analysis chat: cancel requested session_id=%s", session_id)
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
            "filename": job.filename,
            "filenames": job.context_meta.get("filenames", []),
            "log_ids": job.context_meta.get("log_ids", []),
            "attachments": job.context_meta.get("attachments", []),
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
            logger.debug("log-analysis chat: evicted finished job session_id=%s", sid)

    async def _create_context_from_upload(
        self,
        *,
        db: Optional[AsyncSession],
        session_id: str,
        question: str,
        files: List[UploadFile],
        user: Optional[User],
        project_repo_id: Optional[int] = None,
    ) -> Tuple[WorkspaceContext, Dict[str, Any]]:
        if db is None:
            raise RuntimeError("数据库会话不可用，无法保存日志附件")
        if not files:
            raise MissingArchiveError("没有收到可用的日志附件")

        old_context = self._load_context(session_id, user=user)
        old_ctx = old_context[0] if old_context else None

        # project_repo_id is already a project_repo.id. The log's project is taken
        # directly from the user's explicit selection; there is no longer any
        # filename-based auto-classification.
        effective_project_id = project_repo_id
        analysis_group_id = str(uuid.uuid4())
        upload_request = LogUploadRequest(
            # Legacy processing-control sentinel (NOT a classification): forcing the
            # "oam_antenna" project_code makes LogService mark the record completed
            # and skip the protocol-stack processing Celery task, because this
            # endpoint owns analysis itself via the Claude Agent SDK. The real
            # project is assigned right after. This control hook is slated to move
            # to the planned plugin/skill mechanism for protocol-stack handling.
            project_code="oam_antenna",
            project_id=None,
            log_level=LogLevel.INFO,
            metadata=LogMetadata(
                source="ai_chat",
                extra_fields={
                    "chat_session_id": session_id,
                    "analysis_group_id": analysis_group_id,
                },
            ),
            issue_description=question,
        )
        log_records = []
        for upload_file in files:
            log_info = await log_service.upload_log(db, upload_file, upload_request)
            log_record = await log_service.get_by_id(db, log_info.id)
            if log_record is None:
                raise RuntimeError(
                    f"日志附件 {upload_file.filename!r} 已上传但未找到数据库记录"
                )
            log_record.analysis_group_id = analysis_group_id
            log_record.project_id = effective_project_id
            log_record.status = LogStatus.COMPLETED
            log_record.progress = 100.0
            log_record.issue_description = question
            log_records.append(log_record)

        # 立即提交：SSE 流会持有 request 的 AsyncSession 很久（一次 Agent 运行常
        # 达数分钟）；只 flush 不 commit 会让 SQLite 的写锁一直被持有，导致并发
        # 上传/重构包检索触发 "database is locked"。
        await db.commit()

        # When the user explicitly chose a project, skip metadata.json
        # extraction validation in the workspace and resolve repo_info
        # directly from the registry.
        if len(log_records) == 1:
            ctx = prepare(
                log_records[0], require_metadata=project_repo_id is None
            )
        else:
            ctx = prepare_many(
                log_records, require_metadata=project_repo_id is None
            )
        ctx.metadata.update(
            {
                "question": question,
                "project_id": effective_project_id,
                "hints": "",
            }
        )
        if project_repo_id is not None:
            self._inject_repo_info_from_project_id(ctx, project_repo_id)
        else:
            self._inject_repo_info(ctx)

        filenames = [
            record.original_filename or record.filename for record in log_records
        ]
        log_ids = [record.id for record in log_records]
        context_meta = {
            "session_id": session_id,
            "owner_user_id": getattr(user, "id", None),
            "owner_user": self._user_snapshot(user),
            "task_id": ctx.task_id,
            "temp_dir": ctx.temp_dir,
            "logs_dir": ctx.logs_dir,
            "repo_dir": ctx.repo_dir,
            "task_json_path": ctx.task_json_path,
            # Singular fields are retained for older result/metrics readers.
            "log_id": log_ids[0],
            "log_ids": log_ids,
            "analysis_group_id": analysis_group_id,
            "filename": filenames[0],
            "filenames": filenames,
            "attachment_count": len(filenames),
            "upload_kind": ctx.metadata.get("upload_kind"),
            "attachments": ctx.metadata.get("attachments", []),
            "project_id": effective_project_id,
            # Duplicate under the canonical name used by metrics/attribution
            # readers; "project_id" above is kept for saved-context compat.
            "project_repo_id": effective_project_id,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        self._save_context(session_id, context_meta)
        if old_ctx and old_ctx.temp_dir != ctx.temp_dir:
            cleanup(old_ctx)
        return ctx, context_meta

    def _inject_repo_info(self, ctx: WorkspaceContext) -> None:
        try:
            from app.tasks.ai_analysis import SessionLocal, _inject_repo_info

            sync_session = SessionLocal()
            try:
                _inject_repo_info(sync_session, ctx)
            finally:
                sync_session.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("log-analysis chat: repo_info injection skipped: %s", exc)

    def _inject_repo_info_from_project_id(
        self, ctx: WorkspaceContext, project_repo_id: int
    ) -> None:
        try:
            from app.tasks.ai_analysis import (
                SessionLocal,
                _inject_repo_info_from_project_id,
            )

            sync_session = SessionLocal()
            try:
                ok = _inject_repo_info_from_project_id(
                    sync_session, ctx, project_repo_id
                )
                if not ok:
                    logger.warning(
                        "log-analysis chat: project_repo_id=%s not resolvable; "
                        "agent will run without explicit repo_info",
                        project_repo_id,
                    )
            finally:
                sync_session.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "log-analysis chat: repo_info injection from project_id failed: %s", exc
            )

    def _bind_question_and_hints(self, ctx: WorkspaceContext, *, question: str, hints: str) -> None:
        try:
            from app.tasks.ai_analysis import _bind_query_to_workspace

            _bind_query_to_workspace(ctx, query=question, project_id=ctx.metadata.get("project_id"))
        except Exception:
            ctx.metadata["question"] = question

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
        task_path.write_text(json.dumps(task_data, ensure_ascii=False, indent=2), encoding="utf-8")
        ctx.metadata["question"] = question
        ctx.metadata["hints"] = hints

    @staticmethod
    def _bind_locale_to_task(ctx: WorkspaceContext, locale: str) -> None:
        """Persist the active response-language contract into task.json.

        The log-analysis agent is instructed through the system prompt too, but
        it reads task.json early and loaded skills may anchor on that file. Put
        the locale contract there as an additional source of truth.
        """
        task_path = Path(ctx.task_json_path)
        try:
            task_data = json.loads(task_path.read_text(encoding="utf-8"))
            if not isinstance(task_data, dict):
                task_data = {}
        except Exception:
            task_data = {}

        task_data["response_locale"] = normalize_locale(locale)
        task_data["response_language_instruction"] = response_language_directive(locale)
        task_path.write_text(
            json.dumps(task_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _build_history_hint(
        self,
        *,
        db: Optional[AsyncSession],
        user: Optional[User],
        session_id: str,
        history_json: Optional[str],
        source_images: Optional[List[chat_image_store.StoredImage]] = None,
    ) -> str:
        messages: List[ChatMessage] = []
        missing_image_ids: List[str] = []
        if user and db:
            try:
                records = await chat_history_service.fetch_messages(
                    db,
                    user_id=user.id,
                    session_id=session_id,
                )
                messages = chat_history_service.to_chat_messages(records)
                # Text-only analysis providers do not materialize images/.
                # Preserve earlier turns' originals for the independent reviewer.
                if source_images is not None:
                    known_ids = {image.id for image in source_images}
                    for record in records:
                        for item in chat_image_store.parse_meta_json(record.images_json):
                            image_id = item.get("id", "")
                            path = chat_image_store.resolve_path(session_id, image_id)
                            if path is None:
                                missing_image_ids.append(image_id)
                            if path is not None and image_id not in known_ids:
                                source_images.append(chat_image_store.StoredImage(
                                    image_id, item.get("media_type", "image/png"),
                                    item.get("name", ""), item.get("size", 0), str(path),
                                ))
                                known_ids.add(image_id)
            except Exception:
                messages = []

        if not messages:
            messages = self._parse_client_history(history_json)

        if not messages:
            return "历史截图原件不可用，仅可依据 OCR/描述：" + ", ".join(missing_image_ids) if missing_image_ids else ""

        recent = messages[-12:]
        lines = ["以下是同一对话中此前的上下文。用户可能会用“刚才/这个/继续”等指代，请结合这些内容理解当前问题："]
        for msg in recent:
            role = "用户" if msg.role == "user" else "助手"
            content = " ".join(str(msg.content or "").split())
            if len(content) > 1600:
                content = content[:1600] + "..."
            if content:
                lines.append(f"- {role}: {content}")
        if missing_image_ids:
            lines.append("历史截图原件不可用，仅可依据 OCR/描述：" + ", ".join(missing_image_ids))
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
        filename: Optional[str],
        locale: Optional[str] = None,
        images_json: Optional[str] = None,
    ) -> Optional[PendingSessionTitle]:
        """Persist exchange; caller commits before generating the optional title."""
        user_content = question
        if filename:
            user_content = f"{question}\n\n[日志附件] {filename}"
        session = await chat_history_service.save_exchange(
            db,
            user_id=user_id,
            session_id=session_id,
            user_content=user_content,
            ai_content=answer,
            title_hint=question,
            user_images_json=images_json,
        )
        return PendingSessionTitle.from_exchange(session, user_content, answer, locale)

    async def _save_analysis_result(
        self,
        *,
        db: Optional[AsyncSession],
        context_meta: Dict[str, Any],
        result: Dict[str, Any],
        question: Optional[str] = None,
    ) -> None:
        if not db or not context_meta.get("log_id") or context_meta.get("forked_from_log_id"):
            return
        try:
            await log_service.save_ai_analysis_result(
                db, context_meta["log_id"], result, query=question
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("log-analysis chat: failed to save result to log record: %s", exc)

    @staticmethod
    def _user_snapshot(user: Optional[User]) -> Dict[str, Any]:
        if user is None:
            return {}
        return {
            "id": getattr(user, "id", None),
            "username": getattr(user, "username", None),
            "display_name": getattr(user, "display_name", None),
            "email": getattr(user, "email", None),
        }

    @staticmethod
    def _attach_trigger_context(job: AgentJob, result: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return result

        enriched = dict(result)
        user_snapshot = {
            key: value
            for key, value in (job.user_snapshot or {}).items()
            if value is not None
        }
        if not user_snapshot and job.user_id is not None:
            user_snapshot = {"id": str(job.user_id)}

        triggered_by = {
            "source": "ai_chat",
            "run_id": job.run_id,
            "session_id": job.session_id,
            "task_id": job.task_id,
            "user": user_snapshot,
            "started_at": job.started_at_utc or datetime.utcnow().isoformat(),
        }
        enriched["triggered_by"] = triggered_by
        return enriched

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
            raise PermissionError("当前用户无权访问这个日志分析上下文")

        required_paths = ["temp_dir", "logs_dir", "repo_dir", "task_json_path"]
        if any(not meta.get(key) for key in required_paths):
            return None
        if not Path(meta["temp_dir"]).exists() or not Path(meta["task_json_path"]).exists():
            return None

        ctx = WorkspaceContext(
            task_id=meta.get("task_id") or Path(meta["temp_dir"]).name,
            temp_dir=meta["temp_dir"],
            logs_dir=meta["logs_dir"],
            repo_dir=meta["repo_dir"],
            task_json_path=meta["task_json_path"],
            metadata={
                "question": "",
                "project_id": meta.get("project_id"),
                "hints": "",
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
            logger.debug("log-analysis chat: failed to touch context", exc_info=True)

    def _context_file(self, session_id: str) -> Path:
        safe = _SESSION_KEY_RE.sub("_", session_id or "")
        if not safe:
            safe = str(uuid.uuid4())
        return self.registry_dir / f"{safe}.json"

    @staticmethod
    def _uploaded_filename(file: Optional[UploadFile]) -> Optional[str]:
        filename = getattr(file, "filename", None)
        if isinstance(filename, str) and filename.strip():
            return filename.strip()
        return None

    @classmethod
    def _uploaded_files(
        cls,
        files: Optional[List[UploadFile]],
        *,
        legacy_file: Optional[UploadFile] = None,
    ) -> List[UploadFile]:
        """Normalize the repeated ``files`` field plus the legacy ``file``."""
        normalized = [
            upload_file
            for upload_file in (files or [])
            if cls._uploaded_filename(upload_file)
        ]
        if cls._uploaded_filename(legacy_file) and all(
            upload_file is not legacy_file for upload_file in normalized
        ):
            normalized.append(legacy_file)
        return normalized

    @staticmethod
    def _display_filenames(context_meta: Dict[str, Any]) -> Optional[str]:
        filenames = context_meta.get("filenames")
        if isinstance(filenames, list):
            cleaned = [
                str(filename).strip()
                for filename in filenames
                if str(filename).strip()
            ]
            if cleaned:
                return "、".join(cleaned)
        filename = context_meta.get("filename")
        return str(filename).strip() if filename else None

    @staticmethod
    def _format_agent_result(
        result: Dict[str, Any],
        *,
        question: str,
        context_meta: Dict[str, Any],
    ) -> str:
        status = result.get("status") or "unknown"
        filename = context_meta.get("filename") or "当前日志包"
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
            "**日志分析 Agent** 已完成本轮分析。",
            "",
            f"- 日志包：`{filename}`",
            f"- 问题：{question}",
            f"- 状态：`{display_status}`",
            f"- 模型：`{model}`" + (f"，耗时：{duration}s" if duration is not None else ""),
            "- 上下文：已保留本次解压日志与代码工作区，可在当前对话继续追问。",
        ]

        if result.get("error_kind"):
            lines.append(f"- 错误类型：`{result.get('error_kind')}`")

        if answer:
            lines.extend(["", "## 回答", answer])
        elif status == "schema_mismatch":
            lines.extend(["", "## 回答", "模型返回内容不完整，且未能提取出可展示的回答。"])

        if summary and summary != answer:
            lines.extend(["", "## 摘要", summary])

        hypotheses = result.get("root_cause_hypotheses")
        if isinstance(hypotheses, list) and hypotheses:
            lines.extend(["", "## 根因假设"])
            for item in hypotheses:
                if isinstance(item, dict):
                    text = item.get("hypothesis") or item.get("description") or json.dumps(item, ensure_ascii=False)
                    evidence = item.get("evidence")
                    lines.append(f"- {text}")
                    if isinstance(evidence, list) and evidence:
                        lines.append(f"  证据：{'; '.join(str(e) for e in evidence)}")
                else:
                    lines.append(f"- {item}")

        actions = result.get("recommended_actions")
        if isinstance(actions, list) and actions:
            lines.extend(["", "## 建议"])
            for item in actions:
                if isinstance(item, dict):
                    lines.append(f"- {item.get('action') or item.get('description') or json.dumps(item, ensure_ascii=False)}")
                else:
                    lines.append(f"- {item}")

        keywords = result.get("related_keywords")
        if isinstance(keywords, list) and keywords:
            rendered = " ".join(f"`{str(keyword)}`" for keyword in keywords if str(keyword).strip())
            if rendered:
                lines.extend(["", "## 关键词", rendered])

        return "\n".join(lines).strip()

    @staticmethod
    def _sse_event(payload: Dict[str, object]) -> str:
        safe_payload = jsonable_encoder(payload)
        return f"data: {json.dumps(safe_payload, ensure_ascii=False)}\n\n"


log_analysis_chat_service = LogAnalysisChatService()
