"""Bug 修复任务 API。

供已登录用户查看其所属项目的 Bug 修复任务列表与详情，并允许对失败任务发起
重试。访问范围严格按项目成员资格过滤；``role == "admin"`` 的用户可访问全部
任务。任何字段都不含 git token —— ``mr_url`` 仅为可点击的平台地址（不含凭据）。

读取型分页/详情查询走独立的 async 实现（不在面向 Celery 的同步
``bug_fix_service`` 中）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.log_analysis.trace import mask_tokens
from app.api.users import get_current_user, get_request_locale
from app.i18n.messages import t
from app.models.bug_fix import BugFixMergeRequest, BugFixTask, BugFixTaskStatus
from app.models.database import get_db
from app.models.project_repo import ProjectRepo
from app.services import project_repo_member_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/bug-fixes", tags=["Bug 修复"])


# --------------------------------------------------------------------------- #
# 响应模型
# --------------------------------------------------------------------------- #
class BugFixTaskSummary(BaseModel):
    id: str
    title: str
    project_code: Optional[str] = None
    project_name: Optional[str] = None
    status: str
    merge_request_count: int = 0
    source_log_id: Optional[str] = None
    created_at: Optional[str] = None
    finished_at: Optional[str] = None


class BugFixTaskListResponse(BaseModel):
    success: bool = True
    data: List[BugFixTaskSummary]
    total: int
    page: int
    page_size: int
    message: str = "ok"


class BugFixMergeRequestData(BaseModel):
    id: str
    title: str
    status: str
    branch_name: str
    base_branch: str
    mr_url: Optional[str] = None
    mr_iid: Optional[str] = None
    commit_sha: Optional[str] = None
    changed_files: Optional[Any] = None
    diff_stat: Optional[Any] = None


class BugFixTaskDetail(BugFixTaskSummary):
    model: Optional[str] = None
    trace_events: List[Any] = []
    context_availability: Optional[str] = None
    summary: Optional[str] = None
    source_analysis_task_id: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    proposed_fixes: List[Any] = []
    fix_outcomes: List[Any] = []
    merge_requests: List[BugFixMergeRequestData] = []


class BugFixTaskDetailResponse(BaseModel):
    success: bool = True
    data: BugFixTaskDetail
    message: str = "ok"


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _status_value(value) -> str:
    """状态可能是枚举或字符串，统一取其字符串值。"""
    return getattr(value, "value", value)


def _parse_json(raw: Optional[str], default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _is_admin(user) -> bool:
    return getattr(user, "role", None) == "admin"


def _enqueue_bug_fix_retry(task_id: str):
    """把重试任务提交给 Bug Fix 专用 Celery 队列。

    延迟导入避免只读 API 启动时提前加载写入型 Agent；独立函数也让队列失败路径
    可以在 API 测试中稳定替换。
    """
    from app.tasks.bug_fix import run_bug_fix_task

    return run_bug_fix_task.delay(task_id)


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@router.get("", response_model=BugFixTaskListResponse)
async def list_bug_fixes(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> BugFixTaskListResponse:
    """分页列出当前用户可见的 Bug 修复任务。

    非管理员仅可见其作为成员的项目；管理员可见全部。
    """
    admin = _is_admin(current_user)

    # 非管理员先取其成员项目；无项目则直接空结果。
    allowed_project_ids: Optional[List[int]] = None
    if not admin:
        allowed_project_ids = await project_repo_member_service.list_user_projects(
            db, current_user.id
        )
        if not allowed_project_ids:
            return BugFixTaskListResponse(
                data=[], total=0, page=page, page_size=page_size
            )

    base_filter = []
    if allowed_project_ids is not None:
        base_filter.append(BugFixTask.project_repo_id.in_(allowed_project_ids))

    # 总数
    count_stmt = select(func.count(BugFixTask.id))
    for f in base_filter:
        count_stmt = count_stmt.where(f)
    total = int((await db.execute(count_stmt)).scalar() or 0)

    # 分页数据 + 项目信息
    stmt = (
        select(BugFixTask, ProjectRepo.project_code, ProjectRepo.project_name)
        .join(ProjectRepo, ProjectRepo.id == BugFixTask.project_repo_id)
    )
    for f in base_filter:
        stmt = stmt.where(f)
    stmt = (
        stmt.order_by(BugFixTask.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).all()

    task_ids = [row[0].id for row in rows]
    mr_counts = await _merge_request_counts(db, task_ids)

    items = [
        BugFixTaskSummary(
            id=task.id,
            title=task.title,
            project_code=project_code,
            project_name=project_name,
            status=_status_value(task.status),
            merge_request_count=mr_counts.get(task.id, 0),
            source_log_id=task.source_log_id,
            created_at=_iso(task.created_at),
            finished_at=_iso(task.finished_at),
        )
        for task, project_code, project_name in rows
    ]
    return BugFixTaskListResponse(
        data=items, total=total, page=page, page_size=page_size
    )


@router.get("/{task_id}", response_model=BugFixTaskDetailResponse)
async def get_bug_fix(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
    locale: str = Depends(get_request_locale),
) -> BugFixTaskDetailResponse:
    """读取单个 Bug 修复任务详情及其 Merge Request 子记录。

    非成员且非管理员一律返回 404（不泄露任务是否存在）。
    """
    stmt = (
        select(BugFixTask, ProjectRepo.project_code, ProjectRepo.project_name)
        .join(ProjectRepo, ProjectRepo.id == BugFixTask.project_repo_id)
        .where(BugFixTask.id == task_id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=t("task.not_found", locale)
        )

    task, project_code, project_name = row

    if not _is_admin(current_user):
        member = await project_repo_member_service.is_member(
            db, task.project_repo_id, current_user.id
        )
        if not member:
            # 不泄露存在性
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=t("task.not_found", locale),
            )

    mr_stmt = (
        select(BugFixMergeRequest)
        .where(BugFixMergeRequest.task_id == task_id)
        .order_by(BugFixMergeRequest.created_at.asc())
    )
    mrs = (await db.execute(mr_stmt)).scalars().all()

    merge_requests = [
        BugFixMergeRequestData(
            id=mr.id,
            title=mr.title,
            status=_status_value(mr.status),
            branch_name=mr.branch_name,
            base_branch=mr.base_branch,
            mr_url=mr.mr_url,
            mr_iid=mr.mr_iid,
            commit_sha=mr.commit_sha,
            changed_files=_parse_json(mr.changed_files_json, None),
            diff_stat=_parse_json(mr.diff_stat_json, None),
        )
        for mr in mrs
    ]

    detail = BugFixTaskDetail(
        id=task.id,
        title=task.title,
        project_code=project_code,
        project_name=project_name,
        status=_status_value(task.status),
        merge_request_count=len(merge_requests),
        source_log_id=task.source_log_id,
        source_analysis_task_id=task.source_analysis_task_id,
        created_at=_iso(task.created_at),
        finished_at=_iso(task.finished_at),
        started_at=_iso(task.started_at),
        summary=task.summary,
        model=task.model,
        trace_events=_parse_json(task.trace_events_json, []),
        context_availability=(_parse_json(task.source_context_json, {}).get("availability", "snapshot") if task.source_context_json else "legacy_reconstructed"),
        error=mask_tokens(task.error) if task.error else task.error,
        proposed_fixes=_parse_json(task.proposed_fixes_json, []),
        fix_outcomes=_parse_json(task.fix_outcomes_json, []),
        merge_requests=merge_requests,
    )
    return BugFixTaskDetailResponse(data=detail)


@router.post(
    "/{task_id}/retry",
    response_model=BugFixTaskDetailResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_bug_fix(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
    locale: str = Depends(get_request_locale),
) -> BugFixTaskDetailResponse:
    """重新派发一个失败的 Bug 修复任务。

    仅项目成员或管理员可操作。状态更新使用 ``failed`` 条件作为一次性 claim，
    因而并发双击只有一个请求能成功入队；提交 Celery 失败时会恢复成可重试的
    ``failed`` 状态，避免任务永久停留在 ``pending``。
    """
    stmt = select(BugFixTask).where(BugFixTask.id == task_id)
    task = (await db.execute(stmt)).scalar_one_or_none()
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=t("task.not_found", locale),
        )

    if not _is_admin(current_user):
        member = await project_repo_member_service.is_member(
            db, task.project_repo_id, current_user.id
        )
        if not member:
            # 与详情读取一致，不向非成员泄露任务是否存在。
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=t("task.not_found", locale),
            )

    if _status_value(task.status) != BugFixTaskStatus.FAILED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=t("task.retry_only_failed", locale),
        )

    # 条件更新同时承担并发 claim：第一个请求将状态改为 pending 后，后续请求的
    # rowcount 会是 0，因而不会重复启动 Agent。
    claim_stmt = (
        update(BugFixTask)
        .where(
            BugFixTask.id == task_id,
            BugFixTask.status == BugFixTaskStatus.FAILED,
        )
        .values(
            status=BugFixTaskStatus.PENDING,
            error=None,
            celery_task_id=None,
            started_at=None,
            finished_at=None,
            fix_outcomes_json=None,
        )
    )
    claim_result = await db.execute(claim_stmt)
    if claim_result.rowcount != 1:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=t("task.retry_in_progress", locale),
        )

    # 必须先提交 pending，worker 才能看到可执行状态；run_bug_fix_task 会跳过终态。
    await db.commit()

    try:
        async_result = _enqueue_bug_fix_retry(task_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to enqueue bug fix retry task=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        restore_stmt = (
            update(BugFixTask)
            .where(
                BugFixTask.id == task_id,
                BugFixTask.status == BugFixTaskStatus.PENDING,
            )
            .values(
                status=BugFixTaskStatus.FAILED,
                error="retry_enqueue_failed",
                finished_at=datetime.utcnow(),
            )
        )
        await db.execute(restore_stmt)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=t("task.retry_enqueue_failed", locale),
        ) from exc

    celery_task_id = getattr(async_result, "id", None)
    if celery_task_id:
        # worker 可能已经抢先进入 running，因此只补写仍处于 pending 的任务；
        # running 时 mark_running 已经写入真实 celery id。
        await db.execute(
            update(BugFixTask)
            .where(
                BugFixTask.id == task_id,
                BugFixTask.status == BugFixTaskStatus.PENDING,
            )
            .values(celery_task_id=str(celery_task_id))
        )
        await db.commit()

    # expire_on_commit=False 会保留 claim 前的 ORM 状态；清理 identity map 后复用
    # 详情构建逻辑，确保响应中的状态是最新值。
    db.expire_all()
    response = await get_bug_fix(task_id, db, current_user, locale)
    response.message = t("task.retry_queued", locale)
    return response


async def _merge_request_counts(
    db: AsyncSession, task_ids: List[str]
) -> dict[str, int]:
    """批量统计每个任务的 MR 数量。"""
    if not task_ids:
        return {}
    stmt = (
        select(BugFixMergeRequest.task_id, func.count(BugFixMergeRequest.id))
        .where(BugFixMergeRequest.task_id.in_(task_ids))
        .group_by(BugFixMergeRequest.task_id)
    )
    result = await db.execute(stmt)
    return {row[0]: int(row[1]) for row in result.all()}
