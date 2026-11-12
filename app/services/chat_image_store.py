"""Persist authorized chat attachments and prepare original bytes for Agent input.

Images live under the session directory and share its retention policy. Metadata
contains no local paths. Gateway and direct OpenAI execution require originals;
legacy OCR execution can still proceed when thumbnail storage fails.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import shutil
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, List, Optional

from app.models.chat import ImageAttachment

logger = logging.getLogger(__name__)


# Extension per accepted MIME type (kept in lock-step with
# ``app.models.chat.ALLOWED_IMAGE_MIME_TYPES``).
_EXTENSION_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


@dataclass
class StoredImage:
    """One persisted attachment. ``path`` is local-only and never serialized."""

    id: str
    media_type: str
    name: str
    size: int
    path: str

    def to_meta(self) -> dict:
        """Client-facing metadata (drops the on-disk path)."""
        meta = asdict(self)
        meta.pop("path", None)
        return meta


def _base_dir() -> Path:
    """Resolve ``CHAT_IMAGE_STORE_DIR``, relative paths against ``base_dir``."""
    from app.config import settings

    raw = getattr(settings, "chat_image_store_dir", "temp/chat_images")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(getattr(settings, "base_dir", ".")) / path
    return path


def _sanitize_session(session_id: Optional[str]) -> str:
    """Reduce a session id to a safe single path segment.

    Session ids are server-generated UUIDs, but they arrive over the wire, so
    strip anything that could escape the store root (``..``, separators).
    """
    cleaned = "".join(
        ch for ch in (session_id or "") if ch.isalnum() or ch in ("-", "_")
    )[:64]
    return cleaned or "anon"


def _decode(payload: str) -> bytes:
    """Decode a base64 attachment, tolerating a ``data:<mime>;base64,`` prefix."""
    text = (payload or "").strip()
    if text.startswith("data:"):
        comma = text.find(",")
        if comma != -1:
            text = text[comma + 1 :]
    return base64.b64decode("".join(text.split()), validate=False)


def save_turn_images(
    images: Optional[List[ImageAttachment]],
    *,
    session_id: Optional[str],
    names: Optional[List[str]] = None,
) -> List[StoredImage]:
    """Persist this turn's attachments and return their metadata.

    Best-effort per image: one undecodable or unwritable attachment is skipped
    with a warning rather than failing the turn. Returns an empty list when
    there is nothing to store.
    """
    image_list = list(images or [])
    if not image_list:
        return []
    native = native_image_transport_enabled()

    session_dir = _base_dir() / _sanitize_session(session_id)
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("chat_image_store: cannot create %s: %s", session_dir, exc)
        if native:
            raise ValueError("Unable to store original images / 无法保存截图原件") from exc
        return []

    stored: List[StoredImage] = []
    for index, image in enumerate(image_list):
        media_type = (getattr(image, "media_type", "") or "").strip().lower()
        extension = _EXTENSION_BY_MIME.get(media_type, "bin")
        image_id = uuid.uuid4().hex
        target = session_dir / f"{image_id}.{extension}"
        try:
            payload = _decode(getattr(image, "data", "") or "")
        except (binascii.Error, ValueError) as exc:
            logger.warning("chat_image_store: undecodable image #%d: %s", index + 1, exc)
            continue
        if not payload:
            continue
        try:
            target.write_bytes(payload)
        except OSError as exc:
            logger.warning("chat_image_store: write failed for %s: %s", target, exc)
            continue
        fallback_name = f"image-{index + 1}.{extension}"
        name = (names[index] if names and index < len(names) else "") or fallback_name
        stored.append(
            StoredImage(
                id=image_id,
                media_type=media_type or "image/png",
                name=name,
                size=len(payload),
                path=str(target),
            )
        )

    if native and len(stored) != len(image_list):
        raise ValueError("Some original images could not be stored / 部分截图原件无法保存")
    if stored:
        logger.info(
            "chat_image_store: stored %d image(s) for session=%s",
            len(stored),
            _sanitize_session(session_id),
        )
    return stored


def to_meta_json(stored: List[StoredImage]) -> Optional[str]:
    """Serialize metadata for ``chat_messages.images_json`` (None when empty)."""
    if not stored:
        return None
    return json.dumps([item.to_meta() for item in stored], ensure_ascii=False)


def parse_meta_json(raw: Optional[str]) -> List[dict]:
    """Parse a stored ``images_json`` column back into a list of metadata dicts."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def resolve_path(session_id: str, image_id: str) -> Optional[Path]:
    """Locate a stored image's bytes, or ``None`` when it does not exist.

    ``image_id`` is matched as a bare hex stem against the session directory, so
    a caller-supplied id can never traverse outside the store: anything with a
    separator or dot simply fails the alphanumeric check below.
    """
    safe_id = "".join(ch for ch in (image_id or "") if ch.isalnum())
    if not safe_id or safe_id != (image_id or ""):
        return None
    session_dir = _base_dir() / _sanitize_session(session_id)
    if not session_dir.is_dir():
        return None
    for extension in {*_EXTENSION_BY_MIME.values(), "bin"}:
        candidate = session_dir / f"{safe_id}.{extension}"
        if candidate.is_file():
            return candidate
    return None


