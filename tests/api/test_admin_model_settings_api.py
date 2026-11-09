"""HTTP-level tests for the Admin model-settings routes.

The service-level tests already cover validation. What only shows up over real
HTTP is the request-model layer: FastAPI silently drops body keys the pydantic
model does not declare, so a setting can be accepted with 200 and then never
persist. That failure is invisible from the service side and looks, to an
admin, like a form that "won't save".
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin as admin_api
from app.config import settings
from app.services import model_settings_service as mss
from app.services import runtime_settings_service


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    store = tmp_path / "runtime_settings.json"
    monkeypatch.setattr(settings, "runtime_settings_path", str(store), raising=False)
    monkeypatch.setattr(runtime_settings_service, "_CACHE", None, raising=False)
    monkeypatch.setattr(runtime_settings_service, "_CACHE_MTIME", 0.0, raising=False)
    monkeypatch.setattr(runtime_settings_service, "_CACHE_PATH", None, raising=False)

    application = FastAPI()
    application.include_router(admin_api.router)
    application.dependency_overrides[admin_api.require_admin] = lambda: "admin"

    with TestClient(application) as test_client:
        yield test_client

    runtime_settings_service._CACHE = None
    runtime_settings_service._CACHE_MTIME = 0.0
    runtime_settings_service._CACHE_PATH = None


def test_router_policy_round_trips_over_http(client):
    """A saved threshold must come back as an override, not silently vanish."""
    resp = client.put(
        "/admin/model-settings",
        json={
            "model_router_first_token_deadline_ms": 15_000,
            "model_router_cooldown_seconds": 300,
        },
    )
    assert resp.status_code == 200, resp.text

    fields = resp.json()["data"]["fields"]
    assert fields["model_router_first_token_deadline_ms"]["value"] == 15_000
    assert fields["model_router_first_token_deadline_ms"]["source"] == "override"
    assert fields["model_router_cooldown_seconds"]["value"] == 300
    # The overlay is what the agents actually read.
    assert settings.model_router_first_token_deadline_ms == 15_000


def test_agent_runtime_round_trip_redacts_secret_and_rejects_invalid_executor(client):
    response = client.put('/admin/agent-runtime', json={
        'enabled': True, 'model': 'group/default', 'api_key': 'private-gateway-key',
        'agent_overrides': {'project_expert': {'executor': 'antigravity_cli'}},
    })
    assert response.status_code == 200
    assert 'private-gateway-key' not in response.text
    assert response.json()['data']['api_key_set']
    response = client.put('/admin/agent-runtime', json={'executor': 'unknown'})
    assert response.status_code == 400
    assert 'private-gateway-key' not in response.text
    response = client.put('/admin/agent-runtime', json={'api_key': '', 'model': 'provider/next'})
    assert response.status_code == 200
    assert response.json()['data']['api_key_set']
    assert response.json()['data']['agent_overrides']['project_expert']['executor'] == 'antigravity_cli'


def test_agent_runtime_requires_admin():
    application = FastAPI()
    application.include_router(admin_api.router)
    with TestClient(application) as client:
        for method, path in [('get', '/admin/agent-runtime'), ('get', '/admin/agent-runtime/models'),
                             ('put', '/admin/agent-runtime')]:
            response = getattr(client, method)(path)
            assert response.status_code in (401, 403)


def test_task_routing_policy_round_trip_and_validation(client):
    policy = {'enabled': True, 'entry_group': 'group/intent', 'degraded_group': 'group/cloud',
        'local_group': 'group/local', 'local_model': 'yinhe/model', 'backup_model': 'cloud/model',
        'min_tokens_per_second': 12}
    response = client.put('/admin/agent-runtime', json={'task_routing': policy})
    assert response.status_code == 200, response.text
    assert response.json()['data']['task_routing']['min_tokens_per_second'] == 12
    response = client.put('/admin/agent-runtime', json={'task_routing': {**policy, 'probe_seconds': 0}})
    assert response.status_code == 400
    # Updating an unrelated setting does not silently disable the task policy.
    response = client.put('/admin/agent-runtime', json={'model': 'group/intent'})
    assert response.json()['data']['task_routing']['enabled']


def test_router_policy_is_exposed_as_its_own_group(client):
    """The Admin form renders by group; a mislabelled field lands nowhere."""
    fields = client.get("/admin/model-settings").json()["data"]["fields"]

    grouped = {key for key, entry in fields.items() if entry["group"] == "model_router"}
    assert grouped == {spec.key for spec in mss._SPECS if spec.group == "model_router"}


def test_invalid_policy_is_rejected_with_400(client):
    """Bad combinations must fail loudly rather than persist half-applied."""
    resp = client.put(
        "/admin/model-settings",
        json={"model_router_window_size": 4, "model_router_trip_threshold": 9},
    )

    assert resp.status_code == 400
    assert "熔断器永远不会跳闸" in resp.text
    # Nothing from the rejected payload may have been written.
    assert "model_router_window_size" not in runtime_settings_service.get_all()


def test_primary_key_pool_round_trips_as_count_only(client):
    keys = ["sk-http-a", "sk-http-b", "sk-http-c"]
    resp = client.put(
        "/admin/model-settings",
        json={"anthropic_api_keys": keys},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    field = body["data"]["fields"]["anthropic_api_keys"]
    assert field["is_set"] is True
    assert field["count"] == 3
    assert "value" not in field
    assert all(key not in resp.text for key in keys)
    assert runtime_settings_service.get_all()["anthropic_api_keys"] == keys
