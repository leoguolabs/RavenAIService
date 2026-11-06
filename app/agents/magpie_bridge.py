"""Per-run authenticated transport to Magpie, with authoritative route events.

No upstream routing is implemented here. Magpie chooses the provider and this
bridge observes its response headers, including changes between tool turns.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.agents.openai_bridge import _LoopbackServer


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    executor: str
    session_id: str
    agent_kind: str = ""
    timeout_seconds: float = 3600


def create_gateway_app(config: GatewayConfig, token: str, client: httpx.AsyncClient,
                       on_route: Callable[[dict], Awaitable[None]]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    last_route: tuple[str, str] | None = None

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def forward(path: str, request: Request):
        nonlocal last_route
        credential = (request.headers.get("x-api-key") or request.headers.get("x-goog-api-key")
                      or request.headers.get("authorization", "").removeprefix("Bearer ")
                      or request.query_params.get("key", ""))
        if not hmac.compare_digest(credential, token):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if any(segment in {".", ".."} for segment in path.split("/")):
            return JSONResponse({"error": "Unsupported inference route"}, status_code=404)
        if path not in {"v1/messages", "v1/messages/count_tokens", "v1/models", "v1/responses", "v1/chat/completions"} and not path.startswith("v1beta/models/"):
            return JSONResponse({"error": "Unsupported inference route"}, status_code=404)
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "x-api-key": config.api_key,
            "x-goog-api-key": config.api_key,
            "Content-Type": "application/json",
            "User-Agent": f"RavenAI/{config.executor}",
            "X-Magpie-Agent": f"raven-{config.agent_kind or config.executor}",
            "X-Magpie-Session": config.session_id,
        }
        for name in ("anthropic-version", "anthropic-beta", "accept"):
            if name in request.headers:
                headers[name] = request.headers[name]
        params = [(key, value) for key, value in request.query_params.multi_items() if key != "key"]
        outgoing = client.build_request(request.method, f"{config.base_url.rstrip('/')}/{path}",
            content=await request.body(), headers=headers, params=params)
        try:
            response = await client.send(outgoing, stream=True)
        except httpx.HTTPError:
            return JSONResponse({"error": {"type": "gateway_unavailable", "message": "Magpie unavailable / Magpie 不可用"}}, status_code=502)
        provider = response.headers.get("x-magpie-provider", "")
        model = response.headers.get("x-magpie-model", "")
        if response.is_success and provider and model and (provider, model) != last_route:
            last_route = (provider, model)
            await on_route({"executor": config.executor, "provider": provider, "model": model,
                      "requested_model": config.model, "agent_key": config.agent_kind})

        async def chunks():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        safe_headers = {key: value for key, value in response.headers.items() if key.lower() in {
            "content-type", "cache-control", "x-magpie-provider", "x-magpie-model", "retry-after",
        }}
        return StreamingResponse(chunks(), status_code=response.status_code, headers=safe_headers)

    return app


@asynccontextmanager
async def gateway_bridge(config: GatewayConfig, on_route: Callable[[dict], Awaitable[None]]):
    token = secrets.token_urlsafe(32)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    async with httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds, connect=15)) as client:
        server = _LoopbackServer(uvicorn.Config(create_gateway_app(config, token, client, on_route),
            log_level="error", access_log=False, lifespan="off", timeout_graceful_shutdown=2))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        task.result()
                        raise RuntimeError("Magpie bridge failed to start")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}", token
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            finally:
                sock.close()
