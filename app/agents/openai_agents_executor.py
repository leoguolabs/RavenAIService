"""Official OpenAI Agents SDK runtime adapted to Raven's message contract.

Each invocation owns its client, model, tools and tracing policy. Magpie still
owns provider selection; no process-global OpenAI credentials are configured.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import suppress

from claude_agent_sdk import (
    AssistantMessage, ResultMessage, SystemMessage, TextBlock, ThinkingBlock,
    ToolResultBlock, ToolUseBlock, UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from app.agents.antigravity_control import control_bridge
from app.agents.clarification_pause import cancelled_message, continuation_prompt, current_pause
from app.agents.openai_agents_tools import TOOLS, WorkspaceTools


def _json(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


async def input_items(prompt, cwd):
    from app.agents.provider_query import workspace_image_blocks

    if isinstance(prompt, str):
        items = [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}]
    else:
        items = []
        async for row in prompt:
            message = row.get("message", row)
            content = message.get("content", "")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            converted = []
            for block in blocks:
                if block.get("type") == "text":
                    converted.append({"type": "input_text", "text": block["text"]})
                elif block.get("type") == "image" and block.get("source", {}).get("type") == "base64":
                    source = block["source"]
                    converted.append({"type": "input_image", "detail": "auto",
                        "image_url": f"data:{source['media_type']};base64,{source['data']}"})
                else:
                    raise ValueError("Unsupported OpenAI Agent input block / 不支持的输入类型")
            items.append({"role": "user", "content": converted})
    images = workspace_image_blocks(str(cwd))
    if images and not any(block["type"] == "input_image" for item in items for block in item["content"]):
        for image in images:
            source = image["source"]
            items[-1]["content"].append({"type": "input_image", "detail": "auto",
                "image_url": f"data:{source['media_type']};base64,{source['data']}"})
    return items


def build_tools(control, emit, model):
    from agents import FunctionTool

    options = control.options
    workspace = WorkspaceTools(options)
    tools = []
    tool_lock = asyncio.Lock()

    def register(name, description, parameters, server=None, remote_name=None):
        async def execute(context, raw):
            tool_id = context.tool_call_id
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be a JSON object")
            await emit(AssistantMessage(content=[ToolUseBlock(id=tool_id, name=name, input=args)], model=model))
            error = False
            # Parallel tool calls are disabled, so hook context belongs to this call.
            control.pending_context.clear()
            try:
                if server is not None:
                    control.pending_tool_ids.setdefault((name, json.dumps(args, sort_keys=True)), []).append(tool_id)
                    output = await control.call(server, remote_name, args)
                    error = bool(output.get("isError")) if isinstance(output, dict) else False
                else:
                    allowed, updated, reason = await control.authorize(name, args, tool_id)
                    if not allowed:
                        output, error = reason, True
                    else:
                        output = await workspace.call(name, updated)
                        error = bool(output.get("exit_code")) if isinstance(output, dict) else False
                        post = await control.hooks("PostToolUse", name, updated, tool_id, output)
                        output = post.get("updatedMCPToolOutput", output)
            except (OSError, ValueError, TimeoutError) as exc:
                output, error = str(exc) or type(exc).__name__, True
            extra = "\n\nRaven tool context:\n" + "\n".join(control.pending_context) if control.pending_context else ""
            control.pending_context.clear()
            rendered = _json(output) + extra
            await emit(UserMessage(content=[ToolResultBlock(tool_use_id=tool_id, content=rendered, is_error=error)]))
            if isinstance(output, dict) and any(block.get("type") == "image" for block in output.get("content", [])):
                blocks = []
                for block in output["content"]:
                    if block.get("type") == "image":
                        blocks.append({"type": "image", "image_url": f"data:{block['mimeType']};base64,{block['data']}"})
                    elif block.get("type") == "text":
                        blocks.append({"type": "text", "text": block["text"]})
                if extra:
                    blocks.append({"type": "text", "text": extra})
                return blocks
            return rendered

        async def invoke(context, raw):
            async with tool_lock:
                return await execute(context, raw)

        wire_name = name if len(name) <= 64 else "raven_" + hashlib.sha256(name.encode()).hexdigest()[:48]
        tools.append(FunctionTool(name=wire_name, description=description, params_json_schema=parameters,
            on_invoke_tool=invoke, strict_json_schema=False))

    disabled = set(options.disallowed_tools or [])
    for name, (description, parameters) in TOOLS.items():
        # Preserve the Agent's explicit tool surface; bypassPermissions is not
        # interpreted as permission to advertise unrelated native tools.
        if name in (options.allowed_tools or []) and name not in disabled:
            register(name, description, parameters)
    for server, catalog in control.catalog.items():
        for tool in catalog:
            name = f"mcp__{server}__{tool['name']}"
            if name not in disabled:
                register(name, tool.get("description", ""), tool["inputSchema"], server, tool["name"])
    return tools


async def query(*, prompt, options):
    from agents import Agent, ModelSettings, OpenAIResponsesModel, RunConfig, Runner
    from agents.agent import ToolsToFinalOutputResult
    from agents.exceptions import MaxTurnsExceeded
    from openai import AsyncOpenAI

    model = str(options.model)
    session_id = str(uuid.uuid4())
    started = time.monotonic()
    timeout = float(options.env.get("API_TIMEOUT_MS", "3600000")) / 1000
    max_tokens = options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")
    max_turns = options.max_turns or 100
    queue = asyncio.Queue(maxsize=128)
    pause = current_pause.get()

    def stop_for_clarification(_context, _results):
        return ToolsToFinalOutputResult(
            is_final_output=bool(pause.pending),
            final_output="Waiting for user clarification",
        )

    async def emit(message):
        await queue.put(("message", message))

    async def produce():
        run = None
        turns = 0
        stops = 0
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        try:
            async with asyncio.timeout(timeout) as deadline, control_bridge(options) as (control, _, _token):
                system = options.system_prompt
                instructions = system.get("append", "") if isinstance(system, dict) else (system or "")
                # Raven's own prompt advertises materialized Skills. The generic
                # Antigravity catalog is unnecessary with actual function tools.
                control.pending_context.clear()
                async with AsyncOpenAI(base_url=options.env["ANTHROPIC_BASE_URL"].rstrip("/") + "/v1",
                        api_key=options.env["ANTHROPIC_API_KEY"], max_retries=0, timeout=timeout) as client:
                    agent = Agent(name="Raven", instructions=instructions,
                        model=OpenAIResponsesModel(model=model, openai_client=client),
                        tools=build_tools(control, emit, model),
                        tool_use_behavior=stop_for_clarification if pause is not None else "run_llm_again",
                        model_settings=ModelSettings(parallel_tool_calls=False, store=False,
                            max_tokens=int(max_tokens) if max_tokens else None))
                    inputs = await input_items(prompt, options.cwd)
                    await emit(SystemMessage(subtype="init", data={"session_id": session_id, "model": model}))
                    while turns < max_turns:
                        run = Runner.run_streamed(agent, input=inputs, max_turns=max_turns - turns,
                            run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False))
                        async for event in run.stream_events():
                            if event.type in {"raw_response_event", "raw_model_stream_event"}:
                                data = event.data
                                if data.type == "response.completed":
                                    turns += 1
                                    current = data.response.usage
                                    if current is not None:
                                        cached = getattr(current.input_tokens_details, "cached_tokens", 0) or 0
                                        usage["input_tokens"] += max(0, current.input_tokens - cached)
                                        usage["output_tokens"] += current.output_tokens
                                        usage["cache_read_input_tokens"] += cached
                                        usage["cache_creation_input_tokens"] += getattr(current.input_tokens_details, "cache_write_tokens", 0) or 0
                                elif data.type == "response.output_text.delta" and data.delta:
                                    await emit(StreamEvent(uuid=str(uuid.uuid4()), session_id=session_id,
                                        event={"type": "content_block_delta", "index": data.content_index,
                                               "delta": {"type": "text_delta", "text": data.delta}}))
                                elif data.type == "response.reasoning_summary_text.done" and data.text:
                                    await emit(AssistantMessage(content=[ThinkingBlock(thinking=data.text, signature="")], model=model))
                            elif event.type == "run_item_stream_event" and event.item.type == "message_output_item":
                                from agents import ItemHelpers
                                text = ItemHelpers.text_message_output(event.item)
                                if text:
                                    await emit(AssistantMessage(content=[TextBlock(text=text)], model=model))
                        if pause is not None and pause.pending:
                            answers = await pause.wait(segment_timeout=deadline)
                            if answers is None:
                                await emit(cancelled_message())
                                return
                            inputs = run.to_input_list() + [{"role": "user", "content": continuation_prompt(answers)}]
                            continue
                        control.last_assistant_message = str(run.final_output or "")
                        stops += 1
                        decision = await control.native_hook("Stop", {
                            "conversationId": session_id, "executionNum": stops})
                        if decision.get("decision") != "continue":
                            await emit(ResultMessage(subtype="success", duration_ms=int((time.monotonic() - started) * 1000),
                                duration_api_ms=0, is_error=False, num_turns=turns, session_id=session_id,
                                usage=usage, result=control.last_assistant_message))
                            return
                        inputs = run.to_input_list() + [{"role": "user", "content": decision["reason"]}]
                    raise MaxTurnsExceeded("Raven maximum Agent turns reached")
        except MaxTurnsExceeded:
            await emit(ResultMessage(subtype="error_max_turns", duration_ms=int((time.monotonic() - started) * 1000),
                duration_api_ms=0, is_error=True, num_turns=max_turns, session_id=session_id, usage=usage,
                result="", errors=["达到 Agent 最大轮次 / Maximum Agent turns reached"]))
        except Exception as exc:
            await queue.put(("error", exc))
        finally:
            if run is not None:
                run.cancel()
                with suppress(Exception):
                    async for _ in run.stream_events():
                        pass
                if run.run_loop_task is not None:
                    await asyncio.gather(run.run_loop_task, return_exceptions=True)
            if not asyncio.current_task().cancelling():
                await queue.put(("end", None))

    producer = asyncio.create_task(produce())
    try:
        while True:
            kind, value = await queue.get()
            if kind == "end":
                break
            if kind == "error":
                raise value
            yield value
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