def delete_session_images(session_id: Optional[str]) -> None:
    """Idempotently drop a session's image directory. Never raises."""
    session_dir = _base_dir() / _sanitize_session(session_id)
    if not session_dir.exists():
        return
    shutil.rmtree(str(session_dir), ignore_errors=True)
    logger.info("chat_image_store: removed image dir for session=%s", _sanitize_session(session_id))


def native_image_transport_enabled() -> bool:
    """Magpie decides vision per route; direct OpenAI also consumes originals."""
    from app.config import settings
    from app.services.agent_runtime_service import get_config

    return get_config().enabled or settings.anthropic_provider == "openai"


def workspace_materialization_enabled() -> bool:
    """Whether originals may be copied into an agent workspace.

    Requires both the explicit setting and a vision-capable provider: copying
    images where the upstream cannot accept image blocks risks failing a run if
    an agent ever reads one.
    """
    from app.agents.anthropic_client import PROVIDER_PROFILES
    from app.config import settings

    if native_image_transport_enabled():
        return True
    if not getattr(settings, "chat_image_workspace_materialize", True):
        return False
    profile = PROVIDER_PROFILES.get(getattr(settings, "anthropic_provider", ""))
    return bool(profile and profile.supports_image_input)


def materialize_session_images(session_id: str, raw_meta: Optional[str], workspace_dir: Any,
                               history: Optional[list] = None) -> None:
    """Restore only API-authorized session attachments into this run's workspace."""
    from app.config import settings

    # History metadata comes from authorized DB records. Client history omits it.
    metadata = parse_meta_json(raw_meta)
    known = {item["id"] for item in metadata}
    limit = settings.ocr_max_images
    for turn in reversed(history or []):
        for item in parse_meta_json(turn.get("images_json")):
            if item["id"] not in known and len(metadata) < limit:
                metadata.append(item)
                known.add(item["id"])
    stored = []
    for item in metadata:
        path = resolve_path(session_id, item.get("id", ""))
        if path is None:
            if native_image_transport_enabled():
                raise ValueError("Original image unavailable / 截图原件不可用")
            continue
        stored.append(StoredImage(id=item["id"], media_type=item["media_type"],
            name=item.get("name", ""), size=path.stat().st_size, path=str(path)))
    result = materialize_into_workspace(stored, workspace_dir)
    if stored and native_image_transport_enabled() and result is None:
        raise ValueError("Unable to prepare original images / 无法准备截图原件")


def native_image_input_required(workspace_dir: Any) -> bool:
    """Require image transport; Magpie handles text-only candidates internally."""
    return native_image_transport_enabled() and (
        Path(workspace_dir) / "images" / "manifest.json"
    ).is_file()


def materialize_into_workspace(
    stored: List[StoredImage], workspace_dir: Any
) -> Optional[str]:
    """Copy stored originals into ``<workspace>/images/`` with a manifest.

    Gateway and OpenAI executors attach these bytes to model requests. Legacy
    providers may read them when their capability allows it. Native transport
    fails explicitly if any required original cannot be prepared.

    Cleanup is inherited: every caller's workspace is a per-run temp directory
    already removed in a ``finally`` block.
    """
    native = native_image_transport_enabled()
    if stored and not workspace_dir and native:
        raise ValueError("Image workspace unavailable / 图片工作区不可用")
    if not stored or not workspace_dir:
        return None
    if not workspace_materialization_enabled():
        return None

    images_dir = Path(workspace_dir) / "images"
    try:
        images_dir.mkdir(parents=True, exist_ok=True)
        manifest = []
        for index, item in enumerate(stored, start=1):
            source = Path(item.path)
            if not source.is_file():
                if native:
                    raise ValueError("Original image unavailable / 截图原件不可用")
                continue
            filename = f"image_{index}{source.suffix}"
            shutil.copyfile(source, images_dir / filename)
            manifest.append(
                {
                    "file": filename,
                    "media_type": item.media_type,
                    "original_name": item.name,
                    "size": item.size,
                }
            )
        if not manifest:
            return None
        (images_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("chat_image_store: workspace materialization failed: %s", exc)
        if native:
            raise ValueError("Unable to prepare original images / 无法准备截图原件") from exc
        return None

    logger.info(
        "chat_image_store: materialized %d image(s) into %s", len(manifest), images_dir
    )
    return str(images_dir)
