"""Internal OpenAI transport with fail-closed, real-credit admission.

The dashboard source is deliberately isolated from inference credentials. It is
not a public billing API; an expired session denies new inference until renewed.
See openspec/changes/enforce-openai-balance-floor/design.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

BALANCE_URL = "https://api.openai.com/v1/dashboard/billing/credit_grants"
UPSTREAM = "https://api.openai.com/v1/"
SOURCE_PATH = "/run/secrets/openai_balance_source"


class BalanceUnavailable(Exception):
    """Never expose a source exception or upstream body to the caller."""


def read_source(path: Path) -> dict:
    try:
        source = json.loads(path.read_text())
        for key in ("authorization", "organization", "api_key_sha256"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise ValueError("Missing source field")
        if (not source["authorization"].startswith("Bearer ") or
                not source["organization"].startswith("org-") or
                len(source["api_key_sha256"]) != 64):
            raise ValueError("Invalid source")
        return source
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise BalanceUnavailable from exc


def credit_balance(body: bytes) -> Decimal:
    try:
        data = json.loads(body, parse_float=Decimal, parse_int=Decimal)
        value = data["total_available"]
        if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
            raise ValueError("Balance must be numeric")
        amount = Decimal(value)
        if (not amount.is_finite() or not math.isfinite(float(amount)) or
                data.get("currency", "usd") != "usd"):
            raise ValueError("Invalid balance currency or value")
        return amount
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        raise BalanceUnavailable from exc


def create_app(source_path: Path = Path(SOURCE_PATH), minimum: Decimal = Decimal("15"),
               client: httpx.AsyncClient | None = None, balance_timeout: float = 2) -> FastAPI:
    if not minimum.is_finite() or minimum < 0:
        raise ValueError("Balance floor must be a nonnegative USD amount")

    @asynccontextmanager
    async def lifespan(application):
        if client is not None:
            yield
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=15),
                                         follow_redirects=False) as upstream:
                application.state.upstream = upstream
                yield

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    application.state.upstream = client
    state = {"decision": "unverified", "balance_usd": None, "checked_at": None,
             "forwarded": 0, "blocked": 0}

    def refusal(reason: str):
        state["decision"] = reason
        state["blocked"] += 1
        return JSONResponse({"error": {"type": "insufficient_quota", "code": reason,
            "message": ("OpenAI 余额保护已暂停新请求 / OpenAI balance guard paused new requests: "
                        "balance is below the configured floor or cannot be verified")}}, status_code=402)

    @application.get("/health")
    async def health():
        # Liveness remains healthy when the financial guard is intentionally shut.
        return {"status": "ok", "minimum_usd": str(minimum), **state}

    async def verified_balance(source: dict) -> Decimal:
        state["balance_usd"] = None
        try:
            async with asyncio.timeout(balance_timeout):
                request = httpx.Request("GET", BALANCE_URL, headers={
                    "Authorization": source["authorization"],
                    "OpenAI-Organization": source["organization"], "Accept": "application/json",
                    "User-Agent": f"python-httpx/{httpx.__version__}",
                })
                response = await application.state.upstream.send(request, follow_redirects=False)
                if response.status_code != 200 or len(response.content) > 1024 * 1024:
                    raise BalanceUnavailable
                value = credit_balance(response.content)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise BalanceUnavailable from exc
        state.update(balance_usd=str(value), checked_at=datetime.now(timezone.utc).isoformat())
        return value

    @application.api_route("/balance", methods=["GET"])
    @application.api_route("/v1/{path:path}", methods=["GET", "POST", "DELETE"])
    async def forward(request: Request, path: str = ""):
        try:
            source = read_source(source_path)
        except BalanceUnavailable:
            state["balance_usd"] = None
            return refusal("balance_source_unavailable")
        auth = request.headers.get("authorization", "")
        key = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
        if not key or not hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), source["api_key_sha256"]):
            return JSONResponse({"error": {"message": "Unauthorized"}}, status_code=401)
        try:
            amount = await verified_balance(source)
        except BalanceUnavailable:
            return refusal("balance_source_unavailable")
        if request.url.path == "/balance":
            state["decision"] = "balance_below_floor" if amount < minimum else "available"
            return {"balance": float(amount), "currency": "usd", "minimum": float(minimum),
                    "allowed": amount >= minimum, "checked_at": state["checked_at"]}
        if amount < minimum:
            return refusal("balance_below_floor")
        if not path or any(part in {".", ".."} for part in path.split("/")):
            return JSONResponse({"error": {"message": "Unsupported route"}}, status_code=404)

        headers = {k: v for k, v in request.headers.items() if k.lower() in {
            "authorization", "content-type", "accept", "openai-beta", "openai-project",
            "x-client-request-id", "idempotency-key", "user-agent",
        }}
        headers["OpenAI-Organization"] = source["organization"]
        url = httpx.URL(UPSTREAM + path).copy_with(query=request.url.query.encode())
        try:
            # Explicit Requests bypass the client's cookie jar: a dashboard
            # Set-Cookie must never become an inference credential (or vice versa).
            outgoing = httpx.Request(request.method, url,
                headers=headers, content=request.stream())
            response = await application.state.upstream.send(outgoing, stream=True, follow_redirects=False)
        except httpx.HTTPError:
            return JSONResponse({"error": {"message": "OpenAI upstream unavailable"}}, status_code=502)
        # Never follow or expose a redirect to a different credential destination.
        if 300 <= response.status_code < 400:
            await response.aclose()
            return JSONResponse({"error": {"message": "Unexpected OpenAI redirect"}}, status_code=502)
        state["forwarded"] += 1
        state["decision"] = "forwarded"

        async def chunks():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        safe = {k: v for k, v in response.headers.items() if k.lower() in {
            "content-type", "content-encoding", "cache-control", "x-request-id", "retry-after",
            "openai-processing-ms",
        } or k.lower().startswith("x-ratelimit-")}
        return StreamingResponse(chunks(), status_code=response.status_code, headers=safe)

    return application


app = create_app(Path(os.environ.get("OPENAI_BALANCE_SOURCE_FILE", SOURCE_PATH)),
                 Decimal(os.environ.get("OPENAI_BALANCE_MIN_USD", "15")))
