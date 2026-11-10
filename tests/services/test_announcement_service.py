from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services import announcement_service, runtime_settings_service


@pytest.fixture(autouse=True)
def isolated_runtime_settings(tmp_path, monkeypatch):
    path = tmp_path / "runtime-settings.json"
    monkeypatch.setattr(settings, "runtime_settings_path", str(path))
    monkeypatch.setattr(runtime_settings_service, "_CACHE", None)
    monkeypatch.setattr(runtime_settings_service, "_CACHE_MTIME", 0.0)
    monkeypatch.setattr(runtime_settings_service, "_CACHE_PATH", None)
    yield path
    runtime_settings_service._CACHE = None
    runtime_settings_service._CACHE_MTIME = 0.0
    runtime_settings_service._CACHE_PATH = None


def test_publish_replaces_current_with_new_id(isolated_runtime_settings) -> None:
    first = announcement_service.publish(
        title=" First notice ",
        content=" First body ",
        published_by="admin",
    )
    second = announcement_service.publish(
        title="Second notice",
        content="Second body",
        published_by="admin",
    )

    assert first.id != second.id
    assert second.title == "Second notice"
    assert announcement_service.get_current() == second

    persisted = json.loads(isolated_runtime_settings.read_text(encoding="utf-8"))
    assert persisted["system_announcement"]["id"] == second.id


def test_deactivate_hides_user_facing_current() -> None:
    published = announcement_service.publish(
        title="Maintenance",
        content="Tonight",
        published_by="admin",
    )

    inactive = announcement_service.deactivate()

    assert inactive is not None
    assert inactive.id == published.id
    assert inactive.active is False
    assert announcement_service.get_current(include_inactive=False) is None
    assert announcement_service.get_current(include_inactive=True) == inactive


def test_invalid_runtime_value_is_treated_as_no_announcement(
    isolated_runtime_settings,
) -> None:
    isolated_runtime_settings.write_text(
        json.dumps({"system_announcement": {"title": "missing fields"}}),
        encoding="utf-8",
    )
    runtime_settings_service._CACHE = None

    assert announcement_service.get_current() is None


def test_legacy_announcement_defaults_to_classic_text(isolated_runtime_settings) -> None:
    original = {
        "id": "legacy-id",
        "title": "Maintenance",
        "content": "Tonight",
        "published_at": "2026-07-14T00:00:00Z",
        "published_by": "admin",
        "active": True,
    }
    isolated_runtime_settings.write_text(
        json.dumps({"system_announcement": original}), encoding="utf-8"
    )

    current = announcement_service.get_current()

    assert current is not None
    assert current.id == "legacy-id"
    assert current.layout == "text"
    assert current.cover_image is None
    assert current.fireworks is False
