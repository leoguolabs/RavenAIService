"""
Device link manager: tracks connected devices and routes prompts over WebSocket.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.config import settings
from app.models.device_link import (
    DeviceInfo,
    PromptEnvelope,
    PromptResultMessage,
    RegisterMessage,
)
from app.services.base import BaseService

logger = logging.getLogger(__name__)


@dataclass
class _DeviceState:
    websocket: Optional[WebSocket]
    info: DeviceInfo
    client_version: Optional[str] = None
    metadata: Dict[str, object] = field(default_factory=dict)


@dataclass
class _PendingPrompt:
    future: asyncio.Future
    device_id: str
    started_at: float


class DeviceLinkManager(BaseService):
    """Manage connected device WebSocket links and prompt routing."""

    def __init__(self):
        super().__init__()
        self._devices: Dict[str, _DeviceState] = {}
        self._pending_prompts: Dict[str, _PendingPrompt] = {}
        self._lock = asyncio.Lock()
        self._store_path = Path(getattr(settings, "device_link_store_file", "data/device_links.json"))
        self._restore_devices()

    async def register_device(self, websocket: WebSocket, payload: RegisterMessage) -> DeviceInfo:
        """Record a device registration and keep its WebSocket reference."""
        device_id = payload.get("device_id")
        if not device_id:
            raise ValueError("device_id is required for registration")

        info = DeviceInfo(
            id=device_id,
            name=payload.get("device_name") or device_id,
            host=payload.get("host"),
            models=payload.get("models") or [],
            capabilities=payload.get("capabilities") or {},
            last_seen=datetime.utcnow(),
            status="online",
        )
        state = _DeviceState(
            websocket=websocket,
            info=info,
            client_version=payload.get("client_version"),
            metadata={"capabilities": payload.get("capabilities") or {}},
        )

        async with self._lock:
            previous = self._devices.get(device_id)
            replaced = previous and previous.websocket is not websocket
            if replaced:
                self._fail_pending_for_device(device_id, RuntimeError(f"Device {device_id} connection replaced"))
            self._devices[device_id] = state
            self._persist_devices()
        # Publish the replacement before closing the old socket. Its disconnect callback must not own the new state.
        if replaced and previous.websocket and previous.websocket.application_state == WebSocketState.CONNECTED:
            try:
                await previous.websocket.close()
            except Exception:  # noqa: BLE001
                logger.debug("Failed to close previous websocket for %s", device_id, exc_info=True)
        self.log_info("Device registered", extra={"device_id": device_id, "host": info.host})
        return info

    async def update_heartbeat(self, device_id: str, websocket: Optional[WebSocket] = None) -> Optional[DeviceInfo]:
        """Mark device as alive."""
        async with self._lock:
            state = self._devices.get(device_id)
            if not state or (websocket is not None and state.websocket is not websocket):
                return None
            state.info.last_seen = datetime.utcnow()
            state.info.status = "online"
            self._persist_devices()
            return state.info

    async def mark_offline(self, device_id: str, websocket: Optional[WebSocket] = None) -> None:
        """Mark device offline and fail pending prompts."""
        async with self._lock:
            state = self._devices.get(device_id)
            if not state or (websocket is not None and state.websocket is not websocket):
                return
            state.info.status = "offline"
            state.info.last_seen = datetime.utcnow()
            state.websocket = None
            self._persist_devices()
            self._fail_pending_for_device(device_id, RuntimeError(f"Device {device_id} disconnected"))
        self.log_info("Device disconnected", extra={"device_id": device_id})

    async def update_capabilities(
        self, device_id: str, capabilities: dict, websocket: Optional[WebSocket] = None
    ) -> Optional[DeviceInfo]:
        """Update device capabilities metadata (e.g., MCP tools/prompts/resources)."""
        normalized = capabilities if isinstance(capabilities, dict) else {"value": capabilities}
        async with self._lock:
            state = self._devices.get(device_id)
            if not state:
                raise RuntimeError(f"Device {device_id} not registered")
            if websocket is not None and state.websocket is not websocket:
                return None
            state.info.capabilities = normalized or {}
            state.metadata["capabilities"] = normalized or {}
            state.info.last_seen = datetime.utcnow()
            self._persist_devices()
            info = DeviceInfo(**state.info.model_dump())
        self.log_info(
            "Device capabilities updated",
            extra={
                "device_id": device_id,
                "capability_keys": list((normalized or {}).keys()),
                "mcp_servers": len((normalized or {}).get("mcp", {}).get("servers", [])),
            },
        )
        return info

    def list_devices(self) -> list[DeviceInfo]:
        """Return a snapshot list of devices."""
        return [DeviceInfo(**device.info.model_dump()) for device in self._devices.values()]

    async def get_device(self, device_id: str) -> Optional[DeviceInfo]:
        """Fetch a single device snapshot."""
        async with self._lock:
            state = self._devices.get(device_id)
            if not state:
                return None
            return DeviceInfo(**state.info.model_dump())

    async def send_prompt(self, device_id: str, payload: PromptEnvelope) -> PromptResultMessage:
        """
        Send a prompt to the device and wait for its prompt_result reply.

        Raises:
            RuntimeError: when device is offline or no response before timeout.
        """
        state = await self._get_active_state(device_id)
        message = {"type": "prompt", **payload.model_dump(exclude_none=True)}

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        future: asyncio.Future = loop.create_future()
        async with self._lock:
            current = self._devices.get(device_id)
            if current is None or current.websocket is not state.websocket:
                raise RuntimeError(f"Device {device_id} connection replaced")
            self._pending_prompts[payload.request_id] = _PendingPrompt(
                future=future, device_id=device_id, started_at=started_at
            )
            current.info.last_seen = datetime.utcnow()

        timeout = getattr(settings, "device_link_timeout_sec", 120)
        try:
            await state.websocket.send_text(json.dumps(message))
            self.log_info(
                "Prompt dispatched to device",
                extra={
                    "device_id": device_id,
                    "request_id": payload.request_id,
                    "session_id": payload.session_id,
                    "timeout": timeout,
                },
            )
            result = await asyncio.wait_for(future, timeout=timeout)
            await self.update_heartbeat(device_id, state.websocket)
            return result  # type: ignore[return-value]
        except asyncio.TimeoutError as exc:
            elapsed_ms = int((loop.time() - started_at) * 1000)
            self.log_warning(
                "Device prompt timed out",
                extra={
                    "device_id": device_id,
                    "request_id": payload.request_id,
                    "timeout": timeout,
                    "duration_ms": elapsed_ms,
                },
            )
            raise RuntimeError(f"Device {device_id} did not respond within {timeout} seconds") from exc
        except Exception:
            self.log_error(
                "Failed to send prompt to device",
                extra={"device_id": device_id, "request_id": payload.request_id, "session_id": payload.session_id},
            )
            raise
        finally:
            self._pending_prompts.pop(payload.request_id, None)

    async def handle_prompt_result(
        self, device_id: str, payload: PromptResultMessage, websocket: Optional[WebSocket] = None
    ) -> None:
        """Resolve the waiting future for a prompt result."""
        if not await self.update_heartbeat(device_id, websocket):
            return
        request_id = payload.get("request_id")
        if not request_id:
            self.log_warning("Received prompt_result without request_id", extra={"device_id": device_id})
            return

        pending = self._pending_prompts.get(request_id)
        if not pending:
            self.log_warning("No pending prompt for result", extra={"device_id": device_id, "request_id": request_id})
            return

        future = pending.future
        owner = pending.device_id
        if owner != device_id:
            self.log_warning(
                "Prompt result device mismatch",
                extra={"expected_device": owner, "device_id": device_id, "request_id": request_id},
            )
            return
        else:
            elapsed_ms = int((asyncio.get_running_loop().time() - pending.started_at) * 1000)
            self.log_info(
                "Prompt result received",
                extra={
                    "device_id": device_id,
                    "request_id": request_id,
                    "session_id": payload.get("session_id"),
                    "duration_ms": elapsed_ms,
                },
            )
        if not future.done():
            future.set_result(payload)
        self._pending_prompts.pop(request_id, None)

    async def ping_device(self, device_id: str) -> DeviceInfo:
        """Send a ping frame to a device to confirm liveness."""
        state = await self._get_active_state(device_id)
        await state.websocket.send_text(json.dumps({"type": "ping"}))
        await self.update_heartbeat(device_id)
        return DeviceInfo(**state.info.model_dump())

    async def delete_device(self, device_id: str) -> DeviceInfo:
        """Remove device tracking info and close the websocket if needed."""
        async with self._lock:
            state = self._devices.pop(device_id, None)
            self._persist_devices()
        if not state:
            raise RuntimeError(f"Device {device_id} not found")

        websocket = state.websocket
        if websocket and websocket.application_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                logger.debug("Failed to close websocket when deleting %s", device_id, exc_info=True)

        self._fail_pending_for_device(device_id, RuntimeError(f"Device {device_id} removed"))
        self.log_info("Device deleted", extra={"device_id": device_id})
        return DeviceInfo(**state.info.model_dump())

    async def _get_active_state(self, device_id: str) -> _DeviceState:
        async with self._lock:
            state = self._devices.get(device_id)
            if not state:
                raise RuntimeError(f"Device {device_id} not registered")
            websocket = state.websocket
            if websocket is None or websocket.application_state != WebSocketState.CONNECTED:
                raise RuntimeError(f"Device {device_id} is offline")
            if state.info.status != "online":
                raise RuntimeError(f"Device {device_id} is offline")
            return state

    def _fail_pending_for_device(self, device_id: str, exc: Exception) -> None:
        for request_id, pending in list(self._pending_prompts.items()):
            if pending.device_id != device_id:
                continue
            future = pending.future
            if not future.done():
                future.set_exception(exc)
            self._pending_prompts.pop(request_id, None)

    def _restore_devices(self) -> None:
        """Load cached device snapshots so restart keeps known devices."""
        try:
            if not self._store_path.exists():
                return
            raw = self._store_path.read_text(encoding="utf-8")
            if not raw.strip():
                return
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load cached devices: %s", exc)
            return

        restored = 0
        for item in data or []:
            try:
                info = DeviceInfo.model_validate(item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skip invalid cached device: %s", exc, exc_info=True)
                continue
            info.status = "offline"
            self._devices[info.id] = _DeviceState(
                websocket=None, info=info, client_version=None, metadata={"restored": True}
            )
            restored += 1

        if restored:
            self.log_info("Restored device list from cache", extra={"count": restored})

    def _persist_devices(self) -> None:
        """Persist current device list to disk so restart can restore it."""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            payload = [state.info.model_dump(mode="json") for state in self._devices.values()]
            self._store_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to persist device cache: %s", exc, exc_info=True)


device_link_manager = DeviceLinkManager()
