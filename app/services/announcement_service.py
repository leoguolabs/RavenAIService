"""Current system announcement persistence and validation."""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import ValidationError

from app.models.announcement import MAX_COVER_BYTES, AnnouncementLayout, SystemAnnouncement
from app.services import runtime_settings_service


logger = logging.getLogger(__name__)

RUNTIME_KEY = "system_announcement"


def get_current(*, include_inactive: bool = True) -> Optional[SystemAnnouncement]:
    """Return the validated current announcement, if one is configured."""
    raw = runtime_settings_service.get_all().get(RUNTIME_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        announcement = SystemAnnouncement.model_validate(raw)
    except ValidationError as exc:
        logger.warning("Ignoring invalid runtime system announcement: %s", exc.errors(include_input=False))
        return None
    if not include_inactive and not announcement.active:
        return None
    return announcement


def _validate_cover(cover_image: Optional[str]) -> None:
    if not cover_image:
        raise ValueError("请上传封面图片 / Upload a cover image")
    header, _, encoded = cover_image.partition(",")
    if len(encoded) > 4 * ((MAX_COVER_BYTES + 2) // 3):
        raise ValueError("封面图片不能超过 2 MiB / Cover image must be 2 MiB or smaller")
    invalid = "封面须为有效的 PNG、JPEG 或 WebP 图片 / Use a valid PNG, JPEG, or WebP image"
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(invalid) from exc
    if len(data) > MAX_COVER_BYTES:
        raise ValueError("封面图片不能超过 2 MiB / Cover image must be 2 MiB or smaller")
    signatures = {
        "data:image/png;base64": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "data:image/jpeg;base64": data.startswith(b"\xff\xd8\xff"),
        "data:image/webp;base64": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if not signatures.get(header):
        raise ValueError(invalid)


def publish(
    *,
    title: str,
    content: str,
    published_by: str,
    layout: AnnouncementLayout = "text",
    cover_image: Optional[str] = None,
    cover_alt: str = "",
    fireworks: bool = False,
) -> SystemAnnouncement:
    """Replace the current announcement with a newly versioned active one."""
    if layout == "cover":
        _validate_cover(cover_image)
    announcement = SystemAnnouncement(
        id=str(uuid.uuid4()),
        title=title.strip(),
        content=content.strip(),
        published_at=datetime.now(timezone.utc),
        published_by=published_by.strip(),
        active=True,
        layout=layout,
        cover_image=cover_image if layout == "cover" else None,
        cover_alt=cover_alt.strip() if layout == "cover" else "",
        fireworks=fireworks and layout == "cover",
    )
    runtime_settings_service.update(
        {RUNTIME_KEY: announcement.model_dump(mode="json")}
    )
    return announcement


def deactivate() -> Optional[SystemAnnouncement]:
    """Deactivate the current announcement while retaining it for Admin UI."""
    current = get_current(include_inactive=True)
    if current is None:
        return None
    if not current.active:
        return current
    inactive = current.model_copy(update={"active": False})
    runtime_settings_service.update(
        {RUNTIME_KEY: inactive.model_dump(mode="json")}
    )
    return inactive
