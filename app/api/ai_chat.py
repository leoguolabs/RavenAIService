"""
AI 对话相关 API
"""
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat_uploads import ChatUploadRoute, parse_image_uploads
from app.api.users import get_current_user, get_optional_user, get_request_locale
from app.i18n.messages import t
from app.models.chat import (
    ChatRequest,
    ChatResponse,
    ImageValidationError,
    validate_images,
)
from app.models.database import get_db
from app.services import chat_image_store, ocr_service
from app.services.ai_chat_service import ai_chat_service
from app.services.chat_history_service import chat_history_service
from app.services.chat_run_service import chat_run_service
from app.services.owner_scope import resolve_owner_scope
from app.services.title_generator_service import summarize_user_message
from app.services.log_analysis_chat_service import log_analysis_chat_service
from app.services.package_search_chat_service import package_search_chat_service
from app.services.project_expert_chat_service import project_expert_chat_service


class LogAnalysisCancelRequest(BaseModel):
    session_id: str


class ProjectExpertCancelRequest(BaseModel):
    session_id: str


class PackageSearchCancelRequest(BaseModel):
    session_id: str


class SummarizeRequest(BaseModel):
    """Immediate session-title summary for a single user message."""

    user_content: str
    session_id: Optional[str] = None
    max_length: int = 16
    persist: bool = True


class SummarizeResponse(BaseModel):
    success: bool = True
    message: str = "ok"
    summary: str
    session_id: Optional[str] = None
    persisted: bool = False


class ChatPermissionResolveRequest(BaseModel):
    """HITL 决策提交体（前端弹窗 -> 后端 broker）。

    ``run_id`` 优先：精确定位某次 run 的 broker；同一 session 可能存在多个历史 run。
    ``session_id`` 兼容旧前端，没有 ``run_id`` 时按 session 的 active run 查找。
    ``updated_args`` 仅在 ``decision="allow"`` 且用户编辑参数时透传给 SDK。
    """

    decision: str
    updated_args: Optional[dict] = None
    message: Optional[str] = None
    run_id: Optional[str] = None
    session_id: Optional[str] = None


class ChatPermissionResolveResponse(BaseModel):
    success: bool = True
    message: str = "ok"
    request_id: str
    decision: str


class ChatClarificationAnswer(BaseModel):
    """单个澄清问题的用户答案。

    ``selected_labels`` 为用户从预设 options 中选中的 label（多选可多个，单选一个）；
    ``custom_text`` 为用户的自由输入。两者至少其一非空，否则视为未作答。
    """

    question_index: int
    selected_labels: List[str] = []
    custom_text: Optional[str] = None

    def is_answered(self) -> bool:
        if any(str(s).strip() for s in self.selected_labels):
            return True
        return bool((self.custom_text or "").strip())


class ChatClarificationResolveRequest(BaseModel):
    """澄清答案提交体（前端问题卡片 -> 后端 broker）。

    ``run_id`` 优先、``session_id`` 兼容，查找逻辑与工具审批一致。
    """

    answers: List[ChatClarificationAnswer]
    run_id: Optional[str] = None
    session_id: Optional[str] = None


class ChatClarificationResolveResponse(BaseModel):
    success: bool = True
    message: str = "ok"
    request_id: str


router = APIRouter(route_class=ChatUploadRoute)
logger = logging.getLogger(__name__)


