"""AI 对话服务：基于 DeviceAgent / GeneralAgent (Claude Agent SDK) 与持久化会话历史。"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.device_agent.agent import DeviceAgent, DeviceAgentContext
from app.agents.device_agent.permissions import PermissionBroker
from app.agents.general_agent.agent import GeneralAgent, GeneralAgentContext
from app.config import settings
from app.models.chat import ChatMessage, ChatRequest, ChatResponse
from app.models.user import User
from app.services.base import BaseService
from app.services.chat_history_service import chat_history_service
from app.services.openai_task_admission import admission_scope
from app.services.title_generator_service import (
    generate_session_title as _generate_title_external,
)

logger = logging.getLogger(__name__)


def _records_to_history_dicts(records: List[Any]) -> List[Dict[str, str]]:
    """Convert DB ChatMessage records to ``[{"role","content"}, ...]``."""
    out: List[Dict[str, str]] = []
    for record in records:
        role = getattr(record, "role", "user")
        if role not in {"user", "ai", "assistant", "system"}:
            role = "user"
        if role == "ai":
            role = "assistant"
        out.append({"role": role, "content": str(getattr(record, "content", ""))})
    return out


def _chat_messages_to_history_dicts(history: List[ChatMessage]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in history:
        role = (item.role or "user").lower()
        if role in {"ai", "assistant"}:
            role = "assistant"
        elif role == "system":
            role = "system"
        else:
            role = "user"
        out.append({"role": role, "content": item.content})
    return out


def _history_to_chat_messages(history: List[Dict[str, str]]) -> List[ChatMessage]:
    out: List[ChatMessage] = []
    for entry in history:
        role = entry.get("role") or "user"
        if role == "assistant":
            role = "ai"
        if role not in {"user", "ai", "system"}:
            role = "user"
        out.append(ChatMessage(role=role, content=str(entry.get("content") or "")))
    return out


class AIChatService(BaseService):
    """Raven AI 对话服务 (DeviceAgent backed)."""

    def __init__(self) -> None:
        super().__init__()
        # Session-scoped permission brokers — populated by DeviceAgent.run_stream
        # when a session starts and removed in its ``finally``. The HTTP endpoint
        # ``POST /chat/permissions/{request_id}/resolve`` looks brokers up here.
        self.permission_broker_registry: Dict[str, PermissionBroker] = {}

    # ──────────────── history loading ────────────────

    async def _load_history_from_db(
        self,
        db: AsyncSession,
        user: User,
        session_id: Optional[str],
    ) -> List[Dict[str, str]]:
        if not session_id:
            return []
        try:
            records = await chat_history_service.fetch_messages(
                db,
                user_id=user.id,
                session_id=session_id,
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                return []
            raise
        return _records_to_history_dicts(records)

    async def _prepare_history(
        self,
        payload: ChatRequest,
        session_id: str,
        db: Optional[AsyncSession],
        user: Optional[User],
    ) -> List[Dict[str, str]]:
        if user and db:
            stored = await self._load_history_from_db(db, user, session_id)
            if stored:
                return stored

        if payload.history:
            logger.info("chat: 使用前端传入的历史记录，条数: %d", len(payload.history))
            return _chat_messages_to_history_dicts(payload.history)

        return []

    # ──────────────── persistence ────────────────

    async def _persist_exchange(
        self,
        db: AsyncSession,
        user: User,
        session_id: str,
        user_content: str,
        answer_text: str,
        session_title: Optional[str] = None,
        title_hint: Optional[str] = None,
        images_json: Optional[str] = None,
    ) -> None:
        await chat_history_service.save_exchange(
            db,
            user_id=user.id,
            session_id=session_id,
            user_content=user_content,
            ai_content=answer_text,
            session_title=session_title,
            title_hint=title_hint,
            **({"user_images_json": images_json} if images_json else {}),
        )

    async def generate_session_title(
        self,
        user_content: str,
        ai_content: str,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        locale: Optional[str] = None,
    ) -> Optional[str]:
        """Public wrapper used by other services / API endpoints."""
        return await _generate_title_external(
            user_content,
            ai_content,
            user_id=user_id,
            session_id=session_id,
            locale=locale,
        )

    async def _try_generate_and_update_session_title(
        self,
        db: AsyncSession,
        user: User,
        session_id: str,
        user_content: str,
        answer_text: str,
    ) -> None:
        if not answer_text:
            return
        try:
            existing = await chat_history_service._get_session(db, user.id, session_id)
            # On the first exchange the title is just the auto-derived first user
            # message (set by append_message at run start), so it must be regenerated
            # by the lightweight model. Only treat a non-default title as a real
            # user rename — and therefore skip — once we're past the first exchange.
            # This mirrors the specialized agent services, which regenerate while
            # message_count <= 2.
            if existing and (existing.message_count or 0) > 2:
                if existing.title and existing.title.strip() != "新对话":
                    logger.info(
                        "chat: 跳过标题生成（已存在自定义标题）: %s", existing.title
                    )
                    return
        except Exception as exc:  # noqa: BLE001
            logger.debug("chat: 读取会话标题失败，继续尝试生成: %s", exc)

        try:
            session_title = await asyncio.wait_for(
                _generate_title_external(
                    user_content,
                    answer_text,
                    user_id=str(user.id) if getattr(user, "id", None) is not None else None,
                    session_id=session_id,
                    locale=getattr(user, "language", None),
                ),
                timeout=8,
            )
        except asyncio.TimeoutError:
            logger.warning("chat: 生成会话标题超时，跳过更新")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat: 生成会话标题失败，跳过更新: %s", exc)
            return

        if not session_title:
            return

        try:
            await chat_history_service.update_session_title(
                db,
                user_id=user.id,
                session_id=session_id,
                title=session_title,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat: 更新会话标题失败: %s", exc)

    # ──────────────── effective model resolution ────────────────

    @staticmethod
    def _effective_model() -> str:
        from app.agents.anthropic_client import PROVIDER_PROFILES

        profile = PROVIDER_PROFILES.get(settings.anthropic_provider)
        return (
            settings.anthropic_model
            or (profile.default_model if profile else "")
            or "unknown"
        )

    # ──────────────── agent routing ────────────────

    @staticmethod
    def _is_device_agent(payload: ChatRequest) -> bool:
        return (payload.agent_type or "").strip().lower() == "device"

    @staticmethod
    def _suggested_agent_from_events(events: List[Dict[str, Any]]) -> Optional[str]:
        """Pull ``suggested_agent_type`` out of the GeneralAgent run_complete event."""
        for ev in events:
            if isinstance(ev, dict) and ev.get("type") == "run_complete":
                suggested = ev.get("suggested_agent_type")
                if isinstance(suggested, str) and suggested:
                    return suggested
        return None

    # ──────────────── chat (non-streaming) ────────────────

    @admission_scope()
    async def chat(
        self,
        payload: ChatRequest,
        db: Optional[AsyncSession] = None,
        user: Optional[User] = None,
        locale: Optional[str] = None,
    ) -> ChatResponse:
        logger.info("AIChatService.chat: session=%s remember=%s device=%s agent_type=%s",
                    payload.session_id, payload.remember, payload.target_device_id,
                    payload.agent_type)

        session_id = payload.session_id or str(uuid.uuid4())
        history = await self._prepare_history(payload, session_id, db, user)
        is_new_session = len(history) == 0

        from app.services import chat_image_store
        images_json = chat_image_store.to_meta_json(
            chat_image_store.save_turn_images(payload.images, session_id=session_id)
        )

        suggested_agent_type: Optional[str] = None
        if self._is_device_agent(payload):
            ctx = DeviceAgentContext(
                session_id=session_id,
                images_json=images_json,
                user_message=payload.message,
                target_device_id=payload.target_device_id or "",
                target_device_name=payload.target_device_name,
                history=history,
                system_prompt_override=payload.system_prompt,
                broker_registry=self.permission_broker_registry,
                locale=locale,
            )
            events, answer_text, model = await DeviceAgent().run(ctx)
        else:
            ctx_general = GeneralAgentContext(
                session_id=session_id,
                images_json=images_json,
                user_message=payload.message,
                history=history,
                system_prompt_override=payload.system_prompt,
                locale=locale,
            )
            events, answer_text, model = await GeneralAgent().run(ctx_general)
            suggested_agent_type = self._suggested_agent_from_events(events)

        if not model:
            model = self._effective_model()

        messages = _history_to_chat_messages(history) + [
            ChatMessage(role="user", content=payload.message),
            ChatMessage(role="ai", content=answer_text),
        ]

        if payload.remember and user and db:
            try:
                await self._persist_exchange(
                    db, user, session_id,
                    payload.message, answer_text,
                    title_hint=None,
                    **({"images_json": images_json} if images_json else {}),
                )
                if is_new_session:
                    await self._try_generate_and_update_session_title(
                        db, user, session_id, payload.message, answer_text,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("chat: 持久化会话失败: %s", exc)

        return ChatResponse(
            session_id=session_id,
            answer=answer_text,
            model=model,
            messages=messages,
            message="ok",
            suggested_agent_type=suggested_agent_type,
        )

    # ──────────────── chat (streaming, SSE) ────────────────

    async def chat_stream(
        self,
        payload: ChatRequest,
        db: Optional[AsyncSession] = None,
        user: Optional[User] = None,
    ) -> AsyncIterator[str]:
        logger.info("AIChatService.chat_stream: session=%s remember=%s device=%s agent_type=%s",
                    payload.session_id, payload.remember, payload.target_device_id,
                    payload.agent_type)

        session_id = payload.session_id or str(uuid.uuid4())
        history = await self._prepare_history(payload, session_id, db, user)
        is_new_session = len(history) == 0

        yield self._sse_event({"event": "session", "session_id": session_id})

        from app.services import chat_image_store
        images_json = chat_image_store.to_meta_json(
            chat_image_store.save_turn_images(payload.images, session_id=session_id)
        )

        if self._is_device_agent(payload):
            ctx = DeviceAgentContext(
                session_id=session_id,
                images_json=images_json,
                user_message=payload.message,
                target_device_id=payload.target_device_id or "",
                target_device_name=payload.target_device_name,
                history=history,
                system_prompt_override=payload.system_prompt,
                broker_registry=self.permission_broker_registry,
            )
            agent_stream = DeviceAgent().run_stream(ctx)
        else:
            ctx_general = GeneralAgentContext(
                session_id=session_id,
                images_json=images_json,
                user_message=payload.message,
                history=history,
                system_prompt_override=payload.system_prompt,
            )
            agent_stream = GeneralAgent().run_stream(ctx_general)

        answer_text = ""
        model = ""
        suggested_agent_type: Optional[str] = None

        try:
            async for ev in agent_stream:
                if not isinstance(ev, dict):
                    continue
                event_type = ev.get("type")
                if not event_type:
                    continue

                if event_type == "run_start" and ev.get("model"):
                    model = str(ev.get("model") or "")
                if event_type == "run_complete":
                    final_text = ev.get("final_text")
                    if isinstance(final_text, str):
                        answer_text = final_text
                    elif isinstance(final_text, dict):
                        text_val = final_text.get("text")
                        if isinstance(text_val, str):
                            answer_text = text_val
                    suggested = ev.get("suggested_agent_type")
                    if isinstance(suggested, str) and suggested:
                        suggested_agent_type = suggested

                payload_out: Dict[str, Any] = {
                    k: v for k, v in ev.items() if k != "type"
                }
                payload_out["event"] = event_type
                payload_out["session_id"] = session_id
                yield self._sse_event(payload_out)
        except asyncio.CancelledError:
            logger.info("chat_stream: cancelled session=%s", session_id)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("chat_stream: agent failed: %s", exc)
            yield self._sse_event({"event": "error", "message": str(exc)})
            return

        if not model:
            model = self._effective_model()

        messages = _history_to_chat_messages(history) + [
            ChatMessage(role="user", content=payload.message),
            ChatMessage(role="ai", content=answer_text),
        ]

        if payload.remember and user and db:
            try:
                await self._persist_exchange(
                    db, user, session_id,
                    payload.message, answer_text,
                    title_hint=None,
                    **({"images_json": images_json} if images_json else {}),
                )
                if is_new_session:
                    await self._try_generate_and_update_session_title(
                        db, user, session_id, payload.message, answer_text,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("chat_stream: 持久化会话失败: %s", exc)

        yield self._sse_event(
            {
                "event": "done",
                "session_id": session_id,
                "answer": answer_text,
                "model": model,
                "messages": [m.model_dump() for m in messages],
                "suggested_agent_type": suggested_agent_type,
            }
        )

    # ──────────────── SSE helpers ────────────────

    @staticmethod
    def _sse_event(payload: Dict[str, object]) -> str:
        safe_payload = jsonable_encoder(payload)
        return f"data: {json.dumps(safe_payload, ensure_ascii=False)}\n\n"


ai_chat_service = AIChatService()
