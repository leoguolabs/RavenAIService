"""Internal OpenAI transport with fail-closed, balance-based admission.

The dashboard source is deliberately isolated from inference credentials. It is
not a public billing API; when a refresh is required an expired session denies
new admission until renewed.
See openspec/changes/enforce-openai-balance-floor/design.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.services.openai_balance_estimate import BalanceEstimate, RETRY_AFTER, magpie_ledger, utc_now
from app.services.openai_task_admission import ADMISSION_HEADER

BALANCE_URL = "https://api.openai.com/v1/dashboard/billing/credit_grants"
UPSTREAM = "https://api.openai.com/v1/"
SOURCE_PATH = "/run/secrets/openai_balance_source"
RETRYABLE_BALANCE_STATUSES = {408, 429, 500, 502, 503, 504}
logger = logging.getLogger("uvicorn.error").getChild("openai_balance_guard")


class BalanceUnavailable(Exception):
    """Carries only a locally generated diagnostic code."""


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
        raise BalanceUnavailable("invalid_source") from exc


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
        raise BalanceUnavailable("invalid_payload") from exc


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "").strip()
    if value.isascii() and value.isdecimal():
        delay = float(value)
        return delay if math.isfinite(delay) else None
    if value:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is not None:
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def create_app(source_path: Path = Path(SOURCE_PATH), minimum: Decimal = Decimal("15"),
               client: httpx.AsyncClient | None = None, balance_timeout: float = 2,
               task_caller_sha256: str = "", estimate: BalanceEstimate | None = None) -> FastAPI:
    if not minimum.is_finite() or minimum < 0:
        raise ValueError("Balance floor must be a nonnegative USD amount")
    if task_caller_sha256 and (len(task_caller_sha256) != 64
                              or any(c not in "0123456789abcdef" for c in task_caller_sha256)):
        raise ValueError("Invalid task caller fingerprint")
    signing_key = secrets.token_bytes(32)

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
             "forwarded": 0, "blocked": 0, "balance_lookups": 0,
             "estimated_usd": None, "magpie_ledger_reads": 0}
    display_balance = None
    retry_source, retry_until = None, 0.0

    def source_identity(source: dict) -> bytes:
        return json.dumps([source["organization"], source["api_key_sha256"], source["authorization"]]).encode()

    def fingerprint(source: dict) -> str:
        return hashlib.sha256(source_identity(source)).hexdigest()

    def sign(source: dict, payload: str) -> str:
        return hmac.digest(signing_key, source_identity(source) + payload.encode(), "sha256").hex()

    def verdict(source: dict, proof: str) -> str:
        payload, _, signature = proof.rpartition(".")
        expires, _, decision = payload.partition(".")
        decision, _, _ = decision.partition(".")
        if (len(proof) <= 160 and proof.isascii() and expires.isdecimal()
                and int(expires) > time.time() and hmac.compare_digest(signature, sign(source, payload))):
            return decision
        return ""

    def refused():
        state["blocked"] += 1
        # Magpie also classifies credit-related words in 5xx response bodies.
        return JSONResponse({"error": {"type": "service_unavailable", "code": "verification_unavailable",
            "message": "OpenAI 暂时无法核验 / OpenAI admission verification unavailable"}}, status_code=503)

    def unavailable(error: BalanceUnavailable):
        if str(error) not in {"http_429_backoff", "balance_refresh_backoff"}:
            logger.warning("OpenAI balance verification failed (%s)", error)
        state.update(decision="balance_source_unavailable", balance_usd=None)
        return refused()

    def below_floor():
        reason = "balance_below_floor"
        state["decision"] = reason
        state["blocked"] += 1
        return JSONResponse({"error": {"type": "insufficient_quota", "code": reason,
            "message": "OpenAI 余额低于门槛 / OpenAI balance is below the configured floor"}}, status_code=402)

    @application.get("/health")
    async def health():
        # Liveness remains healthy when the financial guard is intentionally shut.
        wait = max(0, math.ceil(retry_until - asyncio.get_running_loop().time()))
        return {"status": "ok", "minimum_usd": str(minimum), **state, "retry_after_seconds": wait}

    async def verified_balance(source: dict, attempts: int = 2) -> Decimal:
        nonlocal retry_source, retry_until
        loop = asyncio.get_running_loop()
        identity = (source["organization"], hashlib.sha256(source["authorization"].encode()).digest())
        if retry_source != identity:
            retry_source, retry_until = identity, 0.0
        deadline = loop.time() + balance_timeout
        request = httpx.Request("GET", BALANCE_URL, headers={
            "Authorization": source["authorization"],
            "OpenAI-Organization": source["organization"], "Accept": "application/json",
            "User-Agent": f"python-httpx/{httpx.__version__}",
        })
        try:
            async with asyncio.timeout_at(deadline):
                for attempt in range(attempts):
                    if retry_source == identity and retry_until > loop.time():
                        raise BalanceUnavailable("http_429_backoff")
                    try:
                        state["balance_lookups"] += 1
                        response = await application.state.upstream.send(request, follow_redirects=False)
                    except httpx.TransportError as exc:
                        error = BalanceUnavailable(type(exc).__name__)
                        delay = 0.25
                    else:
                        if response.status_code == 200:
                            if len(response.content) > 1024 * 1024:
                                raise BalanceUnavailable("oversized_payload")
                            value = credit_balance(response.content)
                            state.update(balance_usd=str(value), checked_at=datetime.now(timezone.utc).isoformat())
                            return value
                        error = BalanceUnavailable(f"http_{response.status_code}")
                        if response.status_code not in RETRYABLE_BALANCE_STATUSES:
                            raise error
                        wait = retry_after_seconds(response)
                        delay = 0.25 if wait is None else wait
                        if response.status_code == 429 and wait is not None and retry_source == identity:
                            retry_until = max(retry_until, loop.time() + wait)
                    if attempt + 1 == attempts or delay >= deadline - loop.time():
                        raise error
                    logger.warning("Retrying OpenAI balance verification (%s)", error)
                    await asyncio.sleep(delay)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise BalanceUnavailable(type(exc).__name__) from exc

    async def task_amount(source: dict, key: str) -> Decimal:
        if estimate is None:
            return await verified_balance(source, attempts=1)
        async with estimate.lock:
            identity = fingerprint(source)
            state["magpie_ledger_reads"] += 1
            ledger = await magpie_ledger(application.state.upstream, key)
            now = utc_now()
            amount = estimate.reusable(identity, ledger, minimum, now)
            if amount is None:
                state["estimated_usd"] = None
                if estimate.retry_at and now < estimate.retry_at:
                    raise BalanceUnavailable("balance_refresh_backoff")
                try:
                    amount = await verified_balance(source, attempts=1)
                except BalanceUnavailable:
                    estimate.retry_at = now + RETRY_AFTER
                    estimate.ledger_reset = True
                    raise
                estimate.remember(identity, amount, ledger, datetime.fromisoformat(state["checked_at"]))
            snap = estimate.verified(identity)
            state.update(estimated_usd=str(amount), balance_usd=str(snap.amount),
                         checked_at=snap.checked_at.isoformat())
            return amount

    @application.post("/task-admission")
    async def task_admission(request: Request):
        nonlocal display_balance
        auth = request.headers.get("authorization", "")
        key = auth.removeprefix("Bearer ")
        if not auth.startswith("Bearer ") or not task_caller_sha256 or not hmac.compare_digest(
                hashlib.sha256(key.encode()).hexdigest(), task_caller_sha256):
            return JSONResponse({"error": {"message": "Unauthorized"}}, status_code=401)
        try:
            source = read_source(source_path)
            amount = await task_amount(source, key)
        except BalanceUnavailable as exc:
            return unavailable(exc)
        if estimate is None:
            display_balance = (fingerprint(source), amount, state["checked_at"])
        decision = "below" if amount < minimum else "allow"
        state["decision"] = "balance_below_floor" if decision == "below" else "available"
        payload = f"{int(time.time()) + 6 * 3600}.{decision}.{secrets.token_hex(16)}"
        return {"admission": f"{payload}.{sign(source, payload)}"}

    @application.api_route("/balance", methods=["GET"])
    @application.api_route("/v1/{path:path}", methods=["GET", "POST", "DELETE"])
    async def forward(request: Request, path: str = ""):
        try:
            source = read_source(source_path)
        except BalanceUnavailable as exc:
            return unavailable(exc)
        auth = request.headers.get("authorization", "")
        key = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
        if not key or not hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), source["api_key_sha256"]):
            return JSONResponse({"error": {"message": "Unauthorized"}}, status_code=401)
        display = request.url.path == "/balance"
        if task_caller_sha256 and not display:
            decision = verdict(source, request.headers.get(ADMISSION_HEADER, ""))
            if decision == "below":
                return below_floor()
            if decision != "allow" and not (request.method == "GET" and path == "models"):
                return refused()
        else:
            if task_caller_sha256:
                identity = fingerprint(source)
                if estimate is not None:
                    snap = estimate.verified(identity)
                    if snap is None:
                        return refused()
                    amount, checked_at = snap.amount, snap.checked_at.isoformat()
                elif display_balance and display_balance[0] == identity:
                    _, amount, checked_at = display_balance
                else:
                    return refused()
            else:
                try:
                    amount = await verified_balance(source)
                except BalanceUnavailable as exc:
                    return unavailable(exc)
                checked_at = state["checked_at"]
                if display:
                    state["decision"] = "balance_below_floor" if amount < minimum else "available"
            if display:
                return {"balance": float(amount), "currency": "usd", "minimum": float(minimum),
                        "allowed": amount >= minimum, "checked_at": checked_at}
            if amount < minimum:
                return below_floor()
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


estimate_path = os.environ.get("OPENAI_BALANCE_ESTIMATE_STATE")
app = create_app(Path(os.environ.get("OPENAI_BALANCE_SOURCE_FILE", SOURCE_PATH)),
                 Decimal(os.environ.get("OPENAI_BALANCE_MIN_USD", "15")),
                 task_caller_sha256=os.environ.get("OPENAI_BALANCE_TASK_CALLER_SHA256", ""),
                 estimate=BalanceEstimate(Path(estimate_path)) if estimate_path else None)
