import asyncio
import hashlib
import json
from decimal import Decimal

import httpx
import pytest
from starlette.requests import Request

from app.services.openai_balance_guard import BALANCE_URL, create_app


KEY = "fixture-inference-key"
AUTH = {"Authorization": "Bearer " + KEY}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"authorization": "Bearer fixture-billing-session", "organization": "org-fixture",
        "api_key_sha256": hashlib.sha256(KEY.encode()).hexdigest()}))
    return path


class Stream(httpx.AsyncByteStream):
    def __init__(self, hold=False):
        self.closed = False
        self.hold = hold

    async def __aiter__(self):
        yield b'data: {"delta":"first"}\n\n'
        if self.hold:
            await asyncio.Event().wait()
        yield b'data: [DONE]\n\n'

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("amount,allowed", [("14.99", False), ("14.99999999", False),
    ("15", True), ("15.00000001", True), ("85.62747525", True), ("-1", False)])
async def test_real_balance_boundary_and_separate_credentials(source, amount, allowed):
    sent = []
    stream = Stream()
    body = b'{"model":"gpt-fixture","messages":[{"role":"tool","content":"evidence"}],"stream":true}'

    async def upstream(request):
        sent.append(request)
        if str(request.url) == BALANCE_URL:
            assert request.headers["authorization"] == "Bearer fixture-billing-session"
            assert request.headers["openai-organization"] == "org-fixture"
            assert request.headers["user-agent"].startswith("python-httpx/")
            assert "cookie" not in request.headers
            return httpx.Response(200, content='{"total_available":' + amount + '}',
                headers={"Set-Cookie": "billing_session=private; Path=/; Secure"})
        assert request.headers["authorization"] == AUTH["Authorization"]
        assert request.headers["openai-organization"] == "org-fixture"
        assert "cookie" not in request.headers
        assert await request.aread() == body
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external:
        app = create_app(source, client=external)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://guard") as client:
            response = await client.post("/v1/responses?test=one", content=body, headers={
                **AUTH, "OpenAI-Organization": "org-wrong", "Content-Type": "application/json"})
            assert response.status_code == (200 if allowed else 402)
            assert len(sent) == (2 if allowed else 1)
            if allowed:
                assert sent[-1].url == "https://api.openai.com/v1/responses?test=one"
                assert response.content == b'data: {"delta":"first"}\n\ndata: [DONE]\n\n'
                assert stream.closed