@router.post("/chat", response_model=ChatResponse, summary="AI 对话")
async def chat_endpoint(
    request: ChatRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> ChatResponse:
    logger.info("=" * 80)
    logger.info("接收到 AI 对话请求")
    logger.info(f"请求消息: {request.message[:100]}...")
    logger.info(f"session_id: {request.session_id}")
    logger.info(f"历史记录条数: {len(request.history) if request.history else 0}")
    logger.info("=" * 80)
    from app.i18n.deps import LOCALE_HEADER, resolve_locale

    locale = resolve_locale(
        header_locale=http_request.headers.get(LOCALE_HEADER),
        accept_language=http_request.headers.get("Accept-Language"),
        user=current_user,
    )
    try:
        validate_images(request.images, locale=locale)
    except ImageValidationError as exc:
        raise HTTPException(status_code=413 if exc.reason == "image_too_large" else 400,
                            detail={"reason": exc.reason, "message": exc.message}) from exc
    try:
        response = await ai_chat_service.chat(
            request, db=db, user=current_user, locale=locale
        )
        logger.info("AI 对话请求处理成功")
        return response
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI chat request failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/chat/stream", summary="AI 对话（流式，create-or-subscribe）")
async def chat_stream_endpoint(
    request: ChatRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Create a new agent run or subscribe to the session's active run.

    Behaviour:
    - ``agent_type="device"`` routes to DeviceAgent; default (None/empty) routes
      to GeneralAgent (system usage assistant, small/fast model).
    - ``message`` non-empty and no active run for session: create a new
      :class:`ChatRunJob`, persist the user message + ``chat_agent_runs`` row,
      then subscribe to the run's SSE buffer.
    - ``message`` non-empty but session already has an active run: 409
      ``{"active_run_id": ...}`` so the frontend can switch to subscribe mode.
    - ``message`` empty and session has an active run: subscribe (resume).
    - ``message`` empty and no active run: 400.
    """
    logger.info(
        "chat/stream: session=%s msg_len=%d remember=%s device=%s agent_type=%s",
        request.session_id,
        len(request.message or ""),
        request.remember,
        request.target_device_id,
        request.agent_type,
    )

    session_id = request.session_id or str(uuid.uuid4())
    message_text = (request.message or "").strip()

    cookie_carrier = Response()
    owner_scope = resolve_owner_scope(http_request, cookie_carrier, current_user)

    from app.i18n.deps import LOCALE_HEADER, resolve_locale

    locale = resolve_locale(
        header_locale=http_request.headers.get(LOCALE_HEADER),
        accept_language=http_request.headers.get("Accept-Language"),
        user=current_user,
    )

    def _carry_cookies(target: StreamingResponse) -> StreamingResponse:
        for raw in cookie_carrier.raw_headers:
            name = raw[0].decode("latin-1") if isinstance(raw[0], bytes) else raw[0]
            value = raw[1].decode("latin-1") if isinstance(raw[1], bytes) else raw[1]
            if name.lower() == "set-cookie":
                target.raw_headers.append((b"set-cookie", value.encode("latin-1")))
        return target

    # Resume path: empty message -> subscribe to the session's active run.
    if not message_text:
        job = chat_run_service.get_active_job_for_session(owner_scope, session_id)
        if job is None:
            raise HTTPException(
                status_code=400,
                detail=t("chat.empty_message_no_run", locale),
            )
        async def _resume_stream():
            yield ai_chat_service._sse_event(  # noqa: SLF001
                {"event": "session", "session_id": session_id, "run_id": job.run_id}
            )
            async for chunk in chat_run_service.subscribe(job.run_id, owner_scope=owner_scope):
                yield chunk

        return _carry_cookies(StreamingResponse(
            _resume_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        ))

    # Image attachments (optional): validate at the boundary (4xx before
    # streaming), then run OCR and merge the recognized text into the user
    # message so the agent receives richer text with no protocol change. The
    # originals are also stored on disk so the frontend can re-render the
    # thumbnails when this conversation is reloaded from history.
    try:
        validate_images(request.images, locale=locale)
    except ImageValidationError as exc:
        raise HTTPException(
            status_code=413 if exc.reason == "image_too_large" else 400,
            detail={"reason": exc.reason, "message": exc.message},
        ) from exc

    enriched_message = request.message
    ocr_meta = None
    stored_images: list = []
    # Minted here rather than inside ``start_*_run`` so the OCR usage event can
    # carry the same run_id as the agent run it preprocesses for; the admin
    # audit feed uses that pairing to show them as one invocation.
    run_id = str(uuid.uuid4())
    if request.images:
        stored_images = chat_image_store.save_turn_images(
            request.images, session_id=session_id
        )
        enriched_message, ocr_meta = await ocr_service.enrich_message(
            request.message,
            request.images,
            user_id=str(current_user.id) if getattr(current_user, "id", None) else None,
            session_id=session_id,
            run_id=run_id,
            locale=locale,
        )

    # Create path: build history from DB (when authenticated) or request payload.
    history = await ai_chat_service._prepare_history(  # noqa: SLF001
        request, session_id, db, current_user
    )

    use_device_agent = (request.agent_type or "").strip().lower() == "device"

    try:
        if use_device_agent:
            job = await chat_run_service.start_device_run(
                db=db,
                user=current_user,
                owner_scope=owner_scope,
                session_id=session_id,
                user_message=enriched_message,
                images_json=chat_image_store.to_meta_json(stored_images),
                run_id=run_id,
                target_device_id=request.target_device_id or "",
                target_device_name=request.target_device_name,
                history=history,
                system_prompt_override=request.system_prompt,
                remember=request.remember,
                locale=locale,
            )
        else:
            job = await chat_run_service.start_general_run(
                db=db,
                user=current_user,
                owner_scope=owner_scope,
                session_id=session_id,
                user_message=enriched_message,
                images_json=chat_image_store.to_meta_json(stored_images),
                run_id=run_id,
                history=history,
                system_prompt_override=request.system_prompt,
                remember=request.remember,
                locale=locale,
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat/stream: start run failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat/stream: db commit failed: %s", exc)

    async def _stream():
        yield ai_chat_service._sse_event(  # noqa: SLF001
            {"event": "session", "session_id": job.session_id, "run_id": job.run_id}
        )
        # Surface OCR degradation so the frontend can show "images not recognized".
        if (
            ocr_meta is not None
            and ocr_meta.image_count > 0
            and ocr_meta.status in ("unconfigured", "failed")
        ):
            yield ai_chat_service._sse_event(  # noqa: SLF001
                {
                    "event": "ocr_status",
                    "status": ocr_meta.status,
                    "image_count": ocr_meta.image_count,
                    "error_kind": ocr_meta.error_kind,
                }
            )
        elif (
            ocr_meta is not None
            and ocr_meta.image_count > 0
            and ocr_meta.status == "succeeded"
            and ocr_meta.text
        ):
            yield ai_chat_service._sse_event(  # noqa: SLF001
                {
                    "event": "ocr_result",
                    "status": ocr_meta.status,
                    "image_count": ocr_meta.image_count,
                    "text": ocr_meta.text,
                }
            )
        async for chunk in chat_run_service.subscribe(job.run_id, owner_scope=owner_scope):
            yield chunk

    return _carry_cookies(StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    ))


@router.post("/chat/summarize", response_model=SummarizeResponse, summary="对用户输入立即生成简短会话摘要")
async def chat_summarize_endpoint(
    payload: SummarizeRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
) -> SummarizeResponse:
    content = (payload.user_content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail=t("chat.user_content_empty", locale))

    max_length = max(4, min(40, int(payload.max_length or 16)))
    summary = await summarize_user_message(content, max_length=max_length)

    persisted = False
    resolved_session_id = payload.session_id
    if payload.persist and current_user is not None:
        try:
            if not resolved_session_id:
                resolved_session_id = str(uuid.uuid4())
            session = await chat_history_service.ensure_session(
                db, current_user.id, session_id=resolved_session_id
            )
            await chat_history_service.update_session_title(
                db,
                user_id=current_user.id,
                session_id=session.id,
                title=summary,
            )
            await db.commit()
            resolved_session_id = session.id
            persisted = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat/summarize: 持久化标题失败: %s", exc)

    return SummarizeResponse(
        summary=summary,
        session_id=resolved_session_id,
        persisted=persisted,
    )


@router.post(
    "/chat/permissions/{request_id}/resolve",
    response_model=ChatPermissionResolveResponse,
    summary="DeviceAgent HITL 工具调用裁决",
)
async def chat_permission_resolve_endpoint(
    request_id: str,
    payload: ChatPermissionResolveRequest,
    http_request: Request,
    response: Response,
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
) -> ChatPermissionResolveResponse:
    """Resolve a pending ``can_use_tool`` request raised by a DeviceAgent run.

    Lookup order:
    1. ``run_id`` (preferred): O(1) lookup in :class:`ChatRunService` broker registry.
    2. ``session_id``: resolve to the session's active run, then lookup broker.
    3. Legacy scan: walk every broker in chat_run_service to find ``request_id``.

    Behaviour:
    - 400 if ``decision`` is not ``"allow"``/``"deny"``, or ``updated_args`` is not dict.
    - 403 if the caller is not the run's owner.
    - 404 if no broker holds ``request_id`` (already resolved, timed out, or unknown).
    - 200 with ``{request_id, decision}`` echoed back on success.
    """
    decision = (payload.decision or "").strip().lower()
    if decision not in {"allow", "deny"}:
        raise HTTPException(status_code=400, detail=t("chat.invalid_decision", locale))

    if payload.updated_args is not None and not isinstance(payload.updated_args, dict):
        raise HTTPException(
            status_code=400, detail=t("chat.updated_args_not_object", locale)
        )

    decision_payload: dict = {"decision": decision}
    if payload.updated_args is not None:
        decision_payload["updated_args"] = payload.updated_args
    if payload.message is not None:
        msg = str(payload.message).strip()
        if msg:
            decision_payload["message"] = msg

    owner_scope = resolve_owner_scope(http_request, response, current_user)

    def _owns(job_owner_scope: str) -> bool:
        return job_owner_scope == owner_scope

    # 1. run_id lookup — preferred. Only resolve if the run belongs to caller.
    if payload.run_id:
        job = chat_run_service.get_job(payload.run_id)
        if job is not None and _owns(job.owner_scope):
            broker = chat_run_service.get_broker_by_run_id(payload.run_id)
            if broker is not None and broker.resolve(request_id, decision_payload):
                return ChatPermissionResolveResponse(request_id=request_id, decision=decision)

    # 2. session_id fallback — resolve to active run within owner_scope.
    if payload.session_id:
        job = chat_run_service.get_active_job_for_session(owner_scope, payload.session_id)
        if job is not None:
            broker = chat_run_service.get_broker_by_run_id(job.run_id)
            if broker is not None and broker.resolve(request_id, decision_payload):
                return ChatPermissionResolveResponse(request_id=request_id, decision=decision)

    # 3. Legacy scan: filter by owner_scope before attempting resolve, so user B
    # can never observe or resolve user A's pending permission via brute force.
    for run_id, broker in list(chat_run_service._brokers.items()):  # noqa: SLF001
        job = chat_run_service.get_job(run_id)
        if job is None or not _owns(job.owner_scope):
            continue
        if broker.resolve(request_id, decision_payload):
            return ChatPermissionResolveResponse(request_id=request_id, decision=decision)

    raise HTTPException(
        status_code=404,
        detail=t("chat.permission_not_found", locale, request_id=request_id),
    )


@router.post(
    "/chat/clarifications/{request_id}/resolve",
    response_model=ChatClarificationResolveResponse,
    summary="AskUserQuestion 澄清答案提交",
)
async def chat_clarification_resolve_endpoint(
    request_id: str,
    payload: ChatClarificationResolveRequest,
    http_request: Request,
    response: Response,
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
) -> ChatClarificationResolveResponse:
    """Resolve a pending clarification owned by an interactive Agent run.

    Lookup / ownership rules mirror :func:`chat_permission_resolve_endpoint`:
    1. ``run_id`` (preferred) → 2. ``session_id`` active run → 3. legacy scan,
    all filtered by ``owner_scope`` so user B can never resolve user A's request.

    Behaviour:
    - 400 if ``answers`` is empty or any answer has neither a selected option nor
      custom text (a required question was left blank).
    - 403/404 handled the same way as permissions (we return 404 on miss).
    - 200 with ``{request_id}`` on success; the broker future receives
      ``{"answers": [...]}`` and the paused run resumes.
    """
    if not payload.answers or any(not ans.is_answered() for ans in payload.answers):
        raise HTTPException(
            status_code=400, detail=t("chat.clarification_answers_invalid", locale)
        )

    answers_payload = [
        {
            "question_index": ans.question_index,
            "selected_labels": [s for s in ans.selected_labels if str(s).strip()],
            "custom_text": (ans.custom_text or "").strip() or None,
        }
        for ans in payload.answers
    ]
    decision_payload: dict = {"answers": answers_payload}

    owner_scope = resolve_owner_scope(http_request, response, current_user)

    def _owns(job_owner_scope: str) -> bool:
        return job_owner_scope == owner_scope

    # 1. run_id lookup — preferred. Only resolve if the run belongs to caller.
    if payload.run_id:
        job = chat_run_service.get_job(payload.run_id)
        if job is not None and _owns(job.owner_scope):
            broker = chat_run_service.get_broker_by_run_id(payload.run_id)
            if broker is not None and broker.resolve(request_id, decision_payload):
                return ChatClarificationResolveResponse(request_id=request_id)

    # 2. session_id fallback — resolve to active run within owner_scope.
    if payload.session_id:
        job = chat_run_service.get_active_job_for_session(owner_scope, payload.session_id)
        if job is not None:
            broker = chat_run_service.get_broker_by_run_id(job.run_id)
            if broker is not None and broker.resolve(request_id, decision_payload):
                return ChatClarificationResolveResponse(request_id=request_id)

    # 3. Legacy scan: filter by owner_scope before attempting resolve.
    for run_id, broker in list(chat_run_service._brokers.items()):  # noqa: SLF001
        job = chat_run_service.get_job(run_id)
        if job is None or not _owns(job.owner_scope):
            continue
        if broker.resolve(request_id, decision_payload):
            return ChatClarificationResolveResponse(request_id=request_id)

    raise HTTPException(
        status_code=404,
        detail=t("chat.clarification_not_found", locale, request_id=request_id),
    )


# ──────────────────────── Chat image attachments ────────────────────────


@router.get(
    "/chat-images/{session_id}/{image_id}",
    summary="读取会话中某张用户附带图片的原图",
)
async def chat_image_endpoint(
    session_id: str,
    image_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Serve one stored attachment, scoped to the caller's own session.

    Authorization is by session ownership: only the user the session belongs to
    can read its images. ``chat_image_store.resolve_path`` refuses any id that
    is not a bare alphanumeric stem, so a crafted ``image_id`` cannot traverse
    outside the store.
    """
    from fastapi.responses import FileResponse
    from sqlalchemy import select

    from app.models.user import ChatSession

    result = await db.execute(
        select(ChatSession.id).where(
            ChatSession.id == session_id,
            ChatSession.user_id == current_user.id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    path = chat_image_store.resolve_path(session_id, image_id)
    if path is None:
        raise HTTPException(status_code=404, detail="图片不存在或已被清理")

    return FileResponse(
        str(path),
        # Private user content: never let a shared cache hold on to it.
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ──────────────────────── Chat Agent Run endpoints ────────────────────────


@router.get(
    "/chat/sessions/{session_id}/active-run",
    summary="查询某会话当前运行中的 chat agent run",
)
async def chat_active_run_endpoint(
    session_id: str,
    http_request: Request,
    response: Response,
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
):
    """Return the session's current active run snapshot or 404 if none.

    Lookup is scoped to ``(owner_scope, session_id)`` so two users with the
    same ``session_id`` see only their own run.
    """
    owner_scope = resolve_owner_scope(http_request, response, current_user)
    snapshot = chat_run_service.get_active_run_snapshot(owner_scope, session_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=t("chat.no_active_run", locale))
    return snapshot


@router.get(
    "/chat/runs/{run_id}",
    summary="获取指定 run 的快照（含 trace events / 状态 / pending permissions）",
)
async def chat_run_snapshot_endpoint(
    run_id: str,
    http_request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
):
    """In-memory snapshot first; fall back to ``chat_agent_runs`` for evicted
    terminal runs so users can still view historical trace events.

    All lookups enforce owner_scope; mismatches return 404 to avoid leaking
    the existence of other users' runs.
    """
    owner_scope = resolve_owner_scope(http_request, response, current_user)
    snapshot = chat_run_service.get_snapshot(run_id, owner_scope)
    if snapshot is not None:
        return snapshot
    snapshot = await chat_run_service.load_terminal_snapshot_from_db(
        db, run_id, owner_scope
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail=t("chat.run_not_found", locale))
    return snapshot


@router.get(
    "/chat/runs/{run_id}/stream",
    summary="订阅指定 run 的 SSE：先 replay 全部事件，再实时接续",
)
async def chat_run_stream_endpoint(
    run_id: str,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
):
    """SSE replay+follow for an existing run. Disconnect does not cancel the
    underlying agent job; clients can reconnect and resume from the buffer.

    Returns 404 for unknown runs and for runs owned by a different scope —
    we never reveal whether ``run_id`` exists for someone else.
    """
    cookie_carrier = Response()
    owner_scope = resolve_owner_scope(http_request, cookie_carrier, current_user)

    job = chat_run_service.get_job(run_id)
    if job is None or job.owner_scope != owner_scope:
        raise HTTPException(
            status_code=404,
            detail=t("chat.run_not_found_evicted", locale),
        )

    # get_optional_user queried this same cached request session. No DB work is
    # needed by replay/follow; return its connection before a long-lived SSE.
    if db is not None:
        await db.close()

    async def _stream():
        async for chunk in chat_run_service.subscribe(run_id, owner_scope=owner_scope):
            yield chunk

    sr = StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    for raw in cookie_carrier.raw_headers:
        name = raw[0].decode("latin-1") if isinstance(raw[0], bytes) else raw[0]
        value = raw[1].decode("latin-1") if isinstance(raw[1], bytes) else raw[1]
        if name.lower() == "set-cookie":
            sr.raw_headers.append((b"set-cookie", value.encode("latin-1")))
    return sr


@router.post(
    "/chat/runs/{run_id}/cancel",
    summary="取消正在运行的 chat agent run",
)
async def chat_run_cancel_endpoint(
    run_id: str,
    http_request: Request,
    response: Response,
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
):
    """Cancel the in-memory job (asyncio.Task.cancel) and let the run-driver
    persist the terminal ``cancelled`` state. Owner_scope mismatch → 404."""
    owner_scope = resolve_owner_scope(http_request, response, current_user)
    try:
        ok = chat_run_service.cancel(run_id, owner_scope=owner_scope)
    except HTTPException:
        raise
    if not ok:
        return {
            "run_id": run_id,
            "cancelled": False,
            "message": t("chat.run_not_found_or_terminal", locale),
        }
    return {"run_id": run_id, "cancelled": True}


@router.post("/log-analysis/stream", summary="主对话日志分析（流式）")
async def log_analysis_stream_endpoint(
    http_request: Request,
    message: str = Form("", description="用户问题"),
    session_id: Optional[str] = Form(None, description="对话会话ID"),
    history: Optional[str] = Form(None, description="前端传入的历史消息 JSON"),
    remember: bool = Form(True, description="是否保存到会话历史"),
    project_repo_id: Optional[int] = Form(
        None,
        description="可选：项目仓库注册表 ID。提供时跳过 metadata.json 校验，直接使用该项目的仓库信息。",
    ),
    files: Optional[List[UploadFile]] = File(
        None, description="可选：多份日志附件（重复 files 字段）"
    ),
    file: Optional[UploadFile] = File(
        None, description="兼容旧客户端：单份日志附件"
    ),
    images: Optional[str] = Form(
        None, description="可选：随消息附带图片的 JSON 数组字符串 [{media_type,data}]"
    ),
    image_files: Optional[List[UploadFile]] = File(
        None, description="可选：随消息附带的图片文件（推荐，重复 image_files 字段）"
    ),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    logger.info("=" * 80)
    logger.info("接收到主对话日志分析请求")
    logger.info("message: %s...", message[:100])
    logger.info(
        "session_id: %s, attachment_count=%d, project_repo_id=%s",
        session_id,
        len(files or []) + int(bool(file and file.filename)),
        project_repo_id,
    )
    logger.info("=" * 80)
    cookie_carrier = Response()
    owner_scope = resolve_owner_scope(http_request, cookie_carrier, current_user)
    from app.i18n.deps import LOCALE_HEADER, resolve_locale

    locale = resolve_locale(
        header_locale=http_request.headers.get(LOCALE_HEADER),
        accept_language=http_request.headers.get("Accept-Language"),
        user=current_user,
    )
    try:
        parsed_images = await parse_image_uploads(images, image_files, locale=locale)
    except ImageValidationError as exc:
        raise HTTPException(
            status_code=413 if exc.reason == "image_too_large" else 400,
            detail={"reason": exc.reason, "message": exc.message},
        ) from exc
    try:
        generator = log_analysis_chat_service.stream(
            message=message,
            session_id=session_id,
            history_json=history,
            file=file,
            files=files,
            images=parsed_images,
            remember=remember,
            project_repo_id=project_repo_id,
            db=db,
            user=current_user,
            owner_scope=owner_scope,
            locale=locale,
        )
        sr = StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        for raw in cookie_carrier.raw_headers:
            name = raw[0].decode("latin-1") if isinstance(raw[0], bytes) else raw[0]
            value = raw[1].decode("latin-1") if isinstance(raw[1], bytes) else raw[1]
            if name.lower() == "set-cookie":
                sr.raw_headers.append((b"set-cookie", value.encode("latin-1")))
        return sr
    except Exception as exc:  # noqa: BLE001
        logger.exception("Log analysis chat stream request failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/log-analysis/cancel", summary="取消进行中的日志分析任务")
async def log_analysis_cancel_endpoint(
    payload: LogAnalysisCancelRequest,
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
):
    try:
        ok = log_analysis_chat_service.cancel(payload.session_id, user=current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not ok:
        return {
            "session_id": payload.session_id,
            "cancelled": False,
            "message": t("chat.no_running_task", locale),
        }
    return {"session_id": payload.session_id, "cancelled": True}


@router.get("/log-analysis/result", summary="查询日志分析任务状态/结果（轮询兜底）")
async def log_analysis_result_endpoint(
    session_id: str = Query(..., description="对话会话 ID"),
    current_user=Depends(get_optional_user),
):
    try:
        return log_analysis_chat_service.get_status(session_id, user=current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/project-expert/stream", summary="主对话项目专家（流式）")
async def project_expert_stream_endpoint(
    http_request: Request,
    message: str = Form("", description="用户问题"),
    session_id: Optional[str] = Form(None, description="对话会话ID"),
    history: Optional[str] = Form(None, description="前端传入的历史消息 JSON"),
    remember: bool = Form(True, description="是否保存到会话历史"),
    project_repo_id: Optional[int] = Form(
        None,
        description="项目仓库注册表 ID。新会话必填，用作权威项目身份来源。",
    ),
    images: Optional[str] = Form(
        None, description="可选：随消息附带图片的 JSON 数组字符串 [{media_type,data}]"
    ),
    image_files: Optional[List[UploadFile]] = File(
        None, description="可选：随消息附带的图片文件（推荐，重复 image_files 字段）"
    ),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    logger.info("=" * 80)
    logger.info("接收到主对话项目专家请求")
    logger.info("message: %s...", message[:100])
    logger.info("session_id: %s, project_repo_id=%s", session_id, project_repo_id)
    logger.info("=" * 80)

    from app.i18n.deps import LOCALE_HEADER, resolve_locale

    locale = resolve_locale(
        header_locale=http_request.headers.get(LOCALE_HEADER),
        accept_language=http_request.headers.get("Accept-Language"),
        user=current_user,
    )

    # New session requires an explicit project: there is no metadata.json
    # fallback for project identity. Fail fast with 4xx before streaming.
    if project_repo_id is None and not project_expert_chat_service.session_has_workspace(
        session_id
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "project_repo_required",
                "message": t("project_expert.project_required", locale),
            },
        )

    try:
        parsed_images = await parse_image_uploads(images, image_files, locale=locale)
    except ImageValidationError as exc:
        raise HTTPException(
            status_code=413 if exc.reason == "image_too_large" else 400,
            detail={"reason": exc.reason, "message": exc.message},
        ) from exc

    cookie_carrier = Response()
    owner_scope = resolve_owner_scope(http_request, cookie_carrier, current_user)
    try:
        generator = project_expert_chat_service.stream(
            message=message,
            session_id=session_id,
            history_json=history,
            remember=remember,
            project_repo_id=project_repo_id,
            images=parsed_images,
            db=db,
            user=current_user,
            owner_scope=owner_scope,
            locale=locale,
        )
        sr = StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        for raw in cookie_carrier.raw_headers:
            name = raw[0].decode("latin-1") if isinstance(raw[0], bytes) else raw[0]
            value = raw[1].decode("latin-1") if isinstance(raw[1], bytes) else raw[1]
            if name.lower() == "set-cookie":
                sr.raw_headers.append((b"set-cookie", value.encode("latin-1")))
        return sr
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Project expert chat stream request failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/project-expert/cancel", summary="取消进行中的项目专家任务")
async def project_expert_cancel_endpoint(
    payload: ProjectExpertCancelRequest,
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
):
    try:
        ok = project_expert_chat_service.cancel(payload.session_id, user=current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not ok:
        return {
            "session_id": payload.session_id,
            "cancelled": False,
            "message": t("chat.no_running_task", locale),
        }
    return {"session_id": payload.session_id, "cancelled": True}


@router.get("/project-expert/result", summary="查询项目专家任务状态/结果（轮询兜底）")
async def project_expert_result_endpoint(
    session_id: str = Query(..., description="对话会话 ID"),
    current_user=Depends(get_optional_user),
):
    try:
        return project_expert_chat_service.get_status(session_id, user=current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/package-search/stream", summary="主对话配置管理员（流式）")
async def package_search_stream_endpoint(
    http_request: Request,
    message: str = Form("", description="用户问题"),
    session_id: Optional[str] = Form(None, description="对话会话ID"),
    history: Optional[str] = Form(None, description="前端传入的历史消息 JSON"),
    remember: bool = Form(True, description="是否保存到会话历史"),
    project_repo_id: Optional[int] = Form(
        None,
        description="项目仓库注册表 ID。新会话必填，用作权威项目身份来源。",
    ),
    images: Optional[str] = Form(
        None, description="可选：随消息附带图片的 JSON 数组字符串 [{media_type,data}]"
    ),
    image_files: Optional[List[UploadFile]] = File(
        None, description="可选：随消息附带的图片文件（推荐，重复 image_files 字段）"
    ),
    files: Optional[List[UploadFile]] = File(
        None, description="可选：一个或多个待制作整包的组件文件（字段名可重复）"
    ),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    logger.info("=" * 80)
    logger.info("接收到主对话配置管理员请求")
    logger.info("message: %s...", message[:100])
    logger.info("session_id: %s, project_repo_id=%s", session_id, project_repo_id)
    logger.info("=" * 80)

    from app.i18n.deps import LOCALE_HEADER, resolve_locale

    locale = resolve_locale(
        header_locale=http_request.headers.get(LOCALE_HEADER),
        accept_language=http_request.headers.get("Accept-Language"),
        user=current_user,
    )

    # Pure package search still requires a project.  An attachment-bearing
    # packaging turn may start unbound: project inference is part of the Skill
    # plan and the server forces the human to confirm it before any side effect.
    if project_repo_id is None and not package_search_chat_service.session_has_workspace(
        session_id
    ) and not files:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "project_repo_required",
                "message": t("package.project_repo_required", locale),
            },
        )

    try:
        parsed_images = await parse_image_uploads(images, image_files, locale=locale)
    except ImageValidationError as exc:
        raise HTTPException(
            status_code=413 if exc.reason == "image_too_large" else 400,
            detail={"reason": exc.reason, "message": exc.message},
        ) from exc

    cookie_carrier = Response()
    owner_scope = resolve_owner_scope(http_request, cookie_carrier, current_user)
    try:
        package_search_chat_service.assert_session_access(
            session_id,
            owner_scope=owner_scope,
            user=current_user,
        )
    except PermissionError as exc:
        # 404 avoids disclosing whether another user's session id exists.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        generator = package_search_chat_service.stream(
            message=message,
            session_id=session_id,
            history_json=history,
            remember=remember,
            project_repo_id=project_repo_id,
            images=parsed_images,
            files=files,
            db=db,
            user=current_user,
            owner_scope=owner_scope,
            locale=locale,
        )
        sr = StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        for raw in cookie_carrier.raw_headers:
            name = raw[0].decode("latin-1") if isinstance(raw[0], bytes) else raw[0]
            value = raw[1].decode("latin-1") if isinstance(raw[1], bytes) else raw[1]
            if name.lower() == "set-cookie":
                sr.raw_headers.append((b"set-cookie", value.encode("latin-1")))
        return sr
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Package search chat stream request failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/package-search/cancel", summary="取消进行中的配置管理员任务")
async def package_search_cancel_endpoint(
    payload: PackageSearchCancelRequest,
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
):
    try:
        ok = package_search_chat_service.cancel(payload.session_id, user=current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not ok:
        return {
            "session_id": payload.session_id,
            "cancelled": False,
            "message": t("chat.no_running_task", locale),
        }
    return {"session_id": payload.session_id, "cancelled": True}


@router.get("/package-search/result", summary="查询配置管理员任务状态/结果（轮询兜底）")
async def package_search_result_endpoint(
    session_id: str = Query(..., description="对话会话 ID"),
    current_user=Depends(get_optional_user),
):
    try:
        return package_search_chat_service.get_status(session_id, user=current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
