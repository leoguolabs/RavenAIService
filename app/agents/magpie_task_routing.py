"""Per-invocation pinning; retry only an inference that has delivered no bytes."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

import httpx

from app.services import magpie_routing_policy as overload
from app.services.magpie_routing_policy import TaskRoutingPolicy


class SlowLocalResponse(Exception):
    pass


def inference_path(path: str) -> bool:
    return path in {"v1/messages", "v1/chat/completions", "v1/responses"} or (
        path.startswith("v1beta/models/") and path.endswith((":generateContent", ":streamGenerateContent")))


def model_ref(response: httpx.Response) -> str:
    provider = response.headers.get("x-magpie-provider", "")
    model = response.headers.get("x-magpie-model", "")
    if not provider or not model:
        return ""
    return model if model.startswith(provider + "/") else f"{provider}/{model}"


def retarget(path: str, body: bytes, model: str) -> tuple[str, bytes]:
    if path.startswith("v1beta/models/"):
        return f"v1beta/models/{model}:{path.rsplit(':', 1)[1]}", body
    data = json.loads(body)
    data["model"] = model
    return path, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


def output_text(event: dict) -> str:
    """Count generated text/thinking/arguments, never heartbeats or prompt usage."""
    kind = event.get("type", "")
    delta = event.get("delta")
    if kind == "content_block_delta" and isinstance(delta, dict):
        return "".join(str(delta.get(k) or "") for k in ("text", "thinking", "partial_json"))
    if kind.startswith("response.") and kind.endswith(".delta") and isinstance(delta, str):
        return delta
    texts = []
    for choice in event.get("choices", []):
        d = choice.get("delta") or {}
        for key in ("content", "reasoning_content", "reasoning"):
            if isinstance(d.get(key), str):
                texts.append(d[key])
        for call in d.get("tool_calls") or []:
            texts.append((call.get("function") or {}).get("arguments") or "")
    for candidate in event.get("candidates", []):
        for part in (candidate.get("content") or {}).get("parts", []):
            texts.append(part.get("text") or "")
            if "functionCall" in part:
                texts.append(json.dumps(part["functionCall"], ensure_ascii=False))
    return "".join(texts)


class StreamMeter:
    def __init__(self, policy: TaskRoutingPolicy):
        self.policy = policy
        self.started = time.monotonic()
        self.first: float | None = None
        self.last = self.started
        self.samples: deque[tuple[float, float]] = deque()
        self.pending = b""

    def feed(self, chunk: bytes) -> None:
        self.pending += chunk
        # SSE data lines can arrive split over any number of TCP chunks.
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
                text = output_text(event) if isinstance(event, dict) else ""
            except (ValueError, TypeError):
                continue
            if text:
                now = time.monotonic()
                self.first = now if self.first is None else self.first
                self.last = now
                self.samples.append((now, len(text.encode("utf-8")) / 4))
        # A malformed stream cannot retain unbounded undecoded data.
        if len(self.pending) > 1024 * 1024:
            raise SlowLocalResponse("unreadable local stream")

    def reason(self) -> str:
        now, p = time.monotonic(), self.policy
        if self.first is None:
            return "first token timeout" if now - self.started >= p.first_token_seconds else ""
        if now - self.last >= p.idle_seconds:
            return "generation idle"
        if now - self.first >= p.probe_seconds:
            while self.samples and self.samples[0][0] < now - p.probe_seconds:
                self.samples.popleft()
            if sum(tokens for _, tokens in self.samples) / p.probe_seconds < p.min_tokens_per_second:
                return "low estimated token rate"
        return ""


class LocalStream:
    def __init__(self, response: httpx.Response, policy: TaskRoutingPolicy,
                 on_slow: Callable[[], Awaitable[None]]):
        self.response = response
        self.policy = policy
        self.on_slow = on_slow
        self.meter = StreamMeter(policy)
        self.iterator = response.aiter_bytes().__aiter__()
        self.pending: asyncio.Task | None = None
        self.prefix: list[bytes] = []
        self.done = False

    async def next_chunk(self) -> bytes | None:
        if self.pending is None:
            self.pending = asyncio.create_task(anext(self.iterator))
        ready, _ = await asyncio.wait({self.pending}, timeout=min(0.1, self.policy.probe_seconds / 4))
        if not ready:
            return None
        pending, self.pending = self.pending, None
        try:
            chunk = pending.result()
        except StopAsyncIteration:
            self.done = True
            return None
        self.meter.feed(chunk)
        return chunk

    async def prime(self) -> None:
        size = 0
        while not self.done:
            reason = self.meter.reason()
            if reason:
                raise SlowLocalResponse(reason)
            if (self.meter.first is not None and
                    time.monotonic() - self.meter.first >= self.policy.probe_seconds):
                return
            chunk = await self.next_chunk()
            if chunk is not None:
                self.prefix.append(chunk)
                size += len(chunk)
                if size >= 256 * 1024:
                    return  # A fast burst needs no additional buffering.

    async def chunks(self) -> AsyncIterator[bytes]:
        for chunk in self.prefix:
            yield chunk
        self.prefix.clear()
        marked = False
        while not self.done:
            if not marked and self.meter.reason():
                marked = True
                await self.on_slow()
            chunk = await self.next_chunk()
            if chunk is not None:
                yield chunk

    async def close(self) -> None:
        if self.pending is not None:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = None
        await self.response.aclose()


@dataclass
class Prepared:
    response: httpx.Response
    lock: asyncio.Lock
    local: LocalStream | None = None
    closed: bool = False

    async def chunks(self):
        try:
            stream = self.local.chunks() if self.local else self.response.aiter_bytes()
            async for chunk in stream:
                yield chunk
        finally:
            await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.local:
                await self.local.close()
            else:
                await self.response.aclose()
        finally:
            self.lock.release()


class TaskRoute:
    def __init__(self, base_url: str, client: httpx.AsyncClient, policy: TaskRoutingPolicy):
        self.base_url, self.client, self.policy = base_url, client, policy
        self.pinned = ""
        self.lock = asyncio.Lock()

    async def slow(self):
        self.pinned = self.policy.backup_model
        await overload.trip(self.base_url, self.policy)

    async def prepare(self, method: str, path: str, body: bytes, headers: dict,
                      params: list) -> Prepared:
        await self.lock.acquire()
        prepared = None
        try:
            p = self.policy
            target = self.pinned
            if target == p.local_model:
                target = p.backup_model if await overload.cooling(self.base_url, p) else p.local_group
            if not target:
                target = p.degraded_group if await overload.cooling(self.base_url, p) else p.entry_group
            for attempt in range(2):
                out_path, out_body = retarget(path, body, target)
                request = self.client.build_request(method, f"{self.base_url.rstrip('/')}/{out_path}",
                    content=out_body, headers=headers, params=params)
                try:
                    response = await self.client.send(request, stream=True)
                except httpx.HTTPError:
                    if attempt == 0 and target == p.local_group:
                        await self.slow()
                        target = p.backup_model
                        continue
                    raise
                prepared = Prepared(response, self.lock)
                actual = model_ref(response)
                if response.is_success and not actual:
                    raise httpx.RemoteProtocolError("Magpie did not identify the serving model")
                if (attempt == 0 and target == p.local_group and
                        (response.status_code in {408, 429} or response.status_code >= 500)):
                    await response.aclose()
                    prepared = None
                    await self.slow()
                    target = p.backup_model
                    continue
                if response.is_success and actual == p.local_model:
                    # Both executors stream inference. A non-SSE response keeps
                    # its transport timeout; its intermediate speed is unknown.
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        local = LocalStream(response, p, self.slow)
                        prepared.local = local
                        try:
                            await local.prime()
                        except (SlowLocalResponse, httpx.HTTPError):
                            await local.close()
                            prepared = None
                            await self.slow()
                            target = p.backup_model
                            if attempt == 0:
                                continue
                            raise
                    self.pinned = actual
                elif response.is_success:
                    if target == p.local_group and actual != p.local_model:
                        await overload.trip(self.base_url, p)
                    self.pinned = actual
                return prepared
            raise httpx.RemoteProtocolError("No task model answered")
        except BaseException:
            if prepared:
                await prepared.close()
            else:
                self.lock.release()
            raise
