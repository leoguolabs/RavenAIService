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


@pytest.mark.parametrize(("configured", "expected"), [
    ("http://127.0.0.1:3430/", "http://127.0.0.1:3430/"),
    ("https://magpie.example.test", "https://magpie.example.test/"),
    ("https://magpie.example.test/?k=web-key", None),
    ("https://magpie.example.test/#web-key", None),
    ("https://admin:web-key@magpie.example.test/", None),
    ("javascript:alert(1)", None),
])
def test_describe_exposes_only_a_credential_free_magpie_admin_url(monkeypatch, configured, expected):
    monkeypatch.setattr(runtime, "get_config", runtime.RuntimeConfig)
    monkeypatch.setattr(runtime.settings, "magpie_admin_url", configured)
    assert runtime.describe()["magpie_admin_url"] == expected


@pytest.mark.parametrize("patch", [{"executor": "antigravity_cli"}, {"executor": "openai_agents"},
    {"agent_overrides": {"general_agent": {"executor": "openai_agents"}}}, {"base_url": "http://user:pass@gateway"},
    {"agent_overrides": {"unknown": {}}}, {"enabled": True}, {"executor": "shell"}])
def test_invalid_runtime_configuration(patch):
    with pytest.raises(ValidationError):
        runtime.RuntimeConfig(**patch)


@pytest.mark.parametrize("agent", runtime.AGENTS)
@pytest.mark.parametrize("executor", runtime.EXECUTORS)
@pytest.mark.parametrize("model", ["raven-yinhe/yinhe-thinking", "raven-yinhe/yinhe-chat",
    "raven-deepseek/deepseek-v4-flash", "raven-openai/gpt-6.1-terra", "group/fallback"])
def test_every_agent_can_select_every_model_and_executor(monkeypatch, agent, executor, model):
    from app.agents.anthropic_client import build_options
    from app.services import model_router

    config = runtime.RuntimeConfig(enabled=True, api_key="gateway-private", model="default/model",
        agent_overrides={agent: {"executor": executor, "model": model}})
    monkeypatch.setattr(runtime, "get_config", lambda: config)
    endpoint, = model_router.candidates(agent_kind=agent)
    assert (endpoint.model, endpoint.executor, endpoint.agent_kind) == (model, executor, agent)
    options = build_options(system_prompt="test", cwd="/tmp", allowed_tools=[], endpoint=endpoint)
    assert options.model == model
    assert options.env["RAVEN_EXECUTOR"] == executor
    assert options.env["RAVEN_AGENT_KIND"] == agent


@pytest.mark.parametrize("agent", runtime.AGENTS)
def test_executor_only_override_inherits_changed_default_model(agent):
    config = runtime.RuntimeConfig(enabled=True, api_key="key", model="raven-yinhe/yinhe-thinking",
        agent_overrides={agent: {"executor": "antigravity_cli"}})
    assert config.selection(agent).model == "raven-yinhe/yinhe-thinking"
    changed = config.model_copy(update={"model": "raven-deepseek/deepseek-v4-flash"})
    assert changed.selection(agent).model == "raven-deepseek/deepseek-v4-flash"
    assert changed.selection(agent).executor == "antigravity_cli"


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


def test_switching_executor_preserves_secret_model_and_unrelated_overrides(monkeypatch):
    store = {runtime.STORE_KEY: runtime.RuntimeConfig(enabled=True, api_key="private-key", model="group/test",
        agent_overrides={"device_agent": {"executor": "antigravity_cli"}}).model_dump()}
    monkeypatch.setattr(runtime.runtime_settings_service, "get_all", lambda: store)
    monkeypatch.setattr(runtime.runtime_settings_service, "update", store.update)
    for executor in ("openai_agents", "claude_sdk", "openai_agents"):
        result = runtime.save({"executor": executor, "api_key": ""})
        assert result["executor"] == executor and result["model"] == "group/test"
        assert result["executors"]["openai_agents"] == "OpenAI Agents SDK"
        assert runtime.get_config().api_key == "private-key"
        assert runtime.get_config().selection("device_agent").executor == "antigravity_cli"
        assert runtime.get_config().selection("general_agent").executor == executor
