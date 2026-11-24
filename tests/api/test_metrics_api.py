"""HTTP-level tests for the metrics APIs in ``app/api/admin_metrics.py``.

Covered tasks (openspec/changes/add-system-user-metrics/tasks.md):

- 7.5 admin API: ``/admin/metrics/overview`` aggregation, ``/admin/metrics/users``
  ranking + sorting, ``/admin/metrics/users/{user_id}`` detail, and
  ``/admin/metrics/events`` raw-event filtering, including time-bucket output.
- 7.6 self API: ``/api/v1/users/me/metrics`` returns ONLY the caller's own
  metrics — a user can never read or infer another user's usage.

These spin up a throwaway SQLite database wired into ``db_manager`` (so the real
aggregation queries run), seed users / sessions / metric events, and drive the
routers through a FastAPI ``TestClient`` with the bearer auth dependencies
overridden.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Import the models package so every table is registered on Base.metadata.
import app.models  # noqa: F401
from app.api import admin_metrics, users
from app.api.admin import require_admin
from app.api.users import get_current_user
from app.config import settings
from app.models.database import Base, db_manager, get_db
from app.models.metrics import MetricEvent
from app.models.user import ChatAgentRun, ChatMessage, ChatSession, User


# ==================== Fixtures / seeding ====================


@pytest.fixture
def metrics_db():
    """Point ``db_manager`` at a fresh temp SQLite file with all tables created."""
    fd, path = tempfile.mkstemp(prefix="metrics-api-db-", suffix=".sqlite")
    os.close(fd)

    prev_url = settings.database_url
    prev_engine = db_manager.engine
    prev_factory = db_manager.session_factory

    settings.database_url = f"sqlite+aiosqlite:///{path}"
    db_manager.initialize()

    async def _create() -> None:
        async with db_manager.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    try:
        yield
    finally:
        asyncio.run(db_manager.close())
        settings.database_url = prev_url
        db_manager.engine = prev_engine
        db_manager.session_factory = prev_factory
        try:
            os.remove(path)
        except OSError:
            pass


def _seed(objects: List[Any]) -> None:
    async def _run() -> None:
        async with db_manager.session_factory() as session:
            for obj in objects:
                session.add(obj)
            await session.commit()

    asyncio.run(_run())


def _make_user(username: str, role: str = "user") -> User:
    return User(
        id=str(uuid.uuid4()),
        username=username,
        display_name=username.title(),
        password_hash="x",
        is_active=True,
        role=role,
    )


def _ai_event(
    *,
    user_id: Optional[str],
    input_tokens: int = 0,
    output_tokens: int = 0,
    status: str = "succeeded",
    source: str = "general_agent",
    agent_kind: str = "general",
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    error_kind: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    metadata_json: Optional[str] = None,
    project_repo_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> MetricEvent:
    total = input_tokens + output_tokens
    return MetricEvent(
        id=str(uuid.uuid4()),
        idempotency_key=f"ai_usage:test:{uuid.uuid4()}",
        occurred_at=occurred_at or (datetime.utcnow() - timedelta(hours=1)),
        event_type="ai_usage",
        source=source,
        user_id=user_id,
        session_id=session_id,
        project_repo_id=project_repo_id,
        agent_kind=agent_kind,
        provider=provider,
        model=model,
        status=status,
        error_kind=error_kind,
        duration_ms=1234,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        metadata_json=metadata_json,
    )


@pytest.fixture
def auth_state() -> Dict[str, Any]:
    """Mutable holder for the user returned by the overridden ``get_current_user``."""
    return {"user": None}


@pytest.fixture
def app(metrics_db, auth_state) -> FastAPI:
    application = FastAPI()
    application.include_router(admin_metrics.admin_router)
    application.include_router(admin_metrics.self_router)

    # Admin bearer auth -> always pass as a fixed admin principal.
    application.dependency_overrides[require_admin] = lambda: "admin"
    # User bearer auth -> whoever the test placed in ``auth_state``.
    application.dependency_overrides[get_current_user] = lambda: auth_state["user"]

    async def _db():
        async with db_manager.session_factory() as session:
            yield session

    application.dependency_overrides[get_db] = _db
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ==================== 7.5 admin API ====================


def test_overview_aggregates_tokens_and_status(client: TestClient) -> None:
    user = _make_user("alice")
    _seed([user])
    _seed(
        [
            _ai_event(user_id=user.id, input_tokens=100, output_tokens=50),
            _ai_event(user_id=user.id, input_tokens=20, output_tokens=10),
            _ai_event(
                user_id=user.id,
                input_tokens=5,
                output_tokens=0,
                status="failed",
                error_kind="provider_error",
            ),
        ]
    )

    resp = client.get("/admin/metrics/overview")
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["tokens"]["total_tokens"] == 185
    assert data["tokens"]["input_tokens"] == 125
    assert data["tokens"]["output_tokens"] == 60
    assert data["invocation_count"] == 3
    assert data["status_counts"]["succeeded"] == 2
    assert data["status_counts"]["failed"] == 1
    assert data["error_count"] == 1
    # No pricing configured by default → cost is an estimate-less null.
    assert data["cost_estimated"] is False

    # Time bucket output is present and well-formed.
    assert data["bucket"] == "day"
    assert isinstance(data["time_series"], list)
    assert len(data["time_series"]) >= 1
    bucket = data["time_series"][0]
    assert bucket["total_tokens"] >= 1
    assert "bucket_start" in bucket
    assert isinstance(data["server_timezone"]["offset_minutes"], int)
    assert data["server_timezone"]["offset_label"].startswith("UTC")


def test_parse_datetime_normalizes_aware_values_to_utc() -> None:
    parsed = admin_metrics._parse_dt("2026-01-01T08:00:00+08:00", "from")
    assert parsed == datetime(2026, 1, 1, 0, 0, 0)


def test_overview_hour_bucket(client: TestClient) -> None:
    user = _make_user("bucketeer")
    _seed([user])
    _seed([_ai_event(user_id=user.id, input_tokens=10, output_tokens=5)])

    resp = client.get("/admin/metrics/overview", params={"bucket": "hour"})
    assert resp.status_code == 200
    assert resp.json()["data"]["bucket"] == "hour"


def test_overview_excludes_title_generator_from_user_request_rollups(
    client: TestClient,
) -> None:
    user = _make_user("requester")
    _seed([user])
    _seed(
        [
            _ai_event(user_id=user.id, input_tokens=10, output_tokens=5),
            _ai_event(
                user_id=user.id,
                input_tokens=1000,
                output_tokens=500,
                source="title_generator",
                agent_kind="title_generator",
            ),
        ]
    )

    resp = client.get("/admin/metrics/overview")
    assert resp.status_code == 200
    overview = resp.json()["data"]

    assert overview["invocation_count"] == 1
    assert overview["tokens"]["total_tokens"] == 15
    assert {g["key"] for g in overview["invocations_by_source"]} == {"general_agent"}

    # The raw feed also excludes paired internal title-generation activity.
    resp = client.get("/admin/metrics/events")
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 1
    assert resp.json()["data"]["events"][0]["source"] == "general_agent"

    # An explicit filter must not bypass the exclusion.
    resp = client.get(
        "/admin/metrics/events", params={"source": "title_generator"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 0


def test_project_filter_scopes_admin_metrics(client: TestClient) -> None:
    alpha_user = _make_user("alpha")
    beta_user = _make_user("beta")
    _seed([alpha_user, beta_user])
    _seed(
        [
            _ai_event(
                user_id=alpha_user.id,
                input_tokens=100,
                output_tokens=50,
                project_repo_id="1",
            ),
            _ai_event(
                user_id=alpha_user.id,
                input_tokens=10,
                output_tokens=5,
                project_repo_id="2",
            ),
            _ai_event(
                user_id=beta_user.id,
                input_tokens=20,
                output_tokens=10,
                project_repo_id="2",
            ),
        ]
    )

    resp = client.get("/admin/metrics/overview")
    assert resp.status_code == 200
    overview = resp.json()["data"]
    by_project = {g["key"]: g for g in overview["invocations_by_project"]}
    assert by_project["1"]["total_tokens"] == 150
    assert by_project["2"]["total_tokens"] == 45

    resp = client.get("/admin/metrics/overview", params={"project_repo_id": 1})
    assert resp.status_code == 200
    scoped = resp.json()["data"]
    assert scoped["tokens"]["total_tokens"] == 150
    assert scoped["invocation_count"] == 1

    resp = client.get(
        "/admin/metrics/users",
        params={"project_repo_id": 1, "sort": "total_tokens"},
    )
    assert resp.status_code == 200
    users = resp.json()["data"]["rows"]
    assert [row["user_id"] for row in users] == [alpha_user.id]
    assert users[0]["total_tokens"] == 150

    resp = client.get(
        f"/admin/metrics/users/{alpha_user.id}",
        params={"project_repo_id": 1},
    )
    assert resp.status_code == 200
    detail = resp.json()["data"]
    assert detail["tokens"]["total_tokens"] == 150
    assert len(detail["recent_events"]) == 1

    resp = client.get("/admin/metrics/events", params={"project_repo_id": 1})
    assert resp.status_code == 200
    events = resp.json()["data"]["events"]
    assert len(events) == 1
    assert events[0]["project_repo_id"] == "1"


def test_overview_requires_admin(app: FastAPI) -> None:
    """Without the admin override, the bearer dependency rejects the call."""
    app.dependency_overrides.pop(require_admin, None)
    unauth = TestClient(app)
    resp = unauth.get("/admin/metrics/overview")
    assert resp.status_code == 401


def test_users_list_sorting_and_rows(client: TestClient) -> None:
    heavy = _make_user("heavy")
    light = _make_user("light")
    _seed([heavy, light])
    _seed(
        [
            _ai_event(user_id=heavy.id, input_tokens=1000, output_tokens=500),
            _ai_event(user_id=light.id, input_tokens=10, output_tokens=5),
        ]
    )

    resp = client.get("/admin/metrics/users", params={"sort": "total_tokens"})
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["sort"] == "total_tokens"
    assert data["total"] == 2
    rows = data["rows"]
    assert len(rows) == 2
    # Highest token user ranks first.
    assert rows[0]["user_id"] == heavy.id
    assert rows[0]["username"] == "heavy"
    assert rows[0]["total_tokens"] == 1500
    assert rows[1]["user_id"] == light.id
    assert rows[0]["total_tokens"] > rows[1]["total_tokens"]


def test_users_list_pagination(client: TestClient) -> None:
    users = [_make_user(f"u{i}") for i in range(3)]
    _seed(users)
    _seed(
        [
            _ai_event(user_id=u.id, input_tokens=(i + 1) * 100)
            for i, u in enumerate(users)
        ]
    )

    resp = client.get("/admin/metrics/users", params={"page": 1, "per_page": 2})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 3
    assert data["per_page"] == 2
    assert len(data["rows"]) == 2


def test_user_detail_series_and_events(client: TestClient) -> None:
    user = _make_user("detail")
    _seed([user])
    _seed(
        [
            _ai_event(user_id=user.id, input_tokens=100, output_tokens=50),
            _ai_event(
                user_id=user.id,
                input_tokens=0,
                output_tokens=0,
                status="failed",
                error_kind="timeout",
            ),
        ]
    )

    resp = client.get(f"/admin/metrics/users/{user.id}")
    assert resp.status_code == 200
    detail = resp.json()["data"]

    assert detail["user_id"] == user.id
    assert detail["username"] == "detail"
    assert detail["tokens"]["total_tokens"] == 150
    assert detail["invocation_count"] == 2
    assert detail["status_counts"]["succeeded"] == 1
    assert detail["status_counts"]["failed"] == 1
    assert len(detail["time_series"]) >= 1
    assert len(detail["recent_events"]) == 2
    # Errors-by-kind surfaces the low-cardinality error grouping.
    kinds = {g["key"] for g in detail["errors_by_kind"]}
    assert "timeout" in kinds


def test_raw_events_filtering(client: TestClient) -> None:
    user = _make_user("auditee")
    _seed([user])
    _seed(
        [
            _ai_event(user_id=user.id, source="general_agent", input_tokens=10),
            _ai_event(user_id=user.id, source="device_agent", input_tokens=20),
        ]
    )

    # No filter → both events.
    resp = client.get("/admin/metrics/events")
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 2

    # Filter by source → only the matching event.
    resp = client.get("/admin/metrics/events", params={"source": "device_agent"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["events"][0]["source"] == "device_agent"


def _log_upload_event(*, user_id: Optional[str]) -> MetricEvent:
    """A log-upload business event (not an AI/agent invocation)."""
    return MetricEvent(
        id=str(uuid.uuid4()),
        idempotency_key=f"log_activity:upload:{uuid.uuid4()}",
        occurred_at=datetime.utcnow() - timedelta(hours=1),
        event_type="log_activity",
        source="log_upload",
        user_id=user_id,
        status="pending",
    )


def test_raw_events_excludes_log_upload(client: TestClient) -> None:
    """Log uploads are not invocations → excluded from the raw-event audit feed."""
    user = _make_user("uploader")
    _seed([user])
    _seed(
        [
            _ai_event(user_id=user.id, source="general_agent", input_tokens=10),
            _log_upload_event(user_id=user.id),
            _log_upload_event(user_id=user.id),
        ]
    )

    resp = client.get("/admin/metrics/events")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # Only the AI invocation survives; both log_upload rows are filtered out.
    assert data["total"] == 1
    assert all(ev["source"] != "log_upload" for ev in data["events"])

    # Even an explicit source filter cannot surface log_upload rows.
    resp = client.get("/admin/metrics/events", params={"source": "log_upload"})
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 0


def test_raw_events_include_triggering_user(client: TestClient) -> None:
    """Each raw event is enriched with the triggering user's name."""
    user = _make_user("triggerer")
    _seed([user])
    _seed([_ai_event(user_id=user.id, source="general_agent", input_tokens=10)])

    resp = client.get("/admin/metrics/events")
    assert resp.status_code == 200
    event = resp.json()["data"]["events"][0]
    assert event["user_id"] == user.id
    assert event["username"] == "triggerer"
    assert event["display_name"] == "Triggerer"


