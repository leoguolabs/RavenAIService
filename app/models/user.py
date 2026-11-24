"""
User and chat history models.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import BaseResponse
from .database import Base, TimestampMixin


class User(Base, TimestampMixin):
    """Application user."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="用户ID",
    )
    username: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        index=True,
        nullable=False,
        comment="用户名（唯一）",
    )
    display_name: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        comment="展示名称",
    )
    email: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="邮箱",
    )
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="密码哈希",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="是否启用",
    )
    role: Mapped[str] = mapped_column(
        String(32),
        default="user",
        nullable=False,
        comment="用户角色（user/admin）",
    )
    disabled_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="管理员禁用该账号时留给用户的留言，登录被拒时原样展示",
    )
    disabled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="账号被禁用的时间；重新启用时清空",
    )
    profile_role: Mapped[str] = mapped_column(
        String(64),
        default="developer",
        server_default="developer",
        nullable=False,
        comment="用户资料角色（如 developer/tester）",
    )
    language: Mapped[str] = mapped_column(
        String(8),
        default="zh",
        server_default="zh",
        nullable=False,
        comment="界面与AI语言偏好（zh/en）",
    )
    # ---- Agent 澄清提问（AskUserQuestion）用户偏好 ----
    clarification_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
        comment="是否允许 Agent 在指令不清晰时向我提问（全局开关）",
    )
    clarification_max_rounds: Mapped[int] = mapped_column(
        Integer,
        default=5,
        server_default="5",
        nullable=False,
        comment="单轮 run 内 Agent 最多向我提问的次数",
    )
    clarification_on_timeout: Mapped[str] = mapped_column(
        String(16),
        default="cancel",
        server_default="cancel",
        nullable=False,
        comment="澄清提问超时(5分钟)后的行为：cancel=取消本轮 / continue=基于已知信息继续",
    )
    last_seen_announcement_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        nullable=True,
        comment="最近确认关闭的系统公告ID",
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="最近登录时间",
    )

    chat_sessions: Mapped[list["ChatSession"]] = relationship(
        "ChatSession",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User id={self.id} username={self.username}>"


class ChatSession(Base, TimestampMixin):
    """Persisted chat session."""

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="会话ID",
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
        comment="所属用户",
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="新对话",
        comment="会话标题",
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        comment="最近消息时间",
    )
    message_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="消息数量",
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否已删除",
    )
    is_pinned: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否置顶",
    )
    pinned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="置顶时间",
    )

    user: Mapped["User"] = relationship("User", back_populates="chat_sessions")
    messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ChatSession id={self.id} user_id={self.user_id}>"


class ChatAgentRun(Base, TimestampMixin):
    """Persisted record of a single chat agent run.

    A run is one user turn driven by an agent (DeviceAgent / LogAnalysisAgent).
    The in-memory `ChatRunJob` is authoritative while running; this table holds
    the recoverable metadata for snapshots, side-bar status overlays and
    terminal replay after the in-memory job has been evicted.
    """

    __tablename__ = "chat_agent_runs"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="Run ID",
    )
    session_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
        comment="所属会话",
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        nullable=True,
        index=True,
        comment="所属用户（匿名为空）",
    )
    owner_scope: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        default="anon:unknown",
        comment="归属作用域：登录用户 user:<id>；匿名用户 anon:<client_scope>",
    )
    agent_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="device / log_analysis",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="running",
        index=True,
        comment="queued/running/succeeded/failed/cancelled/stale",
    )
    user_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="本轮用户输入快照",
    )
    request_json: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="请求 payload JSON",
    )
    workspace_path: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Agent 运行时工作目录",
    )
    answer: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="终态助手答案",
    )
    model: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        comment="使用的模型名称",
    )
    error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="错误描述",
    )
    trace_events_json: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="完成后写入的完整 trace events JSON",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        comment="run 开始时间",
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="run 终态时间",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<ChatAgentRun id={self.id} session={self.session_id} "
            f"kind={self.agent_kind} status={self.status}>"
        )


class ChatMessage(Base, TimestampMixin):
    """Single chat message inside a session."""

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="消息ID",
    )
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chat_sessions.id"),
        nullable=False,
        index=True,
        comment="所属会话",
    )
    role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="角色（user/ai/system）",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="消息内容",
    )
    images_json: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="随该轮附带图片的元数据 JSON [{id,media_type,name,size}]；字节存磁盘",
    )

    session: Mapped["ChatSession"] = relationship("ChatSession", back_populates="messages")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ChatMessage id={self.id} session_id={self.session_id} role={self.role}>"


# ==================== Pydantic Schemas ====================


class UserProfile(BaseModel):
    """用户信息展示模型"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    is_active: bool = True
    disabled_message: Optional[str] = None
    disabled_at: Optional[datetime] = None
    role: str = "user"
    profile_role: str = "developer"
    language: str = "zh"
    clarification_enabled: bool = True
    clarification_max_rounds: int = 5
    clarification_on_timeout: str = "cancel"
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class UserAuthPayload(BaseModel):
    """登录响应数据"""

    token: str
    expires_at: float
    user: UserProfile


class UserListResponse(BaseResponse):
    """用户列表响应"""

    data: list[UserProfile] = Field(default_factory=list)


class ChatSessionSummary(BaseModel):
    """会话摘要"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    last_message_at: datetime
    message_count: int
    created_at: datetime
    updated_at: datetime
    is_pinned: bool = False
    pinned_at: Optional[datetime] = None
    # Optional overlay of the currently active (or most recently active running)
    # chat agent run, used by the sidebar to render running spinners and by the
    # frontend to resume in-flight conversations.
    active_run_id: Optional[str] = None
    run_status: Optional[str] = None
    run_agent_kind: Optional[str] = None
    run_started_at: Optional[datetime] = None
    run_updated_at: Optional[datetime] = None


class ChatMessageRecord(BaseModel):
    """会话消息模型"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    role: str
    content: str
    created_at: datetime
    updated_at: datetime
    run_id: Optional[str] = None
    run_status: Optional[str] = None
    run_agent_kind: Optional[str] = None
    run_model: Optional[str] = None
    trace_events: Optional[list[dict[str, Any]]] = None
    # Attached-image metadata for a user turn; bytes are fetched separately via
    # the authorized chat-images endpoint. Empty/absent for image-free turns.
    images: Optional[list[dict[str, Any]]] = None


class UserAuthResponse(BaseResponse):
    """登录响应"""

    data: UserAuthPayload


class UserDetailResponse(BaseResponse):
    """用户详情响应"""

    data: UserProfile


class ChatSessionListResponse(BaseResponse):
    """会话列表响应"""

    data: list[ChatSessionSummary] = Field(default_factory=list)


class ChatSessionSearchResult(ChatSessionSummary):
    """搜索结果仅包含可显示的正文摘要，不携带内部 trace。"""

    snippet: str = ""


class ChatSessionSearchData(BaseModel):
    items: list[ChatSessionSearchResult] = Field(default_factory=list)
    has_more: bool = False


class ChatSessionSearchResponse(BaseResponse):
    data: ChatSessionSearchData


class ChatMessagesResponse(BaseResponse):
    """会话消息响应"""

    data: list[ChatMessageRecord] = Field(default_factory=list)
    workspace_context: Optional[dict[str, Any]] = None
