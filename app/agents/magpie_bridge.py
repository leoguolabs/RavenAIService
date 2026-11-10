"""Per-run authenticated transport to Magpie, with authoritative route events.

Magpie owns initial selection and provider protocols. An opt-in task policy pins
its actual choice and moves a slow local model to a cloud backup without ever
restarting the Agent or replaying committed inference.
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
from app.agents.magpie_task_routing import TaskRoute, inference_path
from app.services.magpie_routing_policy import TaskRoutingPolicy
from app.services.openai_task_admission import ADMISSION_HEADER, TaskAdmission, current_admission


class GatewayRequestError(RuntimeError):
    """Safe diagnostic: never copy arbitrary upstream bodies into traces."""


def gateway_error(config: "GatewayConfig", status: int, body: bytes = b"") -> GatewayRequestError:
    # Inspect a bounded error response for known causes, but emit only our own text.
    disabled = status == 404 and b"switched off" in body.lower()
    reasons = {
        401: "网关认证失败 / Gateway authentication failed",
        403: "网关拒绝访问此模型 / Gateway denied access to this model",
        404: "所选模型不可用或不存在 / Selected model unavailable or not found",
        429: "模型限流或配额耗尽 / Model rate limit or quota exhausted",
    }
    reason = ("所选供应商已停用 / Selected provider is disabled" if disabled else
              reasons.get(status, "网关或上游请求失败 / Gateway or upstream request failed"))
    hint = ("；请检查管理员的默认模型及该 Agent 的模型覆盖，或选择 Magpie 路由组"
            " / Check the default model and per-agent model override, or select a Magpie routing group") if status == 404 else ""
    return GatewayRequestError(
        f"Magpie HTTP {status} ({config.executor}, requested_model={config.model}): {reason}{hint}"
    )


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    executor: str
    session_id: str
    agent_kind: str = ""
    timeout_seconds: float = 3600
    task_routing: TaskRoutingPolicy = field(default_factory=TaskRoutingPolicy)


def create_gateway_app(config: GatewayConfig, token: str, client: httpx.AsyncClient,
                       on_route: Callable[[dict], Awaitable[None]],
                       on_error: Callable[[GatewayRequestError | None], None] | None = None,
                       admission_proof: str = "") -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    last_route: tuple[str, str] | None = None
    policy = config.task_routing
    task_route = TaskRoute(config.base_url, client, policy) if (
        policy.enabled and config.model == policy.entry_group) else None

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
        if admission_proof and request.method == "POST":
            headers[ADMISSION_HEADER] = admission_proof
        params = [(key, value) for key, value in request.query_params.multi_items() if key != "key"]
        body = await request.body()
        prepared = None
        try:
            if task_route and request.method == "POST" and inference_path(path):
                prepared = await task_route.prepare(request.method, path, body, headers, params)
                response = prepared.response
            else:
                outgoing = client.build_request(request.method, f"{config.base_url.rstrip('/')}/{path}",
                    content=body, headers=headers, params=params)
                response = await client.send(outgoing, stream=True)
        except httpx.HTTPError:
            if on_error:
                on_error(gateway_error(config, 502))
            return JSONResponse({"error": {"type": "gateway_unavailable", "message": "Magpie unavailable / Magpie 不可用"}}, status_code=502)
        if on_error:
            if response.is_success:
                on_error(None)  # A CLI retry recovered; do not blame a later failure on this request.
            else:
                # Errors are small JSON bodies, but bound reads even for a broken upstream.
                body = bytearray()
                on_error(gateway_error(config, response.status_code))
                try:
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk[:max(0, 16384 - len(body))])
                        if len(body) >= 16384:
                            break
                except httpx.HTTPError:
                    pass  # The HTTP status remains useful even if the error body is truncated.
                finally:
                    if prepared:
                        await prepared.close()
                    else:
                        await response.aclose()
                error = gateway_error(config, response.status_code, bytes(body))
                on_error(error)
                # Both CLIs understand their native error envelopes. Do not expose raw bodies.
                message = str(error)
                if path.startswith("v1beta/"):
                    payload = {"error": {"code": response.status_code, "message": message,
                                          "status": "NOT_FOUND" if response.status_code == 404 else "UNKNOWN"}}
                else:
                    payload = {"type": "error", "error": {"type": "api_error", "message": message}}
                return JSONResponse(payload, status_code=response.status_code,
                                    headers={key: value for key, value in response.headers.items()
                                             if key.lower() == "retry-after"})
        provider = response.headers.get("x-magpie-provider", "")
        model = response.headers.get("x-magpie-model", "")
        if (response.is_success and provider and model and (provider, model) != last_route
                and (not task_route or inference_path(path))):
            try:
                await on_route({"executor": config.executor, "provider": provider, "model": model,
                          "requested_model": config.model, "agent_key": config.agent_kind})
                last_route = (provider, model)
            except BaseException:
                if prepared:
                    await prepared.close()
                else:
                    await response.aclose()
                raise

        async def chunks():
            try:
                stream = prepared.chunks() if prepared else response.aiter_bytes()
                async for chunk in stream:
                    yield chunk
            finally:
                if prepared:
                    await prepared.close()
                else:
                    await response.aclose()

        safe_headers = {key: value for key, value in response.headers.items() if key.lower() in {
            "content-type", "cache-control", "x-magpie-provider", "x-magpie-model", "retry-after",
        }}
        return StreamingResponse(chunks(), status_code=response.status_code, headers=safe_headers)

    return app


@asynccontextmanager
async def gateway_bridge(config: GatewayConfig, on_route: Callable[[dict], Awaitable[None]],
                         on_error: Callable[[GatewayRequestError | None], None] | None = None):
    admission = current_admission.get() or TaskAdmission()
    proof = await admission.prepare(config.api_key, config.model, config.agent_kind)
    token = secrets.token_urlsafe(32)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    async with httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds, connect=15)) as client:
        server = _LoopbackServer(uvicorn.Config(create_gateway_app(config, token, client, on_route, on_error, proof),
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
