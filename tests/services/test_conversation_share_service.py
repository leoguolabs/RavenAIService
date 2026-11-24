"""Unit tests for conversation_share_service.

Cover the security-critical invariants: write-time redaction (only role /
content / created_at survive, no trace or owner identity), empty-session
rejection, token reuse on refresh (upsert), and immediate revocation.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.database import Base
from app.models.user import ChatAgentRun, ChatMessage, ChatSession, User
from app.services.conversation_share_service import conversation_share_service


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest_asyncio.fixture
async def user(session: AsyncSession) -> User:
    u = User(
        id=str(uuid.uuid4()),
        username=f"u-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        is_active=True,
        role="user",
    )
    session.add(u)
    await session.commit()
    return u


async def _seed_session_with_messages(
    session: AsyncSession,
    *,
    user_id: str,
    title: str = "排障对话",
    contents: tuple[tuple[str, str], ...] = (("user", "你好"), ("ai", "你好，有什么可以帮你")),
) -> str:
    sid = str(uuid.uuid4())
    session.add(
        ChatSession(
            id=sid,
            user_id=user_id,
            title=title,
            last_message_at=datetime.utcnow(),
            message_count=len(contents),
        )
    )
    for role, content in contents:
        session.add(ChatMessage(session_id=sid, role=role, content=content))
    await session.flush()
    return sid


@pytest.mark.asyncio
async def test_snapshot_redacts_to_three_fields(session: AsyncSession, user: User):
    sid = await _seed_session_with_messages(session, user_id=user.id)
    share = await conversation_share_service.create_or_refresh_share(
        session, session_id=sid, user_id=user.id
    )

    snapshot = await conversation_share_service.get_public_snapshot(
        session, token=share.token
    )
    assert snapshot is not None
    assert snapshot["title"] == "排障对话"
    assert snapshot["message_count"] == 2
    messages = snapshot["messages"]
    assert len(messages) == 2
    for message in messages:
        # With no agent run recorded, messages stay minimal: exactly the three
        # public fields — nothing else may leak (trace capture is covered below).
        assert set(message.keys()) == {"role", "content", "created_at"}
        assert "session_id" not in message
        assert "trace_events" not in message
        assert "run_id" not in message
        assert "user_id" not in message
    assert [m["role"] for m in messages] == ["user", "ai"]


@pytest.mark.asyncio
async def test_snapshot_omits_images_by_default(session: AsyncSession, user: User):
    """A public snapshot must not carry image metadata.

    The bytes are only served by authenticated endpoints, so shipping the
    metadata to a public page would render broken thumbnails and needlessly
    disclose the attachments' names and sizes.
    """
    sid = await _seed_session_with_messages(session, user_id=user.id)
    message = (
        await session.execute(
            ChatMessage.__table__.select().where(ChatMessage.session_id == sid)
        )
    ).first()
    await session.execute(
        ChatMessage.__table__.update()
        .where(ChatMessage.id == message.id)
        .values(images_json=json.dumps([{"id": "abc123", "media_type": "image/png"}]))
    )
    await session.commit()

    snapshot = await conversation_share_service.build_live_snapshot(
        session, session_id=sid, user_id=user.id
    )
    assert all("images" not in m for m in snapshot)


@pytest.mark.asyncio
async def test_snapshot_includes_images_when_opted_in(session: AsyncSession, user: User):
    """The admin audit read opts in and gets the user turn's attachments."""
    sid = await _seed_session_with_messages(session, user_id=user.id)
    images = [{"id": "abc123", "media_type": "image/png", "name": "err.png", "size": 42}]
    await session.execute(
        ChatMessage.__table__.update()
        .where(ChatMessage.session_id == sid, ChatMessage.role == "user")
        .values(images_json=json.dumps(images))
    )
    await session.commit()

    snapshot = await conversation_share_service.build_live_snapshot(
        session, session_id=sid, user_id=user.id, include_admin_details=True
    )
    user_turn = next(m for m in snapshot if m["role"] == "user")
    assert user_turn["images"] == images
    # AI turns never carry attachments.
    assert all("images" not in m for m in snapshot if m["role"] != "user")


@pytest.mark.asyncio
async def test_snapshot_tolerates_unusable_images_json(session: AsyncSession, user: User):
    """Bad stored JSON degrades to "no thumbnails", never a failed read."""
    sid = await _seed_session_with_messages(session, user_id=user.id)
    await session.execute(
        ChatMessage.__table__.update()
        .where(ChatMessage.session_id == sid, ChatMessage.role == "user")
        .values(images_json="{not json")
    )
    await session.commit()

    snapshot = await conversation_share_service.build_live_snapshot(
        session, session_id=sid, user_id=user.id, include_admin_details=True
    )
    assert len(snapshot) == 2
    assert all("images" not in m for m in snapshot)