def test_raw_events_mark_linked_conversation_available(client: TestClient) -> None:
    user = _make_user("viewer")
    chat_session = ChatSession(
        id=str(uuid.uuid4()),
        user_id=user.id,
        title="排障会话",
        last_message_at=datetime.utcnow(),
        message_count=1,
        is_deleted=False,
    )
    event = _ai_event(
        user_id=user.id,
        session_id=chat_session.id,
        input_tokens=10,
    )
    no_chat_event = _ai_event(user_id=user.id, input_tokens=5)
    _seed([user, chat_session])
    _seed(
        [
            ChatMessage(
                id=str(uuid.uuid4()),
                session_id=chat_session.id,
                role="user",
                content="为什么服务超时？",
            ),
            event,
            no_chat_event,
        ]
    )

    resp = client.get("/admin/metrics/events")
    assert resp.status_code == 200
    events = {item["id"]: item for item in resp.json()["data"]["events"]}
    assert events[event.id]["conversation_available"] is True
    assert events[no_chat_event.id]["conversation_available"] is False


def test_admin_reads_complete_event_conversation_with_trace(
    client: TestClient,
) -> None:
    user = _make_user("conversation-owner")
    chat_session = ChatSession(
        id=str(uuid.uuid4()),
        user_id=user.id,
        title="数据库连接排障",
        last_message_at=datetime.utcnow(),
        message_count=2,
        is_deleted=True,
    )
    event = _ai_event(
        user_id=user.id,
        session_id=chat_session.id,
        input_tokens=25,
        output_tokens=10,
    )
    answer = "连接池已耗尽，请检查未释放的连接。"
    _seed([user, chat_session])
    _seed(
        [
            ChatMessage(
                id=str(uuid.uuid4()),
                session_id=chat_session.id,
                role="user",
                content="数据库为什么连不上？",
            ),
            ChatMessage(
                id=str(uuid.uuid4()),
                session_id=chat_session.id,
                role="ai",
                content=answer,
            ),
            ChatAgentRun(
                id=str(uuid.uuid4()),
                session_id=chat_session.id,
                user_id=user.id,
                owner_scope=f"user:{user.id}",
                agent_kind="general",
                status="succeeded",
                user_message="数据库为什么连不上？",
                answer=answer,
                trace_events_json=json.dumps(
                    [{"type": "thinking", "content": "检查连接池状态"}],
                    ensure_ascii=False,
                ),
                finished_at=datetime.utcnow(),
            ),
            event,
        ]
    )

    resp = client.get(f"/admin/metrics/events/{event.id}/conversation")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["event_id"] == event.id
    assert data["session_id"] == chat_session.id
    assert data["user_id"] == user.id
    assert data["username"] == "conversation-owner"
    assert data["title"] == "数据库连接排障"
    assert data["message_count"] == 2
    assert data["is_deleted"] is True
    assert [message["role"] for message in data["messages"]] == ["user", "ai"]
    assert data["messages"][0]["content"] == "数据库为什么连不上？"
    assert data["messages"][1]["content"] == answer
    assert data["messages"][1]["trace_events"] == [
        {"type": "thinking", "content": "检查连接池状态"}
    ]


