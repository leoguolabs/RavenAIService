"""Metrics event model and API schemas.

``MetricEvent`` is the persisted, auditable fact source for AI token usage and
selected business activity. One row represents one auditable event; AI calls are
recorded at the granularity of a single invocation/run terminal state.

Privacy contract: this table MUST NOT store prompts, assistant answers, raw tool
input/output, log content, credentials, cookies, or token-bearing URLs. Only the
low-sensitivity ownership identifiers and the allowlisted ``metadata_json`` summary
are persisted (see ``app/services/metrics_service.py``).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseResponse
from .conversation_share import PublicShareMessage
from .database import Base, TimestampMixin


class MetricEvent(Base, TimestampMixin):
    """A single auditable metrics event (AI usage or business activity)."""

    __tablename__ = "metric_events"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="事件ID",
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        comment="幂等键，重复写入只保留一条，例如 ai_usage:chat_run:<run_id>",
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        comment="事件发生时间（独立于 row 创建时间）",
    )
    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="ai_usage / chat_activity / log_activity / package_activity / device_activity",
    )
    source: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="如 general_agent / device_agent / log_analysis_agent / log_upload",
    )
    # Ownership fields (low sensitivity, used for attribution / audit only).
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        nullable=True,
        comment="登录用户ID；匿名或系统任务为空",
    )
    owner_scope: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        comment="归属作用域，仅用于审计与匿名聚合，不返回给普通用户",
    )
    session_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, comment="会话ID（审计用）"
    )
    run_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, comment="Run ID（审计用）"
    )
    task_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, comment="Celery/任务ID（审计用）"
    )
    log_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, comment="日志记录ID（审计用）"
    )
    project_repo_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, comment="项目仓库ID（审计用）"
    )
    # AI invocation descriptors.
    agent_kind: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, comment="device / log_analysis / project / package / general / title"
    )
    provider: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="anthropic / deepseek / custom"
    )
    model: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, comment="使用的模型名称"
    )
    status: Mapped[Optional[str]] = mapped_column(
        String(24),
        nullable=True,
        comment="succeeded/failed/cancelled/stale/timeout 等终态",
    )
    error_kind: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="错误归类（低基数）"
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="耗时（毫秒）"
    )
    # Token counters (never NULL; missing usage is normalized to 0).
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="输入 Token"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="输出 Token"
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="缓存读取 Token"
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="缓存写入 Token"
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="总 Token = input+output+cache_read+cache_write"
    )
    cost_microusd: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="按配置估算的百万分之一美元成本；未配置价格时为空",
    )
    # Allowlisted low-sensitivity summary only (see metrics_service sanitization).
    metadata_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="经 allowlist 过滤后的低敏摘要 JSON"
    )

    __table_args__ = (
        Index("idx_metric_events_occurred_at", "occurred_at"),
        Index("idx_metric_events_user_time", "user_id", "occurred_at"),
        Index("idx_metric_events_event_source_time", "event_type", "source", "occurred_at"),
        Index("idx_metric_events_agent_model_time", "agent_kind", "provider", "model", "occurred_at"),
        Index("idx_metric_events_status_time", "status", "occurred_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<MetricEvent id={self.id} type={self.event_type} "
            f"source={self.source} total_tokens={self.total_tokens}>"
        )


# ==================== Pydantic API Schemas ====================


class TokenBreakdown(BaseModel):
    """Token 分解，所有字段缺失视为 0。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0


class TimeSeriesBucket(BaseModel):
    """时间桶聚合点。"""

    bucket_start: datetime = Field(..., description="桶起始时间")
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    invocation_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    counts_by_agent: Dict[str, int] = Field(default_factory=dict)


def _format_utc_offset(offset_minutes: int) -> str:
    sign = "+" if offset_minutes >= 0 else "-"
    absolute = abs(offset_minutes)
    return f"UTC{sign}{absolute // 60:02d}:{absolute % 60:02d}"


def _read_server_timezone_name() -> Optional[str]:
    """Best-effort IANA timezone name from TZ, /etc/timezone, or /etc/localtime."""
    env_tz = os.environ.get("TZ", "").strip()
    if env_tz and not env_tz.startswith(":"):
        return env_tz

    try:
        with open("/etc/timezone", "r", encoding="utf-8") as f:
            timezone_name = f.read().strip()
        if timezone_name:
            return timezone_name
    except OSError:
        pass

    try:
        localtime_path = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = "/zoneinfo/"
    if marker in localtime_path:
        return localtime_path.split(marker, 1)[1]
    return None


class ServerTimezone(BaseModel):
    """Timezone currently observed by the running server/container."""

    name: Optional[str] = None
    offset_minutes: int
    offset_label: str
    abbreviation: Optional[str] = None


