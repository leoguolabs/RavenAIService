"""Tests for the Admin-editable model settings + the config overlay.

Verifies that runtime overrides persisted through
``model_settings_service`` win over the ``.env`` bootstrap defaults when read
via ``settings.<key>`` (the :meth:`app.config.Settings.__getattribute__`
overlay), that secrets are masked on read, that validation fires, and that
``reset`` reverts everything to the env defaults.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import OVERRIDABLE_MODEL_KEYS, settings
from app.services import model_settings_service as mss
from app.services import runtime_settings_service


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the runtime store at a temp file and clear its module cache."""
    runtime_path = tmp_path / "runtime-settings.json"
    monkeypatch.setattr(settings, "runtime_settings_path", str(runtime_path))
    monkeypatch.setattr(runtime_settings_service, "_CACHE", None)
    monkeypatch.setattr(runtime_settings_service, "_CACHE_MTIME", 0.0)
    monkeypatch.setattr(runtime_settings_service, "_CACHE_PATH", None)
    yield runtime_path


def test_key_lists_are_in_sync():
    assert mss.OVERRIDABLE_KEYS == OVERRIDABLE_MODEL_KEYS


def test_request_model_matches_specs():
    """The Admin PUT body model must mirror ``_SPECS`` exactly.

    FastAPI drops unknown body keys silently, so a spec without a matching
    pydantic field would be accepted by the API and then discarded before
    ``save()`` runs — a setting that "won't stick" with no error anywhere.
    """
    from app.api.admin import UpdateModelSettingsRequest

    assert set(UpdateModelSettingsRequest.model_fields) == mss.OVERRIDABLE_KEYS


def test_no_override_falls_back_to_env(isolated_store):
    env_provider = object.__getattribute__(settings, "anthropic_provider")
    # With an empty store the overlay must return the raw env/default value.
    assert settings.anthropic_provider == env_provider
    describe = mss.describe()
    assert describe["fields"]["anthropic_provider"]["source"] == "env"


def test_override_wins_via_overlay(isolated_store):
    mss.save(
        {
            "anthropic_provider": "deepseek",
            "anthropic_model": "deepseek-v4-pro",
            "anthropic_max_tokens": 4096,
        }
    )
    # Every consumer reads settings.<key>; those must now see the override.
    assert settings.anthropic_provider == "deepseek"
    assert settings.anthropic_model == "deepseek-v4-pro"
    assert settings.anthropic_max_tokens == 4096

    fields = mss.describe()["fields"]
    assert fields["anthropic_provider"]["source"] == "override"
    assert fields["anthropic_provider"]["value"] == "deepseek"


def test_secret_is_masked_and_persisted(isolated_store):
    mss.save({"anthropic_api_key": "sk-secret-xyz"})
    # The overlay exposes the real key to the app...
    assert settings.anthropic_api_key == "sk-secret-xyz"
    # ...but describe() never returns it, only an is_set flag.
    entry = mss.describe()["fields"]["anthropic_api_key"]
    assert entry["is_set"] is True
    assert "value" not in entry
    assert entry["source"] == "override"


def test_blank_secret_keeps_existing_key(isolated_store):
    mss.save({"anthropic_api_key": "sk-first"})
    # Sending an empty string must not wipe the stored key.
    mss.save({"anthropic_api_key": "", "anthropic_model": "some-model"})
    assert settings.anthropic_api_key == "sk-first"
    assert settings.anthropic_model == "some-model"


def test_primary_secret_pool_is_masked_and_legacy_key_remains_fallback(isolated_store):
    keys = ["sk-pool-a", "sk-pool-b", "sk-pool-c"]
    response = mss.save({"anthropic_api_keys": keys})

    assert settings.anthropic_api_keys == keys
    assert mss.api_keys_for_slot(mss.PRIMARY_SLOT) == keys
    entry = response["fields"]["anthropic_api_keys"]
    assert entry == {
        "group": "anthropic",
        "source": "override",
        "is_set": True,
        "count": 3,
    }
    rendered = json.dumps(response)
    assert all(key not in rendered for key in keys)


def test_primary_secret_pool_rejects_duplicates_without_echoing_secret(isolated_store):
    secret = "sk-do-not-echo"
    with pytest.raises(ValueError) as excinfo:
        mss.save({"anthropic_api_keys": [secret, secret]})

    assert "重复" in str(excinfo.value)
    assert secret not in str(excinfo.value)


