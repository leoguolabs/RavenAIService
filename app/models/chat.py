"""
AI 对话相关的请求/响应模型
"""
import json
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from app.models.base import BaseResponse


class ChatMessage(BaseModel):
    """前后端统一的对话消息模型"""
    role: Literal["user", "ai", "assistant", "system"] = Field(..., description="消息角色")
    content: str = Field(..., description="消息内容")


class ImageAttachment(BaseModel):
    """随本轮消息附带的一张图片（用于 OCR/视觉理解，不进入 Agent 图像通道）。

    ``data`` 为图片的 base64 编码，可含或不含 ``data:<mime>;base64,`` 前缀；
    ``media_type`` 为图片 MIME 类型（如 ``image/png``）。原始字节仅用于本轮 OCR，
    不写入历史。
    """

    media_type: str = Field(..., description="图片 MIME 类型，如 image/png")
    data: str = Field(..., description="图片的 base64 编码，可含或不含 data URL 前缀")


class ChatRequest(BaseModel):
    """单轮对话请求"""
    message: str = Field(..., description="用户输入")
    session_id: Optional[str] = Field(None, description="会话ID，未提供时由服务端生成")
    history: List[ChatMessage] = Field(default_factory=list, description="可选的历史消息，由前端传入")
    system_prompt: Optional[str] = Field(None, description="可选系统提示词，未提供时使用默认提示")
    target_device_id: Optional[str] = Field(None, description="可选的目标设备ID，用于设备联动")
    target_device_name: Optional[str] = Field(None, description="可选的目标设备名称，用于设备联动提示")
    remember: bool = Field(True, description="是否将本轮对话写入服务端内存会话")
    agent_type: Optional[str] = Field(
        None,
        description="Agent 类型：'device' 使用设备联动 Agent，None/空 使用默认通用 Agent",
    )
    images: List[ImageAttachment] = Field(
        default_factory=list,
        description="可选图片（base64）；OpenAI 直接接收原图，其他 provider 按 OCR 配置处理 / Optional images; OpenAI receives originals directly",
    )


# ─────────────────────────── Image validation ───────────────────────────
#
# Shared MIME whitelist + per-image size + per-turn count validation used by
# both the JSON and multipart chat entry points. Limits come from the OCR_*
# settings so the frontend and backend stay in lock-step (see ocr_service).

ALLOWED_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)


