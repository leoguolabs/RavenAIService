"""定期回收超过保留期的持久会话工作区。"""

import json
import logging
import os
import shutil
import stat
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import ChatAgentRun, ChatSession
from app.services.chat_run_service import RUN_STATUS_QUEUED, RUN_STATUS_RUNNING
from app.utils.chat_workspace import CHAT_WORKSPACE_REGISTRIES, workspace_lock

logger = logging.getLogger(__name__)
WORKSPACE_RETENTION = timedelta(days=30)
_DETACHED_PREFIX = ".expired-chat-workspace-"


def _is_workspace_id(name: str) -> bool:
    try:
        return str(UUID(name)) == name
    except ValueError:
        return False


def _workspace_path(value: object, root: Path) -> Path | None:
    """兼容现有登记和运行记录里的绝对路径、相对路径。"""
    if not isinstance(value, str):
        return None
    path = Path(value)
    if _is_workspace_id(path.name):
        try:
            if path.parent.resolve() == root:
                return root / path.name
        except (OSError, RuntimeError):
            pass
    return None


def _utc_mtime(info: os.stat_result) -> datetime:
    return datetime.fromtimestamp(info.st_mtime, timezone.utc).replace(tzinfo=None)


def _read_registry(path: Path, root: Path) -> tuple[Path, str, datetime] | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        workspace = _workspace_path(payload["temp_dir"], root)
        session_id = payload["session_id"]
        if workspace is None or not isinstance(session_id, str) or not session_id:
            return None

        last_activity = _utc_mtime(info)
        for key in ("created_at", "updated_at"):
            if value := payload.get(key):
                timestamp = datetime.fromisoformat(value)
                if timestamp.tzinfo is not None:
                    timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
                last_activity = max(last_activity, timestamp)
        return workspace, session_id, last_activity
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _older_directory(path: Path, cutoff: datetime) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and _utc_mtime(info) < cutoff


async def _session_is_in_use(
    db: AsyncSession, sessions: set[str], cutoff: datetime
) -> bool:
    active_run = (
        select(ChatAgentRun.id)
        .where(
            ChatAgentRun.session_id.in_(sessions),
            or_(
                ChatAgentRun.status.in_((RUN_STATUS_QUEUED, RUN_STATUS_RUNNING)),
                ChatAgentRun.updated_at >= cutoff,
            ),
        )
        .exists()
    )
    recent_message = (
        select(ChatSession.id)
        .where(ChatSession.id.in_(sessions), ChatSession.last_message_at >= cutoff)
        .exists()
    )
    in_use = await db.scalar(select(or_(active_run, recent_message)))
    # 释放读事务，避免在递归删除时占用数据库连接。
    await db.rollback()
    return bool(in_use)


def _remove_staged(workspace: Path, stats: dict[str, int]) -> None:
    try:
        shutil.rmtree(workspace)
    except OSError:
        logger.exception("删除已移出的会话工作区失败: %s", workspace.name)
        stats["failed"] += 1
    else:
        stats["deleted"] += 1


async def cleanup_idle_chat_workspaces(
    db: AsyncSession, workspace_root: str | Path
) -> dict[str, int]:
    """在维护任务自己的 DB 会话内回收工作区，保留会话登记和历史。"""
    root = Path(workspace_root).resolve()
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - WORKSPACE_RETENTION
    stats = {"deleted": 0, "skipped": 0, "failed": 0}
    if not root.is_dir():
        return stats

    candidates: set[Path] = set()
    for path in root.iterdir():
        if path.name.startswith(_DETACHED_PREFIX) and _is_workspace_id(
            path.name.removeprefix(_DETACHED_PREFIX)
        ):
            try:
                with workspace_lock(path, exclusive=True) as locked:
                    if locked:
                        _remove_staged(path, stats)
            except OSError:
                logger.exception("无法锁定已移出的会话工作区: %s", path.name)
                stats["failed"] += 1
        elif _is_workspace_id(path.name) and _older_directory(path, cutoff):
            candidates.add(path)
    if not candidates:
        return stats

    registries: dict[Path, list[Path]] = defaultdict(list)
    for name in CHAT_WORKSPACE_REGISTRIES.values():
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            record = _read_registry(path, root)
            if record and record[0] in candidates:
                registries[record[0]].append(path)

    # 旧会话切换项目或重建工作区后，运行历史仍能识别被遗留的目录。
    run_sessions: dict[Path, set[str]] = defaultdict(set)
    runs = await db.execute(
        select(ChatAgentRun.workspace_path, ChatAgentRun.session_id)
        .where(
            ChatAgentRun.workspace_path.is_not(None),
            ChatAgentRun.agent_kind.in_(tuple(CHAT_WORKSPACE_REGISTRIES)),
        )
        .distinct()
    )
    for value, session_id in runs:
        workspace = _workspace_path(value, root)
        if workspace in candidates:
            run_sessions[workspace].add(session_id)
    await db.rollback()

    for workspace in sorted(candidates):
        try:
            with workspace_lock(workspace, exclusive=True) as locked:
                if not locked or not _older_directory(workspace, cutoff):
                    stats["skipped"] += 1
                    continue
                sessions = run_sessions[workspace].copy()
                for path in registries[workspace]:
                    record = _read_registry(path, root)
                    if not record or record[0] != workspace or record[2] >= cutoff:
                        break
                    sessions.add(record[1])
                else:
                    if sessions and not await _session_is_in_use(db, sessions, cutoff):
                        destination = workspace.with_name(_DETACHED_PREFIX + workspace.name)
                        if not os.path.lexists(destination):
                            # 原子移走，后续请求按工作区已过期处理。
                            workspace.rename(destination)
                            _remove_staged(destination, stats)
                            continue
                stats["skipped"] += 1
        except OSError:
            logger.exception("回收会话工作区失败: %s", workspace.name)
            stats["failed"] += 1
    return stats
