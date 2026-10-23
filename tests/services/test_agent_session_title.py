"""Real SQLite regressions for post-answer title generation and concurrent edits."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Base, db_manager
from app.models.metrics import MetricEvent
from app.models.user import ChatMessage, ChatSession, User
from app.services.ai_chat_service import ai_chat_service
from app.services.agent_session_title import PendingSessionTitle, generate_after_commit
from app.services.chat_history_service import chat_history_service
from app.services.log_analysis_chat_service import LogAnalysisChatService
from app.services.project_expert_chat_service import ProjectExpertChatService
from app.services.title_generator_service import _record_title_usage


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    # A file database and two connections reproduce SQLite's single-writer lock.
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'titles.db'}", connect_args={"timeout": .1},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_manager, "session_factory", factory)
    async with factory() as db:
        db.add(User(id="owner", username="owner", password_hash="unused", is_active=True))
        await db.commit()
    yield factory
    await engine.dispose()


@pytest.mark.parametrize("service_type", [LogAnalysisChatService, ProjectExpertChatService])
async def test_title_metrics_can_write_and_answer_is_already_durable(database, monkeypatch, service_type):
    calls = []

    async def title(user_content, answer, **kwargs):
        # A fresh reader must see the complete exchange before title work starts.
        async with database() as db:
            messages = (await db.execute(select(ChatMessage).order_by(ChatMessage.created_at))).scalars().all()
            assert [m.role for m in messages] == ["user", "ai"]
            assert messages[-1].content == answer == "unchanged answer"
        await _record_title_usage(
            model="test", provider="anthropic", token_usage={"input_tokens": 3, "output_tokens": 1},
            duration_seconds=.01, status="succeeded", error_kind=None,
            user_id=kwargs["user_id"], session_id=kwargs["session_id"],
        )
        calls.append((user_content, kwargs))
        return "Generated title"

    monkeypatch.setattr(ai_chat_service, "generate_session_title", title)
    service = object.__new__(service_type)  # Persistence needs no workspace registry.
    job = SimpleNamespace(
        session_id="chat", user_id="owner", remember=True, question="original question",
        context_meta={"project_code": "demo"}, images_json=None, filename="evidence.log", locale="en",
    )
    await service._persist_job_result(job, {"status": "ok", "answer": "unchanged answer"}, "unchanged answer")
    assert len(calls) == 1
    assert calls[0][1]["locale"] == ("en" if service_type is LogAnalysisChatService else None)
    async with database() as db:
        chat = await db.get(ChatSession, "chat")
        assert chat.title == "Generated title"
        assert chat.message_count == 2
        assert await db.scalar(select(func.count()).select_from(MetricEvent)) == 1
    # A follow-up saves its answer but must not generate another initial title.
    await service._persist_job_result(job, {}, "follow-up answer")
    assert len(calls) == 1
    async with database() as db:
        assert (await db.get(ChatSession, "chat")).message_count == 4


@pytest.mark.parametrize("service_type", [LogAnalysisChatService, ProjectExpertChatService])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.TimeoutError, asyncio.CancelledError])
async def test_title_failure_cannot_remove_saved_answer(database, monkeypatch, service_type, error):
    async def fail(*args, **kwargs):
        raise error()

    monkeypatch.setattr(ai_chat_service, "generate_session_title", fail)
    service = object.__new__(service_type)
    job = SimpleNamespace(
        session_id="chat", user_id="owner", remember=True, question="question", context_meta={},
        images_json=None, filename=None, locale="zh",
    )
    if error is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await service._persist_job_result(job, {}, "durable answer")
    else:
        await service._persist_job_result(job, {}, "durable answer")
    async with database() as db:
        assert (await db.get(ChatSession, "chat")).title == "question"
        messages = (await db.execute(select(ChatMessage))).scalars().all()
        assert sorted(m.content for m in messages) == ["durable answer", "question"]


@pytest.mark.parametrize("change", ["rename", "delete", "new_message", "owner", "rename_back"])
async def test_concurrent_session_changes_are_not_overwritten(database, monkeypatch, change):
    async with database() as db:
        chat = await chat_history_service.save_exchange(
            db, user_id="owner", session_id="chat", user_content="question", ai_content="answer",
        )
        pending = PendingSessionTitle.from_exchange(chat, "question", "answer")
        await db.commit()

    async def title(*args, **kwargs):
        async with database() as db:
            chat = await db.get(ChatSession, "chat")
            if change == "rename":
                chat.title = "User chosen title"
            elif change == "delete":
                chat.is_deleted = True
            elif change == "new_message":
                await chat_history_service.append_message(
                    db, user_id="owner", session_id="chat", role="user", content="next question",
                )
            elif change == "owner":
                db.add(User(id="other", username="other", password_hash="unused", is_active=True))
                await db.flush()
                chat.user_id = "other"
            else:
                chat.title = "Temporary title"
                await db.commit()
                chat.title = "question"
            await db.commit()
        return "Stale generated title"

    monkeypatch.setattr(ai_chat_service, "generate_session_title", title)
    await generate_after_commit(pending, database)
    async with database() as db:
        assert (await db.get(ChatSession, "chat")).title != "Stale generated title"


@pytest.mark.parametrize("service_type", [LogAnalysisChatService, ProjectExpertChatService])
async def test_failed_exchange_does_not_start_title_generation(database, monkeypatch, service_type):
    service = object.__new__(service_type)
    calls = []

    async def fail_exchange(**kwargs):
        await chat_history_service.save_exchange(
            kwargs["db"], user_id="owner", session_id="chat", user_content="question", ai_content="answer",
        )
        raise RuntimeError("rollback exchange")

    async def title(*args, **kwargs):
        calls.append(True)

    monkeypatch.setattr(service, "_persist_exchange", fail_exchange)
    monkeypatch.setattr(ai_chat_service, "generate_session_title", title)
    job = SimpleNamespace(
        session_id="chat", user_id="owner", remember=True, question="question", context_meta={},
        images_json=None, filename=None, locale="zh",
    )
    await service._persist_job_result(job, {}, "answer")
    assert not calls
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 0
