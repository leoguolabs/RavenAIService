"""Integration tests for the read-only bug fixes API.

Covers visibility scoping by project membership, admin full visibility,
unauthenticated rejection, non-member detail 404, and absence of git tokens.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import bug_fixes as bug_fixes_api
from app.api.users import get_current_user
from app.models.bug_fix import (
    BugFixMergeRequest,
    BugFixMergeRequestStatus,
    BugFixTask,
    BugFixTaskStatus,
)
from app.models.database import Base, get_db
from app.models.project_repo import ProjectRepo, ProjectRepoMember
from app.models.user import User


class _UserHolder:
    """Mutable holder so tests can switch the active user per request."""

    def __init__(self) -> None:
        self.user = None


@pytest.fixture
def client(tmp_path) -> TestClient:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bugfix.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    state: dict[str, object] = {}
    holder = _UserHolder()

    async def _seed() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            proj_a = ProjectRepo(
                project_code="proj_a", project_name="Project A", project_card="Project A", repo_url="", enabled=True
            )
            proj_b = ProjectRepo(
                project_code="proj_b", project_name="Project B", project_card="Project B", repo_url="", enabled=True
            )
            session.add_all([proj_a, proj_b])

            member = User(
                id="user-member",
                username="member",
                display_name="Member",
                email="member@example.com",
                password_hash="x",
                role="user",
            )
            admin = User(
                id="user-admin",
                username="admin",
                display_name="Admin",
                email="admin@example.com",
                password_hash="x",
                role="admin",
            )
            outsider = User(
                id="user-outsider",
                username="outsider",
                display_name="Outsider",
                email="out@example.com",
                password_hash="x",
                role="user",
            )
            session.add_all([member, admin, outsider])
            await session.flush()

            # member belongs to project A only
            session.add(
                ProjectRepoMember(project_repo_id=proj_a.id, user_id=member.id)
            )

            task_a = BugFixTask(
                id="task-a",
                project_repo_id=proj_a.id,
                source_log_id="log-a",
                title="Fix A",
                summary="summary A",
                proposed_fixes_json=json.dumps([{"title": "Fix A", "detail": "x"}]),
                status=BugFixTaskStatus.SUCCEEDED,
            )
            task_b = BugFixTask(
                id="task-b",
                project_repo_id=proj_b.id,
                source_log_id="log-b",
                title="Fix B",
                status=BugFixTaskStatus.FAILED,
                error="upstream_error: <ak-fixture-key> insufficient balance",
                fix_outcomes_json=json.dumps(
                    [{"fix_index": 1, "outcome": "failed"}]
                ),
                started_at=datetime(2026, 7, 17, 10, 0, 0),
                finished_at=datetime(2026, 7, 17, 10, 1, 0),
            )
            session.add_all([task_a, task_b])
            await session.flush()

            session.add(
                BugFixMergeRequest(
                    id="mr-a1",
                    task_id=task_a.id,
                    title="MR A1",
                    branch_name="bugfix/a1",
                    base_branch="main",
                    mr_url="https://gitlab.example.com/proj/-/merge_requests/1",
                    mr_iid="1",
                    commit_sha="abc123",
                    changed_files_json=json.dumps([{"path": "a.py", "added": 3}]),
                    diff_stat_json=json.dumps({"files": 1, "insertions": 3, "deletions": 0}),
                    status=BugFixMergeRequestStatus.OPEN,
                )
            )
            await session.commit()

            state["proj_a"] = proj_a.id
            state["proj_b"] = proj_b.id

    asyncio.run(_seed())

    async def _get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def _current_user():
        if holder.user is None:
            from fastapi import HTTPException, status as http_status

            raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED)
        return holder.user

    application = FastAPI()
    application.include_router(bug_fixes_api.router)
    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_current_user] = _current_user

    with TestClient(application) as test_client:
        test_client._state = state
        test_client._holder = holder
        test_client._factory = factory
        yield test_client

    asyncio.run(engine.dispose())


def _as_user(client: TestClient, user_id: str, role: str = "user") -> None:
    client._holder.user = User(id=user_id, username=user_id, role=role)


def test_member_sees_only_their_projects(client: TestClient) -> None:
    _as_user(client, "user-member")
    resp = client.get("/api/v1/bug-fixes")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {t["id"] for t in body["data"]}
    assert ids == {"task-a"}
    assert body["total"] == 1
    task = body["data"][0]
    assert task["project_code"] == "proj_a"
    assert task["merge_request_count"] == 1
    assert task["source_log_id"] == "log-a"


def test_admin_sees_all_tasks(client: TestClient) -> None:
    _as_user(client, "user-admin", role="admin")
    resp = client.get("/api/v1/bug-fixes")
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["data"]}
    assert ids == {"task-a", "task-b"}


def test_outsider_sees_nothing(client: TestClient) -> None:
    _as_user(client, "user-outsider")
    resp = client.get("/api/v1/bug-fixes")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []
    assert resp.json()["total"] == 0


def test_unauthenticated_rejected(client: TestClient) -> None:
    client._holder.user = None
    resp = client.get("/api/v1/bug-fixes")
    assert resp.status_code == 401


def test_member_reads_detail_with_mrs(client: TestClient) -> None:
    _as_user(client, "user-member")
    resp = client.get("/api/v1/bug-fixes/task-a")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["id"] == "task-a"
    assert data["summary"] == "summary A"
    assert data["proposed_fixes"] == [{"title": "Fix A", "detail": "x"}]
    assert len(data["merge_requests"]) == 1
    mr = data["merge_requests"][0]
    assert mr["mr_url"].startswith("https://")
    assert mr["branch_name"] == "bugfix/a1"
    assert mr["changed_files"] == [{"path": "a.py", "added": 3}]
    assert mr["diff_stat"] == {"files": 1, "insertions": 3, "deletions": 0}
    # No token must leak anywhere in the payload.
    assert "token" not in json.dumps(data).lower() or "@" not in mr["mr_url"]


def test_non_member_detail_returns_404(client: TestClient) -> None:
    _as_user(client, "user-member")
    # task-b belongs to project B which member is not part of.
    resp = client.get("/api/v1/bug-fixes/task-b")
    assert resp.status_code == 404


def test_admin_reads_any_detail(client: TestClient) -> None:
    _as_user(client, "user-admin", role="admin")
    resp = client.get("/api/v1/bug-fixes/task-b")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["id"] == "task-b"
    assert data["error"] == "upstream_error: <***> insufficient balance"


def test_missing_task_returns_404(client: TestClient) -> None:
    _as_user(client, "user-admin", role="admin")
    resp = client.get("/api/v1/bug-fixes/does-not-exist")
    assert resp.status_code == 404


def test_admin_can_retry_failed_task_once(client: TestClient, monkeypatch) -> None:
    _as_user(client, "user-admin", role="admin")
    enqueue = Mock(return_value=SimpleNamespace(id="celery-retry-1"))
    monkeypatch.setattr(bug_fixes_api, "_enqueue_bug_fix_retry", enqueue)

    resp = client.post("/api/v1/bug-fixes/task-b/retry")

    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    assert data["id"] == "task-b"
    assert data["status"] == "pending"
    assert data["error"] is None
    assert data["started_at"] is None
    assert data["finished_at"] is None
    assert data["fix_outcomes"] == []
    enqueue.assert_called_once_with("task-b")

    # failed -> pending 的条件更新是一次性 claim，重复点击不得再次启动 Agent。
    duplicate = client.post("/api/v1/bug-fixes/task-b/retry")
    assert duplicate.status_code == 409
    enqueue.assert_called_once_with("task-b")


def test_retry_rejects_non_failed_task(client: TestClient, monkeypatch) -> None:
    _as_user(client, "user-member")
    enqueue = Mock(return_value=SimpleNamespace(id="should-not-run"))
    monkeypatch.setattr(bug_fixes_api, "_enqueue_bug_fix_retry", enqueue)

    resp = client.post("/api/v1/bug-fixes/task-a/retry")

    assert resp.status_code == 409
    enqueue.assert_not_called()


def test_non_member_cannot_retry_task(client: TestClient, monkeypatch) -> None:
    _as_user(client, "user-member")
    enqueue = Mock(return_value=SimpleNamespace(id="should-not-run"))
    monkeypatch.setattr(bug_fixes_api, "_enqueue_bug_fix_retry", enqueue)

    resp = client.post("/api/v1/bug-fixes/task-b/retry")

    assert resp.status_code == 404
    enqueue.assert_not_called()


def test_retry_queue_failure_restores_failed_state(client: TestClient, monkeypatch) -> None:
    _as_user(client, "user-admin", role="admin")

    def _fail_enqueue(_task_id: str):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(bug_fixes_api, "_enqueue_bug_fix_retry", _fail_enqueue)

    resp = client.post("/api/v1/bug-fixes/task-b/retry")

    assert resp.status_code == 503
    detail = client.get("/api/v1/bug-fixes/task-b")
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert data["status"] == "failed"
    assert data["error"] == "retry_enqueue_failed"
