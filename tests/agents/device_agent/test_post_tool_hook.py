"""Unit tests for app/agents/device_agent/post_tool_hook.py."""

from __future__ import annotations

import json
from typing import List

import pytest

from app.agents.device_agent.mcp_tools import ToolMeta
from app.agents.device_agent.post_tool_hook import build_post_tool_use_hook
from app.agents.device_agent.trace import RESULT_VALIDATION, SeqCounter


def _meta(*, output_schema=None) -> ToolMeta:
    return ToolMeta(
        sdk_name="mcp__device__task__list",
        server="task",
        tool="list",
        description="",
        input_schema={"type": "object"},
        output_schema=output_schema,
    )


def _wrap(content) -> dict:
    """模拟 SDK 透传给 PostToolUse hook 的 tool_response 结构。"""
    if isinstance(content, str):
        text = content
    else:
        text = json.dumps(content, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}]}


def _run_hook(hook_obj, tool_name: str, tool_response):
    import asyncio

    validator = hook_obj.hooks[0]
    return asyncio.run(
        validator(
            {"hook_event_name": "PostToolUse", "tool_name": tool_name, "tool_response": tool_response},
            "tool-use-id-1",
            {"signal": None},
        )
    )


def _events_and_hook(meta_map, *, excerpt_bytes=16 * 1024, max_bytes=256 * 1024):
    events: List[dict] = []
    seq = SeqCounter()
    hook = build_post_tool_use_hook(
        meta_map,
        excerpt_bytes=excerpt_bytes,
        max_bytes=max_bytes,
        emit=events.append,
        seq_counter=seq,
        task_id="t1",
    )
    return hook, events


def _extract_modified(output: dict) -> dict:
    inner = output["hookSpecificOutput"]["updatedMCPToolOutput"]
    return json.loads(inner["content"][0]["text"])


class TestPostToolHook:
    @pytest.mark.parametrize("wrapped", [False, True])
    def test_executor_mcp_response_shapes_preserve_evidence(self, wrapped):
        meta = _meta(output_schema={"type": "object", "required": ["text"]})
        hook, events = _events_and_hook({meta.sdk_name: meta})
        response = _wrap({"status": "ok", "result": {"text": "random-proof"}})
        out = _run_hook(hook, meta.sdk_name, response if wrapped else response["content"])
        assert _extract_modified(out) == {"status": "ok", "result": {"text": "random-proof"}}
        assert events[-1]["status"] == "ok"

    def test_ok_passes_through(self):
        meta = _meta()
        hook, events = _events_and_hook({meta.sdk_name: meta})
        out = _run_hook(hook, meta.sdk_name, _wrap({"status": "ok", "result": {"items": [1, 2, 3]}}))
        modified = _extract_modified(out)
        assert modified["status"] == "ok"
        assert modified["result"] == {"items": [1, 2, 3]}
        assert events[-1]["type"] == RESULT_VALIDATION
        assert events[-1]["status"] == "ok"

    def test_schema_mismatch_missing_required(self):
        meta = _meta(output_schema={"type": "object", "required": ["count"]})
        hook, events = _events_and_hook({meta.sdk_name: meta})
        out = _run_hook(hook, meta.sdk_name, _wrap({"status": "ok", "result": {"items": []}}))
        modified = _extract_modified(out)
        assert modified["error_kind"] == "schema_mismatch"
        assert "count" in modified["reason"]
        assert events[-1]["status"] == "schema_mismatch"

    def test_schema_mismatch_wrong_type(self):
        meta = _meta(output_schema={"type": "array"})
        hook, _ = _events_and_hook({meta.sdk_name: meta})
        out = _run_hook(hook, meta.sdk_name, _wrap({"status": "ok", "result": {"x": 1}}))
        modified = _extract_modified(out)
        assert modified["error_kind"] == "schema_mismatch"

    def test_known_error_passes_through(self):
        meta = _meta()
        hook, events = _events_and_hook({meta.sdk_name: meta})
        out = _run_hook(
            hook, meta.sdk_name,
            _wrap({"status": "error", "error_kind": "device_offline", "error_message": "lost link"}),
        )
        modified = _extract_modified(out)
        assert modified["status"] == "error"
        assert modified["error_kind"] == "device_offline"
        assert modified["error_message"] == "lost link"
        assert events[-1]["status"] == "ok"  # 已知 error 是合法回包

    def test_unknown_error_kind_normalized(self):
        meta = _meta()
        hook, _ = _events_and_hook({meta.sdk_name: meta})
        out = _run_hook(
            hook, meta.sdk_name,
            _wrap({"status": "error", "error_kind": "weird_thing"}),
        )
        modified = _extract_modified(out)
        assert modified["error_kind"] == "internal_error"

    def test_oversize_replaced(self):
        meta = _meta()
        # max_bytes=200 -- 强制超限
        hook, events = _events_and_hook({meta.sdk_name: meta}, excerpt_bytes=80, max_bytes=200)
        huge = {"status": "ok", "result": {"blob": "x" * 1000}}
        out = _run_hook(hook, meta.sdk_name, _wrap(huge))
        modified = _extract_modified(out)
        assert modified["error_kind"] == "result_too_large"
        assert events[-1]["status"] == "error"

    def test_evidence_truncated(self):
        meta = _meta()
        hook, events = _events_and_hook({meta.sdk_name: meta}, excerpt_bytes=20)
        out = _run_hook(
            hook, meta.sdk_name,
            _wrap({
                "status": "ok",
                "result": {"ok": True},
                "evidence": [
                    {"label": "log", "text": "a" * 200},
                    {"label": "short", "text": "ok"},
                ],
            }),
        )
        modified = _extract_modified(out)
        assert modified["evidence"][0]["truncated"] is True
        assert "truncated" not in modified["evidence"][1]
        assert events[-1]["status"] in {"ok", "truncated"}

    def test_token_url_masked(self):
        meta = _meta()
        hook, _ = _events_and_hook({meta.sdk_name: meta})
        out = _run_hook(
            hook, meta.sdk_name,
            _wrap({"status": "ok", "result": {"url": "https://x:abcdef@host/path"}}),
        )
        modified = _extract_modified(out)
        assert modified["result"]["url"] == "https://***@host/path"

    def test_token_url_in_evidence_masked(self):
        meta = _meta()
        hook, _ = _events_and_hook({meta.sdk_name: meta})
        out = _run_hook(
            hook, meta.sdk_name,
            _wrap({
                "status": "ok",
                "result": {"ok": True},
                "evidence": [{"label": "url", "text": "see https://u:tok@host/x"}],
            }),
        )
        modified = _extract_modified(out)
        assert "tok" not in modified["evidence"][0]["text"]
        assert "https://***@host" in modified["evidence"][0]["text"]

    def test_non_dict_tool_response_handled(self):
        meta = _meta()
        hook, _ = _events_and_hook({meta.sdk_name: meta})
        # SDK 可能直接给 dict（非 in-process tool 形态）
        out = _run_hook(hook, meta.sdk_name, {"status": "ok", "result": {"v": 1}})
        modified = _extract_modified(out)
        assert modified["status"] == "ok"
