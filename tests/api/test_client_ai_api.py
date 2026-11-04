"""Contract tests for RavenClient capability delivery and usage reporting."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.anthropic_client import PROVIDER_PROFILES
from app.api import client_ai
from app.api.users import get_current_user
from app.config import settings
from app.models.database import Base, db_manager
from app.models.metrics import MetricEvent
from app.services import model_router


@pytest.fixture
def metrics_db():
    fd, path = tempfile.mkstemp(prefix="client-ai-api-", suffix=".sqlite")
    os.close(fd)

    previous_url = settings.database_url
    previous_engine = db_manager.engine
    previous_factory = db_manager.session_factory
    settings.database_url = f"sqlite+aiosqlite:///{path}"
    db_manager.initialize()

    async def _create() -> None:
        async with db_manager.engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[MetricEvent.__table__],
            )

    asyncio.run(_create())
    try:
        yield
    finally:
        asyncio.run(db_manager.close())
        settings.database_url = previous_url
        db_manager.engine = previous_engine
        db_manager.session_factory = previous_factory
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def current_user():
    return SimpleNamespace(
        id=str(uuid.uuid4()), username="desktop-user", is_active=True
    )


@pytest.fixture
def app(current_user) -> FastAPI:
    application = FastAPI()
    application.include_router(client_ai.router)
    application.dependency_overrides[get_current_user] = lambda: current_user
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _choice(
    slot: str,
    *,
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
) -> model_router.EndpointChoice:
    return model_router.EndpointChoice(
        slot=slot,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        small_fast_model=f"{model}-fast",
        profile=PROVIDER_PROFILES[provider],
    )


@pytest.fixture
def routes():
    return [
        _choice(
            "primary",
            provider="yinhe",
            api_key="primary-secret",
            base_url="https://primary.example/anthropic",
            model="primary-model",
        ),
        _choice(
            "backup",
            provider="deepseek",
            api_key="backup-secret",
            base_url="https://backup.example/anthropic",
            model="backup-model",
        ),
    ]


def test_capabilities_require_authentication() -> None:
    application = FastAPI()
    application.include_router(client_ai.router)

    async def _unauthenticated():
        raise HTTPException(status_code=401, detail="not logged in")

    application.dependency_overrides[get_current_user] = _unauthenticated
    response = TestClient(application).get("/api/v1/client-ai/capabilities")
    assert response.status_code == 401
    assert "api_key" not in response.text


def test_capabilities_preserve_route_order_and_credentials(
    client: TestClient,
    monkeypatch,
    routes,
) -> None:
    monkeypatch.setattr(model_router, "candidates", lambda **_kwargs: routes)

    response = client.get("/api/v1/client-ai/capabilities")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [route["slot"] for route in data["routes"]] == ["primary", "backup"]
    assert data["routes"][0]["api_key"] == "primary-secret"
    assert data["routes"][1]["api_key"] == "backup-secret"
    assert data["routes"][0]["base_url"] == "https://primary.example/anthropic"
    assert data["routes"][1]["model"] == "backup-model"
    assert data["expires_at"] > data["issued_at"]
    assert data["refresh_after_seconds"] < data["expires_at"] - data["issued_at"]

    assert "no-store" in response.headers["cache-control"]
    assert "private" in response.headers["cache-control"]
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"
    assert response.headers["vary"] == "Authorization"


def test_openai_route_declares_responses_protocol_and_vision(client, monkeypatch):
    choice = _choice("primary", provider="openai", api_key="project-key",
                     base_url="https://api.openai.com/v1", model="gpt-6.1-terra")
    monkeypatch.setattr(model_router, "candidates", lambda **kwargs: [choice])
    route = client.get("/api/v1/client-ai/capabilities").json()["data"]["routes"][0]
    assert route["api_protocol"] == "openai_responses"
    assert route["capabilities"]["image_input"] is True
    assert route["capabilities"]["tool_use"] is True


def test_capability_revision_changes_when_key_or_order_changes(
    client: TestClient,
    monkeypatch,
    routes,
) -> None:
    monkeypatch.setattr(model_router, "candidates", lambda **_kwargs: routes)
    first = client.get("/api/v1/client-ai/capabilities").json()["data"]["revision"]

    rotated = [
        routes[1],
        _choice(
            "primary",
            provider="yinhe",
            api_key="rotated-primary-secret",
            base_url=routes[0].base_url,
            model=routes[0].model,
        ),
    ]
    monkeypatch.setattr(model_router, "candidates", lambda **_kwargs: rotated)
    second = client.get("/api/v1/client-ai/capabilities").json()["data"]["revision"]
    assert second != first


def test_capabilities_return_503_without_usable_route(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(model_router, "candidates", lambda **_kwargs: [])
    response = client.get("/api/v1/client-ai/capabilities")
    assert response.status_code == 503
    assert "api_key" not in response.text


def _usage_payload(invocation_id: str) -> dict:
    return {
        "invocation_id": invocation_id,
        "slot": "primary",
        "provider": "yinhe",
        "model": "yinhe-thinking",
        "status": "succeeded",
        "outcome": "ok",
        "tokens": {
            "input_tokens": 100,
            "output_tokens": 25,
            "cache_read_tokens": 5,
            "cache_write_tokens": 2,
        },
        "duration_ms": 1234,
        "ttft_ms": 250,
    }


def _events() -> list[MetricEvent]:
    async def _read() -> list[MetricEvent]:
        async with db_manager.session_factory() as session:
            return list((await session.execute(select(MetricEvent))).scalars().all())

    return asyncio.run(_read())


def test_usage_is_user_attributed_idempotent_and_updates_route_health(
    client: TestClient,
    current_user,
    metrics_db,
    monkeypatch,
) -> None:
    outcomes = []
    monkeypatch.setattr(
        model_router,
        "record_outcome",
        lambda slot, *, outcome, ttft_ms=None: outcomes.append(
            (slot, outcome, ttft_ms)
        ),
    )
    invocation_id = str(uuid.uuid4())
    payload = _usage_payload(invocation_id)

    first = client.post("/api/v1/client-ai/usage", json=payload)
    duplicate = client.post("/api/v1/client-ai/usage", json=payload)
    assert first.status_code == 200, first.text
    assert duplicate.status_code == 200, duplicate.text

    rows = _events()
    assert len(rows) == 1
    event = rows[0]
    assert (
        event.idempotency_key
        == f"ai_usage:raven_client:{current_user.id}:{invocation_id}"
    )
    assert event.source == "raven_client_assistant"
    assert event.agent_kind == "assistant"
    assert event.user_id == current_user.id
    assert event.provider == "yinhe"
    assert event.model == "yinhe-thinking"
    assert event.total_tokens == 132
    assert '"endpoint_slot": "primary"' in (event.metadata_json or "")
    # Outcome feedback is safe to repeat; only fact persistence is idempotent.
    assert outcomes == [("primary", "ok", 250), ("primary", "ok", 250)]


def test_usage_rejects_conversation_content_without_persisting(
    client: TestClient,
    metrics_db,
) -> None:
    payload = _usage_payload(str(uuid.uuid4()))
    payload["prompt"] = "this must never be accepted"
    payload["api_key"] = "nor this"

    response = client.post("/api/v1/client-ai/usage", json=payload)
    assert response.status_code == 422
    assert _events() == []


def test_usage_normalizes_missing_tokens(
    client: TestClient, metrics_db, monkeypatch
) -> None:
    monkeypatch.setattr(model_router, "record_outcome", lambda *_args, **_kwargs: None)
    payload = _usage_payload(str(uuid.uuid4()))
    payload.pop("tokens")
    response = client.post("/api/v1/client-ai/usage", json=payload)
    assert response.status_code == 200, response.text
    assert _events()[0].total_tokens == 0
