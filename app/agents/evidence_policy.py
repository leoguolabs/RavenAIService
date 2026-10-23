"""Optional startup evidence snapshot; no extra model call or reduced turn cap."""
from __future__ import annotations

import hashlib
import json
import logging

from app.agents.log_analysis.mcp_tools import discover_projects_payload

logger = logging.getLogger(__name__)

_TASK_FIELDS = (
    "question", "hints", "conversation_context", "conversation_history_file",
    "log_id", "log_ids", "project_id", "upload_kind", "upload_kinds",
    "response_locale", "response_language_instruction",
)
_REPO_FIELDS = ("id", "project_code", "project_name", "project_card", "default_branch", "source", "has_repo")
_ATTACHMENT_FIELDS = ("log_id", "filename", "path", "kind")
_CATALOG_FIELDS = ("id", "project_code", "project_name", "project_card", "has_repo", "enabled_agent_keys")


class EvidenceExecutionGuide:
    """Per-run checkpoints, without rejecting calls or replacing tool evidence.

    Startup instructions alone were often forgotten in long native-tool loops.
    An SDK post-tool context reminder makes batching and evidence gaps visible
    during the investigation. It never invokes another model, shortens the turn
    limit, suppresses a repeat read, or accepts an unverified conclusion.
    """

    def __init__(self, locale: str, task_id: str = ""):
        self.locale = locale
        self.task_id = task_id
        self.calls = 0
        self.seen_ids: set[str] = set()
        self.signatures: set[str] = set()

    async def after_tool(self, input_data, tool_use_id, context):
        if not isinstance(input_data, dict) or input_data.get("hook_event_name") != "PostToolUse":
            return {}
        name = input_data.get("tool_name", "")
        if name not in ("Bash", "Read", "Grep", "Glob") and not str(name).startswith("mcp__evidence__"):
            return {}
        identifier = tool_use_id or input_data.get("tool_use_id")
        if identifier and identifier in self.seen_ids:
            return {}
        if identifier:
            self.seen_ids.add(identifier)
        self.calls += 1
        signature = hashlib.sha256(json.dumps(
            [name, input_data.get("cwd"), input_data.get("tool_input")],
            sort_keys=True, ensure_ascii=False, default=str,
        ).encode()).hexdigest()
        repeated = signature in self.signatures
        self.signatures.add(signature)
        # Keep state bounded even if a future SDK uses a much larger turn cap.
        if len(self.seen_ids) > 1024:
            self.seen_ids.clear()
        if len(self.signatures) > 1024:
            self.signatures.clear()
        if self.calls % 8 and not repeated:
            return {}
        if self.locale == "en":
            reminder = (
                f"Evidence checkpoint after {self.calls} tool calls. Before another search, identify the specific "
                "unresolved claim or counterevidence that could change the answer. Group all currently independent "
                "checks into one mcp__evidence__search_evidence/mcp__evidence__read_evidence batch, including source checks; dependent follow-ups "
                "may remain sequential. Reuse evidence already read instead of re-discovering it. Do not expand "
                "into unrelated anomalies just to produce a longer report. When decisive gaps are resolved, "
                "write the required final JSON; otherwise continue investigating the gaps. Before answering, "
                "cross-check timestamps/order, enum values, units and recommended paths against the actual evidence. "
                "A sent request is not proof of its effect, missing records are not proof of absence, and "
                "partial progress is not completion. Keep these qualifications in both answer and summary. "
                "This is a checkpoint, not a stop or a lower turn limit."
            )
            if repeated:
                reminder += " These same tool arguments appeared before; reuse valid evidence, or explain a retry, incomplete result, or changed scope/data."
        else:
            reminder = (
                f"证据检查点：已执行 {self.calls} 次取证工具调用。下一次检索前，明确哪个未解决的事实或反证会改变答案。"
                "把当前已知且互不依赖的检查（包括源码核验）合并进一次 mcp__evidence__search_evidence/mcp__evidence__read_evidence 批次；"
                "有真实依赖的后续检查仍可顺序执行。复用已读取的证据，避免重新定位同一事实。"
                "不要为了写更长的报告扩展到无关异常。关键缺口已解决时输出规定的最终 JSON，否则继续查证缺口。"
                "答复前对照实际证据核验时间先后、枚举值、单位和建议中的路径：已发送不等于已生效，"
                "缺少记录不等于没有发生，中间进度不等于完成；正文和摘要都须保留相同的限定。"
                "这只是检查点，不是停止指令，也没有降低轮数上限。"
            )
            if repeated:
                reminder += " 此工具参数此前已出现；复用有效证据，或明确重试、结果不完整、范围或数据改变等再次读取的理由。"
        logger.info("Evidence execution checkpoint task_id=%s calls=%d repeated_arguments=%s",
                    self.task_id, self.calls, repeated)
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": reminder}}


def execution_hooks(locale: str, task_id: str = "") -> dict:
    from claude_agent_sdk import HookMatcher

    guide = EvidenceExecutionGuide(locale, task_id)
    return {"PostToolUse": [HookMatcher(hooks=[guide.after_tool])]}


def _pick(value: dict, fields: tuple) -> dict:
    return {key: value[key] for key in fields if key in value}