@pytest.mark.asyncio
async def test_snapshot_captures_ai_trace_events(session: AsyncSession, user: User):
    """AI turns carry the agent trace (thinking + tool calls) at share time,
    matched to the run by answer text, while owner/run identity never leaks."""
    sid = await _seed_session_with_messages(session, user_id=user.id)
    events = [
        {"type": "thinking_end", "task_id": "t", "seq": 1, "timestamp": 1.0, "text": "先看堆栈"},
        {
            "type": "step_start",
            "task_id": "t",
            "seq": 2,
            "timestamp": 2.0,
            "step_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "grep -n ERROR app.log"},
        },
        {"type": "step_end", "task_id": "t", "seq": 3, "timestamp": 3.0, "step_id": "s1", "status": "ok"},
        {"type": "run_complete", "task_id": "t", "seq": 4, "timestamp": 4.0},
    ]
    session.add(
        ChatAgentRun(
            session_id=sid,
            user_id=user.id,
            agent_kind="log_analysis",
            status="succeeded",
            answer="你好，有什么可以帮你",  # equals the seeded ai message content
            trace_events_json=json.dumps(events, ensure_ascii=False),
        )
    )
    await session.flush()

    share = await conversation_share_service.create_or_refresh_share(
        session, session_id=sid, user_id=user.id
    )
    snapshot = await conversation_share_service.get_public_snapshot(
        session, token=share.token
    )
    assert snapshot is not None
    user_msg, ai_msg = snapshot["messages"]

    # Trace is AI-only; the user turn stays minimal.
    assert "trace_events" not in user_msg
    # AI turn carries the captured trace verbatim, but no run/session/owner ids.
    assert ai_msg["trace_events"] == events
    assert "run_id" not in ai_msg
    assert "session_id" not in ai_msg
    assert "user_id" not in ai_msg


@pytest.mark.asyncio
async def test_snapshot_ignores_run_without_matching_answer(
    session: AsyncSession, user: User
):
    """A run whose answer matches no message is never bound to a snapshot turn."""
    sid = await _seed_session_with_messages(session, user_id=user.id)
    session.add(
        ChatAgentRun(
            session_id=sid,
            user_id=user.id,
            agent_kind="device",
            status="succeeded",
            answer="一个对不上的答案",
            trace_events_json=json.dumps(
                [{"type": "run_complete", "task_id": "t", "seq": 1, "timestamp": 1.0}]
            ),
        )
    )
    await session.flush()

    share = await conversation_share_service.create_or_refresh_share(
        session, session_id=sid, user_id=user.id
    )
    snapshot = await conversation_share_service.get_public_snapshot(
        session, token=share.token
    )
    assert snapshot is not None
    for message in snapshot["messages"]:
        assert "trace_events" not in message


@pytest.mark.asyncio
async def test_empty_session_rejected(session: AsyncSession, user: User):
    sid = await _seed_session_with_messages(session, user_id=user.id, contents=())
    with pytest.raises(HTTPException) as excinfo:
        await conversation_share_service.create_or_refresh_share(
            session, session_id=sid, user_id=user.id
        )
    assert excinfo.value.status_code == 422


@pytest.mark.asyncio
async def test_non_owner_cannot_create_share(session: AsyncSession, user: User):
    sid = await _seed_session_with_messages(session, user_id=user.id)
    other = User(
        id=str(uuid.uuid4()),
        username=f"o-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        is_active=True,
        role="user",
    )
    session.add(other)
    await session.flush()

    with pytest.raises(HTTPException) as excinfo:
        await conversation_share_service.create_or_refresh_share(
            session, session_id=sid, user_id=other.id
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_refresh_reuses_token_and_refreshes_snapshot(
    session: AsyncSession, user: User
):
    sid = await _seed_session_with_messages(session, user_id=user.id)
    first = await conversation_share_service.create_or_refresh_share(
        session, session_id=sid, user_id=user.id
    )
    first_token = first.token
    first_shared_at = first.shared_at

    # Append another message, then refresh the share.
    session.add(ChatMessage(session_id=sid, role="user", content="再问一句"))
    await session.flush()

    second = await conversation_share_service.create_or_refresh_share(
        session, session_id=sid, user_id=user.id
    )
    assert second.token == first_token  # same token reused
    assert second.message_count == 3  # snapshot refreshed
    assert second.shared_at >= first_shared_at

    # Exactly one active share for the session.
    active = await conversation_share_service.get_share_for_session(
        session, session_id=sid, user_id=user.id
    )
    assert active is not None
    assert active.token == first_token


@pytest.mark.asyncio
async def test_revoke_makes_public_snapshot_unavailable(
    session: AsyncSession, user: User
):
    sid = await _seed_session_with_messages(session, user_id=user.id)
    share = await conversation_share_service.create_or_refresh_share(
        session, session_id=sid, user_id=user.id
    )
    token = share.token

    # Visible before revoke.
    assert await conversation_share_service.get_public_snapshot(session, token=token) is not None

    revoked = await conversation_share_service.revoke_share(
        session, session_id=sid, user_id=user.id
    )
    assert revoked is True

    # 404-equivalent after revoke.
    assert await conversation_share_service.get_public_snapshot(session, token=token) is None
    # get_share_for_session returns no active share.
    assert (
        await conversation_share_service.get_share_for_session(
            session, session_id=sid, user_id=user.id
        )
        is None
    )


@pytest.mark.asyncio
async def test_unknown_token_returns_none(session: AsyncSession, user: User):
    assert (
        await conversation_share_service.get_public_snapshot(session, token="does-not-exist")
        is None
    )