def test_admin_event_conversation_rejects_unlinked_event(
    client: TestClient,
) -> None:
    user = _make_user("no-session")
    event = _ai_event(user_id=user.id, input_tokens=1)
    _seed([user, event])

    resp = client.get(f"/admin/metrics/events/{event.id}/conversation")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "该事件未关联对话会话"


def test_event_conversation_marks_its_reply_and_preserves_each_runs_model(
    client: TestClient, app: FastAPI, auth_state: Dict[str, Any],
) -> None:
    app.include_router(users.router)
    user = _make_user("model-history-owner")
    auth_state["user"] = user
    chat_session = ChatSession(id=str(uuid.uuid4()), user_id=user.id, title="模型切换")
    _seed([user, chat_session])
    runs, messages = [], []
    now = datetime.utcnow()
    for index, model in enumerate((
        "deepseek-flash", "raven-openai/gpt-6.2-sol", "raven-yinhe/yinhe-thinking",
    )):
        started_at = now + timedelta(minutes=index)
        finished_at = started_at + timedelta(seconds=2)
        answer = "重复的回复" if index < 2 else "随后的回复"
        runs.append(ChatAgentRun(
            id=str(uuid.uuid4()), session_id=chat_session.id, user_id=user.id,
            agent_kind="project_expert", status="succeeded", answer=answer,
            model=model, started_at=started_at, finished_at=finished_at,
        ))
        messages.append(ChatMessage(
            session_id=chat_session.id, role="ai", content=answer, created_at=finished_at,
        ))
    event = _ai_event(user_id=user.id, session_id=chat_session.id, model=runs[1].model)
    event.run_id = runs[1].id
    _seed([*runs, *messages, event])

    admin_response = client.get(f"/admin/metrics/events/{event.id}/conversation")
    assert admin_response.status_code == 200
    audited = admin_response.json()["data"]["messages"]
    assert [message.get("is_event_reply") for message in audited] == [None, True, None]
    assert [message["run_model"] for message in audited] == [run.model for run in runs]
    assert all("run_id" not in message for message in audited)

    owner_response = client.get(f"/api/v1/users/chat-sessions/{chat_session.id}/messages")
    assert owner_response.status_code == 200
    history = owner_response.json()["data"]
    assert [message["run_id"] for message in history] == [run.id for run in runs]
    assert [message["run_model"] for message in history] == [run.model for run in runs]
    assert all(not message["trace_events"] for message in history)


