import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.agents.magpie_bridge import GatewayConfig, create_gateway_app
from app.services import openai_task_admission as admission


@pytest.mark.asyncio
async def test_gateway_credentials_route_headers_and_stream():
    observed = []
    routed = []

    async def upstream(request):
        observed.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream",
            "X-Magpie-Provider": "backup", "X-Magpie-Model": "backup/actual-model"},
            content=b'data: {"model":"requested-alias"}\n\n')

    async def on_route(value):
        routed.append(value)

    config = GatewayConfig("http://gateway", "upstream-secret", "group/auto", "claude_sdk", "session-1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
        app = create_gateway_app(config, "run-secret", upstream_client, on_route, admission_proof="task-proof")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://run") as client:
            assert (await client.post("/v1/messages")).status_code == 401
            assert (await client.post("/api/provider/save", headers={"x-api-key": "run-secret"})).status_code == 404
            for _ in range(2):
                response = await client.post("/v1/messages?key=run-secret", json={"model": "group/auto"},
                                             headers={admission.ADMISSION_HEADER: "spoofed"})
                assert response.status_code == 200
                assert 'requested-alias' in response.text
    assert len(routed) == 1
    assert routed[0]["provider"] == "backup"
    assert routed[0]["model"] == "backup/actual-model"
    assert routed[0]["requested_model"] == "group/auto"
    assert observed[0].headers["authorization"] == "Bearer upstream-secret"
    assert [r.headers[admission.ADMISSION_HEADER] for r in observed] == ["task-proof"] * 2
    assert "key=" not in str(observed[0].url)
    assert "upstream-secret" not in repr(config)


@pytest.mark.asyncio
async def test_task_preflight_is_shared_with_continuations_but_not_the_next_task(monkeypatch):
    requests = []
    monkeypatch.setenv("RAVEN_OPENAI_TASK_BALANCE", "true")

    async def guard(request):
        requests.append(request)
        await asyncio.sleep(0)
        if len(requests) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"admission": "task-proof"})

    def client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(guard), **kwargs)

    monkeypatch.setattr(admission, "httpx", SimpleNamespace(AsyncClient=client, HTTPError=httpx.HTTPError))
    async with admission.admission_scope():
        scope = admission.current_admission.get()
        assert await scope.prepare("key", "group/default", "title_generator") == ""
        assert not requests
        assert await asyncio.gather(*(scope.prepare("key", "group/default", "general_agent") for _ in range(2))) == [
            "task-proof", "task-proof"]
        assert await scope.prepare("key", "group/default", "title_generator") == "task-proof"
        assert len(requests) == 1
    async with admission.admission_scope():
        scope = admission.current_admission.get()
        assert await scope.prepare("key", "group/default", "general_agent") == ""
        assert await scope.prepare("key", "group/default", "general_agent") == ""
        assert len(requests) == 2
    assert admission.current_admission.get() is None
    assert all(str(r.url) == admission.ADMISSION_URL and r.headers["authorization"] == "Bearer key" for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/messages", "/v1beta/models/disabled/model:streamGenerateContent"])
async def test_disabled_provider_is_actionable_bounded_and_redacted(path):
    errors, routes = [], []

    async def upstream(request):
        return httpx.Response(404, json={"error": {"message":
            'Provider is switched off in Magpie; private-key prompt-data ' + 'x' * 20000}})

    async def on_route(value):
        routes.append(value)

    config = GatewayConfig("http://gateway", "private-key", "disabled/model", "antigravity_cli", "session")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
        app = create_gateway_app(config, "token", upstream_client, on_route, errors.append)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://run") as client:
            response = await client.post(path, headers={"x-api-key": "token"}, json={})
    assert response.status_code == 404
    assert "Selected provider is disabled" in str(errors[-1])
    assert "disabled/model" in str(errors[-1])
    assert "per-agent model override" in response.text
    assert "private-key" not in response.text and "prompt-data" not in response.text
    assert len(response.content) < 2000
    assert not routes


@pytest.mark.asyncio
async def test_successful_retry_clears_previous_gateway_error():
    errors = []
    count = 0

    async def upstream(request):
        nonlocal count
        count += 1
        return httpx.Response(429 if count == 1 else 200, content=b'{}')

    async def on_route(value):
        pass

    config = GatewayConfig("http://gateway", "key", "model", "claude_sdk", "session")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
        app = create_gateway_app(config, "token", upstream_client, on_route, errors.append)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://run") as client:
            for _ in range(2):
                await client.post("/v1/messages", headers={"x-api-key": "token"}, json={})
    assert "HTTP 429" in str(errors[0])
    assert errors[-1] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("executor", ["claude_sdk", "antigravity_cli", "openai_agents"])
@pytest.mark.parametrize("terminal", [False, True, "assistant"])
async def test_executor_preserves_gateway_error_instead_of_generic_exit(monkeypatch, executor, terminal):
    from contextlib import asynccontextmanager
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock
    from app.agents import antigravity_executor, magpie_bridge, provider_query

    @asynccontextmanager
    async def bridge(config, on_route, on_error):
        on_error(magpie_bridge.gateway_error(config, 404, b'is switched off in Magpie'))
        yield 'http://unused', 'token'

    async def run(**kwargs):
        if terminal == "assistant":
            yield AssistantMessage(content=[TextBlock(text="There's an issue with the selected model")],
                                   model="disabled/model", error="invalid_request")
        elif terminal:
            yield ResultMessage(subtype='error_during_execution', duration_ms=1, duration_api_ms=0,
                is_error=True, num_turns=0, session_id='one', errors=['generic CLI error'])
        else:
            raise RuntimeError('Antigravity failed (exit 3); no successful terminal event')

    monkeypatch.setattr(magpie_bridge, 'gateway_bridge', bridge)
    monkeypatch.setattr(provider_query, '_claude_query', run)
    monkeypatch.setattr(antigravity_executor, 'query', run)
    from app.agents import openai_agents_executor
    monkeypatch.setattr(openai_agents_executor, "query", run)
    options = ClaudeAgentOptions(cwd='/tmp', model='disabled/model', env={
        'RAVEN_GATEWAY': 'magpie', 'RAVEN_EXECUTOR': executor,
        'ANTHROPIC_BASE_URL': 'http://gateway', 'ANTHROPIC_API_KEY': 'private-key'})
    with pytest.raises(magpie_bridge.GatewayRequestError, match='Selected provider is disabled') as error:
        _ = [message async for message in provider_query.query(prompt='test', options=options)]
    assert 'HTTP 404' in str(error.value) and executor in str(error.value)
    assert 'private-key' not in str(error.value)
