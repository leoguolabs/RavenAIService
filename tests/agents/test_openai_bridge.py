"""Behavioral coverage for the actual HTTP/SSE boundary used by the SDK."""

import asyncio
import json

import httpx
import pytest

from app.agents.openai_bridge import (
    OpenAIConfig, create_bridge_app, messages_response, openai_bridge, responses_request,
)


CONFIG = OpenAIConfig("https://openai.test/v1", "sk-private-test", "vision-model")


def test_full_tool_history_preserves_order_images_and_unified_model():
    name = "mcp__" + "long_tool_name_" * 6
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aW1hZ2U="}}
    body, names = responses_request({
        "model": "old-small-model", "system": [{"type": "text", "text": "system rule"}],
        "tools": [{"name": name, "input_schema": {"type": "object", "properties": {}}}],
        "messages": [
            {"role": "system", "content": "SDK runtime rule"},
            {"role": "user", "content": [{"type": "text", "text": "read screenshot"}, image]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": name, "input": {"a": 1}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": [image]}]},
        ],
    }, CONFIG)
    assert body["model"] == "vision-model" and body["store"] is False
    assert body["instructions"] == "system rule"
    assert body["input"][0]["role"] == "system"
    assert body["input"][1]["content"][1]["image_url"] == "data:image/png;base64,aW1hZ2U="
    assert body["input"][2]["type"] == "function_call"
    assert body["input"][3]["type"] == "function_call_output"
    assert body["input"][3]["output"][0]["type"] == "input_image"
    alias = body["tools"][0]["name"]
    assert len(alias) <= 64 and names[alias] == name
    response = messages_response({"output": [{"type": "function_call", "call_id": "call_2",
        "name": alias, "arguments": '{"b": 2}'}], "usage": {
            "input_tokens": 100, "input_tokens_details": {"cached_tokens": 40}, "output_tokens": 7}}, names)
    assert response["content"][0] == {"type": "tool_use", "id": "call_2", "name": name, "input": {"b": 2}}
    assert response["usage"]["input_tokens"] == 60
    assert response["usage"]["cache_read_input_tokens"] == 40
    assert response["stop_reason"] == "tool_use"


async def _request(handler, payload, *, token="local-token"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        app = create_bridge_app(CONFIG, "local-token", upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            return await client.post("/v1/messages", json=payload, headers={"x-api-key": token})


@pytest.mark.asyncio
async def test_http_auth_and_provider_errors_never_expose_secrets():
    called = []
    def handler(request):
        called.append(request)
        assert request.url == "https://openai.test/v1/responses"
        assert request.headers["authorization"] == "Bearer sk-private-test"
        return httpx.Response(429, json={"error": {"code": "credit_balance_exhausted",
            "message": "Your key sk-private-test failed; sensitive user prompt"}})
    denied = await _request(handler, {}, token="wrong")
    assert denied.status_code == 401 and called == []
    result = await _request(handler, {"messages": [{"role": "user", "content": "hello"}]})
    assert result.status_code == 429 and "credit_balance_exhausted" in result.text
    assert "sk-private-test" not in result.text and "sensitive" not in result.text


def _wire(events):
    return "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events)


@pytest.mark.asyncio
async def test_live_sse_text_tools_usage_and_terminal_match_sdk_protocol():
    final = {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "Checking"}]},
        {"type": "function_call", "call_id": "c1", "name": "Read", "arguments": '{"file_path":"/tmp/test"}'},
    ], "usage": {"input_tokens": 30, "output_tokens": 12}}
    events = [
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "Checking"},
        {"type": "response.output_item.done", "output_index": 0, "item": final["output"][0]},
        {"type": "response.output_item.added", "output_index": 1, "item": {"type": "function_call", "call_id": "c1", "name": "Read"}},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"file_path":"/tmp/test"}'},
        {"type": "response.output_item.done", "output_index": 1, "item": final["output"][1]},
        {"type": "response.completed", "response": final},
    ]
    def handler(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=_wire(events), headers={"content-type": "text/event-stream"})
    result = await _request(handler, {"messages": [{"role": "user", "content": "read"}], "stream": True})
    frames = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
    assert frames[0]["type"] == "message_start" and frames[-1]["type"] == "message_stop"
    assert [f["content_block"]["type"] for f in frames if f["type"] == "content_block_start"] == ["text", "tool_use"]
    assert len([f for f in frames if f["type"] == "content_block_stop"]) == 2
    assert frames[-2]["delta"]["stop_reason"] == "tool_use"
    assert frames[-2]["usage"]["input_tokens"] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [
    [{"type": "response.created"}],
    [{"type": "response.failed", "response": {"error": {"code": "server_error"}}}],
    [{"type": "response.output_text.delta", "delta": "partial"}],
])
async def test_missing_or_failed_terminal_never_becomes_success(events):
    result = await _request(lambda r: httpx.Response(200, content=_wire(events)),
        {"stream": True, "messages": [{"role": "user", "content": "hello"}]})
    assert '"type": "error"' in result.text
    assert "message_stop" not in result.text


@pytest.mark.asyncio
async def test_two_runs_have_distinct_loopback_auth_and_close_sockets_on_cancel():
    async with openai_bridge(CONFIG) as (url1, token1):
        async with openai_bridge(CONFIG) as (url2, token2):
            assert url1 != url2 and token1 != token2
            async with httpx.AsyncClient() as client:
                result = await client.post(url1 + "/v1/messages/count_tokens", json={}, headers={"x-api-key": token2})
                assert result.status_code == 401
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(url2)
    entered = asyncio.Event()
    state = {}
    async def run():
        async with openai_bridge(CONFIG) as (url, token):
            state["url"] = url
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(state["url"])


@pytest.mark.asyncio
async def test_reasoning_accompanies_tool_outputs_but_never_sdk_visible_text():
    requests = []
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque-private-state"}
    tool = {"type": "function_call", "call_id": "call_1", "name": "Read", "arguments": "{}"}
    def handler(request):
        requests.append(json.loads(request.content))
        output = [reasoning, tool] if len(requests) == 1 else [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]
        return httpx.Response(200, json={"status": "completed", "output": output})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        app = create_bridge_app(CONFIG, "token", upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local",
                                    headers={"x-api-key": "token"}) as client:
            first = await client.post("/v1/messages", json={"messages": [{"role": "user", "content": "read"}]})
            assert "opaque-private-state" not in first.text
            second = await client.post("/v1/messages", json={"messages": [
                {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "Read", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}]},
            ]})
            assert second.status_code == 200
    assert requests[1]["input"][0] == reasoning
    assert requests[1]["input"][1]["type"] == "function_call"
    assert requests[1]["input"][2]["type"] == "function_call_output"
