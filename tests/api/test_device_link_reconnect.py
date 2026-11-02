import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import device_link
from app.config import settings
from app.services.device_link_service import DeviceLinkManager


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "device_link_store_file", str(tmp_path / "devices.json"))
    monkeypatch.setattr(device_link, "device_link_manager", DeviceLinkManager())
    app = FastAPI()
    app.include_router(device_link.router)
    with TestClient(app) as test_client:
        yield test_client


def test_repeat_registration_and_reconnect_keep_one_online_device(client):
    payload = {"type": "register", "device_id": "same-id", "device_name": "Test client"}
    with client.websocket_connect("/ws/device-link") as old:
        old.send_json(payload)
        assert old.receive_json()["type"] == "register_ack"
        old.send_json(payload)
        assert old.receive_json()["type"] == "register_ack"
        with client.websocket_connect("/ws/device-link") as new:
            new.send_json(payload)
            assert new.receive_json()["device_id"] == "same-id"
            with pytest.raises(WebSocketDisconnect):
                old.receive_json()
            new.send_json({"type": "capabilities_update", "capabilities": {"generation": 2}})
            new.send_json({"type": "ping"})
            assert new.receive_json()["type"] == "pong"
            devices = client.get("/api/v1/device-links").json()["devices"]
            assert len(devices) == 1
            assert devices[0]["status"] == "online"
            assert devices[0]["capabilities"] == {"generation": 2}
        assert client.get("/api/v1/device-links").json()["devices"][0]["status"] == "offline"


def test_a_socket_cannot_leave_ghost_devices_by_changing_its_id(client):
    with client.websocket_connect("/ws/device-link") as ws:
        ws.send_json({"type": "register", "device_id": "first"})
        assert ws.receive_json()["type"] == "register_ack"
        ws.send_json({"type": "register", "device_id": "second"})
        assert ws.receive_json()["type"] == "error"
        assert [d["id"] for d in client.get("/api/v1/device-links").json()["devices"]] == ["first"]
