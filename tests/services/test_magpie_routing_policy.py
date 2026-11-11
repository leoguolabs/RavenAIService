import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx
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


def test_local_first_groups_remove_classifier_and_preserve_cloud_order():
    script = runpy.run_path(str(Path(__file__).parents[2] / "scripts/configure-magpie-task-routing.py"))
    local, fallback, main, cloud = script["routing_groups"]("local/model", "cloud/strong", "cloud/light")
    defaults = policy.TaskRoutingPolicy()
    assert local["firstToken"] == 1  # Magpie supports only whole seconds.
    assert defaults.first_token_seconds == defaults.probe_seconds == 0.5
    assert defaults.min_tokens_per_second == 20
    assert defaults.idle_seconds == 2 and defaults.cooldown_seconds == 120
    assert local["affinity"] == "session"
    assert local["members"] == ["local/model"]
    assert fallback["members"] == main["members"] == ["group/raven-task-yinhe", "cloud/strong", "cloud/light"]
    assert "firstToken" not in fallback
    for group in (local, fallback, main, cloud):
        assert group["classifier"] == "" and group["rules"] == []
        assert group["routing"] == "order"
        assert group["affinity"] == "session"
    assert cloud["members"] == ["cloud/strong", "cloud/light"]
    assert all("local" not in model and "yinhe" not in model for model in cloud["members"])


async def test_titles_only_provisions_singleton_groups_without_saving_main_runtime(monkeypatch, tmp_path):
    from app.services import agent_runtime_service as runtime

    script = runpy.run_path(str(Path(__file__).parents[2] / "scripts/configure-magpie-task-routing.py"))
    existing = [{"id": "raven-task-cloud", "members": ["raven-openai/gpt-6.1-sol"]}]
    models = [{"id": "raven-yinhe/yinhe-thinking"}, {"id": "raven-deepseek/deepseek-v4-flash"}]
    saved = []

    async def api(request):
        if request.url.path == "/api/groups":
            return httpx.Response(200, json={"models": models, "groups": existing})
        if request.url.path == "/api/groups/save":
            saved.append(json.loads(request.content))
        return httpx.Response(200, json={})

    def unexpected(*args):
        pytest.fail("Title provisioning must not change the main runtime")

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(api), **kwargs))
    monkeypatch.setattr(runtime, "get_config", unexpected)
    monkeypatch.setattr(runtime, "save", unexpected)
    monkeypatch.setattr(runtime, "models", unexpected)
    key = tmp_path / "key"
    key.write_text("test-web-key")
    args = SimpleNamespace(titles_only=True, admin_url="http://magpie", web_key_file=key,
                           backup_dir=tmp_path / "backups", apply=False)

    preview = await script["configure"](args)
    assert not saved
    assert [(group["id"], group["members"]) for group in preview["groups"]] == [
        ("raven-title-yinhe", ["raven-yinhe/yinhe-thinking"]),
        ("raven-title-deepseek", ["raven-deepseek/deepseek-v4-flash"]),
    ]
    assert all(group["classifier"] == "" and group["rules"] == [] for group in preview["groups"])
    args.apply = True
    result = await script["configure"](args)
    assert saved == preview["groups"]
    assert json.loads(Path(result["private_backup"]).read_text()) == {"groups": []}
