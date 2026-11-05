"""Default-off original-profile checkpoint with native SDK session resumption."""

from __future__ import annotations

import json
import logging
from contextlib import aclosing
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

logger = logging.getLogger(__name__)

_FINAL_ZH = """RAVEN_EVIDENCE_FINAL
现在基于本会话保留的原始日志和源码证据，回答最初的问题并输出原要求的完整 JSON。
先保留已证实的发现、反证、部分进展和未确定事项；不要为得到确定根因而穷尽所有可能路径。
如有会改变所问结论的具体证据缺口，仍可用全部原工具补查，并合并独立检查；不要省略决定性附件或失败分支。
缺少记录不能证明没有发生、排除执行方或证明完成；当前源码与现场部署未核对时，只能说明可能路径。
摘要、正文、假设和建议保持相同确定程度；建议先核验未证实的前提。全部必需 Skill、项目指令、引用和 schema 保留。
恢复后 Bash 从原工作区开始，使用工作区内绝对路径或显式 cd，不依赖上阶段的目录变化。
"""
_FINAL_EN = """RAVEN_EVIDENCE_FINAL
Answer the original question from this session's retained native log/source evidence using the original complete JSON schema.
Retain supported findings, counterevidence, partial progress and unknowns; do not exhaust every possible path to invent a definite cause.
All original tools remain available for specific gaps that could change the requested findings; group independent checks and retain decisive attachments/failure branches.
Missing records do not prove non-occurrence, exclude actors or establish completion. Unverified deployed source only describes possible behavior.
Keep certainty consistent across all public fields and verify uncertain recommendation premises. Preserve full required Skills, project instructions and citations.
Bash resumes at the original workspace: use contained absolute paths or explicit cd rather than a previous working-directory change.
"""


def _permissions_only(path):
    """Allow inert permission lists, never hooks or other SDK configuration."""
    if path.is_symlink():
        return False
    if not path.exists():
        return True
    try:
        if path.stat().st_size > 65536:
            return False
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate settings key")
                result[key] = value
            return result
        value = json.loads(path.read_text(), object_pairs_hook=unique)
        if not isinstance(value, dict) or set(value) != {"permissions"}:
            return False
        permissions = value["permissions"]
        return (isinstance(permissions, dict) and bool(permissions)
                and set(permissions) <= {"allow", "deny", "ask"}
                and all(isinstance(items, list) and all(isinstance(x, str) for x in items)
                        for items in permissions.values()))
    except (OSError, ValueError, UnicodeError):
        return False


def supported(options, provider):
    """Unsupported profiles retain the original query, including its hooks."""
    if not (
        provider == "yinhe" and version("claude-agent-sdk") == "0.2.165"
        and options.model == "yinhe-thinking" and isinstance(options.system_prompt, str)
        and type(options.max_turns) is int and options.max_turns >= 2
        and not options.hooks and not options.resume and not options.continue_conversation
        and not options.fork_session and options.max_budget_usd is None
        and not options.settings and not options.plugins and not options.agents
        and not options.cli_path and not options.extra_args and not options.add_dirs
        and options.setting_sources in ([], ["project"])
    ):
        return False
    cwd = Path(options.cwd or ".").resolve()
    return all(
        _permissions_only(parent / ".claude" / name)
        for parent in (cwd, *cwd.parents)
        for name in ("settings.json", "settings.local.json")
    )


class CompletionCheckpoint:
    """One bounded checkpoint after completed tools; never interrupt a tool."""

    def __init__(self, after_results=50):
        if type(after_results) is not int or not 0 < after_results <= 1024:
            raise ValueError("Checkpoint threshold must be a bounded positive integer")
        self.threshold = after_results
        self.seen = set()
        self.pending = set()
        self.disabled = False
        self.disable_reason = None
        self.triggered = False

    def _disable(self, reason):
        self.disabled = True
        self.disable_reason = reason
        logger.info("stage_checkpoint: disabled reason=%s completed_results=%d", reason, len(self.seen))
        self.pending.clear()

    @staticmethod
    def _identity(data, tool_id):
        identity = data.get("tool_use_id") or tool_id
        return identity if isinstance(identity, str) and 0 < len(identity) <= 256 else None

    async def before_tool(self, data, tool_id, context):
        if self.disabled or not isinstance(data, dict):
            return {}
        payload = data.get("tool_input")
        if not isinstance(payload, dict) or data.get("tool_name") in ("Task", "Agent", "TaskOutput", "TaskStop") or payload.get("run_in_background"):
            self._disable("background_or_unsupported_tool")
            return {}
        identity = self._identity(data, tool_id)
        if not identity:
            self._disable("missing_tool_identity")
            return {}
        if len(self.seen | self.pending) >= 1024:
            self._disable("tool_identity_bound")
            return {}
        # Hooks run on the SDK control channel. Do not wait for the outer
        # backpressured message consumer to reach an AssistantMessage.
        if identity not in self.seen:
            self.pending.add(identity)
        return {}

    async def after_tool(self, data, tool_id, context):
        if self.disabled or not isinstance(data, dict):
            return {}
        identity = self._identity(data, tool_id)
        if not identity:
            self._disable("missing_tool_identity")
            return {}
        if data.get("hook_event_name") == "PostToolUseFailure":
            if data.get("is_interrupt"):
                self._disable("interrupted_tool")
                return {}
        elif data.get("tool_name") == "Bash":
            response = data.get("tool_response")
            complete_keys = {"stdout", "stderr", "interrupted", "isImage", "noOutputExpected", "returnCodeInterpretation"}
            if not isinstance(response, dict) or set(response) - complete_keys or response.get("interrupted"):
                self._disable("background_or_unsupported_Bash_response")
                return {}
        if len(self.seen | self.pending) >= 1024:
            self._disable("tool_identity_bound")
            return {}
        self.pending.discard(identity)
        self.seen.add(identity)
        if (
            data.get("hook_event_name") != "PostToolUseFailure"
            and not self.triggered and len(self.seen) >= self.threshold and not self.pending
        ):
            self.triggered = True
            # The CLI drops additionalContext on this stopping callback. Other
            # hooks are unsupported; do not attach evidence to this response.
            # continueFalse is not honored on PostToolUseFailure: count that
            # completion but defer the boundary to a successful tool callback.
            return {"continue_": False, "stopReason": "Server evidence-stage checkpoint; final stage retains the original tool history."}
        return {}