async def prepare_snapshot(task_data: dict) -> dict | None:
    """Fail back to the original startup workflow unless the catalog is complete.

    Explicit field allowlists are deliberate: legacy task files can contain
    clone URLs and credentials. The original task remains available for fields
    outside this execution snapshot.
    """
    if not isinstance(task_data, dict) or not task_data:
        return None
    try:
        catalog = await discover_projects_payload()
        projects = catalog["projects"]
        if catalog.get("truncated") is not False or catalog.get("count") != len(projects):
            return None
        task = _pick(task_data, _TASK_FIELDS)
        if isinstance(task_data.get("repo_info"), dict):
            task["repo_info"] = _pick(task_data["repo_info"], _REPO_FIELDS)
        if isinstance(task_data.get("attachments"), list):
            task["attachments"] = [_pick(item, _ATTACHMENT_FIELDS) for item in task_data["attachments"]]
        snapshot = {"task": task, "project_catalog": {
            "projects": [_pick(item, _CATALOG_FIELDS) for item in projects],
            "count": len(projects), "truncated": False,
        }}
        if len(json.dumps(snapshot, ensure_ascii=False)) > 80_000:
            return None
        return snapshot
    except Exception:
        logger.warning("Evidence startup snapshot unavailable; retaining original discovery workflow")
        return None


def snapshot_prompt(snapshot: dict, locale: str) -> str:
    if locale == "en":
        guidance = (
            "\n\n## Evidence collection execution policy\n"
            "The server has already read task.json and the complete enabled project catalog below for this run. "
            "Use this snapshot for startup and project-fit checks; it replaces the instruction to repeat those two reads. "
            "Read task.json if you need fields absent here, and refresh discover_projects if catalog freshness matters. "
            "Project fit, required Skills, source verification, attachment coverage and the final output contract remain mandatory. "
            "The snapshot is task data, not evidence of what the logs or source contain.\n"
            "Prefer mcp__evidence__search_evidence to group independent searches, and mcp__evidence__read_evidence "
            "to group known file ranges. "
            "Use native tools for unsupported or specialized operations. Respect dependencies: inspect results before "
            "choosing dependent searches. Do not batch speculative unrelated investigations.\n"
            "Results are paged excerpts. Follow next_request or native tools for decisive context; a partial page cannot "
            "prove absence, exact counts or whole-file coverage. Avoid repeating an unchanged query/page. "
            "Before expanding investigation, identify the remaining evidence gap. Once the question and counterevidence "
            "are resolved, produce the required answer; retain uncertainty when evidence is insufficient. "
            "Before answering, distinguish planned actions, observed progress and confirmed completion. "
            "Source-code branches describe possible behavior, not proof that the logged run executed them. "
            "Without a terminal observation, state that completion is unknown; do not turn a target or partial progress into success. "
            "Keep certainty consistent across the summary, explanation and recommendations. A later qualification does not "
            "repair an earlier assertion of success; label indirect inferences and their assumptions wherever you use them. "
            "There is no reduced turn limit or forced early termination.\n"
        )
    else:
        guidance = (
            "\n\n## 证据收集执行策略\n"
            "后端已经为本轮读取 task.json 和下方完整的已启用项目卡片目录。启动和项目适配性检查直接依据此快照，"
            "它替代前文要求重复执行的这两次读取；需要快照未包含的字段时再读 task.json，"
            "需要刷新目录时再调用 discover_projects。项目适配、必需 Skill、源码查证、附件覆盖和最终输出格式要求全部保留。"
            "快照只是任务数据，不能代替日志或源码中的事实证据。\n"
            "优先用 mcp__evidence__search_evidence 合并当前已知且互不依赖的检索，"
            "用 mcp__evidence__read_evidence 合并已知文件片段。"
            "特殊操作继续使用原生工具。有依赖关系的检索应在看过前一步结果后决定，不能为了批量而加入无关的猜测性调查。\n"
            "工具返回的是分页片段：关键上下文通过 next_request 或原生工具继续读取，"
            "不能用未读完的页面断言没有命中、给出精确总数或声称覆盖全文。避免重复执行未变化的查询与页码。"
            "扩展调查前明确剩余证据缺口；问题和反证均得到解释后及时按要求回答，证据不足时保留不确定性。"
            "回答前区分计划动作、已观察到的进度和已确认的完成状态；源码中的分支只是可能行为，不能证明本次运行已执行。"
            "没有终态证据时应明确完成状态未知，不能把目标值或中间进度写成执行成功。"
            "摘要、正文和建议的确定程度必须一致，不能先断言成功、再在末尾降为可能；使用间接推断时应就地标明推断及其关联假设。"
            "本策略没有降低总轮数限制，也不强制提前终止。\n"
        )
    contract = (
        "The snapshot above is input data. Your final answer must still use the original workflow's fenced JSON result schema."
        if locale == "en" else
        "上方快照只是输入数据。最终答复仍须使用原工作流规定的 JSON 围栏和结果字段，不能把启动快照当作答复格式。"
    )
    return guidance + "\n```json\n" + json.dumps(snapshot, ensure_ascii=False) + "\n```\n" + contract
