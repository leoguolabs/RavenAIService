"""
Claude Agent SDK Bug Fix Coding Agent。

写入型 Agent：在隔离工作区内基于真实源码做最小改动修复，按问题维度产出一个或
多个 Merge Request，最终输出结构化 ``merge_requests`` 数组。

主入口:
  BugFixCodingAgent().run(ctx)      — async, returns dict
  BugFixCodingAgent().run_sync(ctx) — sync wrapper for Celery
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.agents.bug_fix.workspace import BugFixWorkspaceContext
from app.agents.log_analysis.trace import mask_input, mask_tokens

logger = logging.getLogger(__name__)

# 写入型工具集：读 + 写 + Bash（git / curl）。
ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]

AGENT_KEY = "bug_fix"


def _extract_final_json(text: str) -> Optional[Dict[str, Any]]:
    """从 Agent 输出中提取最后一个围栏 JSON 块（``merge_requests`` 契约）。

    优先匹配 ```json ... ``` 块；多块时取最后一个完整对象。退化到裸 JSON 对象。
    """
    if not text:
        return None
    candidates: List[str] = []
    # ```json ... ``` 或 ``` ... ```
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(m.group(1))
    for body in reversed(candidates):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    # 退化：找文本里最后一个平衡的 JSON 对象。
    last = _last_balanced_object(text)
    if last is not None:
        try:
            parsed = json.loads(last)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def _last_balanced_object(text: str) -> Optional[str]:
    """返回文本中最后一个大括号平衡的 JSON 对象子串（best-effort）。"""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        i = end
        while i >= 0:
            if text[i] == "}":
                depth += 1
            elif text[i] == "{":
                depth -= 1
                if depth == 0:
                    return text[i : end + 1]
            i -= 1
        end = text.rfind("}", 0, end)
    return None


def _normalize_merge_requests(value: Any) -> List[Dict[str, Any]]:
    """校验并规范化 merge_requests 数组，剔除明显不完整的项。token 脱敏。"""
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        branch = item.get("branch_name")
        if not branch:
            continue  # 没分支名的 MR 无意义
        mr_url = item.get("mr_url")
        out.append(
            {
                "title": str(item.get("title") or "Bug fix"),
                "description": mask_tokens(str(item.get("description") or "")),
                "branch_name": str(branch),
                "base_branch": str(item.get("base_branch") or ""),
                "mr_url": mask_tokens(str(mr_url)) if mr_url else None,
                "mr_iid": item.get("mr_iid"),
                "commit_sha": item.get("commit_sha"),
                "changed_files": item.get("changed_files")
                if isinstance(item.get("changed_files"), list)
                else [],
                "diff_stat": item.get("diff_stat")
                if isinstance(item.get("diff_stat"), dict)
                else None,
                "status": item.get("status"),
            }
        )
    return out


# 逐个拟修复项的处理结局取值。created_mr=产出了 MR；already_implemented=基线已
# 实现、无需改动；skipped=判断无需修改；failed=尝试修复但失败。
_FIX_OUTCOME_VALUES = frozenset(
    {"created_mr", "already_implemented", "skipped", "rejected", "failed"}
)
# 视为「无需改动」的结局：全为此类且无 MR 时，0 MR 属正常「已确认无需修复」而非失败。
_NO_ACTION_OUTCOMES = frozenset({"already_implemented", "skipped", "rejected"})


def _normalize_fix_outcomes(value: Any) -> List[Dict[str, Any]]:
    """校验并规范化 fix_outcomes 数组：每个拟修复项一条处理结局。token 脱敏。

    契约要求 Agent 为**每个** ``proposed_fixes`` 项给出一条结局（含未产出 MR 的
    项），使详情页能逐项解释「为何没有 MR」，而不是静默丢弃。

    - ``outcome`` 归一到 ``_FIX_OUTCOME_VALUES``；缺失/非法时：带 ``mr_url`` 视为
      ``created_mr``，否则退化为 ``failed``。
    - 非 dict 元素跳过；``reason``/``mr_url`` 做 token 脱敏兜底。
    """
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        mr_url = item.get("mr_url")
        outcome = str(item.get("outcome") or "").strip().lower()
        if outcome not in _FIX_OUTCOME_VALUES:
            outcome = "created_mr" if mr_url else "failed"
        fix_index = item.get("fix_index")
        try:
            fix_index = int(fix_index) if fix_index is not None else None
        except (TypeError, ValueError):
            fix_index = None
        out.append(
            {
                "fix_index": fix_index,
                "title": (str(item.get("title")).strip() or None)
                if item.get("title")
                else None,
                "outcome": outcome,
                "reason": (mask_tokens(str(item.get("reason"))) or None)
                if item.get("reason")
                else None,
                "branch_name": str(item["branch_name"]).strip()
                if item.get("branch_name")
                else None,
                "mr_url": mask_tokens(str(mr_url)) if mr_url else None,
            }
        )
    return out


def _validate_outcomes(outcomes: list, proposals: list, merge_requests: list) -> tuple:
    """Reject missing/duplicate/unsupported decisions instead of inventing success."""
    if not proposals:
        return outcomes, None  # Compatibility for legacy callers without a manifest.
    by_index = {}
    invalid = False
    for item in outcomes:
        index = item.get("fix_index")
        if index not in range(1, len(proposals) + 1) or index in by_index:
            invalid = True
            continue
        by_index[index] = item
    result = []
    for index, proposal in enumerate(proposals, 1):
        item = by_index.get(index)
        if item is None:
            invalid = True
            item = {"fix_index": index, "title": proposal.get("title"), "outcome": "failed",
                    "reason": "Agent did not account for this proposal"}
        elif item["outcome"] in _NO_ACTION_OUTCOMES and not item.get("reason"):
            invalid = True
            item = {**item, "outcome": "failed", "reason": "Review decision has no evidence or reason"}
        elif item["outcome"] == "created_mr" and not any(
            mr.get("mr_url") and mr.get("mr_url") == item.get("mr_url") for mr in merge_requests
        ):
            invalid = True
            item = {**item, "outcome": "failed", "reason": "No matching created merge request"}
        result.append(item)
    return result, "incomplete_fix_outcomes" if invalid else None


def _sdk_error_kind(error: Exception) -> str:
    text = str(error).lower()
    if isinstance(error, TimeoutError):
        return "request_timeout"
    if "maximum number of turns" in text or "error_max_turns" in text:
        return "max_turns_exceeded"
    if "api error:" in text or "upstream error" in text:
        return "upstream_error"
    return "sdk_error"


def _result(
    *,
    status: str,
    merge_requests: List[Dict[str, Any]],
    fix_outcomes: Optional[List[Dict[str, Any]]] = None,
    error_kind: Optional[str] = None,
    error: Optional[str] = None,
    model: Optional[str] = None,
    duration_seconds: float = 0.0,
) -> Dict[str, Any]:
    return {
        "status": status,
        "error_kind": error_kind,
        "error": mask_tokens(error) if error else None,
        "merge_requests": merge_requests,
        "fix_outcomes": fix_outcomes or [],
        "model": model,
        "duration_seconds": round(duration_seconds, 2),
        "engine": "claude_agent_sdk",
        "agent_kind": AGENT_KEY,
    }


class BugFixCodingAgent:
    """写入型 Bug 修复 Agent。"""

    async def run(self, ctx: BugFixWorkspaceContext) -> Dict[str, Any]:
        from app.agents.anthropic_client import build_options
        from app.agents.bug_fix.prompts import get_prompts, render_user_prompt
        from app.config import settings

        try:
            from claude_agent_sdk import query  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "claude-agent-sdk is required. Install with: pip install claude-agent-sdk>=0.1"
            ) from exc

        system_prompt_text, user_prompt_template = get_prompts()
        system_prompt_text += (
            "\n\n## 当前运行工作区\n"
            f"当前工作目录是 `{ctx.temp_dir}`。源码克隆在 `repo/`，"
            f"任务详情在 `task.json`。git 操作前先 `cd repo/`。\n"
        )
        if ctx.logs_dir:
            system_prompt_text += (
                "触发本次修复的原始日志已同步到 `logs/`（内容与日志分析 Agent "
                "工作区一致）。定位根因、确认错误上下文时优先查阅其中的日志证据。\n"
            )
        # 改代码能力对齐原生 Claude Code：使用 claude_code preset，
        # 把 Bug 修复约束与最终 merge_requests JSON 输出契约作为 append 叠加。
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt_text,
        }
        user_prompt = render_user_prompt(
            user_prompt_template,
            task_id=ctx.task_id,
            workspace_dir=ctx.temp_dir,
            default_branch=ctx.default_branch,
        )

        # 完整记录本次调用的提示词，便于事后评估 Agent 修复的准确性。
        # 系统提示词实际以 claude_code preset 为底座、下方文本作为 append 叠加；
        # preset 本体由 SDK 内置，这里记录的是我们可控的全部注入内容。
        # 提示词当前不含凭据，mask_tokens 兜底防止未来变更引入带 token 的 URL。
        logger.info(
            "BugFixCodingAgent prompt task=%s kind=system "
            "(preset=claude_code + append, %d chars):\n%s",
            ctx.task_id,
            len(system_prompt_text),
            mask_tokens(system_prompt_text),
        )
        logger.info(
            "BugFixCodingAgent prompt task=%s kind=user (%d chars):\n%s",
            ctx.task_id,
            len(user_prompt),
            mask_tokens(user_prompt),
        )

        from app.services.model_router import _resolve
        from app.services.model_settings_service import BUG_FIX_SLOT

        endpoint = _resolve(BUG_FIX_SLOT)
        if endpoint is None:
            return _result(status="failed", merge_requests=[],
                           error_kind="bug_fix_model_unconfigured",
                           error="请在后台配置 BugFix 独立模型端点和密钥 / Configure the dedicated BugFix endpoint and key in Admin")
        effective_model = endpoint.model
        if not endpoint.profile.supports_image_input:
            system_prompt["append"] += (
                "\n当前模型不支持原图输入。截图原件保存在 images/ 供追溯，"
                "不要 Read 图片二进制；请读取 source_analysis.json 中的 OCR/用户描述。"
                "若视觉证据不足，不得臆测，必须说明限制并拒绝缺乏依据的修复。\n"
            )

        options = build_options(
            system_prompt=system_prompt,
            allowed_tools=list(ALLOWED_TOOLS),
            cwd=ctx.temp_dir,
            permission_mode="bypassPermissions",
            max_turns=settings.bug_fix_agent_max_turns,
            endpoint=endpoint,
            max_tokens=settings.bug_fix_agent_max_tokens,
            request_timeout_seconds=settings.bug_fix_agent_request_timeout_seconds,
        )

        start = time.monotonic()
        collected_text: List[str] = []
        logger.info(
            "BugFixCodingAgent run_start task=%s model=%s", ctx.task_id, effective_model
        )

        from app.agents.provider_query import query as _query

        run_error: Optional[Exception] = None
        try:
            async with asyncio.timeout(settings.bug_fix_agent_request_timeout_seconds):
                async for message in _query(prompt=user_prompt, options=options):
                    _log_message(ctx.task_id, message)
                    if getattr(message, "is_error", False):
                        errors = getattr(message, "errors", None) or []
                        detail = _message_text(message) or "; ".join(map(str, errors)) or str(getattr(message, "subtype", "sdk_error"))
                        run_error = RuntimeError(detail)
                    text = _message_text(message)
                    if text:
                        collected_text.append(text)
        except Exception as exc:  # noqa: BLE001
            # SDK 在 CLI 报错（如超出 max_turns）时抛裸异常。此前已收集的输出里
            # 可能已有可用的最终 JSON（如 MR 已建好、只是收尾超回合）；没有时也
            # 返回结构化 failed，让任务侧记录明确的 error_kind 而非裸 traceback。
            run_error = exc
            logger.error(
                "BugFixCodingAgent run_error task=%s error=%s",
                ctx.task_id,
                mask_tokens(str(exc)),
            )

        duration = time.monotonic() - start
        full_text = "\n".join(collected_text)
        parsed = _extract_final_json(full_text)
        # A turn-limit/transport failure must not discard already-published MRs.
        # The prompt maintains a structured checkpoint after each proposal.
        if not isinstance(parsed, dict) or not any(key in parsed for key in ("merge_requests", "fix_outcomes")):
            checkpoint = Path(ctx.temp_dir) / "bug_fix_result.json"
            if checkpoint.is_file() and not checkpoint.is_symlink():
                try:
                    parsed = json.loads(checkpoint.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass

        if not isinstance(parsed, dict):
            if run_error is not None:
                error_kind = _sdk_error_kind(run_error)
                return _result(
                    status="failed",
                    merge_requests=[],
                    error_kind=error_kind,
                    error=str(run_error) or _sdk_error_kind(run_error),
                    model=effective_model,
                    duration_seconds=duration,
                )
            logger.warning(
                "BugFixCodingAgent task=%s produced no parseable result JSON", ctx.task_id
            )
            return _result(
                status="failed",
                merge_requests=[],
                error_kind="no_output_json",
                error="Agent did not emit a parseable merge_requests JSON",
                model=effective_model,
                duration_seconds=duration,
            )

        merge_requests = _normalize_merge_requests(parsed.get("merge_requests"))
        fix_outcomes = _normalize_fix_outcomes(parsed.get("fix_outcomes"))
        fix_outcomes, validation_error = _validate_outcomes(
            fix_outcomes, ctx.metadata.get("proposed_fixes") or [], merge_requests
        )
        status = parsed.get("status")
        error_kind = parsed.get("error_kind")
        if status not in ("succeeded", "partial", "failed"):
            status = "succeeded" if merge_requests else "failed"
        failed_items = any(o["outcome"] == "failed" for o in fix_outcomes)
        if validation_error or failed_items or run_error is not None:
            status = "partial" if any(mr.get("mr_url") for mr in merge_requests) else "failed"
            error_kind = _sdk_error_kind(run_error) if run_error else (validation_error or error_kind or "fix_failed")
        elif fix_outcomes and all(o["outcome"] in _NO_ACTION_OUTCOMES for o in fix_outcomes):
            status = "succeeded"
        elif status == "succeeded" and not merge_requests:
            status, error_kind = "failed", "no_merge_requests"

        logger.info(
            "BugFixCodingAgent run_complete task=%s status=%s mrs=%d outcomes=%d duration=%.1fs",
            ctx.task_id,
            status,
            len(merge_requests),
            len(fix_outcomes),
            duration,
        )
        return _result(
            status=status,
            merge_requests=merge_requests,
            fix_outcomes=fix_outcomes,
            error_kind=error_kind,
            error=(str(run_error) or _sdk_error_kind(run_error)) if run_error else parsed.get("error"),
            model=effective_model,
            duration_seconds=duration,
        )

    def run_sync(self, ctx: BugFixWorkspaceContext) -> Dict[str, Any]:
        """同步包装供 Celery 调用。"""
        # Celery 会在同一个 worker 子进程中连续执行多个任务。不要复用
        # get_event_loop() 返回的线程当前 loop：上一次调用关闭后它仍可能留在
        # event-loop policy 中，下一次 run_until_complete() 会直接抛出
        # ``RuntimeError: Event loop is closed``。asyncio.run() 为每次调用创建
        # 独立 loop，并负责 async generator/default executor 的完整收尾与解绑。
        return asyncio.run(self.run(ctx))


def _message_text(message: Any) -> str:
    """从 SDK 消息中提取文本内容（兼容 AssistantMessage / ResultMessage）。"""
    parts: List[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    # ResultMessage 常带最终 result 文本
    result_text = getattr(message, "result", None)
    if isinstance(result_text, str):
        parts.append(result_text)
    return "\n".join(parts)


_WORKFLOW_LOG_LIMIT = 600


def _truncate_for_log(text: str, limit: int = _WORKFLOW_LOG_LIMIT) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "..."


def _log_workflow(task_id: str, event: str, **fields: Any) -> None:
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    logger.info("BugFixCodingAgent workflow task=%s %s", task_id, " ".join(parts))


def _log_message(task_id: str, message: Any) -> None:
    """把一条 SDK 消息按内容块落 workflow 日志（thinking/tool_call/tool_result/文本）。

    修复运行可长达数十分钟，没有中间日志时外部无法区分「仍在执行」和「已卡死」。
    """
    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            thinking = getattr(block, "thinking", None)
            if thinking:
                _log_workflow(
                    task_id,
                    "thinking",
                    content=_truncate_for_log(mask_tokens(str(thinking))),
                )
                continue
            tool_use_id = getattr(block, "tool_use_id", None)
            if tool_use_id is not None:
                is_error = bool(getattr(block, "is_error", False))
                _log_workflow(
                    task_id,
                    "tool_result",
                    status="error" if is_error else "ok",
                    output=_truncate_for_log(
                        mask_tokens(_message_text_of_block(block))
                    ),
                )
                continue
            name = getattr(block, "name", None)
            tool_input = getattr(block, "input", None)
            if name and tool_input is not None:
                _log_workflow(
                    task_id,
                    "tool_call",
                    tool=str(name),
                    input=_truncate_for_log(
                        mask_tokens(
                            json.dumps(mask_input(tool_input), ensure_ascii=False)
                        )
                        if isinstance(tool_input, (dict, list))
                        else mask_tokens(str(tool_input))
                    ),
                )
                continue
            text = getattr(block, "text", None)
            if text:
                _log_workflow(
                    task_id,
                    "assistant_text",
                    content=_truncate_for_log(mask_tokens(str(text))),
                )
        return
    result_text = getattr(message, "result", None)
    if isinstance(result_text, str):
        _log_workflow(
            task_id,
            "result",
            excerpt=_truncate_for_log(mask_tokens(result_text)),
        )


def _message_text_of_block(block: Any) -> str:
    """提取 tool_result 块的文本输出（content 可能是 str 或块列表）。"""
    content = getattr(block, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif hasattr(item, "text"):
                parts.append(str(getattr(item, "text", "")))
        return "".join(parts)
    return "" if content is None else str(content)
