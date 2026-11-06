import pytest
from pydantic import ValidationError

from app.services import agent_runtime_service as runtime


def test_config_is_redacted_and_selection_is_independent(monkeypatch):
    config = runtime.RuntimeConfig(enabled=True, api_key="private", model="group/default",
        agent_overrides={"log_analysis": {"executor": "antigravity_cli", "model": "yinhe/thinking"}})
    monkeypatch.setattr(runtime, "get_config", lambda: config)
    assert "private" not in repr(config)
    assert "api_key" not in runtime.describe()
    assert runtime.describe()["api_key_set"]
    endpoint = runtime.gateway_endpoint("log_analysis")
    assert endpoint.executor == "antigravity_cli"
    assert endpoint.model == "yinhe/thinking"
    assert runtime.gateway_endpoint("device_agent").executor == "claude_sdk"


@pytest.mark.parametrize("patch", [{"executor": "antigravity_cli"}, {"base_url": "http://user:pass@gateway"},
    {"agent_overrides": {"unknown": {}}}, {"enabled": True}, {"executor": "shell"}])
def test_invalid_runtime_configuration(patch):
    with pytest.raises(ValidationError):
        runtime.RuntimeConfig(**patch)


@pytest.mark.asyncio
async def test_bootstrap_imports_key_pool_and_refreshes_catalog(monkeypatch):
    import json
    import runpy
    from pathlib import Path
    from types import SimpleNamespace
    import httpx
    from app.services import model_router, model_settings_service

    script = runpy.run_path(str(Path(__file__).parents[2] / 'scripts/bootstrap-magpie.py'))
    saved, providers, catalog_calls = [], [], []
    endpoint = SimpleNamespace(provider='yinhe', base_url='https://fixture.invalid', api_key='first',
        model='thinking', small_fast_model='chat', profile=SimpleNamespace(api_protocol='anthropic_messages', label='Yinhe'))
    monkeypatch.setattr(model_router, '_resolve', lambda slot: endpoint if slot == model_settings_service.PRIMARY_SLOT else None)
    monkeypatch.setattr(model_settings_service, 'api_keys_for_slot', lambda slot: ['first', 'second'])
    monkeypatch.setattr(runtime, 'get_config', lambda: runtime.RuntimeConfig())
    monkeypatch.setattr(runtime, 'save', lambda values: saved.append(values))
    async def catalog():
        catalog_calls.append(True)
        return []
    monkeypatch.setattr(runtime, 'models', catalog)
    def handler(request):
        if request.url.path == '/api/providers':
            return httpx.Response(200, json=[])
        if request.url.path == '/api/provider/save':
            providers.append(json.loads(request.content))
        if request.url.path == '/api/caller-keys/add-key':
            return httpx.Response(200, json={'secret': 'caller-secret'})
        return httpx.Response(200, json={})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(handler)))
    result = await script['bootstrap']('http://gateway.invalid', 'admin-secret', 'http://gateway.invalid:3425', True)
    assert providers[0]['key'] == 'first'
    assert providers[0]['keys'] == [{'key': 'second'}]
    assert saved[0]['api_key'] == 'caller-secret'
    assert catalog_calls == [True]
    assert 'caller-secret' not in json.dumps(result)