def get_server_timezone() -> ServerTimezone:
    now = datetime.now().astimezone()
    offset = now.utcoffset()
    offset_minutes = int(offset.total_seconds() // 60) if offset else 0
    return ServerTimezone(
        name=_read_server_timezone_name(),
        offset_minutes=offset_minutes,
        offset_label=_format_utc_offset(offset_minutes),
        abbreviation=now.tzname(),
    )


class GroupCount(BaseModel):
    """按某一维度（source/agent_kind/provider/model/status）的计数与 Token 汇总。"""

    key: Optional[str] = Field(None, description="维度取值，None 表示未知")
    invocation_count: int = 0
    total_tokens: int = 0


class StatusCounts(BaseModel):
    """终态状态计数。"""

    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    stale: int = 0
    timeout: int = 0
    other: int = 0


class ChatActivitySummary(BaseModel):
    """聊天/用户活跃度摘要。"""

    total_users: int = 0
    active_users: int = 0
    chat_session_count: int = 0
    chat_message_count: int = 0
    run_counts_by_status: Dict[str, int] = Field(default_factory=dict)


class LogActivitySummary(BaseModel):
    """日志上传与 AI 分析摘要。"""

    upload_count: int = 0
    uploaded_bytes: int = 0
    counts_by_project: Dict[str, int] = Field(default_factory=dict)
    counts_by_status: Dict[str, int] = Field(default_factory=dict)
    ai_analysis_counts: Dict[str, int] = Field(default_factory=dict)


class PackageActivitySummary(BaseModel):
    """Raven 包库存与活动摘要。"""

    package_count: int = 0
    total_bytes: int = 0
    counts_by_project: Dict[str, int] = Field(default_factory=dict)
    activity_counts: Dict[str, int] = Field(default_factory=dict)
    search_count: int = 0


class DeviceActivitySummary(BaseModel):
    """设备连接摘要。"""

    counts_by_state: Dict[str, int] = Field(default_factory=dict)


class SystemOverview(BaseModel):
    """系统级 overview 响应数据。"""

    from_time: datetime
    to_time: datetime
    server_timezone: ServerTimezone = Field(default_factory=get_server_timezone)
    bucket: str
    tokens: TokenBreakdown = Field(default_factory=TokenBreakdown)
    estimated_cost_usd: Optional[float] = Field(
        None, description="估算成本（美元）；未配置价格时为 null"
    )
    cost_estimated: bool = Field(False, description="是否存在可用价格估算")
    invocation_count: int = 0
    status_counts: StatusCounts = Field(default_factory=StatusCounts)
    error_count: int = 0
    duration_ms_avg: Optional[float] = None
    duration_ms_p95: Optional[float] = None
    invocations_by_source: List[GroupCount] = Field(default_factory=list)
    invocations_by_agent_kind: List[GroupCount] = Field(default_factory=list)
    invocations_by_provider: List[GroupCount] = Field(default_factory=list)
    invocations_by_model: List[GroupCount] = Field(default_factory=list)
    invocations_by_project: List[GroupCount] = Field(default_factory=list)
    invocations_by_status: List[GroupCount] = Field(default_factory=list)
    time_series: List[TimeSeriesBucket] = Field(default_factory=list)
    chat: ChatActivitySummary = Field(default_factory=ChatActivitySummary)
    logs: LogActivitySummary = Field(default_factory=LogActivitySummary)
    packages: PackageActivitySummary = Field(default_factory=PackageActivitySummary)
    devices: DeviceActivitySummary = Field(default_factory=DeviceActivitySummary)


class SystemOverviewResponse(BaseResponse):
    data: SystemOverview


class UserMetricsRow(BaseModel):
    """用户列表中的一行统计。"""

    user_id: str
    username: Optional[str] = None
    display_name: Optional[str] = None
    role: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: Optional[float] = None
    run_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    message_count: int = 0
    last_active_at: Optional[datetime] = None
    top_agent_kind: Optional[str] = None


class UserMetricsListData(BaseModel):
    from_time: datetime
    to_time: datetime
    server_timezone: ServerTimezone = Field(default_factory=get_server_timezone)
    page: int
    per_page: int
    total: int
    sort: str
    rows: List[UserMetricsRow] = Field(default_factory=list)


class UserMetricsListResponse(BaseResponse):
    data: UserMetricsListData


class MergedOcrEvent(BaseModel):
    """折叠进父事件的 OCR 子事件。

    图片 OCR 是专家模型 / 日志分析等 agent run 的预处理步骤，与父事件共享
    ``run_id``（见 ``metrics_service._not_merged_ocr_filter``），因此在审计列表里
    不单独占一行，而是挂在父事件的 ``ocr_events`` 下。
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    occurred_at: datetime
    source: str
    agent_kind: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    status: Optional[str] = None
    error_kind: Optional[str] = None
    duration_ms: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_microusd: Optional[int] = None
    # 本次 OCR 处理的图片张数（由 ocr_service 写入 metadata）。
    image_count: Optional[int] = None


class RawMetricEvent(BaseModel):
    """原始事件（已 sanitize），供 admin 审计。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    idempotency_key: str
    occurred_at: datetime
    event_type: str
    source: str
    user_id: Optional[str] = None
    # 触发用户（由 API 层按 user_id 关联 users 表补全，便于审计）。
    username: Optional[str] = None
    display_name: Optional[str] = None
    conversation_available: bool = False
    owner_scope: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    log_id: Optional[str] = None
    project_repo_id: Optional[str] = None
    agent_kind: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    status: Optional[str] = None
    error_kind: Optional[str] = None
    duration_ms: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_microusd: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    # 与本事件同属一次请求的 OCR 子事件（见 MergedOcrEvent）。恒为列表，
    # 无图片附件的普通请求为空。
    ocr_events: List[MergedOcrEvent] = Field(default_factory=list)


class UserMetricsDetail(BaseModel):
    """单用户详情。"""

    user_id: str
    username: Optional[str] = None
    display_name: Optional[str] = None
    role: Optional[str] = None
    from_time: datetime
    to_time: datetime
    server_timezone: ServerTimezone = Field(default_factory=get_server_timezone)
    bucket: str
    tokens: TokenBreakdown = Field(default_factory=TokenBreakdown)
    estimated_cost_usd: Optional[float] = None
    cost_estimated: bool = False
    invocation_count: int = 0
    status_counts: StatusCounts = Field(default_factory=StatusCounts)
    message_count: int = 0
    last_active_at: Optional[datetime] = None
    invocations_by_source: List[GroupCount] = Field(default_factory=list)
    invocations_by_agent_kind: List[GroupCount] = Field(default_factory=list)
    invocations_by_provider: List[GroupCount] = Field(default_factory=list)
    invocations_by_model: List[GroupCount] = Field(default_factory=list)
    errors_by_kind: List[GroupCount] = Field(default_factory=list)
    time_series: List[TimeSeriesBucket] = Field(default_factory=list)
    recent_events: List[RawMetricEvent] = Field(default_factory=list)


class UserMetricsDetailResponse(BaseResponse):
    data: UserMetricsDetail


class SelfMetricsSummary(BaseModel):
    """用户自查摘要（仅当前登录用户）。"""

    user_id: str
    from_time: datetime
    to_time: datetime
    server_timezone: ServerTimezone = Field(default_factory=get_server_timezone)
    bucket: str
    tokens: TokenBreakdown = Field(default_factory=TokenBreakdown)
    estimated_cost_usd: Optional[float] = None
    cost_estimated: bool = False
    invocation_count: int = 0
    status_counts: StatusCounts = Field(default_factory=StatusCounts)
    message_count: int = 0
    last_active_at: Optional[datetime] = None
    invocations_by_agent_kind: List[GroupCount] = Field(default_factory=list)
    time_series: List[TimeSeriesBucket] = Field(default_factory=list)


class SelfMetricsResponse(BaseResponse):
    data: SelfMetricsSummary


class RawMetricEventsData(BaseModel):
    from_time: datetime
    to_time: datetime
    server_timezone: ServerTimezone = Field(default_factory=get_server_timezone)
    page: int
    per_page: int
    total: int
    events: List[RawMetricEvent] = Field(default_factory=list)


class RawMetricEventsResponse(BaseResponse):
    data: RawMetricEventsData


class AdminConversationImage(BaseModel):
    """一张用户附带图片的元数据；原图字节由 admin 专属接口按需返回。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    media_type: Optional[str] = None
    name: Optional[str] = None
    size: Optional[int] = None


class AdminConversationMessage(PublicShareMessage):
    """管理员视角的单条消息。

    在公开分享的消息形状之上补充用户轮次的图片附件元数据。之所以另立一个模型而
    不是直接扩展 ``PublicShareMessage``：公开快照没有可用的取图接口，带上图片元
    数据只会渲染出一堆坏图。
    """

    images: List[AdminConversationImage] = Field(default_factory=list)
    run_model: Optional[str] = None
    is_event_reply: Optional[bool] = None


class AdminConversationDetail(BaseModel):
    """Live, admin-only conversation linked to one metrics event."""

    event_id: str
    session_id: str
    user_id: str
    username: Optional[str] = None
    display_name: Optional[str] = None
    title: str
    message_count: int
    created_at: datetime
    last_message_at: datetime
    is_deleted: bool = False
    messages: List[AdminConversationMessage] = Field(default_factory=list)


class AdminConversationDetailResponse(BaseResponse):
    data: AdminConversationDetail