def test_event_conversation_requires_admin(app: FastAPI) -> None:
    app.dependency_overrides.pop(require_admin, None)
    unauth = TestClient(app)
    resp = unauth.get(
        f"/admin/metrics/events/{uuid.uuid4()}/conversation"
    )
    assert resp.status_code == 401


def test_raw_events_invalid_datetime_is_400(client: TestClient) -> None:
    resp = client.get("/admin/metrics/events", params={"from": "not-a-date"})
    assert resp.status_code == 400


# ==================== 7.6 self API ====================


def test_self_metrics_only_returns_own_usage(client: TestClient, auth_state) -> None:
    me = _make_user("me")
    other = _make_user("other")
    _seed([me, other])
    _seed(
        [
            _ai_event(user_id=me.id, input_tokens=100, output_tokens=50),
            # Another user's much larger usage must never leak into my summary.
            _ai_event(user_id=other.id, input_tokens=9000, output_tokens=9000),
        ]
    )

    auth_state["user"] = me
    resp = client.get("/api/v1/users/me/metrics")
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["user_id"] == me.id
    # Only my 150 tokens — none of the other user's 18000.
    assert data["tokens"]["total_tokens"] == 150
    assert data["invocation_count"] == 1


def test_self_metrics_switches_with_caller(client: TestClient, auth_state) -> None:
    """The same endpoint scoped to a different caller returns that caller's data."""
    me = _make_user("first")
    other = _make_user("second")
    _seed([me, other])
    _seed(
        [
            _ai_event(user_id=me.id, input_tokens=100, output_tokens=0),
            _ai_event(user_id=other.id, input_tokens=7, output_tokens=0),
        ]
    )

    auth_state["user"] = other
    resp = client.get("/api/v1/users/me/metrics")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["user_id"] == other.id
    assert data["tokens"]["total_tokens"] == 7
    assert data["invocation_count"] == 1


