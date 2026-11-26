import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.celery_app import celery_app
from app.config import settings
from app.models.user import ChatAgentRun, ChatSession, User
from app.services import chat_workspace_cleanup
from app.services.package_search_chat_service import PackageSearchChatService
from app.services.project_expert_chat_service import ProjectExpertChatService
from app.tasks.cleanup_tasks import cleanup_idle_chat_workspaces as celery_cleanup
from app.utils.chat_workspace import CHAT_WORKSPACE_REGISTRIES, workspace_lock

pytestmark = pytest.mark.skipif(os.name != "posix", reason="workspace locks require POSIX")


@pytest.fixture
async def database(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            for model in (User, ChatSession, ChatAgentRun):
                await connection.run_sync(model.__table__.create)
        async with async_sessionmaker(engine)() as db:
            yield db, url
    finally:
        await engine.dispose()


@pytest.fixture
def dates():
    now = datetime.now(timezone.utc)
    return now - timedelta(days=31), now - timedelta(days=1)


def _set_mtime(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def _workspace(root: Path, when: datetime) -> Path:
    workspace = root / str(uuid4())
    (workspace / "logs").mkdir(parents=True)
    (workspace / "logs" / "trace.log").write_text("uploaded evidence")
    (workspace / "repo").mkdir()
    (workspace / "repo" / "code.txt").write_text("working copy")
    (workspace / "task.json").write_text("{}")
    _set_mtime(workspace, when)
    return workspace


def _registry(workspace, session, kind, mtime, *, updated=None, relative=False):
    registry = workspace.parent / CHAT_WORKSPACE_REGISTRIES[kind]
    registry.mkdir(exist_ok=True)
    path = registry / f"{session}.json"
    path.write_text(json.dumps({
        "session_id": session,
        "temp_dir": os.path.relpath(workspace) if relative else str(workspace),
        "repo_dir": str(workspace / "repo"),
        "task_json_path": str(workspace / "task.json"),
        "updated_at": (updated or mtime).isoformat(),
    }))
    _set_mtime(path, mtime)
    return path


def _run(workspace, session, when, *, status="succeeded", kind="log_analysis"):
    return ChatAgentRun(
        session_id=session,
        agent_kind=kind,
        workspace_path=str(workspace),
        status=status,
        updated_at=when.replace(tzinfo=None),
    )


async def test_daily_task_removes_registered_and_legacy_workspaces(database, dates, tmp_path, monkeypatch):
    db, url = database
    old, _ = dates
    root = tmp_path / "temp" / "code_repos"
    expired = [_workspace(root, old) for _ in CHAT_WORKSPACE_REGISTRIES]
    registry_files = [
        _registry(path, kind, kind, old, relative=kind == "log_analysis")
        for path, kind in zip(expired, CHAT_WORKSPACE_REGISTRIES)
    ]
    legacy = _workspace(root, old)
    db.add(_run(legacy, "legacy-session", old))
    await db.commit()

    unrelated = _workspace(root, old)
    source_upload = root.parent / "original-upload.log"
    source_upload.write_text("original")
    link = root / str(uuid4())
    link.symlink_to(unrelated, target_is_directory=True)
    _registry(link, "linked-session", "log_analysis", old)

    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "code_repo_clone_base_dir", str(root))
    project_chat = ProjectExpertChatService()
    package_chat = PackageSearchChatService()
    assert project_chat.session_has_workspace("project_expert")
    assert package_chat.session_has_workspace("package_search")
    entry = celery_app.conf.beat_schedule["cleanup-idle-chat-workspaces-daily"]
    assert entry["task"] == celery_cleanup.name
    assert entry["schedule"].hour == {4}
    assert entry["schedule"].minute == {0}
    assert celery_app.amqp.router.route({}, celery_cleanup.name)["queue"].name == "maintenance"

    result = await asyncio.to_thread(celery_cleanup.run)

    assert result == {"status": "success", "stats": {"deleted": 4, "skipped": 1, "failed": 0}}
    assert all(not path.exists() for path in [*expired, legacy])
    assert all(path.exists() for path in registry_files)
    assert not project_chat.session_has_workspace("project_expert")
    assert not package_chat.session_has_workspace("package_search")
    assert unrelated.is_dir()
    assert link.is_symlink()
    assert source_upload.read_text() == "original"
    assert await db.scalar(select(ChatAgentRun.session_id)) == "legacy-session"


async def test_recent_queued_running_and_leased_workspaces_are_kept(database, dates, tmp_path):
    db, _ = database
    old, recent = dates
    root = tmp_path / "code_repos"
    protected = {}
    for session_id in ("recent-registry", "recent-chat", "recent-run", "queued", "running", "leased"):
        workspace = _workspace(root, old)
        _registry(
            workspace,
            session_id,
            "log_analysis",
            old,
            updated=recent if session_id == "recent-registry" else old,
        )
        protected[session_id] = workspace
    user = User(username="workspace-retention", password_hash="test")
    db.add(user)
    await db.flush()
    db.add(ChatSession(id="recent-chat", user_id=user.id, last_message_at=recent.replace(tzinfo=None)))
    db.add(_run(protected["recent-run"], "recent-run", recent))
    for status in ("queued", "running"):
        db.add(_run(protected[status], status, old, status=status))
    await db.commit()
    fresh = _workspace(root, recent)
    _registry(fresh, "fresh", "log_analysis", old)

    leased = protected["leased"]
    with workspace_lock(leased) as locked:
        assert locked
        assert leased.stat().st_mtime > recent.timestamp()
        # 模拟仍持锁且已运行超过保留期的会话。
        _set_mtime(leased, old)
        stats = await chat_workspace_cleanup.cleanup_idle_chat_workspaces(db, root)
        assert stats == {"deleted": 0, "skipped": 6, "failed": 0}
        assert all(path.is_dir() for path in protected.values())
        assert fresh.is_dir()

    stats = await chat_workspace_cleanup.cleanup_idle_chat_workspaces(db, root)
    assert stats == {"deleted": 1, "skipped": 5, "failed": 0}
    assert not leased.exists()


async def test_partial_removal_is_retried_after_workspace_is_detached(database, dates, tmp_path, monkeypatch):
    db, _ = database
    old, recent = dates
    root = tmp_path / "code_repos"
    expired = _workspace(root, old)
    _registry(expired, "same-session", "package_search", old)
    remove = chat_workspace_cleanup.shutil.rmtree

    def fail_once(path):
        monkeypatch.setattr(chat_workspace_cleanup.shutil, "rmtree", remove)
        raise PermissionError("simulated interrupted removal")

    monkeypatch.setattr(chat_workspace_cleanup.shutil, "rmtree", fail_once)

    stats = await chat_workspace_cleanup.cleanup_idle_chat_workspaces(db, root)
    assert stats == {"deleted": 0, "skipped": 0, "failed": 1}
    assert not expired.exists()
    detached = list(root.glob(".expired-chat-workspace-*"))
    assert len(detached) == 1

    replacement = _workspace(root, recent)
    _registry(replacement, "same-session", "package_search", recent)
    stats = await chat_workspace_cleanup.cleanup_idle_chat_workspaces(db, root)

    assert stats == {"deleted": 1, "skipped": 0, "failed": 0}
    assert not detached[0].exists()
    assert (replacement / "repo" / "code.txt").exists()
