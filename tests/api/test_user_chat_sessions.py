from __future__ import annotations

import asyncio
from datetime import datetime
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import users as users_api
from app.models.database import Base, get_db
from app.models.user import ChatAgentRun, ChatSession


@pytest.fixture
def client_with_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat-sessions.db'}")

    async def _create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_schema())
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    application = FastAPI()
    application.include_router(users_api.router)
    application.dependency_overrides[get_db] = _get_db

    with TestClient(application) as test_client:
        yield test_client, factory

    asyncio.run(engine.dispose())


def _register(client: TestClient) -> tuple[str, str]:
    resp = client.post(
        "/api/v1/users/auth/register",
        json={
            "username": "history_user",
            "password": "secret123",
            "email": "history_user@example.test",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    return data["token"], data["user"]["id"]


def test_chat_session_summary_includes_latest_agent_kind(client_with_db) -> None:
    client, factory = client_with_db
    token, user_id = _register(client)
    session_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    async def _seed() -> None:
        async with factory() as db:
            db.add(
                ChatSession(
                    id=session_id,
                    user_id=user_id,
                    title="项目专家历史",
                    last_message_at=datetime.utcnow(),
                    message_count=2,
                )
            )
            db.add(
                ChatAgentRun(
                    id=run_id,
                    session_id=session_id,
                    user_id=user_id,
                    owner_scope=f"user:{user_id}",
                    agent_kind="project_expert",
                    status="succeeded",
                    user_message="解释鉴权流程",
                    answer="鉴权流程如下",
                    started_at=datetime.utcnow(),
                    finished_at=datetime.utcnow(),
                )
            )
            await db.commit()

    asyncio.run(_seed())

    resp = client.get(
        "/api/v1/users/chat-sessions",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert rows[0]["id"] == session_id
    assert rows[0]["run_agent_kind"] == "project_expert"
    assert rows[0]["run_status"] == "succeeded"
    assert rows[0]["active_run_id"] is None


@pytest.mark.parametrize("active_overlay", [False, True])
def test_sidebar_keeps_history_payloads_unloaded(client_with_db, monkeypatch, active_overlay) -> None:
    from datetime import timedelta
    from types import SimpleNamespace
    import time

    from sqlalchemy import event, inspect
    from app.models.user import User
    from app.services.chat_run_service import chat_run_service

    client, factory = client_with_db
    token, user_id = _register(client)
    now = datetime.utcnow()

    async def seed():
        async with factory() as db:
            db.add(User(id="other", username="other", password_hash="unused"))
            db.add_all([
                ChatSession(id="visible", user_id=user_id, title="历史分析", message_count=4),
                ChatSession(id="deleted", user_id=user_id, title="已删除", is_deleted=True),
            ])
            await db.flush()
            for rid, sid, owner, kind, age in [
                ("older", "visible", user_id, "log_analysis", 2),
                ("latest", "visible", user_id, "project_expert", 1),
                ("foreign", "visible", "other", "device", 0),
                ("hidden", "deleted", user_id, "device", 0),
            ]:
                db.add(ChatAgentRun(
                    id=rid, session_id=sid, user_id=owner, owner_scope=f"user:{owner}",
                    agent_kind=kind, status="succeeded", user_message="question",
                    answer="完整答案" * 1000, request_json='{"private":"request"}',
                    trace_events_json='[{"text":"' + 'evidence' * 262144 + '"}]',
                    started_at=now - timedelta(minutes=age), finished_at=now,
                ))
            await db.commit()

    asyncio.run(seed())
    loaded = []

    def on_load(run, _context):
        loaded.append((run.id, inspect(run).unloaded.copy()))

    def overlay(owner, sid):
        assert owner == f"user:{user_id}" and sid == "visible"
        if active_overlay:
            return SimpleNamespace(run_id="live", status="running", agent_kind="log_analysis",
                                   started_at=time.monotonic() - 5, updated_at=time.monotonic())
        return None

    monkeypatch.setattr(chat_run_service, "get_active_job_for_session", overlay)
    event.listen(ChatAgentRun, "load", on_load)
    try:
        response = client.get("/api/v1/users/chat-sessions", headers={"Authorization": f"Bearer {token}"})
    finally:
        event.remove(ChatAgentRun, "load", on_load)

    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert len(rows) == 1 and rows[0]["id"] == "visible"
    assert rows[0]["run_agent_kind"] == ("log_analysis" if active_overlay else "project_expert")
    assert rows[0]["run_status"] == ("running" if active_overlay else "succeeded")
    assert rows[0]["active_run_id"] == ("live" if active_overlay else None)
    assert {rid for rid, _ in loaded} == {"older", "latest"}
    assert all({"trace_events_json", "answer", "request_json", "user_message"} <= fields
               for _, fields in loaded)


def test_search_sessions_matches_content_and_enforces_privacy(client_with_db) -> None:
    from datetime import timedelta
    from app.models.user import ChatMessage, User

    client, factory = client_with_db
    token, user_id = _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    async def seed():
        async with factory() as db:
            db.add(User(id="other", username="other", password_hash="unused"))
            for sid, title, owner, deleted, days in [
                ("title", "部署 RavenAI Docker", user_id, False, 0),
                ("content", "旧故障排查", user_id, False, 10),
                ("deleted", "Docker deleted", user_id, True, 0),
                ("foreign", "Docker private", "other", False, 0),
                ("system", "内部消息", user_id, False, 0),
                ("literal", "100%_path\\name", user_id, False, 2),
            ]:
                db.add(ChatSession(id=sid, user_id=owner, title=title, is_deleted=deleted,
                                   last_message_at=datetime.utcnow() - timedelta(days=days)))
            await db.flush()
            for sid, role, text in [
                ("content", "user", "分析网络"),
                ("content", "ai", "前文" * 300 + "DoCkEr 网络故障已定位" + "后文" * 300),
                ("content", "assistant", "第二条 docker 命中"),
                ("foreign", "ai", "docker secret"),
                ("deleted", "ai", "docker deleted"),
                ("system", "system", "docker internal"),
            ]:
                db.add(ChatMessage(session_id=sid, role=role, content=text))
            await db.commit()
    asyncio.run(seed())
    path = "/api/v1/users/chat-sessions/search"
    assert client.get(path).status_code == 401
    data = client.get(path, params={"q": "  DOCKER  "}, headers=headers).json()["data"]
    assert [r["id"] for r in data["items"]] == ["title", "content"]
    assert "docker" in data["items"][1]["snippet"].lower()
    assert len(data["items"][1]["snippet"]) <= 282
    assert not data["has_more"]
    user_match = client.get(path, params={"q": "分析网络"}, headers=headers).json()["data"]["items"]
    assert [r["id"] for r in user_match] == ["content"]
    first = client.get(path, params={"q": "docker", "limit": 1}, headers=headers).json()["data"]
    second = client.get(path, params={"q": "docker", "limit": 1, "offset": 1}, headers=headers).json()["data"]
    assert first["has_more"] and not second["has_more"]
    assert first["items"][0]["id"] == "title"
    assert second["items"][0]["id"] == "content"
    for term in ["%", "_", "\\", "100%_path\\name"]:
        matches = client.get(path, params={"q": term}, headers=headers).json()["data"]["items"]
        assert [r["id"] for r in matches] == ["literal"]
    assert client.get(path, params={"q": "找不到"}, headers=headers).json()["data"]["items"] == []
    recent = client.get(path, params={"q": "  "}, headers=headers).json()["data"]["items"]
    assert len(recent) == 4
    for params in [{"q": "x" * 201}, {"limit": 51}, {"offset": -1}]:
        assert client.get(path, params=params, headers=headers).status_code == 422


def test_search_long_chinese_excerpt_and_stable_order(client_with_db) -> None:
    from app.models.user import ChatMessage
    client, factory = client_with_db
    token, user_id = _register(client)

    async def seed():
        async with factory() as db:
            for sid in ["a", "b", "c"]:
                db.add(ChatSession(id=sid, user_id=user_id, title="故障记录", last_message_at=datetime(2020, 1, 1)))
                db.add(ChatMessage(session_id=sid, role="ai", content="背景" * 500 + "遥测检索关键字" + "后文" * 500))
            await db.commit()
    asyncio.run(seed())
    rows = []
    for offset in range(3):
        response = client.get("/api/v1/users/chat-sessions/search", params={"q": "遥测检索关键字", "limit": 1, "offset": offset}, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        rows.extend(response.json()["data"]["items"])
    assert [r["id"] for r in rows] == ["c", "b", "a"]
    assert all("遥测检索关键字" in r["snippet"] and len(r["snippet"]) <= 282 for r in rows)
