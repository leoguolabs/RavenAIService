"""
Persistence helpers for user chat history.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
import json
import uuid
from typing import List, Optional, Sequence

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatMessage as ChatMessageSchema
from app.models.user import (
    ChatAgentRun,
    ChatMessage,
    ChatSession,
    ChatSessionSearchData,
    ChatSessionSearchResult,
)
from app.services import chat_image_store
from app.services.base import BaseService


class ChatHistoryService(BaseService):
    """Store and load chat history for authenticated users."""

    @staticmethod
    def _title_from_hint(hint: Optional[str], fallback: str = "新对话") -> str:
        if not hint:
            return fallback
        cleaned = " ".join(hint.strip().split())
        return cleaned[:60] if cleaned else fallback

    async def _get_session(
        self,
        db: AsyncSession,
        user_id: str,
        session_id: str,
        include_deleted: bool = False,
    ) -> Optional[ChatSession]:
        stmt = select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user_id)
        if not include_deleted:
            stmt = stmt.where(ChatSession.is_deleted == False)  # noqa: E712
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def ensure_session(
        self,
        db: AsyncSession,
        user_id: str,
        *,
        session_id: Optional[str] = None,
    ) -> ChatSession:
        session = None
        if session_id:
            session = await self._get_session(db, user_id, session_id, include_deleted=True)
        if not session:
            session = ChatSession(
                id=session_id or str(uuid.uuid4()),
                user_id=user_id,
                title="新对话",
                last_message_at=datetime.utcnow(),
                message_count=0,
                is_deleted=False,
            )
            db.add(session)
            await db.flush()
            await db.refresh(session)
            return session

        # revive soft-deleted sessions on new activity
        if session.is_deleted:
            session.is_deleted = False
        return session

    async def ensure_session_summary(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        session_id: str,
        title_hint: Optional[str] = None,
    ) -> ChatSession:
        """Ensure a session row exists before a long-running agent finishes.

        External chat agents persist the full user/assistant exchange at
        terminal time. Creating the lightweight session row at run start lets
        authenticated clients show the user-triggered conversation in history
        immediately, without duplicating the eventual user message.
        """
        session = await self.ensure_session(db, user_id, session_id=session_id)
        if (
            (session.message_count or 0) == 0
            and title_hint
            and (not session.title or session.title.strip() == "新对话")
        ):
            session.title = self._title_from_hint(title_hint, fallback=session.title)
        if (session.message_count or 0) == 0:
            session.last_message_at = datetime.utcnow()
        await db.flush()
        await db.refresh(session)
        return session

    async def append_message(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        session_id: Optional[str],
        role: str,
        content: str,
        title_hint: Optional[str] = None,
        images_json: Optional[str] = None,
    ) -> tuple[ChatSession, ChatMessage]:
        """Append a single message to a session, creating the session if needed.

        Used by the chat-run flow which persists the user message at run start
        and the assistant message at run terminal, instead of writing both at
        once. Maintains `message_count` / `last_message_at` and revives any
        soft-deleted session.

        ``images_json`` carries the metadata for images attached to a user turn
        (bytes live on disk, see ``chat_image_store``) so history reloads can
        re-render the thumbnails.
        """
        session = await self.ensure_session(db, user_id, session_id=session_id)
        if (
            (session.message_count or 0) == 0
            and role == "user"
            and (not session.title or session.title.strip() == "新对话")
        ):
            preferred_title = title_hint or content
            if preferred_title:
                session.title = self._title_from_hint(preferred_title, fallback=session.title)

        record = ChatMessage(
            session_id=session.id, role=role, content=content, images_json=images_json
        )
        db.add(record)
        session.message_count = (session.message_count or 0) + 1
        session.last_message_at = datetime.utcnow()
        await db.flush()
        await db.refresh(session)
        await db.refresh(record)
        return session, record

    async def save_exchange(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        session_id: str,
        user_content: str,
        ai_content: str,
        title_hint: Optional[str] = None,
        session_title: Optional[str] = None,
        user_images_json: Optional[str] = None,
    ) -> ChatSession:
        session, _ = await self.append_message(
            db,
            user_id=user_id,
            session_id=session_id,
            role="user",
            content=user_content,
            title_hint=session_title or title_hint,
            images_json=user_images_json,
        )
        session, _ = await self.append_message(
            db,
            user_id=user_id,
            session_id=session.id,
            role="ai",
            content=ai_content,
        )
        return session

    async def list_sessions(self, db: AsyncSession, user_id: str) -> List[ChatSession]:
        result = await db.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user_id, ChatSession.is_deleted == False)  # noqa: E712
            .order_by(
                ChatSession.is_pinned.desc(),
                ChatSession.pinned_at.desc(),
                ChatSession.last_message_at.desc(),
                ChatSession.created_at.desc(),
            )
        )
        return result.scalars().all()

    @staticmethod
    def _search_snippet(content: str, query: str, max_length: int = 280) -> str:
        # 在命中附近截取原文，保留字面匹配；只压平展示中的空白。
        position = content.lower().find(query.lower()) if query else 0
        start = max(0, position - 40)
        end = min(len(content), start + max_length)
        excerpt = " ".join(content[start:end].split())
        return ("…" if start else "") + excerpt + ("…" if end < len(content) else "")

    async def search_sessions(
        self,
        db: AsyncSession,
        user_id: str,
        *,
        query: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> ChatSessionSearchData:
        """检索当前用户的标题和可见正文，每个会话只返回一条摘要。"""
        query = query.strip()
        message_filter = [
            ChatMessage.session_id == ChatSession.id,
            ChatMessage.role.in_(("user", "ai", "assistant")),
        ]
        if query:
            # autoescape 防止 %、_ 等用户输入被解释为 SQL 通配符。
            message_filter.append(ChatMessage.content.icontains(query, autoescape=True))
        matched_message = (
            select(ChatMessage.id)
            .where(*message_filter)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
            .correlate(ChatSession)
            .scalar_subquery()
        )
        stmt = (
            select(ChatSession, ChatMessage.content)
            .outerjoin(ChatMessage, ChatMessage.id == matched_message)
            .where(ChatSession.user_id == user_id, ChatSession.is_deleted.is_(False))
        )
        if query:
            stmt = stmt.where(or_(
                ChatSession.title.icontains(query, autoescape=True),
                ChatMessage.id.is_not(None),
            ))
        rows = (await db.execute(
            stmt.order_by(ChatSession.last_message_at.desc(), ChatSession.id.desc())
            .offset(offset).limit(limit + 1)
        )).all()
        items = []
        for session, content in rows[:limit]:
            item = ChatSessionSearchResult.model_validate(session)
            item.snippet = self._search_snippet(content or "", query)
            items.append(item)
        return ChatSessionSearchData(items=items, has_more=len(rows) > limit)

    async def fetch_messages(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[ChatMessage]:
        session = await self._get_session(db, user_id, session_id)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="会话不存在或已删除",
            )
        stmt = select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        return result.scalars().all()

    async def match_assistant_runs(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        session_id: str,
        records: Sequence[ChatMessage],
    ) -> tuple[dict[str, ChatAgentRun], list[ChatAgentRun]]:
        """Match stored replies to same-session runs, consuming repeated answers in order."""
        result = await db.execute(
            select(ChatAgentRun)
            .where(ChatAgentRun.user_id == user_id, ChatAgentRun.session_id == session_id)
            .order_by(ChatAgentRun.finished_at.asc(), ChatAgentRun.started_at.asc(), ChatAgentRun.id.asc())
        )
        runs = list(result.scalars().all())
        by_answer: dict[str, deque[ChatAgentRun]] = defaultdict(deque)
        for run in runs:
            if run.answer:
                by_answer[run.answer].append(run)

        matches = {
            record.id: by_answer[record.content].popleft()
            for record in records
            if record.role == "ai" and by_answer.get(record.content)
        }
        matched_ids = {run.id for run in matches.values()}
        return matches, [run for run in runs if run.id not in matched_ids]

    @staticmethod
    def parse_run_trace(run: ChatAgentRun) -> list[dict] | None:
        try:
            trace = json.loads(run.trace_events_json or "[]")
        except (TypeError, ValueError):
            return None
        return trace if isinstance(trace, list) else None

    async def delete_session(self, db: AsyncSession, *, user_id: str, session_id: str) -> bool:
        session = await self._get_session(db, user_id, session_id, include_deleted=True)
        if not session:
            return False
        session.is_deleted = True
        await db.flush()
        # Attached-image bytes follow the conversation: drop them with the
        # session so retention needs no separate sweeper. Best-effort — a
        # failure here must not fail the delete.
        chat_image_store.delete_session_images(session_id)
        return True

    async def update_session_title(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        session_id: str,
        title: str,
    ) -> bool:
        session = await self._get_session(db, user_id, session_id, include_deleted=False)
        if not session:
            return False
        session.title = self._title_from_hint(title, fallback=session.title)
        await db.flush()
        return True

    async def set_session_pinned(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        session_id: str,
        pinned: bool,
    ) -> bool:
        session = await self._get_session(db, user_id, session_id, include_deleted=False)
        if not session:
            return False
        session.is_pinned = pinned
        session.pinned_at = datetime.utcnow() if pinned else None
        await db.flush()
        return True

    @staticmethod
    def to_chat_messages(records: Sequence[ChatMessage]) -> List[ChatMessageSchema]:
        """Convert DB records to ChatMessage schema objects."""
        messages: list[ChatMessageSchema] = []
        for record in records:
            role = record.role if record.role in {"user", "ai", "system"} else "user"
            messages.append(ChatMessageSchema(role=role, content=record.content))
        return messages


chat_history_service = ChatHistoryService()
