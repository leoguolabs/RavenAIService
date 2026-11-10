from __future__ import annotations

import asyncio
import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import announcements as announcements_api
from app.config import settings
from app.models.announcement import MAX_COVER_BYTES
from app.models.database import Base, get_db
from app.models.user import User
from app.security.user_auth import user_auth_manager
from app.services import runtime_settings_service


PNG_COVER = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    runtime_path = tmp_path / "runtime-settings.json"
    monkeypatch.setattr(settings, "runtime_settings_path", str(runtime_path))
    monkeypatch.setattr(runtime_settings_service, "_CACHE", None)
    monkeypatch.setattr(runtime_settings_service, "_CACHE_MTIME", 0.0)
    monkeypatch.setattr(runtime_settings_service, "_CACHE_PATH", None)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'announcements.db'}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    state: dict[str, str] = {}

    async def _seed() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all(
                [
                    User(
                        id="admin-user-id",
                        username="admin",
                        display_name="Admin",
                        password_hash="x",
                        role="admin",
                    ),
                    User(
                        id="alice-user-id",
                        username="alice",
                        display_name="Alice",
                        password_hash="x",
                        role="user",
                    ),
                    User(
                        id="bob-user-id",
                        username="bob",
                        display_name="Bob",
                        password_hash="x",
                        role="user",
                    ),
                ]
            )
            await session.commit()
        for key, user_id, username in (
            ("admin", "admin-user-id", "admin"),
            ("alice", "alice-user-id", "alice"),
            ("bob", "bob-user-id", "bob"),
        ):
            state[key] = user_auth_manager.issue_token(user_id, username)[0]

    asyncio.run(_seed())

    async def _get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    application = FastAPI()
    application.include_router(announcements_api.admin_router)
    application.include_router(announcements_api.user_router)
    application.dependency_overrides[get_db] = _get_db

    with TestClient(application) as test_client:
        test_client.token_state = state
        yield test_client

    asyncio.run(engine.dispose())


def _headers(client: TestClient, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {client.token_state[key]}"}


def _publish(client: TestClient, title: str = "Maintenance") -> dict:
    response = client.put(
        "/admin/announcements/current",
        json={"title": title, "content": "The service will restart tonight."},
        headers=_headers(client, "admin"),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_global_admin_publishes_and_regular_user_is_rejected(client: TestClient) -> None:
    response = client.put(
        "/admin/announcements/current",
        json={"title": "Notice", "content": "Body"},
        headers=_headers(client, "alice"),
    )
    assert response.status_code == 403

    announcement = _publish(client)
    assert announcement["active"] is True
    assert announcement["published_by"] == "admin"


def test_dismissal_is_once_per_user_and_new_publish_resets_eligibility(
    client: TestClient,
) -> None:
    first = _publish(client, "First")

    alice_pending = client.get(
        "/api/v1/announcements/pending", headers=_headers(client, "alice")
    )
    bob_pending = client.get(
        "/api/v1/announcements/pending", headers=_headers(client, "bob")
    )
    assert alice_pending.json()["data"]["id"] == first["id"]
    assert bob_pending.json()["data"]["id"] == first["id"]

    dismissed = client.post(
        f"/api/v1/announcements/{first['id']}/dismiss",
        headers=_headers(client, "alice"),
    )
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["data"]["dismissed"] is True

    assert client.get(
        "/api/v1/announcements/pending", headers=_headers(client, "alice")
    ).json()["data"] is None
    assert client.get(
        "/api/v1/announcements/pending", headers=_headers(client, "bob")
    ).json()["data"]["id"] == first["id"]

    second = _publish(client, "Second")
    assert second["id"] != first["id"]
    assert client.get(
        "/api/v1/announcements/pending", headers=_headers(client, "alice")
    ).json()["data"]["id"] == second["id"]


def test_stale_dismissal_conflicts_without_acknowledging_new_announcement(
    client: TestClient,
) -> None:
    first = _publish(client, "First")
    second = _publish(client, "Second")

    response = client.post(
        f"/api/v1/announcements/{first['id']}/dismiss",
        headers=_headers(client, "alice"),
    )

    assert response.status_code == 409
    pending = client.get(
        "/api/v1/announcements/pending", headers=_headers(client, "alice")
    )
    assert pending.json()["data"]["id"] == second["id"]


def test_deactivation_hides_announcement_and_user_endpoints_require_auth(
    client: TestClient,
) -> None:
    _publish(client)
    stopped = client.delete(
        "/admin/announcements/current", headers=_headers(client, "admin")
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["data"]["active"] is False

    pending = client.get(
        "/api/v1/announcements/pending", headers=_headers(client, "alice")
    )
    assert pending.json()["data"] is None
    assert client.get("/api/v1/announcements/pending").status_code == 401


def test_cover_round_trip_and_switch_back_to_text(client: TestClient) -> None:
    cover = {
        "title": "Release",
        "content": "**Available now**",
        "layout": "cover",
        "cover_image": PNG_COVER,
        "cover_alt": " Release artwork ",
        "fireworks": True,
    }
    admin = _headers(client, "admin")
    user = _headers(client, "alice")
    response = client.put("/admin/announcements/current", json=cover, headers=admin)
    assert response.status_code == 200, response.text
    published = response.json()["data"]
    assert published["cover_alt"] == "Release artwork"
    assert published["cover_image"] == PNG_COVER
    assert published["fireworks"] is True

    runtime_settings_service._CACHE = None
    assert client.get("/admin/announcements/current", headers=admin).json()["data"] == published
    assert client.get("/api/v1/announcements/pending", headers=user).json()["data"] == published
    assert client.post(f"/api/v1/announcements/{published['id']}/dismiss", headers=user).status_code == 200
    assert client.get("/api/v1/announcements/pending", headers=user).json()["data"] is None

    response = client.put("/admin/announcements/current", json={**cover, "layout": "text"}, headers=admin)
    assert response.status_code == 200, response.text
    classic = client.get("/api/v1/announcements/pending", headers=user).json()["data"]
    assert classic["id"] != published["id"]
    assert classic["layout"] == "text"
    assert classic["cover_image"] is None
    assert classic["cover_alt"] == ""
    assert classic["fireworks"] is False


@pytest.mark.parametrize(
    "cover_image",
    [
        None,
        "https://example.invalid/cover.png",
        "data:image/png;base64,!!!",
        PNG_COVER.replace("image/png", "image/jpeg"),
    ],
)
def test_invalid_cover_keeps_current_announcement(client: TestClient, cover_image: str | None) -> None:
    original = _publish(client)
    headers = _headers(client, "admin")
    response = client.put(
        "/admin/announcements/current",
        json={"title": "Release", "content": "Body", "layout": "cover", "cover_image": cover_image},
        headers=headers,
    )
    assert response.status_code == 422
    assert client.get("/admin/announcements/current", headers=headers).json()["data"] == original


def test_rejects_cover_over_decoded_size_limit(client: TestClient) -> None:
    oversized = base64.b64encode(b"x" * (MAX_COVER_BYTES + 1)).decode("ascii")
    response = client.put(
        "/admin/announcements/current",
        json={
            "title": "Release",
            "content": "Body",
            "layout": "cover",
            "cover_image": f"data:image/png;base64,{oversized}",
        },
        headers=_headers(client, "admin"),
    )
    assert response.status_code == 422
    assert "2 MiB" in response.json()["detail"]