def _combine(first, last):
    usage = dict(last.usage or {})
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        usage[key] = ((first.usage or {}).get(key) or 0) + ((last.usage or {}).get(key) or 0)
    for key in ("output_tokens_details", "server_tool_use", "cache_creation"):
        a, b = (first.usage or {}).get(key) or {}, (last.usage or {}).get(key) or {}
        if isinstance(a, dict) and isinstance(b, dict):
            usage[key] = {**a, **b}
            for field in a.keys() | b.keys():
                x, y = a.get(field, 0), b.get(field, 0)
                if type(x) in (int, float) and type(y) in (int, float):
                    usage[key][field] = x + y
    # Native proof: duration/counters/usage are per invocation; API duration and
    # total cost at the resumed final already include the session's first stage.
    return replace(last, usage=usage, num_turns=first.num_turns + last.num_turns,
                   duration_ms=first.duration_ms + last.duration_ms)


def make_checkpoint_query(sdk_query, *, provider, locale, check_cancel, on_phase, after_results=50):
    from claude_agent_sdk import HookMatcher, ResultMessage

    async def query(*, prompt, options):
        eligible = supported(options, provider)
        logger.info("stage_checkpoint: eligible=%s", eligible)
        if not eligible:
            async with aclosing(sdk_query(prompt=prompt, options=options)) as stream:
                async for message in stream:
                    yield message
            return
        checkpoint = CompletionCheckpoint(after_results)
        hooks = {"PreToolUse": [HookMatcher(hooks=[checkpoint.before_tool])],
                 "PostToolUse": [HookMatcher(hooks=[checkpoint.after_tool])],
                 "PostToolUseFailure": [HookMatcher(hooks=[checkpoint.after_tool])]}
        first = None
        try:
            async with aclosing(sdk_query(prompt=prompt, options=replace(options, hooks=hooks))) as stream:
                async for message in stream:
                    check_cancel()
                    if isinstance(message, ResultMessage) and checkpoint.triggered:
                        if first is not None:
                            raise RuntimeError("SDK checkpoint returned multiple terminal results")
                        first = message
                    else:
                        yield message
        finally:
            logger.info("stage_checkpoint: stopped=%s disabled=%s reason=%s completed_results=%d pending=%d",
                        checkpoint.triggered, checkpoint.disabled, checkpoint.disable_reason,
                        len(checkpoint.seen), len(checkpoint.pending))
        check_cancel()
        if not checkpoint.triggered:
            return
        if checkpoint.disabled or checkpoint.pending or first is None:
            raise RuntimeError("SDK checkpoint cannot safely resume its completed tool history")
        if first.is_error or first.subtype != "success":
            yield first
            return
        if not isinstance(first.session_id, str) or not first.session_id or type(first.num_turns) is not int or not 0 < first.num_turns < options.max_turns:
            raise RuntimeError("SDK checkpoint lacks a verified remaining session budget")
        check_cancel()
        on_phase("finalization")
        check_cancel()
        final_options = replace(options, resume=first.session_id, max_turns=options.max_turns - first.num_turns)
        last = None
        async with aclosing(sdk_query(prompt=_FINAL_EN if locale == "en" else _FINAL_ZH, options=final_options)) as stream:
            async for message in stream:
                check_cancel()
                if isinstance(message, ResultMessage):
                    if last is not None:
                        raise RuntimeError("SDK final stage returned multiple terminal results")
                    last = message
                else:
                    yield message
        check_cancel()
        if last is None or last.session_id != first.session_id:
            raise RuntimeError("SDK final stage did not retain the evidence session")
        if type(last.num_turns) is not int or last.num_turns < 0 or (not last.is_error and last.num_turns > final_options.max_turns):
            raise RuntimeError("SDK final stage returned invalid turn accounting")
        yield _combine(first, last)

    return query
