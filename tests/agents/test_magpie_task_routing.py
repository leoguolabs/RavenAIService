import asyncio
import json

import httpx
import pytest

from app.agents.magpie_bridge import GatewayConfig, create_gateway_app
from app.agents.magpie_task_routing import TaskRoute, output_text, retarget
from app.services import magpie_routing_policy as overload
from app.services.magpie_routing_policy import TaskRoutingPolicy


def policy(**patch):
    return TaskRoutingPolicy(enabled=True, entry_group="group/intent", degraded_group="group/cloud",
        local_model="yinhe/local", local_group="group/local", backup_model="cloud/strong",
        first_token_seconds=0.06, probe_seconds=0.06, idle_seconds=0.09,
        min_tokens_per_second=100, **patch)


def event(text):
    return ('data: ' + json.dumps({"type": "content_block_delta", "delta": {
        "type": "text_delta", "text": text}}) + '\n\n').encode()


class TimedStream(httpx.AsyncByteStream):
    def __init__(self, rows):
        self.rows, self.closed, self.finished = rows, False, False

    async def __aiter__(self):
        for delay, chunk in self.rows:
            await asyncio.sleep(delay)
            yield chunk
        self.finished = True

    async def aclose(self):
        self.closed = True


def reply(model, stream=None, status=200):
    headers = {"content-type": "text/event-stream", "x-magpie-provider": model.split('/')[0],
               "x-magpie-model": model}
    return httpx.Response(status, headers=headers, stream=stream or TimedStream([(0, event("CLOUD"))]))


@pytest.fixture(autouse=True)
def isolated_cooldown(monkeypatch):
    state = {"open": False, "trips": 0}

    async def cooling(*_):
        return state["open"]

    async def trip(*_):
        state["open"] = True
        state["trips"] += 1

    monkeypatch.setattr(overload, "cooling", cooling)
    monkeypatch.setattr(overload, "trip", trip)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1/messages", "v1/responses", "v1/chat/completions",
    "v1beta/models/group/intent:streamGenerateContent"])
