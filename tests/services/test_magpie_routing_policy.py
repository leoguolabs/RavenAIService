import runpy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services import magpie_routing_policy as policy


@pytest.mark.parametrize("patch", [{"min_tokens_per_second": 0}, {"probe_seconds": 0},
    {"cooldown_seconds": -1}, {"enabled": True}, {"unexpected": True}])
def test_invalid_policy_is_rejected(patch):
    with pytest.raises(ValidationError):
        policy.TaskRoutingPolicy(**patch)


@pytest.mark.asyncio
async def test_cooldown_shared_then_local_when_redis_unavailable(monkeypatch):
    data = {}

    class Redis:
        def set(self, key, value, ex):
            data[key] = (value, ex)

        def exists(self, key):
            return key in data

    monkeypatch.setattr(policy, "_redis", Redis)
    monkeypatch.setattr(policy, "_local_cooldown", {})
    p = policy.TaskRoutingPolicy(local_model="local/model")
    assert not await policy.cooling("http://one", p)
    await policy.trip("http://one", p)
    policy._local_cooldown.clear()  # Simulate another worker.
    assert await policy.cooling("http://one", p)
    assert not await policy.cooling("http://other", p)

    def unavailable():
        raise OSError("offline")

    monkeypatch.setattr(policy, "_redis", unavailable)
    await policy.trip("http://one", p)
    assert await policy.cooling("http://one", p)
    assert not await policy.cooling("http://other", p)


def test_intent_groups_have_cloud_classifier_and_safe_default():
    script = runpy.run_path(str(Path(__file__).parents[2] / "scripts/configure-magpie-task-routing.py"))
    local, fallback, main, cloud = script["routing_groups"]("local/model", "cloud/strong", "cloud/light")
    defaults = policy.TaskRoutingPolicy()
    assert local["firstToken"] == defaults.first_token_seconds == defaults.probe_seconds == 1
    assert local["affinity"] == "session"
    assert local["members"] == ["local/model"]
    assert fallback["members"] == ["group/raven-task-yinhe", "cloud/strong"]
    assert "firstToken" not in fallback
    for group in (main, cloud):
        assert group["classifier"] == "cloud/light"
        assert group["members"][0] == "cloud/strong"
        assert group["affinity"] == "session"
        assert len([r for r in group["rules"] if r.get("intent")]) == 3
    assert all("local" not in model and "yinhe" not in model for model in cloud["members"])