def test_legacy_primary_key_is_one_item_pool(isolated_store, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_keys", [])
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-legacy")

    assert mss.api_keys_for_slot(mss.PRIMARY_SLOT) == ["sk-legacy"]


def test_custom_provider_requires_base_url_and_model(isolated_store):
    with pytest.raises(ValueError, match="custom"):
        mss.save({"anthropic_provider": "custom", "anthropic_base_url": "", "anthropic_model": ""})


def test_custom_provider_accepts_full_config(isolated_store):
    mss.save(
        {
            "anthropic_provider": "custom",
            "anthropic_base_url": "https://example.test/anthropic",
            "anthropic_model": "my-model",
        }
    )
    assert settings.anthropic_provider == "custom"
    assert settings.anthropic_base_url == "https://example.test/anthropic"


def test_invalid_provider_rejected(isolated_store):
    with pytest.raises(ValueError):
        mss.save({"anthropic_provider": "not-a-provider"})


def test_max_tokens_bounds(isolated_store):
    with pytest.raises(ValueError):
        mss.save({"anthropic_max_tokens": 0})
    with pytest.raises(ValueError):
        mss.save({"anthropic_max_tokens": 10**9})


# ── Routing policy ─────────────────────────────────────────────────────────
#
# This group decides when to stop using the free primary and start paying for
# the backup, so every rejection below is guarding a bill or a blind spot — not
# tidiness. It is also why the group is safe to expose in Admin at all.


def test_router_policy_is_admin_editable(isolated_store):
    """The knobs reach the overlay like any other model setting."""
    mss.save({"model_router_first_token_deadline_ms": 15_000})

    assert settings.model_router_first_token_deadline_ms == 15_000
    assert mss.describe()["fields"]["model_router_first_token_deadline_ms"] == {
        "group": "model_router",
        "source": "override",
        "value": 15_000,
        "env_default": object.__getattribute__(
            settings, "model_router_first_token_deadline_ms"
        ),
    }


def test_router_bounds_rejected(isolated_store):
    with pytest.raises(ValueError, match="model_router_cooldown_seconds"):
        mss.save({"model_router_cooldown_seconds": 1})
    with pytest.raises(ValueError, match="model_router_window_size"):
        mss.save({"model_router_window_size": 0})


def test_trip_threshold_above_window_rejected(isolated_store):
    """A threshold the window can never reach disarms the breaker silently.

    That is precisely the state the ttft_ms=0 bug produced, and it took a
    production log review to notice — the form must not let anyone re-create it.
    """
    with pytest.raises(ValueError, match="熔断器永远不会跳闸"):
        mss.save({"model_router_window_size": 8, "model_router_trip_threshold": 9})


def test_deadline_below_slow_threshold_rejected(isolated_store):
    """Preempting before a sample counts as slow starves the breaker's window."""
    with pytest.raises(ValueError, match="model_router_slow_ttft_ms"):
        mss.save(
            {"model_router_slow_ttft_ms": 30_000, "model_router_first_token_deadline_ms": 10_000}
        )


def test_deadline_floor_rejected_but_zero_allowed(isolated_store):
    """0 means "preemption off"; a small non-zero value would kill healthy runs."""
    with pytest.raises(ValueError, match="不能小于"):
        mss.save({"model_router_first_token_deadline_ms": 500})

    mss.save({"model_router_first_token_deadline_ms": 0})
    assert settings.model_router_first_token_deadline_ms == 0


def test_router_validation_sees_the_post_save_state(isolated_store):
    """Bounds are checked against the merged result, not the payload alone.

    Saving one half of a cross-field pair must not be able to leave the stored
    policy in a combination the form would have rejected outright.
    """
    mss.save({"model_router_window_size": 4, "model_router_trip_threshold": 3})

    # window_size alone now, but it must be judged against the *stored* threshold.
    with pytest.raises(ValueError, match="熔断器永远不会跳闸"):
        mss.save({"model_router_window_size": 2})


def test_frontend_bounds_mirror_the_backend():
    """The Admin number inputs carry min/max copied from ``_ROUTER_BOUNDS``.

    Drift is silently misleading rather than broken: the browser would show a
    range the server does not accept (or accept one it does), and the admin
    only finds out from a 400 that contradicts the field's own hint.
    """
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "frontend/src/utils/routerKnobs.ts"
    text = source.read_text(encoding="utf-8")

    entries = re.findall(
        r"key:\s*'(model_router_\w+)',.*?min:\s*(-?\d+),\s*max:\s*(\d+),",
        text,
        re.S,
    )
    assert entries, "could not parse routerKnobs.ts — update this test with it"

    frontend = {key: (int(low), int(high)) for key, low, high in entries}
    assert frontend == mss._ROUTER_BOUNDS


def test_unknown_key_rejected(isolated_store):
    with pytest.raises(ValueError, match="未知"):
        mss.save({"totally_unknown_key": "x"})


def test_ocr_bool_coercion_and_reset(isolated_store):
    env_ocr = object.__getattribute__(settings, "ocr_enabled")
    mss.save({"ocr_enabled": False})
    assert settings.ocr_enabled is False

    mss.reset()
    # After reset every key reverts to its env/default value.
    assert settings.ocr_enabled == env_ocr
    assert runtime_settings_service.get_all() == {}
    for key in OVERRIDABLE_MODEL_KEYS:
        assert mss.describe()["fields"][key]["source"] in {"env", "unset"}


# ─────────────────────────── Provider catalogue ────────────────────────────


def test_provider_list_matches_profiles():
    """The Admin dropdown and the runtime capability matrix must agree.

    A provider selectable here but missing from ``PROVIDER_PROFILES`` would fall
    into ``build_options``' most-restrictive fallback at the first chat turn.
    """
    from app.agents.anthropic_client import PROVIDER_PROFILES

    assert mss.ANTHROPIC_PROVIDERS == tuple(PROVIDER_PROFILES)


@pytest.mark.parametrize(
    "provider,base_url,model",
    [
        ("aliyun", "cn-beijing.maas.aliyuncs.com/apps/anthropic", "qwen3.7-max"),
        ("zhipu", "https://open.bigmodel.cn/api/anthropic", "glm-5.2"),
        ("moonshot", "https://api.moonshot.cn/anthropic", "kimi-k3"),
        ("minimax", "https://api.minimaxi.com/anthropic", "MiniMax-M3"),
        ("stepfun", "https://api.stepfun.com", "step-3.7-flash"),
        ("mimo", "https://api.xiaomimimo.com/anthropic", "mimo-v2.6-pro"),
        ("hunyuan", "https://api.hunyuan.cloud.tencent.com/anthropic", "hunyuan-2.0-thinking-20251109"),
        ("yinhe", "http://oneapi.yhroot.com", "yinhe-thinking"),
    ],
)
def test_new_provider_defaults(provider, base_url, model):
    from app.agents.anthropic_client import PROVIDER_PROFILES

    profile = PROVIDER_PROFILES[provider]
    assert base_url in profile.default_base_url
    assert profile.default_model == model
    # The flagship must be offered as a preset, and SDK in-process MCP tools
    # work on every Anthropic-compatible gateway.
    assert model in profile.models
    assert profile.supports_mcp_server_tools is True


def test_describe_exposes_presets_for_dropdown(isolated_store):
    profiles = {p["name"]: p for p in mss.describe()["provider_profiles"]}
    assert set(profiles) == set(mss.ANTHROPIC_PROVIDERS)

    aliyun = profiles["aliyun"]
    assert aliyun["label"]
    assert "qwen3.7-flash" in aliyun["models"]
    # 百炼's endpoint is a template — the form must warn before saving it.
    assert aliyun["base_url_needs_input"] is True
    assert profiles["zhipu"]["base_url_needs_input"] is False


def test_new_provider_is_saveable(isolated_store):
    mss.save({"anthropic_provider": "moonshot", "anthropic_model": "kimi-k3"})
    assert settings.anthropic_provider == "moonshot"
    assert settings.anthropic_model == "kimi-k3"


def test_unfilled_base_url_placeholder_rejected(isolated_store):
    # Explicitly typed…
    with pytest.raises(ValueError, match="占位符"):
        mss.save(
            {
                "anthropic_provider": "aliyun",
                "anthropic_base_url": "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/apps/anthropic",
            }
        )
    # …and left blank, which resolves to the same template at call time.
    with pytest.raises(ValueError, match="占位符"):
        mss.save({"anthropic_provider": "aliyun", "anthropic_base_url": ""})

    # A filled-in workspace id saves fine.
    mss.save(
        {
            "anthropic_provider": "aliyun",
            "anthropic_base_url": "https://ws-123.cn-beijing.maas.aliyuncs.com/apps/anthropic",
        }
    )
    assert settings.anthropic_provider == "aliyun"


# ─────────────────────────── Connectivity test ─────────────────────────────


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def blank_model_env(monkeypatch):
    """Blank the ``.env`` bootstrap values for the probe tests.

    Without this a developer machine's real ANTHROPIC_* / OCR_* settings leak
    into the fallback chain and the assertions become environment-dependent.
    """
    for key in (
        "anthropic_api_key",
        "anthropic_api_keys",
        "anthropic_base_url",
        "anthropic_model",
        "anthropic_backup_api_key",
        "anthropic_backup_base_url",
        "anthropic_backup_model",
        "anthropic_backup_provider",
        "ocr_api_key",
        "ocr_base_url",
        "ocr_model",
    ):
        monkeypatch.setattr(settings, key, "")


@pytest.fixture
def fake_upstream(monkeypatch):
    """Stub httpx so the probe never touches the network.

    Returns ``(box, calls)``: set ``box['response']`` / ``box['exc']`` to shape
    the upstream, and read ``calls`` to assert what was actually sent.
    """
    import httpx

    # Default: a healthy Anthropic-shaped reply, so tests about routing and
    # auth only have to override the interesting cases.
    calls: list = []
    box: dict = {
        "response": _FakeResponse(payload={"content": [{"type": "text", "text": "pong"}]}),
        "models_response": _FakeResponse(payload={"data": [{"id": "yinhe-thinking"}]}),
        "exc": None,
        "in_flight": 0,
        "max_in_flight": 0,
    }

    class _Client:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def post(self, url, json=None, headers=None):  # noqa: A002
            calls.append(
                {
                    "method": "POST",
                    "url": url,
                    "body": json,
                    "headers": headers,
                    "timeout": self.kwargs.get("timeout"),
                }
            )
            box["in_flight"] += 1
            box["max_in_flight"] = max(box["max_in_flight"], box["in_flight"])
            try:
                # Give concurrently scheduled probes a chance to overlap.  The
                # pool regression below therefore fails if ``gather`` returns.
                await asyncio.sleep(0)
                if box["exc"] is not None:
                    raise box["exc"]
                return box["response"]
            finally:
                box["in_flight"] -= 1

        async def get(self, url, headers=None):
            calls.append({"method": "GET", "url": url, "headers": headers})
            if box["exc"] is not None:
                raise box["exc"]
            return box["models_response"]

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return box, calls


async def test_anthropic_probe_uses_form_values_and_sdk_auth(isolated_store, blank_model_env, fake_upstream):
    box, calls = fake_upstream
    box["response"] = _FakeResponse(
        payload={"content": [{"type": "text", "text": "pong"}], "usage": {"input_tokens": 3}}
    )

    result = await mss.test_connection(
        {
            "target": "anthropic",
            "provider": "moonshot",
            "base_url": "https://api.moonshot.cn/anthropic/",
            "model": "kimi-k3",
            "api_key": "sk-typed",
        }
    )

    assert result["ok"] is True
    assert result["reply"] == "pong"
    assert result["model"] == "kimi-k3"
    assert result["usage"] == {"input_tokens": 3}

    sent = calls[0]
    assert sent["url"] == "https://api.moonshot.cn/anthropic/v1/messages"
    # Must mirror what the Claude Agent SDK sends, or a green test would not
    # imply a working agent run.
    assert sent["headers"]["x-api-key"] == "sk-typed"
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    assert sent["body"]["model"] == "kimi-k3"


async def test_anthropic_probe_falls_back_to_saved_key_and_provider_defaults(
    isolated_store, blank_model_env, fake_upstream, monkeypatch
):
    _box, calls = fake_upstream
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-stored")

    # Blank base_url / model → the provider profile defaults, exactly as
    # build_options resolves them at call time.
    result = await mss.test_connection(
        {"target": "anthropic", "provider": "zhipu", "base_url": "", "model": ""}
    )

    assert result["ok"] is True
    assert result["model"] == "glm-5.2"
    assert calls[0]["url"] == "https://open.bigmodel.cn/api/anthropic/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "sk-stored"


async def test_anthropic_probe_reports_upstream_error(isolated_store, blank_model_env, fake_upstream, monkeypatch):
    box, _calls = fake_upstream
    box["response"] = _FakeResponse(
        status_code=401, payload={"error": {"message": "invalid api key"}}
    )
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-bad")

    result = await mss.test_connection({"target": "anthropic", "provider": "zhipu"})

    assert result["ok"] is False
    assert result["error_kind"] == "http_401"
    assert result["status_code"] == 401
    assert "invalid api key" in result["detail"]
    # The key must never be echoed back to the browser.
    assert "sk-bad" not in json.dumps(result)


async def test_anthropic_probe_redacts_key_echoed_by_upstream(
    isolated_store, blank_model_env, fake_upstream, monkeypatch
):
    box, _calls = fake_upstream
    secret = "sk-provider-echo"
    box["response"] = _FakeResponse(
        status_code=401,
        payload={"error": {"message": f"invalid x-api-key {secret}"}},
    )
    monkeypatch.setattr(settings, "anthropic_api_key", secret)

    result = await mss.test_connection({"target": "anthropic", "provider": "zhipu"})

    assert result["ok"] is False
    assert secret not in json.dumps(result)
    assert "[REDACTED]" in result["detail"]


async def test_primary_pool_probe_tests_every_key_without_returning_secrets(
    isolated_store, blank_model_env, fake_upstream
):
    box, calls = fake_upstream
    keys = ["sk-pool-a", "sk-pool-b", "sk-pool-c"]

    result = await mss.test_connection(
        {
            "target": "anthropic",
            "provider": "yinhe",
            "api_keys": keys,
        }
    )

    assert result["ok"] is True
    assert result["tested_key_count"] == 3
    assert result["healthy_key_count"] == 3
    assert len(result["key_results"]) == 3
    model_calls = [call for call in calls if call["method"] == "GET"]
    message_calls = [call for call in calls if call["method"] == "POST"]
    assert {call["headers"]["x-api-key"] for call in model_calls} == set(keys)
    assert len(message_calls) == 1
    assert message_calls[0]["headers"]["x-api-key"] in keys
    assert box["max_in_flight"] == 1
    assert message_calls[0]["body"]["max_tokens"] == 8
    assert message_calls[0]["timeout"] == 120
    assert result["compatibility_result"]["ok"] is True
    rendered = json.dumps(result)
    assert all(key not in rendered for key in keys)


async def test_yinhe_pool_separates_key_health_from_shared_completion_limit(
    isolated_store, blank_model_env, fake_upstream
):
    box, calls = fake_upstream
    keys = ["sk-pool-a", "sk-pool-b", "sk-pool-c"]
    box["response"] = _FakeResponse(
        status_code=429,
        payload={"error": {"message": "shared RPM exhausted"}},
    )

    result = await mss.test_connection(
        {"target": "anthropic", "provider": "yinhe", "api_keys": keys}
    )

    assert result["ok"] is False
    assert result["tested_key_count"] == 3
    assert result["healthy_key_count"] == 3
    assert result["failed_key_count"] == 0
    assert result["compatibility_result"]["error_kind"] == "http_429"
    assert "3/3" in result["detail"]
    assert len([call for call in calls if call["method"] == "GET"]) == 3
    assert len([call for call in calls if call["method"] == "POST"]) == 1


async def test_yinhe_pool_requires_each_key_to_authorize_the_configured_model(
    isolated_store, blank_model_env, fake_upstream
):
    box, calls = fake_upstream
    box["models_response"] = _FakeResponse(payload={"data": [{"id": "other-model"}]})

    result = await mss.test_connection(
        {
            "target": "anthropic",
            "provider": "yinhe",
            "api_keys": ["sk-pool-a", "sk-pool-b"],
        }
    )

    assert result["ok"] is False
    assert result["healthy_key_count"] == 0
    assert all(
        item["error_kind"] == "model_not_authorized"
        for item in result["key_results"]
    )
    assert not [call for call in calls if call["method"] == "POST"]


# ──────────────────────── Backup endpoint (failover slot) ───────────────────
#
# The backup slot reuses the primary's validation and probe implementation via
# ``AnthropicSlot``, so these mirror the primary's cases to pin that the
# generalisation actually covers both and did not silently keep validating only
# the primary keys.


@pytest.fixture
def valid_primary(monkeypatch):
    """Pin a known-good primary so backup assertions are the only thing failing.

    ``save`` validates *every* slot, so a dev machine whose ``.env`` selects
    ``custom`` would otherwise raise on the primary's own rules before the
    backup is ever reached. Distinct from ``blank_model_env``, which blanks
    fields precisely to test the probe's fallback chain.
    """
    monkeypatch.setattr(settings, "anthropic_provider", "deepseek")
    monkeypatch.setattr(settings, "anthropic_base_url", "")
    monkeypatch.setattr(settings, "anthropic_model", "")


def test_disabled_backup_tolerates_partial_config(isolated_store):
    """An admin must be able to park a half-filled backup without enabling it."""
    mss.save({"anthropic_backup_provider": "moonshot"})
    assert settings.anthropic_backup_provider == "moonshot"
    assert settings.anthropic_backup_enabled is False


def test_enabling_backup_requires_provider(isolated_store, valid_primary):
    with pytest.raises(ValueError, match="anthropic_backup_provider"):
        mss.save({"anthropic_backup_enabled": True})


def test_backup_custom_provider_requires_base_url_and_model(isolated_store, valid_primary):
    with pytest.raises(ValueError, match="anthropic_backup_base_url"):
        mss.save(
            {
                "anthropic_backup_enabled": True,
                "anthropic_backup_provider": "custom",
                "anthropic_backup_base_url": "",
                "anthropic_backup_model": "",
            }
        )


def test_backup_invalid_provider_rejected(isolated_store):
    with pytest.raises(ValueError, match="anthropic_backup_provider"):
        mss.save({"anthropic_backup_provider": "not-a-provider"})


def test_backup_placeholder_base_url_rejected(isolated_store, valid_primary):
    # aliyun's default carries a {WorkspaceId} template; enabling the slot with
    # it unresolved would point every failover at a non-existent host.
    with pytest.raises(ValueError, match="占位符"):
        mss.save(
            {
                "anthropic_backup_enabled": True,
                "anthropic_backup_provider": "aliyun",
                "anthropic_backup_base_url": "",
            }
        )


def test_backup_full_config_saves(isolated_store, valid_primary):
    mss.save(
        {
            "anthropic_backup_enabled": True,
            "anthropic_backup_provider": "deepseek",
            "anthropic_backup_api_key": "sk-backup",
            "anthropic_backup_model": "deepseek-v4-pro",
        }
    )
    assert settings.anthropic_backup_enabled is True
    assert settings.anthropic_backup_provider == "deepseek"
    assert settings.anthropic_backup_model == "deepseek-v4-pro"
    # Secrets stay server-side for the backup slot too.
    entry = mss.describe()["fields"]["anthropic_backup_api_key"]
    assert entry["is_set"] is True
    assert "value" not in entry


def test_backup_validation_does_not_affect_primary(isolated_store, blank_model_env):
    """Enabling a valid backup must not retroactively invalidate the primary."""
    mss.save(
        {
            "anthropic_provider": "custom",
            "anthropic_base_url": "http://oneapi.example.test",
            "anthropic_model": "yinhe-thinking",
            "anthropic_backup_enabled": True,
            "anthropic_backup_provider": "deepseek",
        }
    )
    assert settings.anthropic_provider == "custom"
    assert settings.anthropic_backup_provider == "deepseek"


async def test_backup_probe_targets_backup_endpoint(
    isolated_store, blank_model_env, fake_upstream
):
    _box, calls = fake_upstream

    result = await mss.test_connection(
        {
            "target": "anthropic_backup",
            "provider": "moonshot",
            "base_url": "https://api.moonshot.cn/anthropic/",
            "model": "kimi-k3",
            "api_key": "sk-backup-typed",
        }
    )

    assert result["ok"] is True
    assert result["target"] == "anthropic_backup"
    assert calls[0]["url"] == "https://api.moonshot.cn/anthropic/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "sk-backup-typed"


async def test_backup_probe_falls_back_to_backup_keys_not_primary(
    isolated_store, blank_model_env, fake_upstream, monkeypatch
):
    """Omitted fields must resolve from the *backup* config, never the primary."""
    _box, calls = fake_upstream
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-PRIMARY")
    monkeypatch.setattr(settings, "anthropic_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_backup_api_key", "sk-BACKUP")
    monkeypatch.setattr(settings, "anthropic_backup_provider", "zhipu")

    result = await mss.test_connection({"target": "anthropic_backup"})

    assert result["ok"] is True
    assert result["model"] == "glm-5.2"  # zhipu's default, not anthropic's
    assert calls[0]["url"] == "https://open.bigmodel.cn/api/anthropic/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "sk-BACKUP"


async def test_backup_probe_reports_missing_provider(
    isolated_store, blank_model_env, fake_upstream
):
    _box, calls = fake_upstream
    result = await mss.test_connection({"target": "anthropic_backup"})

    assert result["ok"] is False
    assert result["error_kind"] == "missing_provider"
    # Pre-flight failure must not touch the network.
    assert calls == []


async def test_anthropic_probe_reports_timeout(isolated_store, blank_model_env, fake_upstream, monkeypatch):
    import httpx

    box, _calls = fake_upstream
    box["exc"] = httpx.TimeoutException("too slow")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-x")

    result = await mss.test_connection({"target": "anthropic", "provider": "zhipu"})
    assert result["ok"] is False
    assert result["error_kind"] == "timeout"


async def test_probe_flags_missing_key_and_placeholder_without_calling_upstream(
    isolated_store, blank_model_env, fake_upstream, monkeypatch
):
    _box, calls = fake_upstream

    no_key = await mss.test_connection({"target": "anthropic", "provider": "zhipu"})
    assert (no_key["ok"], no_key["error_kind"]) == (False, "missing_api_key")

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-x")
    unfilled = await mss.test_connection({"target": "anthropic", "provider": "aliyun"})
    assert (unfilled["ok"], unfilled["error_kind"]) == (False, "placeholder_base_url")

    assert calls == []


async def test_ocr_probe_hits_chat_completions(isolated_store, blank_model_env, fake_upstream):
    box, calls = fake_upstream
    box["response"] = _FakeResponse(
        payload={"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
    )

    result = await mss.test_connection(
        {
            "target": "ocr",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.5-ocr",
            "api_key": "sk-ocr",
        }
    )

    assert result["ok"] is True
    assert result["reply"] == "pong"
    assert calls[0]["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-ocr"
    # A text-only ping: no image is ever shipped to the upstream.
    assert calls[0]["body"]["messages"] == [{"role": "user", "content": "ping"}]


async def test_unknown_target_rejected(isolated_store, blank_model_env, fake_upstream):
    with pytest.raises(ValueError, match="target"):
        await mss.test_connection({"target": "nope"})


def test_bugfix_slot_is_independent_masked_and_runtime_editable(isolated_store):
    from app.services.model_router import _resolve
    before = (settings.anthropic_provider, settings.anthropic_model, settings.anthropic_api_key)
    mss.save({'bug_fix_agent_provider': 'anthropic', 'bug_fix_agent_model': 'advanced-review',
              'bug_fix_agent_api_key': 'dedicated-secret', 'bug_fix_agent_max_turns': 220})
    endpoint = _resolve(mss.BUG_FIX_SLOT)
    assert endpoint.slot == 'bug_fix'
    assert endpoint.model == 'advanced-review'
    assert endpoint.api_key == 'dedicated-secret'
    assert settings.bug_fix_agent_max_turns == 220
    assert before == (settings.anthropic_provider, settings.anthropic_model, settings.anthropic_api_key)
    assert 'dedicated-secret' not in json.dumps(mss.describe())
    mss.save({'bug_fix_agent_api_key': ''})
    assert settings.bug_fix_agent_api_key == 'dedicated-secret'


@pytest.mark.parametrize('payload', [
    {'bug_fix_agent_provider': 'unknown'},
    {'bug_fix_agent_max_turns': 0},
    {'bug_fix_agent_max_tokens': 200001},
    {'bug_fix_agent_request_timeout_seconds': 0},
    {'bug_fix_agent_provider': 'custom', 'bug_fix_agent_base_url': '', 'bug_fix_agent_model': ''},
])
def test_bugfix_invalid_settings_rejected(isolated_store, payload):
    with pytest.raises(ValueError):
        mss.save(payload)