async def test_task_pin_preserves_entire_tool_context(path):
    sent = []

    async def upstream(request):
        sent.append(request)
        return reply("cloud/strong")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = TaskRoute("http://gateway", client, policy())
        for n in range(2):
            body = {"model": "group/intent", "messages": [{"role": "user", "content": "request"},
                {"role": "assistant", "tool_calls": [{"id": "one", "name": "read"}]},
                {"role": "tool", "tool_call_id": "one", "content": f"evidence-{n}"}], "tools": [{"name": "read"}]}
            prepared = await route.prepare("POST", path, json.dumps(body).encode(), {}, [])
            _ = [chunk async for chunk in prepared.chunks()]
            actual = json.loads(sent[-1].content)
            assert actual["messages"] == body["messages"] and actual["tools"] == body["tools"]
        if path.startswith("v1beta"):
            assert "cloud/strong:streamGenerateContent" in sent[1].url.path
        else:
            assert json.loads(sent[1].content)["model"] == "cloud/strong"
        assert not route.lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [(0, event("x")), (0.2, event("LOCAL-LEAK"))],
    [(0, b'event: ping\ndata: {"type":"ping"}\n\n'), (0.2, event("LOCAL-LEAK"))],
])
async def test_slow_uncommitted_local_retries_once_and_stays_on_backup(rows, isolated_cooldown):
    sent, streams = [], []

    async def upstream(request):
        model = json.loads(request.content)["model"]
        sent.append(model)
        stream = TimedStream(rows if len(sent) == 1 else [(0, event("CLOUD"))])
        streams.append(stream)
        return reply("yinhe/local" if len(sent) == 1 else "cloud/strong", stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = TaskRoute("http://gateway", client, policy())
        for _ in range(2):
            prepared = await route.prepare("POST", "v1/messages", b'{"model":"group/intent"}', {}, [])
            output = b''.join([chunk async for chunk in prepared.chunks()])
            assert b'CLOUD' in output and b'LOCAL-LEAK' not in output and b'"text": "x"' not in output
    assert sent == ["group/intent", "cloud/strong", "cloud/strong"]
    assert streams[0].closed and not streams[0].finished
    assert isolated_cooldown["trips"] == 1


@pytest.mark.asyncio
async def test_slow_committed_stream_finishes_then_next_request_switches(isolated_cooldown):
    sent = []
    local = TimedStream([(0, event('L' * 100)), (0.025, event('L' * 100)),
        (0.025, event('L' * 100)), (0.025, event('L' * 100)),
        (0.15, event('LOCAL-END'))])

    async def upstream(request):
        sent.append(json.loads(request.content)["model"])
        return reply("yinhe/local", local) if len(sent) == 1 else reply("cloud/strong")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = TaskRoute("http://gateway", client, policy())
        prepared = await route.prepare("POST", "v1/messages", b'{"model":"group/intent"}', {}, [])
        assert not local.finished  # The gate did not buffer the whole response.
        output = b''.join([chunk async for chunk in prepared.chunks()])
        assert b'LOCAL-END' in output and b'CLOUD' not in output
        assert sent == ["group/intent"]  # No replay after commitment.
        isolated_cooldown["open"] = False  # Recovery must not move this task back.
        again = await route.prepare("POST", "v1/messages", b'{"model":"group/intent"}', {}, [])
        _ = [chunk async for chunk in again.chunks()]
        assert sent == ["group/intent", "cloud/strong"]


@pytest.mark.asyncio
async def test_new_task_during_cooldown_still_uses_cloud_intent_group(isolated_cooldown):
    isolated_cooldown["open"] = True
    sent = []

    async def upstream(request):
        sent.append(json.loads(request.content)["model"])
        return reply("deepseek/flash")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = TaskRoute("http://gateway", client, policy())
        for _ in range(2):
            prepared = await route.prepare("POST", "v1/messages", b'{"model":"group/intent"}', {}, [])
            _ = [chunk async for chunk in prepared.chunks()]
        assert sent == ["group/cloud", "deepseek/flash"]


@pytest.mark.asyncio
async def test_native_local_failover_is_pinned_and_cloud_errors_do_not_reroute(isolated_cooldown):
    sent = []

    async def upstream(request):
        sent.append(json.loads(request.content)["model"])
        if len(sent) == 1:
            return reply("yinhe/local", TimedStream([(0, event("brief local answer"))]))
        return reply("cloud/strong", status=503 if len(sent) == 3 else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = TaskRoute("http://gateway", client, policy())
        for _ in range(3):
            prepared = await route.prepare("POST", "v1/messages", b'{"model":"group/intent"}', {}, [])
            _ = [chunk async for chunk in prepared.chunks()]
        assert sent == ["group/intent", "group/local", "cloud/strong"]
        assert route.pinned == "cloud/strong"
        assert isolated_cooldown["trips"] == 1


@pytest.mark.asyncio
async def test_missing_route_identity_fails_without_selecting_another_model():
    async def upstream(_):
        return httpx.Response(200, content=b'unknown responder')

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = TaskRoute("http://gateway", client, policy())
        with pytest.raises(httpx.RemoteProtocolError):
            await route.prepare("POST", "v1/messages", b'{"model":"group/intent"}', {}, [])
        assert not route.pinned and not route.lock.locked()


@pytest.mark.asyncio
async def test_concurrent_requests_wait_until_chosen_response_closes():
    sent = []

    async def upstream(request):
        sent.append(json.loads(request.content)["model"])
        return reply("cloud/strong")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = TaskRoute("http://gateway", client, policy())
        first = await route.prepare("POST", "v1/messages", b'{"model":"group/intent"}', {}, [])
        pending = asyncio.create_task(route.prepare("POST", "v1/messages", b'{"model":"group/intent"}', {}, []))
        await asyncio.sleep(0.01)
        assert sent == ["group/intent"]
        await first.close()
        second = await pending
        await second.close()
        assert sent == ["group/intent", "cloud/strong"]


@pytest.mark.asyncio
async def test_cancel_gate_closes_upstream_and_releases_task():
    stream = TimedStream([(10, event("NEVER"))])

    async def upstream(_):
        return reply("yinhe/local", stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = TaskRoute("http://gateway", client, policy())
        pending = asyncio.create_task(route.prepare("POST", "v1/messages", b'{"model":"group/intent"}', {}, []))
        await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert stream.closed and not route.lock.locked()


@pytest.mark.asyncio
async def test_auxiliary_requests_and_explicit_overrides_do_not_select_task():
    sent, routes = [], []

    async def upstream(request):
        sent.append(json.loads(request.content)["model"])
        return reply("cloud/strong")

    async def on_route(row):
        routes.append(row)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
        config = GatewayConfig("http://gateway", "secret", "group/intent", "claude_sdk", "task",
            task_routing=policy())
        app = create_gateway_app(config, "token", upstream_client, on_route)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            for path in ("v1/messages/count_tokens", "v1/messages", "v1/messages"):
                assert (await client.post(path, json={"model": "group/intent"},
                    headers={"x-api-key": "token"})).is_success
        assert sent == ["group/intent", "group/intent", "cloud/strong"]
        assert routes[-1]["requested_model"] == "group/intent"
        config = GatewayConfig("http://gateway", "secret", "explicit/model", "claude_sdk", "other",
            task_routing=policy())
        app = create_gateway_app(config, "token", upstream_client, on_route)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://bridge") as client:
            await client.post("v1/messages", json={"model": "explicit/model"}, headers={"x-api-key": "token"})
        assert sent[-1] == "explicit/model"


@pytest.mark.parametrize("payload,expected", [
    ({"type": "content_block_delta", "delta": {"thinking": "reason"}}, "reason"),
    ({"type": "content_block_delta", "delta": {"partial_json": '{"x":1}'}}, '{"x":1}'),
    ({"type": "response.output_text.delta", "delta": "hello"}, "hello"),
    ({"choices": [{"delta": {"reasoning_content": "reason", "content": "answer"}}]}, "answerreason"),
    ({"candidates": [{"content": {"parts": [{"text": "gemini"}]}}]}, "gemini"),
    ({"type": "ping"}, ""),
])
def test_speed_counts_model_content_not_heartbeats(payload, expected):
    assert output_text(payload) == expected
