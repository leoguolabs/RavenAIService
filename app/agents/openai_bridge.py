"""Per-run, loopback-only Messages → native OpenAI Responses protocol bridge.

The SDK remains the tool/permission runner. Only inference crosses this bridge;
no public proxy, process-global endpoint mutation, or alternate OCR model is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import socket
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


@dataclass(frozen=True)
class OpenAIConfig:
    base_url: str
    api_key: str
    model: str
    max_output_tokens: int = 8192
    timeout_seconds: float = 3600


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list) or any(not isinstance(x, dict) for x in content):
        raise ValueError("Unsupported message content / 不支持的消息内容")
    return content


def _image(block: dict) -> dict:
    source = block.get("source") or {}
    if source.get("type") == "base64":
        mime = source.get("media_type")
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise ValueError("Unsupported image type / 不支持的图片类型")
        return {"type": "input_image", "image_url": f"data:{mime};base64,{source['data']}"}
    if source.get("type") == "url":
        return {"type": "input_image", "image_url": source["url"]}
    raise ValueError("Unsupported image source / 不支持的图片来源")


def _tool_name(name: str) -> str:
    # SDK MCP names may exceed OpenAI's 64-character function-name limit.
    if len(name) <= 64 and all(c.isascii() and (c.isalnum() or c in "_-") for c in name):
        return name
    return "raven_" + hashlib.sha256(name.encode()).hexdigest()[:48]


def responses_request(body: dict, config: OpenAIConfig) -> tuple[dict, dict[str, str]]:
    """Translate full SDK history, retaining tool order and original image bytes."""
    names = {}
    tools = []
    for tool in body.get("tools") or []:
        name = str(tool.get("name") or "")
        if not name or "input_schema" not in tool:
            raise ValueError("Unsupported tool definition / 不支持的工具定义")
        alias = _tool_name(name)
        names[alias] = name
        tools.append({"type": "function", "name": alias,
                      "description": tool.get("description") or "",
                      "parameters": tool["input_schema"], "strict": False})

    items = []
    for message in body.get("messages") or []:
        role = message.get("role")
        if role not in ("user", "assistant", "system", "developer"):
            raise ValueError("Unsupported message role / 不支持的消息角色")
        pending = []

        def flush() -> None:
            if pending:
                items.append({"role": role, "content": list(pending)})
                pending.clear()

        for block in _blocks(message.get("content", "")):
            kind = block.get("type")
            if kind == "text":
                pending.append({"type": "output_text" if role == "assistant" else "input_text",
                                "text": block.get("text") or ""})
            elif kind == "image" and role == "user":
                pending.append(_image(block))
            elif kind in ("thinking", "redacted_thinking"):
                # These are SDK/provider-specific, never reusable OpenAI reasoning items.
                continue
            elif kind == "tool_use" and role == "assistant":
                flush()
                items.append({"type": "function_call", "call_id": block["id"],
                              "name": _tool_name(block["name"]),
                              "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)})
            elif kind == "tool_result" and role == "user":
                flush()
                output = []
                if block.get("is_error"):
                    output.append({"type": "input_text", "text": "Tool execution failed / 工具执行失败"})
                for part in _blocks(block.get("content", "")):
                    if part.get("type") == "text":
                        output.append({"type": "input_text", "text": part.get("text") or ""})
                    elif part.get("type") == "image":
                        output.append(_image(part))
                    else:
                        raise ValueError("Unsupported tool result content / 不支持的工具结果内容")
                items.append({"type": "function_call_output", "call_id": block["tool_use_id"],
                              "output": output or ""})
            else:
                raise ValueError(f"Unsupported content block: {kind} / 不支持的内容块")
        flush()

    system = "\n".join(str(x.get("text") or "") for x in _blocks(body.get("system", "")))
    result = {"model": config.model, "input": items, "store": False,
              "max_output_tokens": min(int(body.get("max_tokens") or config.max_output_tokens),
                                       config.max_output_tokens),
              "stream": bool(body.get("stream"))}
    if config.model.startswith(("gpt-5", "gpt-6", "o3", "o4")):
        result["include"] = ["reasoning.encrypted_content"]
    if system:
        result["instructions"] = system
    if tools:
        result["tools"] = tools
        choice = body.get("tool_choice") or {}
        kind = choice.get("type")
        if kind in ("auto", "none"):
            result["tool_choice"] = kind
        elif kind == "any":
            result["tool_choice"] = "required"
        elif kind == "tool":
            result["tool_choice"] = {"type": "function", "name": _tool_name(choice["name"])}
        if choice.get("disable_parallel_tool_use"):
            result["parallel_tool_calls"] = False
    return result, names


def _usage(response: dict) -> dict:
    usage = response.get("usage") or {}
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    return {"input_tokens": max(0, int(usage.get("input_tokens") or 0) - cached),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}


def _stop_reason(response: dict) -> str:
    if response.get("status") == "incomplete":
        return "max_tokens"
    return "tool_use" if any(x.get("type") == "function_call" for x in response.get("output") or []) else "end_turn"


def messages_response(response: dict, names: dict[str, str]) -> dict:
    content = []
    for item in response.get("output") or []:
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    content.append({"type": "text", "text": part.get("text") or ""})
                elif part.get("type") == "refusal":
                    content.append({"type": "text", "text": part.get("refusal") or ""})
        elif item.get("type") == "function_call":
            args = json.loads(item.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError("OpenAI returned non-object tool arguments")
            content.append({"type": "tool_use", "id": item["call_id"],
                            "name": names.get(item["name"], item["name"]), "input": args})
    return {"id": response.get("id") or "msg_" + secrets.token_hex(12),
            "type": "message", "role": "assistant", "model": response.get("model"),
            "content": content, "stop_reason": _stop_reason(response),
            "stop_sequence": None, "usage": _usage(response)}


def _error(message: str, kind: str = "api_error") -> dict:
    return {"type": "error", "error": {"type": kind, "message": message}}


def _upstream_error(status: int, payload: Any) -> dict:
    # Provider messages can echo keys/prompts. Expose only a bounded safe code.
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    code = str(code or "request_failed")
    if len(code) > 80 or not all(c.isalnum() or c in "_-" for c in code):
        code = "request_failed"
    kind = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
            404: "not_found_error", 429: "rate_limit_error"}.get(status, "api_error")
    return _error(f"OpenAI HTTP {status}: {code}", kind)


def _sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _response_events(response: httpx.Response) -> AsyncIterator[dict]:
    data = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                raw = "\n".join(data)
                data.clear()
                if raw != "[DONE]":
                    yield json.loads(raw)
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data and "\n".join(data) != "[DONE]":
        yield json.loads("\n".join(data))


async def messages_stream(response: httpx.Response, model: str,
                          names: dict[str, str],
                          on_response: Callable[[dict], None] | None = None) -> AsyncIterator[str]:
    """Translate live text/function argument deltas; require an explicit terminal."""
    started = False
    blocks: dict[tuple, dict] = {}
    message_id = "msg_" + secrets.token_hex(12)

    def start() -> list[dict]:
        nonlocal started
        if started:
            return []
        started = True
        return [{"type": "message_start", "message": {
            "id": message_id, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}}]

    def open_block(key: tuple, content: dict) -> list[dict]:
        if key in blocks:
            return []
        index = len(blocks)
        blocks[key] = {"index": index, "closed": False, "deltas": False}
        return start() + [{"type": "content_block_start", "index": index, "content_block": content}]

    def close_block(key: tuple) -> list[dict]:
        state = blocks.get(key)
        if not state or state["closed"]:
            return []
        state["closed"] = True
        return [{"type": "content_block_stop", "index": state["index"]}]

    try:
        async for event in _response_events(response):
            kind = event.get("type")
            output_index = event.get("output_index", 0)
            emitted = []
            if kind == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    key = (output_index, "tool")
                    emitted = open_block(key, {"type": "tool_use", "id": item["call_id"],
                        "name": names.get(item["name"], item["name"]), "input": {}})
            elif kind in ("response.output_text.delta", "response.refusal.delta"):
                key = (output_index, event.get("content_index", 0))
                emitted = open_block(key, {"type": "text", "text": ""})
                blocks[key]["deltas"] = True
                emitted.append({"type": "content_block_delta", "index": blocks[key]["index"],
                    "delta": {"type": "text_delta", "text": event.get("delta") or ""}})
            elif kind == "response.function_call_arguments.delta":
                key = (output_index, "tool")
                if key not in blocks:
                    raise ValueError("Function arguments arrived before the tool definition")
                blocks[key]["deltas"] = True
                emitted.append({"type": "content_block_delta", "index": blocks[key]["index"],
                    "delta": {"type": "input_json_delta", "partial_json": event.get("delta") or ""}})
            elif kind == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    key = (output_index, "tool")
                    emitted = open_block(key, {"type": "tool_use", "id": item["call_id"],
                        "name": names.get(item["name"], item["name"]), "input": {}})
                    if not blocks[key]["deltas"]:
                        emitted.append({"type": "content_block_delta", "index": blocks[key]["index"],
                            "delta": {"type": "input_json_delta", "partial_json": item.get("arguments") or "{}"}})
                    emitted += close_block(key)
                elif item.get("type") == "message":
                    for content_index, part in enumerate(item.get("content") or []):
                        key = (output_index, content_index)
                        if part.get("type") not in ("output_text", "refusal"):
                            continue
                        emitted += open_block(key, {"type": "text", "text": ""})
                        if not blocks[key]["deltas"]:
                            emitted.append({"type": "content_block_delta", "index": blocks[key]["index"],
                                "delta": {"type": "text_delta", "text": part.get("text") or part.get("refusal") or ""}})
                        emitted += close_block(key)
            elif kind in ("response.completed", "response.incomplete"):
                final = event.get("response") or {}
                # Validate terminal tool arguments even when deltas were emitted.
                messages_response(final, names)
                if on_response is not None:
                    on_response(final)
                emitted = start()
                for key in blocks:
                    emitted += close_block(key)
                emitted += [{"type": "message_delta", "delta": {
                    "stop_reason": _stop_reason(final), "stop_sequence": None}, "usage": _usage(final)},
                    {"type": "message_stop"}]
                for item in emitted:
                    yield _sse(item)
                return
            elif kind in ("error", "response.failed"):
                yield _sse(_upstream_error(502, event.get("response") or event))
                return
            for item in emitted:
                yield _sse(item)
        yield _sse(_error("OpenAI stream ended without a terminal response / 上游流缺少终态"))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        yield _sse(_error("Invalid or interrupted OpenAI response stream / 上游响应流无效或中断"))


def create_bridge_app(config: OpenAIConfig, token: str, client: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    # Encrypted reasoning belongs to this one run, not the SDK transcript or DB.
    # Responses reasoning models require it alongside subsequent tool outputs.
    reasoning_by_call: dict[str, list[dict]] = {}

    def remember_reasoning(response: dict) -> None:
        reasoning = [item for item in response.get("output") or []
                     if item.get("type") == "reasoning" and item.get("encrypted_content")]
        for item in response.get("output") or []:
            if item.get("type") == "function_call" and reasoning:
                reasoning_by_call[item["call_id"]] = reasoning

    async def authorize(request: Request) -> bool:
        supplied = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ")
        return hmac.compare_digest(supplied.encode(), token.encode())

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request):
        if not await authorize(request):
            return JSONResponse(_error("Unauthorized", "authentication_error"), status_code=401)
        # Context-management hint only (never billing): UTF-8 bytes conservatively
        # upper-bound tokens without sending another inference request.
        raw = await request.body()
        return {"input_tokens": len(raw)}

    @app.post("/v1/messages")
    async def messages(request: Request):
        if not await authorize(request):
            return JSONResponse(_error("Unauthorized", "authentication_error"), status_code=401)
        try:
            body, names = responses_request(await request.json(), config)
        except (ValueError, KeyError, TypeError):
            return JSONResponse(_error("Unsupported Messages payload / 不支持的消息格式", "invalid_request_error"), status_code=400)
        expanded = []
        included_reasoning = set()
        for item in body["input"]:
            if item.get("type") == "function_call":
                for reasoning in reasoning_by_call.get(item["call_id"], []):
                    identity = reasoning.get("id") or reasoning["encrypted_content"]
                    if identity not in included_reasoning:
                        expanded.append(reasoning)
                        included_reasoning.add(identity)
            expanded.append(item)
        body["input"] = expanded
        try:
            upstream = await client.send(client.build_request(
                "POST", config.base_url.rstrip("/") + "/responses", json=body,
                headers={"Authorization": "Bearer " + config.api_key}), stream=True)
        except httpx.TimeoutException:
            return JSONResponse(_error("OpenAI request timed out / 上游请求超时"), status_code=504)
        except httpx.HTTPError:
            return JSONResponse(_error("OpenAI connection failed / 无法连接上游"), status_code=502)
        if upstream.status_code >= 400:
            try:
                await upstream.aread()
                try:
                    payload = upstream.json()
                except ValueError:
                    payload = {}
                return JSONResponse(_upstream_error(upstream.status_code, payload), status_code=upstream.status_code)
            finally:
                await upstream.aclose()
        if body["stream"]:
            async def stream():
                try:
                    async for chunk in messages_stream(upstream, config.model, names, remember_reasoning):
                        yield chunk
                finally:
                    await upstream.aclose()
            return StreamingResponse(stream(), media_type="text/event-stream")
        try:
            await upstream.aread()
            payload = upstream.json()
            if payload.get("status") not in ("completed", "incomplete"):
                return JSONResponse(_upstream_error(502, payload), status_code=502)
            remember_reasoning(payload)
            return messages_response(payload, names)
        except (ValueError, KeyError, TypeError):
            return JSONResponse(_error("Invalid OpenAI response / 无效的上游响应"), status_code=502)
        finally:
            await upstream.aclose()

    return app


class _LoopbackServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The enclosing FastAPI/Celery process owns its signal handlers.
        yield

    def install_signal_handlers(self):
        pass  # Compatibility with older uvicorn versions.


@asynccontextmanager
async def openai_bridge(config: OpenAIConfig):
    token = secrets.token_urlsafe(32)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    async with httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds, connect=15)) as client:
        app = create_bridge_app(config, token, client)
        server = _LoopbackServer(uvicorn.Config(app, log_level="error", access_log=False,
            lifespan="off", timeout_graceful_shutdown=2))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        task.result()
                        raise RuntimeError("OpenAI loopback bridge failed to start")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}", token
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            finally:
                sock.close()
