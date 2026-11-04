"""Authenticated RavenClient AI capability and usage endpoints.

RavenClient sends Assistant requests directly to the selected upstream, so this
surface returns the effective model-router routes (including credentials) to a
trusted, authenticated desktop process. The companion usage endpoint accepts
only bounded counters and route metadata: conversation content has no field in
the schema and ``extra="forbid"`` prevents it being added accidentally.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.users import get_current_user
from app.services import metrics_service, model_router


router = APIRouter(prefix="/api/v1/client-ai", tags=["RavenClient AI"])

REFRESH_AFTER_SECONDS = 60
CAPABILITY_TTL_SECONDS = 5 * 60

_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "Vary": "Authorization",
}


class ClientAIRouteCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_input: bool
    document_input: bool
    tool_use: bool
    partial_streaming: bool
    thinking_budget: bool


class ClientAIRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: Literal["primary", "backup"]
    provider: str
    api_protocol: Literal["anthropic_messages", "openai_responses"] = "anthropic_messages"
    base_url: str
    api_key: str
    model: str
    small_fast_model: Optional[str] = None
    capabilities: ClientAIRouteCapabilities


class ClientAICapabilitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str
    issued_at: int
    expires_at: int
    refresh_after_seconds: int
    routes: List[ClientAIRoute]


class ClientAIUsageTokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(0, ge=0, le=2_000_000_000)
    output_tokens: int = Field(0, ge=0, le=2_000_000_000)
    cache_read_tokens: int = Field(0, ge=0, le=2_000_000_000)
    cache_write_tokens: int = Field(0, ge=0, le=2_000_000_000)


class ClientAIUsageReport(BaseModel):
    """Strict content-free usage report from the desktop Assistant runtime."""

    model_config = ConfigDict(extra="forbid")

    invocation_id: UUID
    slot: Literal["primary", "backup"]
    provider: str = Field(..., min_length=1, max_length=64)
    model: str = Field(..., min_length=1, max_length=128)
    status: Literal["succeeded", "failed", "cancelled", "timeout"]
    outcome: Optional[Literal["ok", "slow", "timeout", "hard_failure"]] = None
    tokens: ClientAIUsageTokens = Field(default_factory=ClientAIUsageTokens)
    duration_ms: Optional[int] = Field(None, ge=0, le=86_400_000)
    ttft_ms: Optional[int] = Field(None, ge=0, le=86_400_000)
    error_kind: Optional[str] = Field(
        None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9_.-]+$",
    )


def _serialize_route(choice: model_router.EndpointChoice) -> ClientAIRoute:
    profile = choice.profile
    return ClientAIRoute(
        slot=choice.slot,
        provider=choice.provider,
        api_protocol=getattr(profile, "api_protocol", "anthropic_messages"),
        base_url=choice.base_url,
        api_key=choice.api_key,
        model=choice.model,
        small_fast_model=choice.small_fast_model,
        capabilities=ClientAIRouteCapabilities(
            image_input=bool(getattr(profile, "supports_image_input", False)),
            document_input=bool(getattr(profile, "supports_document_input", False)),
            tool_use=bool(getattr(profile, "supports_mcp_server_tools", False)),
            partial_streaming=bool(
                getattr(profile, "supports_partial_streaming", False)
            ),
            thinking_budget=bool(
                getattr(profile, "thinking_budget_tokens_effective", False)
            ),
        ),
    )


def _revision(routes: List[ClientAIRoute]) -> str:
    """Opaque digest that changes for config, credential, model, or route order."""
    payload = [route.model_dump(mode="json") for route in routes]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


@router.get("/capabilities")
async def get_client_ai_capabilities(
    response: Response,
    _current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    choices = model_router.candidates(agent_kind="raven_client")
    if not choices:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RavenAIService has no usable client AI provider route",
        )

    routes = [_serialize_route(choice) for choice in choices]
    now = int(time.time())
    snapshot = ClientAICapabilitySnapshot(
        revision=_revision(routes),
        issued_at=now,
        expires_at=now + CAPABILITY_TTL_SECONDS,
        refresh_after_seconds=REFRESH_AFTER_SECONDS,
        routes=routes,
    )
    response.headers.update(_NO_STORE_HEADERS)
    return {
        "success": True,
        "message": "ok",
        "data": snapshot.model_dump(mode="json"),
    }


@router.post("/usage")
async def report_client_ai_usage(
    payload: ClientAIUsageReport,
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    invocation_id = str(payload.invocation_id)
    await metrics_service.record_ai_usage(
        source="raven_client_assistant",
        idempotency_key=f"ai_usage:raven_client:{current_user.id}:{invocation_id}",
        agent_kind="assistant",
        provider=payload.provider,
        model=payload.model,
        status=payload.status,
        usage=payload.tokens.model_dump(),
        user_id=current_user.id,
        owner_scope="raven_client",
        duration_ms=payload.duration_ms,
        error_kind=payload.error_kind,
        metadata={"endpoint_slot": payload.slot, "ttft_ms": payload.ttft_ms},
    )

    if payload.outcome is not None:
        model_router.record_outcome(
            payload.slot,
            outcome=payload.outcome,
            ttft_ms=payload.ttft_ms,
        )

    return {
        "success": True,
        "message": "usage accepted",
        "data": {"invocation_id": invocation_id},
    }
