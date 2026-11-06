import httpx
import pytest

from app.agents.magpie_bridge import GatewayConfig, create_gateway_app


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
        app = create_gateway_app(config, "run-secret", upstream_client, on_route)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://run") as client:
            assert (await client.post("/v1/messages")).status_code == 401
            assert (await client.post("/api/provider/save", headers={"x-api-key": "run-secret"})).status_code == 404
            for _ in range(2):
                response = await client.post("/v1/messages?key=run-secret", json={"model": "group/auto"})
                assert response.status_code == 200
                assert 'requested-alias' in response.text
    assert len(routed) == 1
    assert routed[0]["provider"] == "backup"
    assert routed[0]["model"] == "backup/actual-model"
    assert routed[0]["requested_model"] == "group/auto"
    assert observed[0].headers["authorization"] == "Bearer upstream-secret"
    assert "key=" not in str(observed[0].url)
    assert "upstream-secret" not in repr(config)