def test_self_metrics_user_with_no_events(client: TestClient, auth_state) -> None:
    lonely = _make_user("lonely")
    busy = _make_user("busy")
    _seed([lonely, busy])
    _seed([_ai_event(user_id=busy.id, input_tokens=500)])

    auth_state["user"] = lonely
    resp = client.get("/api/v1/users/me/metrics")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["user_id"] == lonely.id
    assert data["tokens"]["total_tokens"] == 0
    assert data["invocation_count"] == 0


@pytest.fixture
def follow_up_source(client, tmp_path, monkeypatch):
    from app.agents.device_agent import workspace as workspace_module

    base = tmp_path / "workspaces"
    workspace = base / "original"
    workspace.mkdir(parents=True)
    (workspace / "evidence.txt").write_text(
        "original workspace evidence", encoding="utf-8"
    )
    monkeypatch.setattr(workspace_module, "_resolve_base_dir", lambda: base)
    user = _make_user("temporary-review-owner")
    session = ChatSession(
        id=str(uuid.uuid4()),
        user_id=user.id,
        title="Original conversation",
        last_message_at=datetime.utcnow(),
        message_count=2,
    )
    run = ChatAgentRun(
        id=str(uuid.uuid4()),
        session_id=session.id,
        user_id=user.id,
        owner_scope=f"user:{user.id}",
        agent_kind="project_expert",
        status="succeeded",
        user_message="Original question",
        answer="Original answer",
        workspace_path=str(workspace),
    )
    event = _ai_event(user_id=user.id, session_id=session.id)
    event.run_id = run.id
    _seed([user, session])
    _seed(
        [
            run,
            event,
            ChatMessage(
                session_id=session.id, role="user", content="Original question"
            ),
            ChatMessage(session_id=session.id, role="ai", content="Original answer"),
        ]
    )
    return event, run, workspace


