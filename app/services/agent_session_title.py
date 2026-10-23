"""Generate optional Agent titles only after the answer transaction commits."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.user import ChatSession
from app.services.chat_history_service import chat_history_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingSessionTitle:
    """Plain values captured before commit; never retain a live ORM session."""

    user_id: str
    session_id: str
    user_content: str
    answer: str
    previous_title: str
    message_count: int
    updated_at: datetime
    locale: Optional[str] = None

    @classmethod
    def from_exchange(
        cls, session: ChatSession, user_content: str, answer: str,
        locale: Optional[str] = None,
    ) -> Optional[PendingSessionTitle]:
        if (session.message_count or 0) > 2:
            return None
        return cls(
            user_id=session.user_id, session_id=session.id,
            user_content=user_content, answer=answer,
            previous_title=session.title, message_count=session.message_count,
            updated_at=session.updated_at, locale=locale,
        )


async def generate_after_commit(
    pending: Optional[PendingSessionTitle],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Best effort, outside the writer lock also needed by title usage metrics.

    The caller must commit and close its answer transaction first. A separate
    conditional update preserves concurrent renames, deletion or new messages.
    Failure cannot roll back the already committed answer.
    """
    if pending is None:
        return
    try:
        from app.services.ai_chat_service import ai_chat_service

        title = await asyncio.wait_for(
            ai_chat_service.generate_session_title(
                pending.user_content, pending.answer,
                user_id=pending.user_id, session_id=pending.session_id,
                locale=pending.locale,
            ),
            timeout=8,
        )
        if not title:
            return
        async with session_factory() as db:
            await db.execute(
                update(ChatSession)
                .where(
                    ChatSession.id == pending.session_id,
                    ChatSession.user_id == pending.user_id,
                    ChatSession.is_deleted.is_(False),
                    ChatSession.title == pending.previous_title,
                    ChatSession.message_count == pending.message_count,
                    ChatSession.updated_at == pending.updated_at,
                )
                .values(title=chat_history_service._title_from_hint(
                    title, fallback=pending.previous_title,
                ))
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Agent session title skipped session_id=%s error_type=%s",
            pending.session_id, type(exc).__name__,
        )
