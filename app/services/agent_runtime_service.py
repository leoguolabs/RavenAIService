"""Administrator-owned execution policy; selection is pinned per invocation.

Provider keys remain in Magpie. This store contains only the gateway credential
and independent executor/model selections, leaving legacy settings intact.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, ValidationError, field_validator, model_validator,
)

from app.config import settings
from app.services import runtime_settings_service
from app.services.magpie_routing_policy import TaskRoutingPolicy

STORE_KEY = "agent_runtime"
CATALOG_KEY = "agent_runtime_catalog"
EXECUTORS = {"claude_sdk": "Claude Agent SDK", "antigravity_cli": "Antigravity CLI"}
AGENTS = ("general_agent", "log_analysis", "device_agent", "project_expert", "package_search", "bug_fix")
_ADMIN_URL = TypeAdapter(HttpUrl)
runtime_settings_service.register_allowed_keys([STORE_KEY, CATALOG_KEY])


class AgentSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    executor: Literal["claude_sdk", "antigravity_cli"] | None = None
    model: str | None = Field(default=None, max_length=256)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    executor: Literal["claude_sdk", "antigravity_cli"] = "claude_sdk"
    base_url: str = "http://magpie:3425"
    api_key: str = Field(default="", repr=False, max_length=4096)
    model: str = Field(default="", max_length=256)
    agent_overrides: dict[str, AgentSelection] = Field(default_factory=dict)
    task_routing: TaskRoutingPolicy = Field(default_factory=TaskRoutingPolicy)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Magpie 地址必须是无凭据的 HTTP(S) 地址 / Invalid Magpie URL")
        return value.rstrip("/")

    @field_validator("agent_overrides")
    @classmethod
    def known_agents(cls, value: dict) -> dict:
        if set(value) - set(AGENTS):
            raise ValueError("未知 Agent / Unknown agent")
        return value

    @model_validator(mode="after")
    def usable(self):
        if self.enabled and (not self.api_key.strip() or not self.model.strip()):
            raise ValueError("启用 Magpie 需要网关密钥和模型 / Gateway key and model are required")
        if not self.enabled and (self.executor != "claude_sdk" or any(
            item.executor == "antigravity_cli" for item in self.agent_overrides.values()
        )):
            raise ValueError("Antigravity 执行器需要启用 Magpie / Antigravity requires Magpie")
        return self

    def selection(self, agent_kind: str = "") -> AgentSelection:
        override = self.agent_overrides.get(agent_kind)
        return AgentSelection(
            executor=(override.executor if override else None) or self.executor,
            model=(override.model if override else None) or self.model,
        )


def get_config() -> RuntimeConfig:
    raw = runtime_settings_service.get_all().get(STORE_KEY) or {}
    return RuntimeConfig.model_validate(raw)


def _magpie_admin_url() -> str | None:
    try:
        url = _ADMIN_URL.validate_python(settings.magpie_admin_url)
    except ValidationError:
        return None
    # Magpie 的登录密钥可放在查询参数中，管理入口只能返回无凭据地址。
    if any(part is not None for part in (url.username, url.password, url.query, url.fragment)):
        return None
    return str(url)


def describe() -> dict:
    config = get_config()
    return {
        **config.model_dump(exclude={"api_key"}),
        "api_key_set": bool(config.api_key),
        "magpie_admin_url": _magpie_admin_url(),
        "executors": EXECUTORS,
        "agents": list(AGENTS),
    }


def save(values: dict) -> dict:
    current = get_config().model_dump()
    patch = dict(values)
    # Like the existing model form, an empty password box keeps its secret.
    if not patch.get("api_key"):
        patch.pop("api_key", None)
    config = RuntimeConfig.model_validate({**current, **patch})
    runtime_settings_service.update({STORE_KEY: config.model_dump()})
    return describe()


async def models() -> list[dict]:
    config = get_config()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{config.base_url}/v1/models", headers={
            "Authorization": f"Bearer {config.api_key}", "User-Agent": "RavenAI/catalog",
        })
        response.raise_for_status()
        rows = response.json().get("data", [])
    # Do not forward arbitrary gateway fields (or upstream credentials) to UI.
    allowed = {"id", "display_name", "owned_by", "context_window", "modalities"}
    catalog = [{key: value for key, value in row.items() if key in allowed} for row in rows]
    runtime_settings_service.update({CATALOG_KEY: catalog})
    return catalog


def gateway_endpoint(agent_kind: str = ""):
    config = get_config()
    if not config.enabled:
        return None
    from app.agents.anthropic_client import ProviderProfile
    from app.services.model_router import EndpointChoice

    selection = config.selection(agent_kind)
    catalog = runtime_settings_service.get_all().get(CATALOG_KEY) or []
    row = next((item for item in catalog if item.get("id") == selection.model), {})
    modalities = (row.get("modalities") or {}).get("input", [])
    profile = ProviderProfile(
        name="magpie", label="Magpie", default_base_url=config.base_url,
        default_model=selection.model, default_small_fast_model=selection.model,
        supports_image_input="image" in modalities, supports_document_input="pdf" in modalities,
        supports_mcp_server_tools=True, thinking_budget_tokens_effective=False,
        disable_parallel_tool_use_effective=False, supports_partial_streaming=True,
    )
    return EndpointChoice(slot="magpie", provider="magpie", base_url=config.base_url,
        api_key=config.api_key, model=selection.model, small_fast_model=selection.model,
        profile=profile, executor=selection.executor, agent_kind=agent_kind)