def test_admin_follow_up_is_transient_and_reuses_original_workspace(
    client, follow_up_source, monkeypatch
):
    from app.services import admin_follow_up_service as service
    from sqlalchemy import select

    event, run, workspace = follow_up_source
    seen = []

    async def database_snapshot():
        async with db_manager.session_factory() as db:
            return {
                table.name: [
                    dict(row) for row in (await db.execute(select(table))).mappings()
                ]
                for table in Base.metadata.sorted_tables
            }

    before = asyncio.run(database_snapshot())

    async def fake_stream(context, payload):
        seen.append((context, payload))
        assert (
            service.WorkspaceReader(context.workspace).read("evidence.txt")
            == "original workspace evidence"
        )
        yield {"type": "delta", "text": "Temporary answer"}
        yield {"type": "done", "answer": "Temporary answer"}

    monkeypatch.setattr(service, "stream_follow_up", fake_stream)
    url = f"/admin/metrics/events/{event.id}/conversation/follow-up"
    first = client.post(url, json={"question": "Review this"})
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert '"type": "done"' in first.text
    history = [
        {"role": "user", "content": "Review this"},
        {"role": "assistant", "content": "Temporary answer"},
    ]
    assert (
        client.post(url, json={"question": "Why?", "history": history}).status_code
        == 200
    )
    assert seen[1][1].history[1].content == "Temporary answer"
    assert all(context.workspace == workspace for context, _ in seen)
    assert seen[0][0].transcript == [
        {"role": "user", "content": "Original question"},
        {"role": "ai", "content": "Original answer"},
    ]
    assert asyncio.run(database_snapshot()) == before
    assert list(workspace.iterdir()) == [workspace / "evidence.txt"]
    assert (workspace / "evidence.txt").read_text() == "original workspace evidence"


