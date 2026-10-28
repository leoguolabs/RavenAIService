"""Optional startup evidence snapshot; no extra model call or reduced turn cap."""
from __future__ import annotations

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


def use_snapshot_in_templates(system_prompt: str, user_template: str) -> tuple[str, str]:
    """Resolve built-in startup contradictions before interpolating user data.

    Only known task-reading instructions are changed. Project fit, required
    source access, all attachments, output rules and custom text are retained.
    Call only after a complete snapshot is available; otherwise leave the
    original templates byte-for-byte unchanged.
    """
    zh = "先使用后端启动快照中的任务字段；仅在缺少所需字段时读取 `task.json`。"
    en = "Use the server startup snapshot's task fields first; read `task.json` only for missing required fields."
    replacements = (
        ('第一次 Read 调用请使用 `{"file_path":"task.json"}`。', zh),
        ('### 第 1 步 —— 读取 task.json（必须）', '### 第 1 步 —— 使用任务元数据（必须）'),
        ('首先读取当前工作目录下的 `task.json`。Read 工具的第一次调用应为 `{"file_path":"task.json"}`。', zh),
        ('首先读取当前工作目录下的 `task.json`（`{"file_path":"task.json"}`）。', zh),
        ('读取当前工作目录的 task.json。', zh),
        ('然后读取 `task.json`。', '然后使用后端任务快照；缺少所需字段时再读取 `task.json`。'),
        ('首先执行第 0 步分类并读取 `task.json`；', '首先执行第 0 步分类并使用后端任务快照；缺少所需字段时再读取 `task.json`；'),
        ('第一次 Read 调用优先使用 `{"file_path":"task.json"}`。', '需要读取任务文件时使用 `{"file_path":"task.json"}`。'),
        ('### Step 1 — Read task.json (required)', '### Step 1 — Use task metadata (required)'),
        ('First read `task.json` in the current working directory. The first Read tool call should be `{"file_path":"task.json"}`.', en),
        ('First read `task.json` in the current working directory (`{"file_path":"task.json"}`).', en),
        ('Read task.json in the current workspace.', en),
        ('then read `task.json`.', "then use the server task snapshot; read `task.json` only for missing required fields."),
        ('Start with Step 0 classification and read `task.json`;', "Start with Step 0 classification and use the server task snapshot; read `task.json` only for missing required fields;"),
        ('Prefer `{"file_path":"task.json"}` for the first Read call.', 'When reading the task file, use `{"file_path":"task.json"}`.'),
    )
    for old, new in replacements:
        system_prompt = system_prompt.replace(old, new)
        user_template = user_template.replace(old, new)
    return system_prompt, user_template


def execution_hooks(locale: str, task_id: str = "", workspace: str = "", *, source_context: bool = False, read_window: bool = False, final_review=None) -> dict:
    from claude_agent_sdk import HookMatcher
    from app.agents.evidence_context import MCP_READ_TOOL, MCP_SEARCH_TOOL, SourceContextEnricher

    if not workspace:
        return {}
    hooks = {}
    if source_context:
        enricher = SourceContextEnricher(workspace, task_id, locale)
        hooks["PostToolUse"] = [HookMatcher(matcher=f"Bash|{MCP_SEARCH_TOOL}|{MCP_READ_TOOL}", hooks=[enricher.after_tool])]
    if read_window:
        from app.agents.evidence_read_window import SourceReadWindow

        window = SourceReadWindow(workspace, task_id, locale)
        hooks["PreToolUse"] = [HookMatcher(matcher="Read", hooks=[window.before_tool])]
    if final_review is not None:
        hooks["Stop"] = [HookMatcher(hooks=[final_review.before_stop])]
    return hooks


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
            "Retrieval dispatch: ordinary log/source text searches must use mcp__evidence__search_evidence; "
            "file-range reads must use mcp__evidence__read_evidence, including single requests. "
            "Do not substitute native Read/Grep or Bash rg/grep/sed/cat when these evidence tools support the request. "
            "Native tools remain available for file discovery, Git, deterministic whole-file statistics, binary formats, "
            "unsupported operations or recovery from an evidence-tool error.\n"
            "Batch 1-8 currently known independent requests. When interpreting a match, request its nearby context "
            "in the same search with context_lines (for example 8-20); read only missing decisive ranges afterward. "
            "Use tail_lines for endings, without start_line/column/line_count. Inspect results before choosing dependent "
            "requests; do not add speculative unrelated searches just to fill a batch.\n"
            "Source searches and supported short MCP or shell reads can include verified surrounding source excerpts. Use these as read evidence; "
            "do not issue a separate read of the same range unless decisive context is still missing.\n"
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
            "取证工具选择：普通日志/源码文本检索必须使用 mcp__evidence__search_evidence，"
            "文件范围读取必须使用 mcp__evidence__read_evidence，单个请求也适用。"
            "证据工具支持的普通检索/读取，不要改用原生 Read/Grep 或 Bash 的 rg/grep/sed/cat。"
            "文件发现、Git、确定性全文统计、二进制格式、不支持的操作以及证据工具报错后的恢复仍可使用原生工具。\n"
            "将当前已知且互不依赖的 1–8 个请求合并。需要解释命中内容时，在同次检索设置 context_lines"
            "（例如 8–20）一起取周边上下文，随后只续读缺少的关键范围。末尾读取使用 tail_lines，"
            "不要同时传 start_line/column/line_count。依赖前一步结果的请求须先看结果再决定，"
            "不要为了凑批量而加入无关的猜测性检索。\n"
            "源码检索及受支持的短 MCP/Bash 读取可能已附带经过核验的周边源码片段，可直接作为读取证据使用；只有仍缺关键上下文时才另行读取，避免重复同一行范围。\n"
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
