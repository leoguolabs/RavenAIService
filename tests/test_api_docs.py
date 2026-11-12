"""HTTP regression coverage for documentation exposure on the actual app."""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import create_app
from app.models.database import get_db


@pytest.fixture
def build_client(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html>Raven test frontend</html>", encoding="utf-8")
    (assets / "app.js").write_text("// Raven test asset", encoding="utf-8")
    monkeypatch.setattr(settings, "frontend_dist_dir", str(dist))

    def build(environment, enabled, serve_frontend=False):
        monkeypatch.setattr(settings, "environment", environment)
        monkeypatch.setattr(settings, "api_docs_enabled", enabled)
        monkeypatch.setattr(settings, "serve_frontend", serve_frontend)
        application = create_app()

        async def no_database():
            yield None

        # Unauthenticated requests must be rejected before a database is used.
        # No lifespan is started, so these tests cannot migrate or modify app data.
        application.dependency_overrides[get_db] = no_database
        return TestClient(application)

    return build


@pytest.mark.parametrize("serve_frontend", [False, True])
@pytest.mark.parametrize(
    "environment,enabled",
    [("development", False), ("production", False), ("production", True)],
)
def test_docs_disabled_including_spa_fallback(build_client, environment, enabled, serve_frontend):
    client = build_client(environment, enabled, serve_frontend)
    for path in (
        "/openapi.json", "/openapi.json?download=1", "/openapi.json/",
        "/docs", "/docs/", "/docs/oauth2-redirect", "/redoc", "/redoc/",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 404, path
        assert "Raven test frontend" not in response.text

    if serve_frontend:
        for path in ("/", "/raven/packages"):
            response = client.get(path)
            assert response.status_code == 200
            assert "Raven test frontend" in response.text
        assert client.get("/assets/app.js").text == "// Raven test asset"


def test_development_docs_require_explicit_opt_in(build_client):
    client = build_client("development", True)
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/v1/ai-chat/chat/stream" in schema.json()["paths"]
    assert "/admin/model-settings" in schema.json()["paths"]
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "SwaggerUIBundle" in docs.text
    assert "/openapi.json" in docs.text
    redoc = client.get("/redoc")
    assert redoc.status_code == 200
    assert '<redoc spec-url="/openapi.json"' in redoc.text


@pytest.mark.parametrize("enabled", [False, True])
def test_documentation_switch_preserves_api_authentication(build_client, enabled):
    client = build_client("development", enabled)
    for path in (
        "/admin/agent-runtime", "/admin/model-settings", "/api/v1/client-ai/capabilities",
    ):
        assert client.get(path).status_code == 401, path
