"""Self-service password changes use the authenticated account's credentials."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import users as users_api
from app.config import settings
from app.exceptions import register_exception_handlers
from app.middleware.request_logging import RequestLoggingMiddleware
from app.models.database import Base, get_db
from app.services import user_service as user_service_module


AUTH = "/api/v1/users/auth"
USERNAME = "password_tester"
CURRENT_PASSWORD = "current-password"
NEW_PASSWORD = "  新密码🙂  "


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runtime_settings_path", str(tmp_path / "runtime-settings.json"))
    monkeypatch.setattr(users_api.admin_auth_manager, "list_config_users", lambda: [])
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'users.db'}")

    async def create_schema():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def get_test_db():
        async with sessions() as session:
            yield session
            await session.commit()

    application = FastAPI()
    register_exception_handlers(application)
    application.add_middleware(RequestLoggingMiddleware, include_body=True)
    application.include_router(users_api.router)
    application.dependency_overrides[get_db] = get_test_db

    with TestClient(application) as test_client:
        yield test_client

    asyncio.run(engine.dispose())


def register(client, username=USERNAME):
    response = client.post(
        f"{AUTH}/register",
        json={
            "username": username,
            "password": CURRENT_PASSWORD,
            "email": f"{username}@example.test",
        },
    )
    assert response.status_code == 201
    return response.json()["data"]


def login(client, password, username=USERNAME):
    return client.post(f"{AUTH}/login", json={"username": username, "password": password})


def change_password(client, token, current_password=CURRENT_PASSWORD, new_password=NEW_PASSWORD, **extra):
    headers = {"X-App-Locale": "en"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        f"{AUTH}/change-password",
        json={"current_password": current_password, "new_password": new_password, **extra},
        headers=headers,
    )


def test_change_password_only_updates_the_signed_in_account(client):
    account = register(client)
    other = register(client, "another_tester")
    token = account["token"]

    response = change_password(client, token, user_id=other["user"]["id"])

    assert response.status_code == 200
    assert response.json()["message"] == "Password changed"
    assert NEW_PASSWORD not in response.text
    assert login(client, CURRENT_PASSWORD).status_code == 401
    assert login(client, NEW_PASSWORD.strip()).status_code == 401
    assert login(client, NEW_PASSWORD).status_code == 200
    assert login(client, CURRENT_PASSWORD, "another_tester").status_code == 200
    assert client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


@pytest.mark.parametrize(
    ("current_password", "new_password", "status_code"),
    [
        ("wrong-password", NEW_PASSWORD, 400),
        (CURRENT_PASSWORD, CURRENT_PASSWORD, 400),
        ("", NEW_PASSWORD, 422),
        (CURRENT_PASSWORD, "short", 422),
        (CURRENT_PASSWORD, "x" * 257, 422),
    ],
)
def test_rejected_changes_preserve_current_password(client, current_password, new_password, status_code):
    account = register(client)

    response = change_password(client, account["token"], current_password, new_password)

    assert response.status_code == status_code
    assert login(client, CURRENT_PASSWORD).status_code == 200


@pytest.mark.parametrize("token", [None, "invalid-token"])
def test_change_requires_a_user_session(client, token):
    assert change_password(client, token).status_code == 401


def test_validation_errors_and_debug_logs_do_not_expose_passwords(client, caplog):
    token = register(client)["token"]
    caplog.set_level(logging.DEBUG, logger="app.middleware.request_logging")
    rejected_password = "private-password-marker" * 12

    response = change_password(client, token, new_password=rejected_password)

    assert response.status_code == 422
    assert "Request started" in caplog.text
    assert "current-password" not in caplog.text + response.text
    assert "private-password-marker" not in caplog.text + response.text


def test_concurrent_changes_cannot_both_use_the_same_current_password(client, monkeypatch):
    token = register(client)["token"]
    barrier = Barrier(2)
    hash_password = user_service_module.hash_password

    def hash_concurrently(password):
        # Both requests reach hashing only after verifying the same old credential.
        barrier.wait(timeout=5)
        return hash_password(password)

    monkeypatch.setattr(user_service_module, "hash_password", hash_concurrently)

    passwords = [NEW_PASSWORD, "another-new-password"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda password: change_password(client, token, new_password=password), passwords))

    assert sorted(response.status_code for response in responses) == [200, 409]
    for password, response in zip(passwords, responses, strict=True):
        assert login(client, password).status_code == (200 if response.is_success else 401)
