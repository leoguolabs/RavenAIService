import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState

from app.config import settings
from app.models.device_link import PromptEnvelope
from app.services.device_link_service import DeviceLinkManager


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "device_link_store_file", str(tmp_path / "devices.json"))
    return DeviceLinkManager()


def socket():
    ws = AsyncMock()
    ws.application_state = WebSocketState.CONNECTED
    return ws


def registration(**updates):
    return {"device_id": "stable-device", "device_name": "Workstation", "host": "test-host", **updates}


def prompt(request_id="request-1"):
    return PromptEnvelope(request_id=request_id, session_id="session-1", target_device_id="stable-device", prompt="hello")


async def test_repeat_registration_does_not_close_its_own_socket(manager):
    ws = socket()
    await manager.register_device(ws, registration())
    await manager.register_device(ws, registration(client_version="2.0"))
    ws.close.assert_not_awaited()
    assert len(manager.list_devices()) == 1
    assert (await manager.get_device("stable-device")).status == "online"


async def test_old_disconnect_cannot_mark_replacement_offline(manager):
    old, new = socket(), socket()
    await manager.register_device(old, registration())
    # Simulate the old endpoint's callback running *during* close: this also verifies we don't close under the lock.
    async def close_old():
        await manager.mark_offline("stable-device", old)

    old.close.side_effect = close_old
    await manager.register_device(new, registration(capabilities={"generation": 2}))
    await manager.mark_offline("stable-device", old)
    await manager.update_capabilities("stable-device", {"generation": 1}, old)
    assert await manager.update_heartbeat("stable-device", old) is None
    info = await manager.get_device("stable-device")
    assert info.status == "online"
    assert info.capabilities == {"generation": 2}
    assert len(manager.list_devices()) == 1


async def test_superseded_prompts_fail_and_new_socket_handles_new_prompts(manager):
    old, new = socket(), socket()
    await manager.register_device(old, registration())
    pending = asyncio.create_task(manager.send_prompt("stable-device", prompt()))
    await asyncio.sleep(0)
    await manager.register_device(new, registration())
    with pytest.raises(RuntimeError, match="connection replaced"):
        await pending

    pending = asyncio.create_task(manager.send_prompt("stable-device", prompt("request-2")))
    await asyncio.sleep(0)
    result = {"request_id": "request-2", "answer": "new socket"}
    await manager.handle_prompt_result("stable-device", {**result, "answer": "stale socket"}, old)
    assert not pending.done()
    await manager.register_device(socket(), registration(device_id="another-device"))
    await manager.handle_prompt_result("another-device", result)
    assert not pending.done()
    await manager.handle_prompt_result("stable-device", result, new)
    assert (await pending)["answer"] == "new socket"
    assert json.loads(new.send_text.call_args.args[0])["request_id"] == "request-2"


async def test_offline_old_socket_cannot_revive_device(manager):
    old = socket()
    await manager.register_device(old, registration())
    await manager.mark_offline("stable-device", old)
    assert await manager.update_heartbeat("stable-device", old) is None
    assert (await manager.get_device("stable-device")).status == "offline"


async def test_service_restart_restores_same_device_then_reconnects(manager):
    await manager.register_device(socket(), registration())
    restored = DeviceLinkManager()
    assert restored.list_devices()[0].status == "offline"
    await restored.register_device(socket(), registration(client_version="new-version"))
    assert len(restored.list_devices()) == 1
    assert restored.list_devices()[0].id == "stable-device"
    assert restored.list_devices()[0].status == "online"


async def test_same_socket_reregistration_during_dispatch_remains_idempotent(manager, monkeypatch):
    ws = socket()
    await manager.register_device(ws, registration())
    get_active = manager._get_active_state

    async def reregister_during_lookup(device_id):
        state = await get_active(device_id)
        await manager.register_device(ws, registration(client_version="updated"))
        return state

    monkeypatch.setattr(manager, "_get_active_state", reregister_during_lookup)
    pending = asyncio.create_task(manager.send_prompt("stable-device", prompt()))
    await asyncio.sleep(0)
    await manager.handle_prompt_result("stable-device", {"request_id": "request-1", "answer": "ok"}, ws)
    assert (await pending)["answer"] == "ok"
    ws.close.assert_not_awaited()
