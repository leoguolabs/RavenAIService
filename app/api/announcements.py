"""Global-admin announcement management and authenticated-user delivery."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import require_global_admin
from app.api.users import get_current_user
from app.models.announcement import (
    AnnouncementDismissData,
    AnnouncementDismissResponse,
    AnnouncementDraft,
    AnnouncementResponse,
)
from app.models.database import get_db
from app.services import announcement_service


admin_router = APIRouter(prefix="/admin/announcements", tags=["Admin"])
user_router = APIRouter(prefix="/api/v1/announcements", tags=["系统公告"])


class PublishAnnouncementRequest(AnnouncementDraft):
    @field_validator("title", "content", mode="before")
    @classmethod
    def strip_required_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("内容不能为空")
        return normalized


@admin_router.get("/current", response_model=AnnouncementResponse)
async def get_current_announcement(
    _username: str = Depends(require_global_admin),
) -> AnnouncementResponse:
    return AnnouncementResponse(
        data=announcement_service.get_current(include_inactive=True)
    )


@admin_router.put("/current", response_model=AnnouncementResponse)
async def publish_announcement(
    payload: PublishAnnouncementRequest,
    username: str = Depends(require_global_admin),
) -> AnnouncementResponse:
    try:
        announcement = announcement_service.publish(published_by=username, **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AnnouncementResponse(data=announcement, message="公告已发布")


@admin_router.delete("/current", response_model=AnnouncementResponse)
async def deactivate_announcement(
    _username: str = Depends(require_global_admin),
) -> AnnouncementResponse:
    announcement = announcement_service.deactivate()
    if announcement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="当前没有公告",
        )
    return AnnouncementResponse(data=announcement, message="公告已停止展示")


@user_router.get("/pending", response_model=AnnouncementResponse)
async def get_pending_announcement(
    current_user=Depends(get_current_user),
) -> AnnouncementResponse:
    announcement = announcement_service.get_current(include_inactive=False)
    if (
        announcement is None
        or current_user.last_seen_announcement_id == announcement.id
    ):
        return AnnouncementResponse(data=None)
    return AnnouncementResponse(data=announcement)


@user_router.post(
    "/{announcement_id}/dismiss",
    response_model=AnnouncementDismissResponse,
)
async def dismiss_announcement(
    announcement_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnnouncementDismissResponse:
    current = announcement_service.get_current(include_inactive=False)
    if current is None or current.id != announcement_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="公告已更新，请刷新后查看最新公告",
        )
    current_user.last_seen_announcement_id = current.id
    await db.flush()
    return AnnouncementDismissResponse(
        data=AnnouncementDismissData(announcement_id=current.id),
        message="公告已确认",
    )