class ImageValidationError(Exception):
    """Raised when an image attachment violates the MIME/size/count limits.

    Carries a machine-readable ``reason`` and a human-readable ``message`` so the
    API boundary can map it to an explicit 4xx with a helpful detail.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _base64_decoded_len(payload: str) -> int:
    """Estimate the decoded byte length of a base64 string without decoding it.

    Avoids materializing a (potentially large) bytes object just to measure it,
    which keeps validation cheap and bounds memory on the reject path.
    """
    text = (payload or "").strip()
    if text.startswith("data:"):
        comma = text.find(",")
        if comma != -1:
            text = text[comma + 1 :]
    text = "".join(text.split())  # drop any embedded whitespace/newlines
    n = len(text)
    if n == 0:
        return 0
    padding = text.count("=", max(0, n - 2))
    return (n * 3) // 4 - padding


def image_limits() -> tuple[int, int]:
    from app.config import settings

    return (
        max(1, int(getattr(settings, "ocr_max_images", 6) or 6)),
        max(1, int(getattr(settings, "ocr_max_image_mb", 5) or 5)) * 1024 * 1024,
    )


def validate_image_count(count: int, *, locale: str = "zh") -> None:
    max_images, _ = image_limits()
    if count > max_images:
        raise ImageValidationError(
            "too_many_images",
            f"本轮共 {count} 张图片，超过 {max_images} 张上限，请分批发送。" if locale == "zh" else
            f"This turn contains {count} images; the limit is {max_images}. Send them in separate turns.",
        )


def validate_image(media_type: str, size: int, *, index: int, locale: str = "zh") -> None:
    _, max_bytes = image_limits()
    if media_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise ImageValidationError(
            "unsupported_type",
            f"第 {index} 张图片类型 {media_type!r} 不受支持，请转换为 PNG、JPEG、WebP 或 GIF。" if locale == "zh" else
            f"Image {index} has unsupported type {media_type!r}. Convert it to PNG, JPEG, WebP or GIF.",
        )
    if size > max_bytes:
        # 向上保留两位小数，避免刚超限的文件显示成与限额相同的大小。
        size_mb = ((size * 100 + 1024 * 1024 - 1) // (1024 * 1024)) / 100
        max_mb = max_bytes // (1024 * 1024)
        raise ImageValidationError(
            "image_too_large",
            f"第 {index} 张图片大小 {size_mb:.2f} MiB（{size} 字节），超过单图 {max_mb} MiB 上限。"
            "请压缩图片，或拆分为多张图片后发送。" if locale == "zh" else
            f"Image {index} is {size_mb:.2f} MiB ({size} bytes), exceeding the {max_mb} MiB per-image limit. "
            "Compress it or split it into smaller images.",
        )


def validate_images(images: Optional[List[ImageAttachment]], *, locale: str = "zh") -> None:
    """Validate a list of image attachments against the OCR_* limits.

    Raises :class:`ImageValidationError` on the first violation (unsupported MIME
    type, oversize image, or too many images). A ``None``/empty list is valid.
    """
    if not images:
        return
    validate_image_count(len(images), locale=locale)
    for idx, img in enumerate(images, start=1):
        media_type = (getattr(img, "media_type", "") or "").strip().lower()
        size = _base64_decoded_len(getattr(img, "data", "") or "")
        validate_image(media_type, size, index=idx, locale=locale)


def parse_images_form(raw: Optional[str], *, locale: str = "zh") -> List[ImageAttachment]:
    """Parse the multipart ``images`` form field (a JSON string) into models.

    Returns an empty list for a missing/blank field. Raises
    :class:`ImageValidationError` when the JSON is malformed or not a list of
    ``{media_type, data}`` objects, so the API boundary can return a 4xx.
    """
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise ImageValidationError(
            "invalid_images_payload", "图片附件格式无效，请重新选择图片。" if locale == "zh" else
            "Invalid image attachment data. Select the images again."
        ) from exc
    if not isinstance(parsed, list):
        raise ImageValidationError(
            "invalid_images_payload", "图片附件格式无效，应为图片数组。" if locale == "zh" else
            "Invalid image attachment data: expected an image array."
        )
    images: List[ImageAttachment] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ImageValidationError(
                "invalid_images_payload", "图片附件格式无效，应为图片数组。" if locale == "zh" else
                "Invalid image attachment data: expected an image array."
            )
        media_type = str(item.get("media_type") or "").strip()
        data = str(item.get("data") or "")
        if not media_type or not data:
            raise ImageValidationError(
                "invalid_images_payload", "图片附件缺少 media_type 或 data 字段。" if locale == "zh" else
                "Image attachment is missing media_type or data."
            )
        images.append(ImageAttachment(media_type=media_type, data=data))
    return images


class ChatResponse(BaseResponse):
    """对话响应"""
    session_id: str = Field(..., description="会话ID")
    answer: str = Field(..., description="模型回复内容")
    model: Optional[str] = Field(None, description="实际使用的模型名称")
    messages: List[ChatMessage] = Field(default_factory=list, description="包含本轮在内的对话消息")
    usage: Optional[Dict[str, Any]] = Field(None, description="可选的Token用量统计")
    suggested_agent_type: Optional[str] = Field(
        None,
        description=(
            "通用 Agent 给出的路由建议：device|log_analysis|package_search|"
            "project_expert，表示该请求更适合用对应专门 Agent；无建议时为 None"
        ),
    )
