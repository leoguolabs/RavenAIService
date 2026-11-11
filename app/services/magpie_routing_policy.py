"""Opt-in task stability and shared local-model overload cooldown."""

from __future__ import annotations

import asyncio
import hashlib
import time
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Each group contains one model. Magpie does not expand a group's members'
# provider fallback chains, so these requests stay within the title routes.
TITLE_YINHE_GROUP = "raven-title-yinhe"
TITLE_DEEPSEEK_GROUP = "raven-title-deepseek"
TITLE_YINHE_MODEL = "raven-yinhe/yinhe-thinking"
TITLE_DEEPSEEK_MODEL = "raven-deepseek/deepseek-v4-flash"


class TaskRoutingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    entry_group: str = ""
    degraded_group: str = ""
    local_model: str = ""
    local_group: str = ""
    backup_model: str = ""
    first_token_seconds: float = Field(default=0.5, ge=0.05, le=120)
    probe_seconds: float = Field(default=0.5, ge=0.05, le=30)
    min_tokens_per_second: float = Field(default=20, ge=0.1, le=1000)
    idle_seconds: float = Field(default=2, ge=0.05, le=120)
    cooldown_seconds: int = Field(default=120, ge=1, le=3600)

    @model_validator(mode="after")
    def valid_routes(self):
        if self.enabled:
            for name in ("entry_group", "degraded_group", "local_group"):
                if not getattr(self, name).startswith("group/"):
                    raise ValueError(f"{name} must name a Magpie group")
            for name in ("local_model", "backup_model"):
                value = getattr(self, name)
                if "/" not in value or value.startswith("group/") or "://" in value:
                    raise ValueError(f"{name} must name an explicit Magpie provider/model")
            if self.local_model == self.backup_model:
                raise ValueError("Local and backup models must differ")
        return self


_local_cooldown: dict[str, float] = {}


@lru_cache(maxsize=1)
def _redis():
    import redis
    from app.config import settings

    return redis.Redis(host=settings.redis_host, port=settings.redis_port,
        db=settings.redis_db, password=settings.redis_password,
        socket_connect_timeout=0.15, socket_timeout=0.15)


def _key(base_url: str, model: str) -> str:
    scope = hashlib.sha256(f"{base_url}|{model}".encode()).hexdigest()[:24]
    return f"magpie:local-cooldown:{scope}"


async def cooling(base_url: str, policy: TaskRoutingPolicy) -> bool:
    key = _key(base_url, policy.local_model)
    if _local_cooldown.get(key, 0) > time.monotonic():
        return True
    try:
        return bool(await asyncio.to_thread(_redis().exists, key))
    except Exception:
        # A failed shared store must not undo this process's observations.
        return False


async def trip(base_url: str, policy: TaskRoutingPolicy) -> None:
    key = _key(base_url, policy.local_model)
    now = time.monotonic()
    for stale in [k for k, until in _local_cooldown.items() if until <= now]:
        _local_cooldown.pop(stale, None)
    _local_cooldown[key] = now + policy.cooldown_seconds
    try:
        await asyncio.to_thread(_redis().set, key, "slow", ex=policy.cooldown_seconds)
    except Exception:
        pass  # Pinning and the process-local cooldown still work.
