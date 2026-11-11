"""
Bug Fix Coding Agent 工作区准备与清理。

职责：
- 为每个 Bug 修复任务在隔离临时目录下浅克隆项目仓库（复用 ``build_clone_url``
  注入 token，token 仅用于 clone 命令，不写入 task.json）
- 写入 task.json（仅含非敏感字段：任务总结、proposed_fixes、来源、默认分支、git 身份）
- 任务结束后幂等清理临时目录

工作区布局对齐 log_analysis：``temp_dir/`` 下含 ``repo/``（克隆出的源码）、
``task.json``，以及（当来源日志归档可用时）``logs/`` —— 用触发本次修复的
原始日志归档重建，内容与日志分析 Agent 工作区的 ``logs/`` 一致。Agent 的
cwd 设为 ``temp_dir``，git 操作在 ``repo/`` 中进行。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.agents.log_analysis.mcp_tools import build_clone_url
from app.agents.log_analysis.trace import mask_tokens
from app.config import settings

logger = logging.getLogger(__name__)


class BugFixWorkspaceError(Exception):
    """Bug 修复工作区相关错误基类。"""


class CloneFailedError(BugFixWorkspaceError):
    """git clone 失败。"""


@dataclass
class BugFixWorkspaceContext:
    task_id: str            # bug_fix_task.id（非 celery id）
    temp_dir: str           # 绝对路径，如 /base/<uuid>/
    repo_dir: str           # temp_dir/repo/
    task_json_path: str     # temp_dir/task.json
    default_branch: str
    logs_dir: Optional[str] = None  # temp_dir/logs/；来源日志同步成功时非 None
    metadata: Dict[str, Any] = field(default_factory=dict)
    repo_url: str = field(default="", repr=False)


def _git_identity() -> Dict[str, str]:
    """提交身份。固定为系统账号，便于在 MR/提交历史中识别自动修复来源。"""
    return {"name": "RavenAI Bug Fix Agent", "email": "bot@ravenai.local"}


def _sync_source_logs(
    temp_dir: Path,
    *,
    bug_fix_task_id: str,
    source_log_archive_path: Optional[str],
    source_log_filename: Optional[str],
) -> Optional[Path]:
    """用来源日志归档在工作区重建 ``logs/``（与日志分析工作区内容一致）。

    日志分析的临时工作区在分析结束后即被清理，且 Bug Fix 任务在独立队列
    异步执行，因此这里从持久化的原始归档重新解压，而非拷贝分析工作区。

    Best-effort：日志只是修复的辅助上下文，任何失败（归档缺失、解压超限、
    格式不支持）只记 warning，不阻断修复流程。
    """
    if not source_log_archive_path:
        return None
    archive = Path(source_log_archive_path)
    if not archive.exists():
        logger.warning(
            "Bug fix workspace: source log archive missing, skip logs sync: "
            "task=%s archive=%s",
            bug_fix_task_id, archive,
        )
        return None

    from app.agents.log_analysis.workspace import populate_logs_dir

    logs_dir = temp_dir / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        populate_logs_dir(
            archive, logs_dir, preferred_name=source_log_filename or "",
        )
        logger.info(
            "Bug fix workspace: source logs synced: task=%s logs_dir=%s",
            bug_fix_task_id, logs_dir,
        )
        return logs_dir
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Bug fix workspace: failed to sync source logs (non-fatal): "
            "task=%s error=%s",
            bug_fix_task_id, mask_tokens(str(exc)),
        )
        shutil.rmtree(str(logs_dir), ignore_errors=True)
        return None


def prepare(
    *,
    bug_fix_task_id: str,
    repo_url: str,
    default_branch: str,
    git_token: Optional[str],
    title: str,
    summary: Optional[str],
    proposed_fixes: List[Dict[str, Any]],
    source_log_id: Optional[str] = None,
    source_analysis_task_id: Optional[str] = None,
    source_log_archive_path: Optional[str] = None,
    source_log_filename: Optional[str] = None,
    source_context: Optional[Dict[str, Any]] = None,
) -> BugFixWorkspaceContext:
    """准备隔离工作区并浅克隆仓库。

    ``source_log_archive_path`` 提供时，会把触发本次修复的原始日志重建到
    ``temp_dir/logs/``（与日志分析 Agent 工作区一致），供修复 Agent 交叉
    验证根因；同步失败不阻断任务。

    Raises:
        CloneFailedError: git clone 失败（错误信息已脱敏）
    """
    workspace_id = str(uuid.uuid4())
    base_dir = Path(settings.code_repo_clone_base_dir).resolve()
    temp_dir = base_dir / workspace_id
    repo_dir = temp_dir / "repo"

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)

        clone_url = build_clone_url(repo_url, git_token or None)
        # 浅克隆默认分支以加速；--single-branch 限制只取默认分支。
        cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--branch",
            default_branch,
            clone_url,
            str(repo_dir),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            # 退化：某些仓库默认分支名不确定时，去掉 --branch 再试一次。
            fallback = [
                "git",
                "clone",
                "--depth",
                "1",
                clone_url,
                str(repo_dir),
            ]
            proc = subprocess.run(
                fallback, capture_output=True, text=True, timeout=600
            )
            if proc.returncode != 0:
                raise CloneFailedError(
                    f"git clone failed: {mask_tokens(proc.stderr or proc.stdout or '')}"
                )

        # Clone fallback may select the remote's real default branch.
        branch_result = subprocess.run(
            ["git", "-C", str(repo_dir), "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if branch_result.returncode == 0 and branch_result.stdout.strip():
            default_branch = branch_result.stdout.strip()

        # 配置提交身份（仅在该 clone 内生效）。
        identity = _git_identity()
        for key, value in (("user.name", identity["name"]), ("user.email", identity["email"])):
            subprocess.run(
                ["git", "-C", str(repo_dir), "config", key, value],
                capture_output=True,
                text=True,
                timeout=30,
            )

        context_metadata = {}
        if source_context:
            from app.services.bug_fix_context import restore
            try:
                context_metadata = restore(source_context, temp_dir)
            except (OSError, ValueError, KeyError) as exc:
                raise BugFixWorkspaceError(f"source_context_unavailable: {exc}") from exc
            logs_dir = temp_dir / "logs"
        else:
            logs_dir = _sync_source_logs(
                temp_dir, bug_fix_task_id=bug_fix_task_id,
                source_log_archive_path=source_log_archive_path,
                source_log_filename=source_log_filename,
            )
            context_metadata["availability"] = "legacy_reconstructed"
            (temp_dir / "source_analysis.json").write_text(
                json.dumps(context_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        task_data = {
            "bug_fix_task_id": bug_fix_task_id,
            "title": title,
            "summary": summary or "",
            "proposed_fixes": proposed_fixes,
            "source_log_id": source_log_id,
            "source_analysis_task_id": source_analysis_task_id,
            "default_branch": default_branch,
            "git_identity": identity,
            "source_analysis": "source_analysis.json",
            "context_availability": source_context.get("availability", "snapshot") if source_context else "legacy_reconstructed",
            "images_dir": "images" if (temp_dir / "images").exists() else None,
            # 相对 temp_dir 的路径；None 表示本次没有可用的来源日志。
            "logs_dir": "logs" if logs_dir is not None else None,
        }
        task_json_path = temp_dir / "task.json"
        task_json_path.write_text(
            json.dumps(task_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        logger.info(
            "Bug fix workspace prepared: task=%s temp_dir=%s branch=%s",
            bug_fix_task_id,
            temp_dir,
            default_branch,
        )
        return BugFixWorkspaceContext(
            task_id=bug_fix_task_id,
            temp_dir=str(temp_dir),
            repo_dir=str(repo_dir),
            task_json_path=str(task_json_path),
            default_branch=default_branch,
            logs_dir=str(logs_dir) if logs_dir is not None else None,
            metadata={"title": title, "proposed_fixes": proposed_fixes, "source_context": context_metadata},
            repo_url=repo_url,
        )
    except BugFixWorkspaceError:
        shutil.rmtree(str(temp_dir), ignore_errors=True)
        raise
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(str(temp_dir), ignore_errors=True)
        raise CloneFailedError(f"git clone timed out: {exc}") from exc
    except Exception:
        shutil.rmtree(str(temp_dir), ignore_errors=True)
        raise


def cleanup(ctx: BugFixWorkspaceContext) -> None:
    """幂等删除临时工作区目录。"""
    temp = Path(ctx.temp_dir)
    if temp.exists():
        shutil.rmtree(str(temp), ignore_errors=True)
        logger.info("Bug fix workspace cleaned up: %s", ctx.temp_dir)
