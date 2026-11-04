"""Original screenshots enter the main model without separate OCR."""

import base64
import json

import pytest

from app.agents.provider_query import workspace_image_blocks
from app.models.chat import ImageAttachment
from app.services import chat_image_store, ocr_service


@pytest.mark.asyncio
async def test_openai_bypasses_even_configured_ocr_and_uses_original_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.anthropic_provider", "openai")
    monkeypatch.setattr("app.config.settings.ocr_enabled", True)
    monkeypatch.setattr("app.config.settings.chat_image_workspace_materialize", False)
    monkeypatch.setattr(chat_image_store, "_base_dir", lambda: tmp_path / "store")
    async def forbidden(*args, **kwargs):
        raise AssertionError("A separate OCR model must never be called")
    monkeypatch.setattr(ocr_service, "extract_text", forbidden)
    image = ImageAttachment(media_type="image/png", data=base64.b64encode(b"original image bytes").decode())
    text, meta = await ocr_service.enrich_message("question", [image])
    assert text == "question" and meta.status == "native"
    stored = chat_image_store.save_turn_images([image], session_id="session")
    chat_image_store.materialize_session_images("session", chat_image_store.to_meta_json(stored), tmp_path / "run")
    blocks = workspace_image_blocks(str(tmp_path / "run"))
    assert base64.b64decode(blocks[0]["source"]["data"]) == b"original image bytes"


def test_image_manifest_refuses_escape_symlinks_and_missing_originals(tmp_path):
    directory = tmp_path / "images"
    directory.mkdir()
    manifest = directory / "manifest.json"
    for filename in ("../outside.png", "missing.png"):
        manifest.write_text(json.dumps([{"file": filename, "media_type": "image/png"}]))
        with pytest.raises(ValueError):
            workspace_image_blocks(str(tmp_path))
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"private")
    (directory / "link.png").symlink_to(outside)
    manifest.write_text(json.dumps([{"file": "link.png", "media_type": "image/png"}]))
    with pytest.raises(ValueError):
        workspace_image_blocks(str(tmp_path))


def test_native_images_fail_explicitly_on_missing_source_or_bad_storage(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.anthropic_provider", "openai")
    missing = chat_image_store.StoredImage(id="x", media_type="image/png", name="x.png",
                                          size=1, path=str(tmp_path / "missing.png"))
    with pytest.raises(ValueError, match="Original image unavailable"):
        chat_image_store.materialize_into_workspace([missing], tmp_path / "run")
    monkeypatch.setattr(chat_image_store, "_base_dir", lambda: tmp_path / "not-a-directory")
    (tmp_path / "not-a-directory").write_text("blocked")
    with pytest.raises(ValueError, match="Unable to store"):
        chat_image_store.save_turn_images([ImageAttachment(media_type="image/png", data="aGk=")], session_id="s")
