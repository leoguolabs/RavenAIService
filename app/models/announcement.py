"""System announcement API models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


MAX_COVER_BYTES = 2 * 1024 * 1024
AnnouncementLayout = Literal["text", "cover"]


class AnnouncementDraft(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    content: str = Field(..., min_length=1, max_length=4000)
    layout: AnnouncementLayout = "text"
    cover_image: Optional[str] = Field(
        None,
        max_length=23 + 4 * ((MAX_COVER_BYTES + 2) // 3),
        pattern=r"^data:image/(png|jpeg|webp);base64,",
    )
    cover_alt: str = Field("", max_length=240)
    fireworks: bool = False


class SystemAnnouncement(AnnouncementDraft):
    """The single current announcement persisted in runtime settings."""

    id: str = Field(..., min_length=1, max_length=36)
    published_at: datetime
    published_by: str = Field(..., min_length=1, max_length=128)
    active: bool = True


class AnnouncementResponse(BaseModel):
    success: bool = True
    data: Optional[SystemAnnouncement] = None
    message: str = "ok"


class AnnouncementDismissData(BaseModel):
    announcement_id: str
    dismissed: bool = True


class AnnouncementDismissResponse(BaseModel):
    success: bool = True
    data: AnnouncementDismissData
    message: str = "ok"
