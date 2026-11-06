"""Bug 修复 Celery 任务（Claude Agent SDK 写入型 Agent）。

``run_bug_fix_task`` 由日志分析成功且判定需要代码修复时派发（见
``ai_analysis._maybe_dispatch_bug_fix``）。它在独立队列上运行，避免与分析任务争抢。

终态：全部成功 → ``succeeded``；部分成功 → ``partial``；无产出/全部失败 → ``failed``。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from celery import current_task

from app.agents.bug_fix.agent import BugFixCodingAgent
from app.agents.log_analysis.trace import mask_tokens
from app.agents.bug_fix.workspace import BugFixWorkspaceError, cleanup, prepare
from app.celery_app import celery_app
from app.config import settings
from app.models.bug_fix import BugFixTask
from app.models.log import LogRecord
from app.models.project_repo import ProjectRepo
from app.services import bug_fix_service
from app.tasks.ai_analysis import SessionLocal  # 复用同步引擎/会话工厂

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="app.tasks.bug_fix.run_bug_fix_task")
def run_bug_fix_task(self, bug_fix_task_id: str) -> Dict[str, Any]:
    """执行一个 Bug 修复任务。"""
    celery_id = getattr(self.request, "id", None)
    session = SessionLocal()
    ctx = None
    try:
        task: Optional[BugFixTask] = session.get(BugFixTask, bug_fix_task_id)
        if task is None:
            logger.warning("run_bug_fix_task: task %s not found", bug_fix_task_id)
            return {"status": "missing", "task_id": bug_fix_task_id}

        # acks_late 下消息可能被重投（worker 重启、broker 可见性超时）。已到
        # 终态的任务直接跳过，避免重复执行 Agent、重复提 MR。pending/running
        # 仍允许执行：running 覆盖 worker 崩溃后重投的恢复场景。
        current_status = getattr(task.status, "value", task.status)
        if current_status in ("succeeded", "partial", "failed", "cancelled"):
            logger.info(
                "run_bug_fix_task: task %s already finished (status=%s), skip",
                bug_fix_task_id, current_status,
            )
            return {"status": "skipped", "task_id": bug_fix_task_id}

        repo: Optional[ProjectRepo] = session.get(ProjectRepo, task.project_repo_id)
        if repo is None:
            bug_fix_service.finalize(
                session, bug_fix_task_id, merge_request_count=0,
                error="project_repo_not_found",
            )
            session.commit()
            return {"status": "failed", "task_id": bug_fix_task_id}

        bug_fix_service.mark_running(session, bug_fix_task_id, celery_task_id=celery_id)
        session.commit()

        proposed_fixes = []
        if task.proposed_fixes_json:
            try:
                proposed_fixes = json.loads(task.proposed_fixes_json) or []
            except json.JSONDecodeError:
                proposed_fixes = []

        git_token = repo.git_token or settings.code_repo_git_token

        # 来源日志归档：用于在修复工作区重建与日志分析一致的 logs/。日志
        # 分析工作区在分析结束后已清理，这里从持久化归档重新解压。
        source_context = json.loads(task.source_context_json) if task.source_context_json else None
        source_log_archive_path = None
        source_log_filename = None
        if task.source_log_id:
            log_record = session.get(LogRecord, task.source_log_id)
            if log_record is not None and not getattr(log_record, "is_deleted", False):
                source_log_archive_path = (
                    getattr(log_record, "archive_path", None)
                    or getattr(log_record, "file_path", None)
                )
                source_log_filename = getattr(log_record, "original_filename", None)
                if source_context is None:
                    from app.services.bug_fix_context import recover_legacy
                    source_context = recover_legacy(session, task, log_record)
                    task.source_context_json = json.dumps(source_context)
                    session.commit()
            elif source_context is None:
                raise BugFixWorkspaceError("source_context_unavailable: original log is missing or deleted")

        try:
            ctx = prepare(
                bug_fix_task_id=bug_fix_task_id,
                repo_url=repo.repo_url,
                default_branch=repo.default_branch or "main",
                git_token=git_token,
                title=task.title,
                summary=task.summary,
                proposed_fixes=proposed_fixes,
                source_log_id=task.source_log_id,
                source_analysis_task_id=task.source_analysis_task_id,
                source_log_archive_path=source_log_archive_path,
                source_log_filename=source_log_filename,
                source_context=source_context,
            )
        except BugFixWorkspaceError as exc:
            bug_fix_service.finalize(
                session, bug_fix_task_id, merge_request_count=0, error=str(exc),
            )
            session.commit()
            return {"status": "failed", "task_id": bug_fix_task_id}

        try:
            result = BugFixCodingAgent().run_sync(ctx)
        finally:
            cleanup(ctx)

        task.model = result.get("model")
        task.trace_events_json = json.dumps(result.get("trace_events") or [], ensure_ascii=False)
        merge_requests = result.get("merge_requests") or []
        recorded_mrs = 0
        recording_errors = []
        for mr in merge_requests:
            try:
                with session.begin_nested():
                    bug_fix_service.record_merge_request(session, bug_fix_task_id, mr)
                if mr.get("mr_url"):
                    recorded_mrs += 1
            except Exception as exc:  # noqa: BLE001
                recording_errors.append(mask_tokens(str(exc)))
                logger.warning(
                    "run_bug_fix_task: failed to record MR for task=%s: %s",
                    bug_fix_task_id, exc,
                )

        # partial/failed 时把 agent 的 error_kind 透传，便于详情展示。
        agent_status = result.get("status")
        error = None
        if agent_status in ("partial", "failed"):
            error = ": ".join(str(value) for value in (result.get("error_kind"), result.get("error")) if value) or "agent_failed"
        if recording_errors:
            error = "mr_persistence_failed: " + "; ".join(recording_errors)

        # 逐项结局：解释每个拟修复项为何有/没有 MR（already_implemented 等）。
        fix_outcomes = result.get("fix_outcomes") or []
        # 无 MR 但每个拟修复项都是「无需改动」类结局（已在基线实现/主动跳过）时，
        # 这是「已确认无需修复」而非失败——据此让 finalize 判定为 succeeded。
        no_action_needed = (
            not merge_requests
            and not error
            and agent_status == "succeeded"
            and bool(fix_outcomes)
            and len(fix_outcomes) == len(proposed_fixes)
            and all(
                (o or {}).get("outcome") in ("already_implemented", "skipped", "rejected")
                for o in fix_outcomes
            )
        )

        bug_fix_service.finalize(
            session,
            bug_fix_task_id,
            merge_request_count=recorded_mrs,
            error=error,
            fix_outcomes=fix_outcomes,
            no_action_needed=no_action_needed,
        )
        session.commit()

        logger.info(
            "run_bug_fix_task complete: task=%s agent_status=%s mrs=%d outcomes=%d",
            bug_fix_task_id, agent_status, len(merge_requests), len(fix_outcomes),
        )
        return {
            "status": "completed",
            "task_id": bug_fix_task_id,
            "merge_request_count": len(merge_requests),
        }

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "run_bug_fix_task failed: task=%s error=%s",
            bug_fix_task_id, exc, exc_info=True,
        )
        if ctx is not None:
            try:
                cleanup(ctx)
            except Exception:
                pass
        try:
            session.rollback()
            bug_fix_service.finalize(
                session, bug_fix_task_id, merge_request_count=0,
                error=mask_tokens(f"task_exception: {exc}"),
            )
            session.commit()
        except Exception:
            pass
        return {"status": "error", "task_id": bug_fix_task_id}
    finally:
        session.close()