@pytest.mark.parametrize(
    "problem", ["missing", "outside", "wrong_owner", "wrong_run", "unlinked"]
)
def test_follow_up_rejects_invalid_workspace(
    client, follow_up_source, monkeypatch, tmp_path, problem
):
    from app.services import admin_follow_up_service as service

    event, run, workspace = follow_up_source

    async def alter():
        async with db_manager.session_factory() as db:
            row = await db.get(ChatAgentRun, run.id)
            if problem == "missing":
                row.workspace_path = str(workspace / "cleaned")
            elif problem == "outside":
                row.workspace_path = str(tmp_path)
            elif problem == "wrong_owner":
                row.user_id = "another-user"
            elif problem == "wrong_run":
                (await db.get(MetricEvent, event.id)).run_id = str(uuid.uuid4())
            else:
                (await db.get(MetricEvent, event.id)).session_id = None
            await db.commit()

    asyncio.run(alter())

    async def never(*args):
        pytest.fail("AI must not run without a valid source workspace")
        yield

    monkeypatch.setattr(service, "stream_follow_up", never)
    response = client.post(
        f"/admin/metrics/events/{event.id}/conversation/follow-up",
        json={"question": "Review"},
    )
    assert response.status_code == (404 if problem == "unlinked" else 409)


def test_follow_up_requires_admin_and_validates_input(app, client, follow_up_source):
    event, _, _ = follow_up_source
    url = f"/admin/metrics/events/{event.id}/conversation/follow-up"
    for body in [
        {"question": " "},
        {"question": "x" * 8001},
        {"question": "x", "history": [{"role": "assistant", "content": "orphan"}]},
        {"question": "x", "history": [{"role": "user", "content": "x" * 32001}]},
    ]:
        assert client.post(url, json=body).status_code == 422
    app.dependency_overrides.pop(require_admin)
    assert client.post(url, json={"question": "Review"}).status_code in (401, 403)


def test_follow_up_errors_do_not_leak_content(client, follow_up_source, monkeypatch):
    from app.services import admin_follow_up_service as service

    event, _, _ = follow_up_source

    async def failed(*args):
        raise RuntimeError("SECRET_PROMPT_AND_CREDENTIAL")
        yield

    monkeypatch.setattr(service, "stream_follow_up", failed)
    response = client.post(
        f"/admin/metrics/events/{event.id}/conversation/follow-up",
        json={"question": "Review"},
    )
    assert '"type":"error"' in response.text
    assert "SECRET_PROMPT_AND_CREDENTIAL" not in response.text


def test_raw_events_exclude_package_download_before_pagination(client):
    user = _make_user("download-event-owner")
    upload = _log_upload_event(user_id=user.id)
    upload.source = "package_upload"
    upload.event_type = "package_activity"
    download = _log_upload_event(user_id=user.id)
    download.source = "package_download"
    download.event_type = "package_activity"
    _seed([user, upload, download, _ai_event(user_id=user.id)])
    response = client.get("/admin/metrics/events?per_page=1").json()["data"]
    assert response["total"] == 2
    second = client.get("/admin/metrics/events?per_page=1&page=2").json()["data"]
    assert {response["events"][0]["source"], second["events"][0]["source"]} == {
        "package_upload",
        "general_agent",
    }
    assert (
        client.get("/admin/metrics/events?source=package_download").json()["data"][
            "total"
        ]
        == 0
    )

    async def still_stored():
        async with db_manager.session_factory() as db:
            return await db.get(MetricEvent, download.id) is not None

    assert asyncio.run(still_stored())
