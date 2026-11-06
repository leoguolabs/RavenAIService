"""
Bug 修复任务与其 Merge Request 子记录的 SQLAlchemy 模型。

一个 ``BugFixTask`` 来源于一次成功且判定「需要代码修复」的日志分析；它在后台由
Bug Fix Coding Agent 执行，并按问题维度产出一个或多个 ``BugFixMergeRequest``。
任何 MR 行中都不存储 token，``mr_url`` 仅保存可点击的平台地址（不含凭据）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base, TimestampMixin


class BugFixTaskStatus(str, Enum):
    """Bug 修复任务状态机。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BugFixMergeRequestStatus(str, Enum):
    """单个 Merge Request 子记录的状态。"""

    CREATED = "created"
    OPEN = "open"
    PUSH_FAILED = "push_failed"
    MR_FAILED = "mr_failed"


class BugFixTask(Base, TimestampMixin):
    """一次 Bug 修复任务（一对多 MR）。"""

    __tablename__ = "bug_fix_task"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="任务主键 UUID",
    )
    project_repo_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("project_repo.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属项目仓库（project_repo.id）",
    )
    source_log_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        nullable=True,
        index=True,
        comment="来源日志记录 ID（log_records.id），可空",
    )
    source_analysis_task_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="来源 AI 分析任务 ID，可空",
    )
    title: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="任务标题（来自分析总结）",
    )
    summary: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="任务总结（分析结论摘要）",
    )
    proposed_fixes_json: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="拟修复项 JSON 数组字符串",
    )
    source_context_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    trace_events_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fix_outcomes_json: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "逐个拟修复项的处理结局 JSON 数组"
            "（created_mr/already_implemented/skipped/rejected/failed）"
        ),
    )
    status: Mapped[BugFixTaskStatus] = mapped_column(
        String(32),
        nullable=False,
        default=BugFixTaskStatus.PENDING,
        index=True,
        comment="任务状态：pending/running/succeeded/partial/failed/cancelled",
    )
    error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="失败时的 typed 错误描述",
    )
    celery_task_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Celery 任务 ID",
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="开始执行时间",
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="终态时间",
    )

    merge_requests: Mapped[list["BugFixMergeRequest"]] = relationship(
        "BugFixMergeRequest",
        back_populates="task",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<BugFixTask id={self.id} status={self.status} repo={self.project_repo_id}>"


class BugFixMergeRequest(Base, TimestampMixin):
    """Bug 修复任务产出的单个 Merge Request 汇总信息。"""

    __tablename__ = "bug_fix_merge_request"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="MR 子记录主键 UUID",
    )
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("bug_fix_task.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属 Bug 修复任务",
    )
    title: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="MR 标题",
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="MR 描述",
    )
    branch_name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="修复分支名",
    )
    base_branch: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="目标分支（默认分支）",
    )
    mr_url: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="可点击的 MR/PR 地址（不含凭据）",
    )
    mr_iid: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="平台内的 MR/PR IID/编号",
    )
    commit_sha: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="提交 SHA",
    )
    changed_files_json: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="改动文件清单 JSON（文件名 + 增删行）",
    )
    diff_stat_json: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="diff 统计 JSON（files/insertions/deletions）",
    )
    status: Mapped[BugFixMergeRequestStatus] = mapped_column(
        String(32),
        nullable=False,
        default=BugFixMergeRequestStatus.CREATED,
        comment="MR 状态：created/open/push_failed/mr_failed",
    )

    task: Mapped["BugFixTask"] = relationship("BugFixTask", back_populates="merge_requests")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<BugFixMergeRequest id={self.id} branch={self.branch_name} status={self.status}>"