@pytest.mark.parametrize("payload,status", [
    (b'{"total_available":null}', 200), (b'{}', 200), (b'[]', 200),
    (b'{"total_available":true}', 200), (b'{"total_available":"85"}', 200),
    (b'{"total_available":NaN}', 200), (b'{"total_available":1e999999}', 200),
    (b'{"total_available":85,"currency":"cny"}', 200), (b'not json', 200),
    (b'{"secret":"do not leak"}', 401), (b'bad gateway', 503),
    (b'', 302),
])
async def test_unknown_balance_never_forwards_or_leaks_source(source, payload, status):
    calls = []

    async def upstream(request):
        calls.append(str(request.url))
        return httpx.Response(status, content=payload, headers={
            "Location": "https://other.invalid", "Retry-After": "60"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external:
        app = create_app(source, client=external)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://guard") as client:
            response = await client.post("/v1/responses", json={}, headers=AUTH)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "verification_unavailable"
            assert "do not leak" not in response.text and "fixture-billing" not in response.text
            assert calls == [BALANCE_URL]


async def test_timeout_stops_request(source):
    calls = []

    async def upstream(request):
        calls.append(str(request.url))
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external:
        app = create_app(source, client=external, balance_timeout=0.02)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://guard") as client:
            assert (await client.post("/v1/responses", json={}, headers=AUTH)).status_code == 503
            assert calls == [BALANCE_URL]


async def test_no_cached_allowance_after_error_or_missing_secret(source):
    reads, calls = [], []

    async def upstream(request):
        if str(request.url) == BALANCE_URL:
            reads.append(request.headers["authorization"])
            return httpx.Response(200, json={"total_available": 85}) if len(reads) == 1 else httpx.Response(401)
        calls.append(request)
        return httpx.Response(200, stream=Stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external:
        app = create_app(source, client=external)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://guard") as client:
            assert (await client.post("/v1/responses", json={}, headers=AUTH)).status_code == 200
            data = json.loads(source.read_text())
            data["authorization"] = "Bearer replaced-session"
            source.write_text(json.dumps(data))
            assert (await client.post("/v1/responses", json={}, headers=AUTH)).status_code == 503
            source.unlink()
            assert (await client.post("/v1/responses", json={}, headers=AUTH)).status_code == 503
            assert len(calls) == 1 and reads[-1] == "Bearer replaced-session"


@pytest.mark.parametrize("recover", [True, False])
async def test_rate_limited_balance_retries_before_inference(source, recover, caplog):
    calls = []

    async def upstream(request):
        calls.append(str(request.url))
        if str(request.url) != BALANCE_URL:
            return httpx.Response(200, stream=Stream())
        if recover and len(calls) == 2:
            return httpx.Response(200, json={"total_available": 15})
        return httpx.Response(429, content=b"private upstream detail", headers={"Retry-After": "0"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external:
        app = create_app(source, client=external)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://guard") as client:
            response = await client.post("/v1/responses?test=retry", json={}, headers=AUTH)
            assert response.status_code == (200 if recover else 503)
            assert calls[:2] == [BALANCE_URL, BALANCE_URL]
            assert calls[2:] == (["https://api.openai.com/v1/responses?test=retry"] if recover else [])
            assert "Retrying OpenAI balance verification (http_429)" in caplog.text
            assert "private upstream detail" not in caplog.text + response.text


async def test_retries_a_dropped_balance_connection(source):
    attempts = 0

    async def upstream(request):
        nonlocal attempts
        assert str(request.url) == BALANCE_URL
        attempts += 1
        if attempts == 1:
            raise httpx.RemoteProtocolError("private transport detail")
        return httpx.Response(200, json={"total_available": 15})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external:
        app = create_app(source, client=external)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://guard") as client:
            assert (await client.get("/balance", headers=AUTH)).status_code == 200
    assert attempts == 2


async def test_rate_limit_window_skips_queries_for_the_same_credential(source):
    reads = []

    async def upstream(request):
        assert str(request.url) == BALANCE_URL
        reads.append(request.headers["authorization"])
        if len(reads) == 1:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json={"total_available": 15})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external:
        app = create_app(source, client=external)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://guard") as client:
            assert (await client.post("/v1/responses", json={}, headers=AUTH)).status_code == 503
            assert (await client.get("/balance", headers=AUTH)).status_code == 503
            assert (await client.get("/health")).json()["retry_after_seconds"] > 0
            assert reads == ["Bearer fixture-billing-session"]
            data = json.loads(source.read_text())
            data["authorization"] = "Bearer replaced-session"
            source.write_text(json.dumps(data))
            assert (await client.get("/balance", headers=AUTH)).status_code == 200
            assert reads == ["Bearer fixture-billing-session", "Bearer replaced-session"]


async def test_wrong_caller_never_contacts_balance_or_inference(source):
    async def upstream(request):
        pytest.fail("Unauthorized caller reached upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external:
        app = create_app(source, client=external)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://guard") as client:
            for path in ("/v1/responses", "/balance"):
                assert (await client.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401
            assert (await client.get("/health")).status_code == 200


async def test_cancelled_response_closes_upstream(source):
    stream = Stream(hold=True)

    async def upstream(request):
        if str(request.url) == BALANCE_URL:
            return httpx.Response(200, json={"total_available": 85})
        await request.aread()
        return httpx.Response(200, stream=stream)

    async def receive():
        return {"type": "http.request", "body": b'{}', "more_body": False}

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external:
        app = create_app(source, client=external)
        endpoint = next(r.endpoint for r in app.routes if r.path == "/v1/{path:path}")
        request = Request({"type": "http", "method": "POST", "path": "/v1/responses",
            "query_string": b'', "headers": [(b'authorization', AUTH['Authorization'].encode())]}, receive)
        response = await endpoint(request, "responses")
        iterator = response.body_iterator
        assert b'first' in await anext(iterator)
        task = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed


async def test_balance_display_is_actual_even_when_below_floor(source):
    async def upstream(request):
        assert str(request.url) == BALANCE_URL
        return httpx.Response(200, json={"total_available": 14})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external:
        app = create_app(source, client=external)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://guard") as client:
            result = (await client.get("/balance", headers=AUTH)).json()
            assert result["balance"] == 14 and result["allowed"] is False


def test_invalid_floor_is_not_silently_disabled(source):
    for value in ("NaN", "Infinity", "-1"):
        with pytest.raises(ValueError):
            create_app(source, minimum=Decimal(value))
