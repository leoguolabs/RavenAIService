"""
Device link WebSocket endpoint and REST device list API.
"""

import json
import logging
import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.config import settings
from app.models.device_link import DeviceInfo
from app.services.device_link_service import device_link_manager

router = APIRouter()
logger = logging.getLogger(__name__)


class DeviceListResponse(BaseModel):
    """Device list response payload."""

    devices: List[DeviceInfo]


@router.websocket("/ws/device-link")
async def device_link_websocket(websocket: WebSocket):
    """Handle device link WebSocket handshake and messages."""
    await websocket.accept()
    device_id: Optional[str] = None
    client = websocket.client
    client_addr = f"{client.host}:{client.port}" if client else "unknown"
    logger.info("Device link websocket connected", extra={"client": client_addr})

    try:
        while True:
            raw_message = await websocket.receive_text()
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "message": "Invalid JSON payload"}))
                continue

            message_type = message.get("type")
            if message_type == "register":
                try:
                    if device_id and device_id != message.get("device_id"):
                        raise ValueError("修改设备ID前请重新连接 / Reconnect before changing device_id")
                    info = await device_link_manager.register_device(websocket, message)  # type: ignore[arg-type]
                    device_id = info.id
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Device register failed: %s", exc)
                    await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
                    continue

                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "register_ack",
                            "device_id": info.id,
                            "heartbeat_interval": settings.device_link_heartbeat_sec,
                            "server_time": time.time(),
                        }
                    )
                )
                logger.info(
                    "Device register acknowledged",
                    extra={"device_id": info.id, "client": client_addr, "device_name": info.name},
                )
            elif message_type == "ping":
                if device_id:
                    await device_link_manager.update_heartbeat(device_id, websocket)
                await websocket.send_text(json.dumps({"type": "pong"}))
            elif message_type == "pong":
                if device_id:
                    await device_link_manager.update_heartbeat(device_id, websocket)
            elif message_type == "prompt_result":
                if not device_id:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Device not registered"}))
                    continue
                await device_link_manager.handle_prompt_result(device_id, message, websocket)  # type: ignore[arg-type]
            elif message_type == "prompt_ack":
                if device_id:
                    await device_link_manager.update_heartbeat(device_id, websocket)
            elif message_type == "capabilities_update":
                if not device_id:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Device not registered"}))
                    continue
                await device_link_manager.update_capabilities(
                    device_id, message.get("capabilities") or {}, websocket
                )  # type: ignore[arg-type]
            else:
                await websocket.send_text(
                    json.dumps({"type": "error", "message": f"Unsupported message type: {message_type}"})
                )
    except WebSocketDisconnect:
        logger.info(
            "Device websocket disconnected", extra={"device_id": device_id or "<unregistered>", "client": client_addr}
        )
        if device_id:
            await device_link_manager.mark_offline(device_id, websocket)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Device link websocket error: %s", exc)
        if device_id:
            await device_link_manager.mark_offline(device_id, websocket)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@router.get("/api/v1/device-links", response_model=DeviceListResponse)
async def list_device_links() -> DeviceListResponse:
    """Return current device link snapshots."""
    devices = device_link_manager.list_devices()
    return DeviceListResponse(devices=devices)


@router.get("/api/v1/device-links/{device_id}/ping", response_model=DeviceInfo)
async def ping_device(device_id: str) -> DeviceInfo:
    """Force a ping to a connected device to refresh liveness info."""
    try:
        return await device_link_manager.ping_device(device_id)
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 404 if "not registered" in detail else 503
        raise HTTPException(status_code=status_code, detail=detail) from exc


@router.delete("/api/v1/device-links/{device_id}", response_model=DeviceInfo)
async def delete_device(device_id: str) -> DeviceInfo:
    """Delete a device record and close its connection if present."""
    try:
        return await device_link_manager.delete_device(device_id)
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 404 if "not found" in detail else 500
        raise HTTPException(status_code=status_code, detail=detail) from exc
