"""Latency-aware routing between the primary and backup model endpoints.

The deployment's primary endpoint is a free internal gateway that slows down
badly at weekday peak; the backup is a paid public endpoint that is always
available. Routing exists purely to spend money only when the free path is
actually degraded — so the default is always the primary, and every mechanism
here is about deciding when to *stop* using it and when it is safe to go back.

Three moving parts:

* **Rolling window** — the last N model calls per slot, each flagged slow-or-
  failed. A slot trips when ``trip_threshold`` of ``window_size`` are bad. Not
  a p95: this deployment runs a handful of agent runs per minute, so a
  percentile needs minutes of data to mean anything and reacts far too slowly
  to a degradation that starts at 09:00. (p95 is the *sentinel's* job — it
  probes 7x24 and is the oracle for choosing these thresholds offline.)
* **Circuit breaker as a TTL key** — key present means open, and it expires on
  its own. No state machine, no reset owner, no clock skew between processes;
  a process dying mid-transition just means the key lapses and the next window
  re-evaluates.
* **Half-open via real traffic** — when the breaker is open, exactly one
  request per cooldown wins a ``SET NX EX`` token and is sent to the primary as
  a probe. There is no background prober, deliberately: polling a gateway that
  is already overloaded at peak adds load to the thing we are waiting on.

State lives in Redis because agent runs are spread across the uvicorn process
and several Celery workers; an in-process window would let each worker reach
its own conclusion. Redis is treated as strictly optional, mirroring
:mod:`app.services.agent_trace_redis`: every failure degrades to per-process
state and logs at WARNING, because a Redis outage must never stop agent runs —
it should at worst cost us a slower route decision.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Slot ids. Strings rather than a bool so a third endpoint is an additive
# change; ``candidates`` always returns them in preference order.
SLOT_PRIMARY = "primary"
SLOT_BACKUP = "backup"

_KEY_PREFIX = "model_router:"


def _samples_key(slot: str) -> str:
    return f"{_KEY_PREFIX}samples:{slot}"


def _breaker_key(slot: str) -> str:
    return f"{_KEY_PREFIX}breaker:{slot}"


def _probe_key(slot: str) -> str:
    return f"{_KEY_PREFIX}probe:{slot}"


def _key_cursor_key(slot: str) -> str:
    return f"{_KEY_PREFIX}api_key_cursor:{slot}"


# Outcomes recorded per model call.
OUTCOME_OK = "ok"
OUTCOME_SLOW = "slow"
OUTCOME_TIMEOUT = "timeout"
# Connection refused / DNS failure / auth rejection: no point waiting for the
# window to fill, these trip on their own faster counter.
OUTCOME_HARD_FAILURE = "hard_failure"

_BAD_OUTCOMES = {OUTCOME_SLOW, OUTCOME_TIMEOUT, OUTCOME_HARD_FAILURE}


class AllEndpointsUnavailable(RuntimeError):
    """Every candidate endpoint failed before producing a model response.

    Carries the per-slot causes so the caller can surface something better than
    "request failed" — with two endpoints down, *why* each one failed is the
    only actionable information left.
    """

    def __init__(self, failures: List[Tuple[str, BaseException]]) -> None:
        self.failures = failures
        detail = "; ".join(
            f"{slot}: {type(exc).__name__}: {exc}" for slot, exc in failures
        ) or "no endpoint configured"
        super().__init__(f"All model endpoints failed ({detail})")


@dataclass(frozen=True)
class EndpointChoice:
    """A fully-resolved endpoint, ready to be handed to ``build_options``.

    Resolution happens once, up front, so a run is pinned to one endpoint for
    its whole lifetime. ``build_options`` otherwise reads the process-global
    ``settings``, which concurrent runs would race on the moment routing starts
    flipping between slots.
    """

    slot: str
    provider: str
    base_url: str
    api_key: str
    model: str
    small_fast_model: Optional[str]
    profile: Any  # ProviderProfile; typed loosely to avoid an import cycle
    api_key_id: str = "key-single"
    api_key_index: int = 0
    api_key_count: int = 1

    @property
    def is_backup(self) -> bool:
        return self.slot != SLOT_PRIMARY


# ─────────────────────────── Shared state store ────────────────────────────


class _RouterStore:
    """Rolling window + breaker state, Redis-backed with a local fallback.

    The Redis client is the **synchronous** one, matching ``agent_trace_redis``.
    ``redis.asyncio`` pools bind to the loop that created them, and four call
    sites run agents through ``asyncio.run`` (a fresh loop per call) inside
    long-lived Celery workers — an async singleton would fail on the second
    task with "attached to a different loop". Async callers hop through
    ``asyncio.to_thread``; two round trips per agent run is nothing beside a
    multi-second model call.
    """

    def __init__(self, *, redis_client: Optional[Any] = None) -> None:
        self._client = redis_client
        self._client_built = redis_client is not None
        # Fallback window used when Redis is unreachable. Per-process, so its
        # verdict is narrower than the shared one — but it is strictly better
        # than treating an unreachable Redis as "everything healthy".
        self._local_samples: Dict[str, List[str]] = {}
        self._local_breaker: Dict[str, float] = {}  # slot -> expiry monotonic
        self._local_probe: Dict[str, float] = {}
        self._local_key_cursor: Dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def client(self) -> Optional[Any]:
        if self._client_built:
            return self._client
        self._client_built = True
        try:
            import redis  # type: ignore[import-not-found]
            from app.config import settings

            self._client = redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("model_router: failed to build redis client: %s", exc)
            self._client = None
        return self._client

    # ── window ──────────────────────────────────────────────────────────────

    def record(self, slot: str, *, bad: bool, window_size: int, ttl: int) -> None:
        """Append one sample. Never raises."""
        flag = "1" if bad else "0"
        client = self.client
        if client is not None:
            try:
                pipe = client.pipeline()
                pipe.lpush(_samples_key(slot), flag)
                pipe.ltrim(_samples_key(slot), 0, max(window_size - 1, 0))
                pipe.expire(_samples_key(slot), ttl)
                pipe.execute()
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("model_router: sample write failed slot=%s: %s", slot, exc)
        with self._lock:
            window = self._local_samples.setdefault(slot, [])
            window.insert(0, flag)
            del window[window_size:]

    def samples(self, slot: str, *, window_size: int) -> List[str]:
        client = self.client
        if client is not None:
            try:
                return list(client.lrange(_samples_key(slot), 0, window_size - 1) or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("model_router: sample read failed slot=%s: %s", slot, exc)
        with self._lock:
            return list(self._local_samples.get(slot, []))[:window_size]

    def clear_samples(self, slot: str) -> None:
        """Drop the window so a recovered slot is not re-tripped by old samples."""
        client = self.client
        if client is not None:
            try:
                client.delete(_samples_key(slot))
            except Exception as exc:  # noqa: BLE001
                logger.warning("model_router: sample clear failed slot=%s: %s", slot, exc)
        with self._lock:
            self._local_samples.pop(slot, None)

    # ── breaker ─────────────────────────────────────────────────────────────

    def breaker_open(self, slot: str) -> bool:
        client = self.client
        if client is not None:
            try:
                return bool(client.exists(_breaker_key(slot)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("model_router: breaker read failed slot=%s: %s", slot, exc)
        with self._lock:
            expiry = self._local_breaker.get(slot)
            if expiry is None:
                return False
            if expiry <= time.monotonic():
                self._local_breaker.pop(slot, None)
                return False
            return True

    def open_breaker(self, slot: str, *, cooldown: int) -> None:
        client = self.client
        if client is not None:
            try:
                client.set(_breaker_key(slot), str(int(time.time())), ex=cooldown)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("model_router: breaker open failed slot=%s: %s", slot, exc)
        with self._lock:
            self._local_breaker[slot] = time.monotonic() + cooldown

    def close_breaker(self, slot: str) -> None:
        client = self.client
        if client is not None:
            try:
                client.delete(_breaker_key(slot))
            except Exception as exc:  # noqa: BLE001
                logger.warning("model_router: breaker close failed slot=%s: %s", slot, exc)
        with self._lock:
            self._local_breaker.pop(slot, None)
            self._local_probe.pop(slot, None)

    def breaker_opened_at(self, slot: str) -> Optional[int]:
        """Unix seconds the breaker was opened, for the Admin display."""
        client = self.client
        if client is not None:
            try:
                raw = client.get(_breaker_key(slot))
                return int(raw) if raw else None
            except Exception:  # noqa: BLE001
                return None
        return None

    # ── half-open probe token ───────────────────────────────────────────────

    def try_claim_probe(self, slot: str, *, cooldown: int) -> bool:
        """Win the right to send one real request to a broken slot.

        ``SET NX EX`` makes this a single atomic round trip across every
        process, so exactly one request per cooldown is spent probing.
        """
        client = self.client
        if client is not None:
            try:
                return bool(client.set(_probe_key(slot), "1", nx=True, ex=cooldown))
            except Exception as exc:  # noqa: BLE001
                logger.warning("model_router: probe claim failed slot=%s: %s", slot, exc)
        with self._lock:
            now = time.monotonic()
            expiry = self._local_probe.get(slot)
            if expiry is not None and expiry > now:
                return False
            self._local_probe[slot] = now + cooldown
            return True

    # ── API-key pool cursor ─────────────────────────────────────────────────

    def next_key_index(self, slot: str, *, pool_size: int) -> int:
        """Return the next round-robin index, shared through Redis when possible."""
        if pool_size <= 1:
            return 0
        client = self.client
        if client is not None:
            try:
                # Redis starts at one; subtract before modulo so the first
                # resolved run uses pool index zero.
                value = int(client.incr(_key_cursor_key(slot)))
                return (value - 1) % pool_size
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "model_router: api-key cursor failed slot=%s: %s", slot, exc
                )
        with self._lock:
            index = self._local_key_cursor.get(slot, 0) % pool_size
            self._local_key_cursor[slot] = (index + 1) % pool_size
            return index


_store: Optional[_RouterStore] = None


def get_store() -> _RouterStore:
    global _store
    if _store is None:
        _store = _RouterStore()
    return _store


def reset_store_for_tests(redis_client: Optional[Any] = None) -> _RouterStore:
    global _store
    _store = _RouterStore(redis_client=redis_client)
    return _store


# ─────────────────────────── Endpoint resolution ───────────────────────────


def _resolve(
    slot_spec: Any,
    *,
    advance_key: bool = False,
    key_index: Optional[int] = None,
) -> Optional[EndpointChoice]:
    """Build an :class:`EndpointChoice` from one configured slot.

    Returns ``None`` when the slot cannot serve traffic at all (disabled, no
    provider, no key, unknown provider) — an unusable slot must never reach
    ``candidates``, or a run would fail over into a dead end.
    """
    from app.agents.anthropic_client import PROVIDER_PROFILES
    from app.config import settings
    from app.services.api_key_pool import key_identifier
    from app.services.model_settings_service import api_keys_for_slot

    if not slot_spec.is_enabled():
        return None

    provider = str(getattr(settings, slot_spec.provider_key) or "").strip()
    profile = PROVIDER_PROFILES.get(provider)
    if profile is None:
        return None

    api_keys = api_keys_for_slot(slot_spec)
    if not api_keys:
        return None
    if key_index is None:
        key_index = (
            get_store().next_key_index(slot_spec.name, pool_size=len(api_keys))
            if advance_key
            else 0
        )
    key_index %= len(api_keys)
    api_key = api_keys[key_index]

    base_url = str(getattr(settings, slot_spec.base_url_key) or "").strip() or (
        profile.default_base_url or ""
    )
    model = str(getattr(settings, slot_spec.model_key) or "").strip() or (
        profile.default_model or ""
    )
    if not base_url or not model:
        return None

    small_fast = str(getattr(settings, slot_spec.small_fast_model_key) or "").strip() or (
        profile.default_small_fast_model or None
    )
    if profile.api_protocol == "openai_responses":
        small_fast = model

    return EndpointChoice(
        slot=slot_spec.name,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        small_fast_model=small_fast,
        profile=profile,
        api_key_id=key_identifier(api_key),
        api_key_index=key_index,
        api_key_count=len(api_keys),
    )


def alternate_api_key(choice: EndpointChoice) -> Optional[EndpointChoice]:
    """Resolve the next key for the same primary endpoint, if a pool exists."""
    if choice.slot != SLOT_PRIMARY or choice.api_key_count <= 1:
        return None
    from app.services.model_settings_service import PRIMARY_SLOT, api_keys_for_slot

    api_keys = api_keys_for_slot(PRIMARY_SLOT)
    if len(api_keys) <= 1:
        return None
    alternate = _resolve(
        PRIMARY_SLOT,
        key_index=(choice.api_key_index + 1) % len(api_keys),
    )
    if alternate is None or alternate.api_key_id == choice.api_key_id:
        return None
    return alternate


def _supports(choice: EndpointChoice, *, require_mcp: bool, require_image: bool,
              require_document: bool, require_small_fast: bool) -> bool:
    """Capability gate.

    Several agents bake capability into ``options`` *before* ``query()`` runs —
    device_agent refuses to start without MCP tools, log_analysis and
    project_expert swap in a different system prompt without them — so failing
    over to an endpoint with a narrower matrix would silently change what the
    agent does. Exclude those candidates instead.
    """
    profile = choice.profile
    if require_mcp and not getattr(profile, "supports_mcp_server_tools", False):
        return False
    if require_image and not getattr(profile, "supports_image_input", False):
        return False
    if require_document and not getattr(profile, "supports_document_input", False):
        return False
    if require_small_fast and not choice.small_fast_model:
        return False
    return True


def candidates(
    *,
    agent_kind: str = "",
    require_mcp: bool = False,
    require_image: bool = False,
    require_document: bool = False,
    require_small_fast: bool = False,
) -> List[EndpointChoice]:
    """Endpoints to try, in order. Never returns an empty list when one is usable.

    With routing disabled, or no usable backup, this is just ``[primary]`` and
    behaviour is identical to before routing existed.
    """
    from app.config import settings
    from app.services.model_settings_service import BACKUP_SLOT, PRIMARY_SLOT

    caps = dict(
        require_mcp=require_mcp,
        require_image=require_image,
        require_document=require_document,
        require_small_fast=require_small_fast,
    )

    primary = _resolve(PRIMARY_SLOT, advance_key=True)
    if primary is not None and not _supports(primary, **caps):
        primary = None

    # The switch gates *failover*, not measurement: with it off the primary is
    # still returned as a resolved choice so TTFT keeps flowing into the window.
    # That is what makes an observe-only rollout possible — you need real
    # latency data before you can pick a sane slow-threshold, and there is no
    # way to collect it without running through this path.
    if not settings.model_router_enabled:
        return [primary] if primary else []

    backup = _resolve(BACKUP_SLOT)
    if backup is not None and not _supports(backup, **caps):
        logger.info(
            "model_router: backup excluded for agent_kind=%s — capability mismatch "
            "(provider=%s)",
            agent_kind,
            backup.provider,
        )
        backup = None

    if primary is None:
        return [backup] if backup else []
    if backup is None:
        # Nothing to fail over to. An open breaker would otherwise strand every
        # request, so clear it — being slow beats being down.
        if get_store().breaker_open(SLOT_PRIMARY):
            logger.warning(
                "model_router: primary breaker open but no usable backup — "
                "clearing breaker and routing to primary anyway"
            )
            get_store().close_breaker(SLOT_PRIMARY)
        return [primary]

    store = get_store()
    if not store.breaker_open(SLOT_PRIMARY):
        return [primary, backup]

    # Breaker open: one request per cooldown goes back to the primary as a
    # half-open probe; everyone else is served by the backup.
    cooldown = int(settings.model_router_cooldown_seconds)
    if store.try_claim_probe(SLOT_PRIMARY, cooldown=cooldown):
        logger.info("model_router: half-open probe → primary (agent_kind=%s)", agent_kind)
        return [primary, backup]
    return [backup, primary]


def _backup_available() -> bool:
    """Whether a usable failover target is configured right now."""
    from app.services.model_settings_service import BACKUP_SLOT

    return _resolve(BACKUP_SLOT) is not None


def describe_endpoint(choice: Optional[EndpointChoice]) -> Tuple[str, str]:
    """``(model, provider)`` an agent should report for ``choice``.

    Every agent emits these on ``run_start`` and in its result dict, and each one
    previously derived them from the process-global settings — which would name
    the primary even on a run the backup served. ``None`` (routing off or nothing
    configured) reproduces the original settings-based resolution exactly.
    """
    if choice is not None:
        return choice.model, choice.provider

    from app.agents.anthropic_client import PROVIDER_PROFILES
    from app.config import settings

    provider = str(settings.anthropic_provider)
    profile = PROVIDER_PROFILES.get(provider)
    model = settings.anthropic_model or (
        profile.default_model if profile else "unknown"
    )
    return str(model or "unknown"), provider


# ─────────────────────────── Outcome recording ─────────────────────────────


def record_outcome(
    slot: str,
    *,
    outcome: str,
    ttft_ms: Optional[int] = None,
) -> None:
    """Feed one model call into the slot's window and update the breaker.

    Never raises: routing telemetry must not be able to fail an agent run.
    """
    try:
        _record_outcome(slot, outcome=outcome, ttft_ms=ttft_ms)
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_router: record_outcome failed slot=%s: %s", slot, exc)


def _record_outcome(slot: str, *, outcome: str, ttft_ms: Optional[int]) -> None:
    from app.config import settings

    store = get_store()
    window_size = int(settings.model_router_window_size)
    threshold = int(settings.model_router_trip_threshold)
    min_samples = int(settings.model_router_min_samples)
    cooldown = int(settings.model_router_cooldown_seconds)
    ttl = int(settings.model_router_sample_ttl_seconds)

    # A response that arrived but took too long counts as bad: the endpoint is
    # up, which is exactly the peak-hour condition routing exists to catch.
    if outcome == OUTCOME_OK and ttft_ms is not None:
        if ttft_ms > int(settings.model_router_slow_ttft_ms):
            outcome = OUTCOME_SLOW

    bad = outcome in _BAD_OUTCOMES
    store.record(slot, bad=bad, window_size=window_size, ttl=ttl)

    if slot != SLOT_PRIMARY:
        # Only the primary has a breaker: it is the one we route *away* from.
        # Tripping the backup would leave nowhere to go.
        return

    if not settings.model_router_enabled:
        # Observe-only: the window keeps filling (so thresholds can be tuned
        # against real data) but nothing is ever routed away from the primary.
        return

    if not _backup_available():
        # Nothing to fail over to. Tripping here would log "routing to backup"
        # when there is no backup, and ``candidates`` would clear the breaker on
        # the very next call anyway — so keep measuring and stay quiet.
        return

    was_open = store.breaker_open(slot)

    if not bad:
        if was_open:
            # This was the half-open probe and it came back healthy.
            logger.warning(
                "model_router: primary recovered (ttft_ms=%s) — closing breaker, "
                "routing back to primary",
                ttft_ms,
            )
            store.close_breaker(slot)
            store.clear_samples(slot)
        return

    if was_open:
        # Probe failed; extend the cooldown rather than letting it lapse.
        logger.warning(
            "model_router: half-open probe failed (outcome=%s) — extending cooldown %ds",
            outcome,
            cooldown,
        )
        store.open_breaker(slot, cooldown=cooldown)
        return

    window = store.samples(slot, window_size=window_size)
    bad_count = sum(1 for flag in window if flag == "1")

    hard_trip = int(settings.model_router_hard_failure_trip)
    if outcome == OUTCOME_HARD_FAILURE and bad_count >= hard_trip:
        logger.warning(
            "model_router: primary tripped on hard failures (%d/%d) — routing to "
            "backup for %ds. This spends money; check the primary endpoint.",
            bad_count,
            len(window),
            cooldown,
        )
        store.open_breaker(slot, cooldown=cooldown)
        return

    if len(window) >= min_samples and bad_count >= threshold:
        logger.warning(
            "model_router: primary tripped (%d/%d recent calls slow or failed) — "
            "routing to backup for %ds. This spends money; check the primary endpoint.",
            bad_count,
            len(window),
            cooldown,
        )
        store.open_breaker(slot, cooldown=cooldown)


# ─────────────────────────── Admin observability ───────────────────────────


def health_snapshot() -> Dict[str, Any]:
    """Current routing state for the Admin page. Never raises."""
    from app.config import settings

    snapshot: Dict[str, Any] = {
        "enabled": bool(settings.model_router_enabled),
        "slow_ttft_ms": int(settings.model_router_slow_ttft_ms),
        "window_size": int(settings.model_router_window_size),
        "trip_threshold": int(settings.model_router_trip_threshold),
        "cooldown_seconds": int(settings.model_router_cooldown_seconds),
        "slots": {},
    }
    try:
        from app.services.model_settings_service import BACKUP_SLOT, PRIMARY_SLOT

        store = get_store()
        window_size = int(settings.model_router_window_size)
        for slot_spec in (PRIMARY_SLOT, BACKUP_SLOT):
            choice = _resolve(slot_spec)
            window = store.samples(slot_spec.name, window_size=window_size)
            snapshot["slots"][slot_spec.name] = {
                "configured": choice is not None,
                "provider": choice.provider if choice else None,
                "model": choice.model if choice else None,
                "key_count": choice.api_key_count if choice else 0,
                "samples": len(window),
                "bad_samples": sum(1 for flag in window if flag == "1"),
            }
        open_ = store.breaker_open(SLOT_PRIMARY)
        snapshot["primary_breaker_open"] = open_
        snapshot["serving_slot"] = SLOT_BACKUP if open_ else SLOT_PRIMARY
        snapshot["breaker_opened_at"] = store.breaker_opened_at(SLOT_PRIMARY) if open_ else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_router: health_snapshot failed: %s", exc)
    return snapshot
