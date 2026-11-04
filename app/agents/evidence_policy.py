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


def execution_hooks(locale: str, task_id: str = "", workspace: str = "", *, source_context: bool = False,
                    read_window: bool = False, topic_context: bool = False,
                    search_endpoints: bool = False, final_review=None) -> dict:
    from claude_agent_sdk import HookMatcher
    from app.agents.evidence_context import MCP_CLONE_TOOL, MCP_READ_TOOL, MCP_SEARCH_TOOL, SourceContextEnricher

    if not workspace:
        return {}
    hooks = {}
    if source_context:
        enricher = SourceContextEnricher(workspace, task_id, locale)
        hooks["PostToolUse"] = [HookMatcher(matcher=f"Bash|{MCP_SEARCH_TOOL}|{MCP_READ_TOOL}|{MCP_CLONE_TOOL}", hooks=[enricher.after_tool])]
    if topic_context or search_endpoints:
        from app.agents.log_topic_context import LogTopicContextEnricher

        topics = LogTopicContextEnricher(workspace, locale, include_endpoints=search_endpoints, producer_context=topic_context)
        matcher = f"Bash|{MCP_SEARCH_TOOL}"
        hooks.setdefault("PostToolUse", []).append(HookMatcher(matcher=matcher, hooks=[topics.after_tool]))
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


def batch_plan_prompt(locale: str) -> str:
    if locale == "en":
        return (
            "\n\n## Batch-first evidence dispatch (local experiment)\n"
            "Before ordinary text searches, identify the concrete evidence questions implied by the original task: "
            "the trigger and timeline, relevant identifiers or values, source behavior when applicable, and counterevidence or terminal state. "
            "Before assigning an exact failed component, device node, error code or deployment cause, inspect all relevant source failure branches. "
            "A generic failure return does not identify which branch ran; keep unresolved details and deployment conventions explicitly hypothetical unless logs confirm them. "
            "When multiple independent searches are already known, submit them together in one "
            "mcp__evidence__search_evidence call (up to 8 requests), rather than one tool call per keyword or file. "
            "Narrow each request to relevant attachments/source and set context_lines so matches arrive with nearby evidence. "
            "After inspecting that batch, group decisive ranges and known missing context in one "
            "mcp__evidence__read_evidence call (up to 8 ranges); inspect results before forming dependent follow-up requests. "
            "Batch later independent gaps in the same way. Do not replace supported text searches with separate Bash grep/rg/sed/cat or Read/Grep calls. "
            "Native tools remain available for file discovery, Git, complete deterministic statistics, binary or unsupported operations, "
            "and recovery from an evidence-tool error. This changes dispatch only: it does not cap total calls, stop after the first batch, "
            "skip attachments, required Skills, source checks, counterevidence, pagination, or uncertainty requirements.\n"
        )
    return (
        "\n\n## 先批量取证（本地实验）\n"
        "普通文本检索前，先从原问题列出需要核实的证据点：触发与时序、相关标识或数值、适用时的源码行为，以及反证或终态。"
        "断言具体故障部件、设备节点、错误码或部署原因前，先检查源码中所有相关失败分支。"
        "通用失败返回值不能证明执行了哪个分支；日志未直接确认时，具体节点和部署惯例须保留为待核实假设。"
        "若已知多个互不依赖的检索，将它们放进一次 mcp__evidence__search_evidence 调用（最多 8 个请求），"
        "不要按关键词或文件逐次调用工具。每个请求应限定到相关附件/源码，并设置 context_lines 一并取得命中周边证据。"
        "检查首批结果后，将决定性范围和已知缺失上下文合并到一次 mcp__evidence__read_evidence 调用（最多 8 个范围）；"
        "依赖新证据的后续请求须先检查当前结果，再提出。之后发现的独立缺口也按批次合并。"
        "证据工具支持的文本检索不要拆成多次 Bash grep/rg/sed/cat 或 Read/Grep 调用。"
        "文件发现、Git、完整确定性统计、二进制或不支持的操作，以及证据工具失败后的恢复仍可使用原生工具。"
        "此规则只改变调用编排，不限制总调用数，不得首批后停止、跳过附件/必需 Skill/源码核验/反证、分页或不确定性要求。\n"
    )


def batch_plan_v3_prompt(locale: str) -> str:
    """Small, gap-led batches intended to reduce context cost without weakening evidence."""
    if locale == "en":
        return (
            "\n\n## Focused evidence batches (local experiment v3)\n"
            "Reduce the task to 2-4 distinct evidence questions that could change the answer. Put only currently known, "
            "independent searches in one batch of at most 4 requests; do not add synonym searches just to fill it. "
            "Prefer a narrow file/identifier, limit 8-12 matches, and context_lines 3-5. Inspect the results before deciding follow-up. "
            "For each material question, track confirmed evidence and the specific unresolved gap; batch only independent checks for that gap, "
            "then read only decisive ranges not already shown. For claims of absence, disappearance, non-receipt or non-forwarding, "
            "verify file/time coverage, pagination, relevant action and terminal state. A missing match, partial excerpt or absent terminal "
            "record means only that it was not observed in the inspected scope; it does not prove the event never occurred or exclude an actor. "
            "Keep observations, hypotheses and exclusions separate; state the smallest next verification when evidence is insufficient. "
            "Once the requested findings and material counterevidence are supported, answer promptly; retain required Skills, attachments, "
            "source checks and uncertainty. This adds no total-call or early-stop cap and does not change the 150-turn limit.\n"
        )
    return (
        "\n\n## 聚焦式证据批次（本地实验 v3）\n"
        "先把问题归纳为 2–4 个可能改变结论的不同证据问题。每批只合并当前已知且互不依赖的检索，最多 4 项；"
        "不要为凑数添加同义词检索。优先限定文件或标识，每项设置 limit 8–12、context_lines 3–5；看完结果再决定后续。"
        "逐个记录重要问题的已证实证据和具体未决缺口；只为该缺口批量核查互不依赖的事项，"
        "再读取检索结果中尚未展示的决定性范围。对于未出现、消失、未收到或未转发等判断，核实文件/时间覆盖、分页、"
        "相关动作和终态。未命中、片段或缺少终态只能说明在当前检查范围内未观察到，不能证明事件从未发生或排除某个执行方。"
        "区分观察事实、假设和排除结论；证据不足时说明限制及最小的下一步核验。"
        "请求中的发现和重要反证均有依据后及时回答；仍须满足必需 Skill、附件、源码核验和不确定性要求。"
        "本策略不限制总调用数、不设置强制提前终止，也不改变 150 轮上限。\n"
    )


def batch_plan_v4_prompt(locale: str) -> str:
    """Compact coverage-first batching; page size is not an evidence budget."""
    if locale == "en":
        return (
            "\n\n## Front-loaded evidence batches (local experiment v4)\n"
            "Cover the user's question across the relevant attachments before narrowing to an example. "
            "Combine currently known independent checks in one mcp__evidence__search_evidence batch (up to 8 requests); "
            "inspect results, then batch missing decisive ranges with mcp__evidence__read_evidence. Choose page/context sizes "
            "for the evidence needed, not a fixed small sample; continue material incomplete pages. "
            "Reuse established facts unless counterevidence or a coverage gap can change them.\n"
            "Match field meanings, enums, units and grouping keys to source definitions. Verify a state transition against "
            "the same object's preceding/following records; do not conflate distinct states. Trace a status to its producer "
            "and freshness: cached or intermediate values do not prove completion. Generic failures do not identify a branch. "
            "For all/only/none, absence or exclusion claims, verify file/time coverage and the latest relevant records; "
            "otherwise limit the claim to inspected evidence and leave the current state unknown. No missing message proves "
            "an event never happened. Keep observations, hypotheses and certainty consistent throughout the result.\n"
            "Answer when the requested findings and material counterevidence are resolved; identify the smallest remaining "
            "check when evidence is unavailable instead of repeating searches. Preserve original tools, attachments, required "
            "Skills, source checks and JSON schema. There is no total-call cap or early stop; retain the 150-turn cap.\n"
        )
    return (
        "\n\n## 前置合并证据检索（本地实验 v4）\n"
        "先覆盖用户问题涉及的相关附件，再缩小到具体示例。当前已知且互不依赖的核查合并到一次 "
        "mcp__evidence__search_evidence（最多 8 项）；看完结果，再用 mcp__evidence__read_evidence 批量补齐决定性范围。"
        "页大小和上下文按所需证据选择，不固定为少量样本；重要的未完成页面须续查。"
        "按已证实的事实复用证据，仅在反证或覆盖缺口可能改变事实时重查。\n"
        "字段含义、枚举、单位和分组键须对应源码定义。状态转换须核对同一对象的前后记录，不混同不同状态。"
        "追查状态的产生路径和新鲜度：缓存或中间值不能证明完成；通用失败值不能定位具体分支。"
        "声称全部、唯一、没有、消失或排除前，核实文件/时间覆盖及最后相关记录；否则限定为已检查证据，当前状态保留未知。"
        "缺少消息不能证明事件从未发生。正文、摘要和建议一致地区分观察、假设和确定程度。\n"
        "所问发现和重要反证已解决就回答；证据缺失时给出最小下一步，不反复检索。"
        "原工具、附件、必需 Skill、源码查证和 JSON 格式均保留。总调用数不设上限，不强制提前终止，保留 150 轮上限。\n"
    )


def snapshot_prompt(snapshot: dict, locale: str, *, batch_plan: bool = False,
                    batch_plan_v3: bool = False, batch_plan_v4: bool = False) -> str:
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
    if not batch_plan_v4 and batch_plan_v3:
        guidance += batch_plan_v3_prompt(locale)
    elif not batch_plan_v4 and batch_plan:
        guidance += batch_plan_prompt(locale)
    result = guidance + "\n```json\n" + json.dumps(snapshot, ensure_ascii=False) + "\n```\n" + contract
    return result + (batch_plan_v4_prompt(locale) if batch_plan_v4 else "")


def comparison_execution_prompt(locale: str) -> str:
    """Final, task-conditional guidance; no forced extra model turn or diagnosis."""
    if locale == "en":
        return (
            "\n\n## Deterministic grouped comparison\n"
            "When comparing repeated record groups or value sets, first verify the grouping field from original rows. "
            "Prefer ONE mcp__evidence__compare_evidence call with group/value captures and the two observed identifiers "
            "before iterating shell count/diff pipelines. Batch independent comparisons together. If the format is unsupported, "
            "the tool errors or coverage is incomplete, continue with original tools; after one failed correction, stop retrying that comparison and use original tools; do not force a comparison for other tasks. "
            "Inspect complete, observed and original line references. An unobserved group is not an observed empty snapshot; "
            "set differences do not establish physical deletion, chronology or a cause. Agent-authored echo/print labels "
            "are notes, not source evidence. Preserve required Skills, source checks, uncertainty and the 150-turn limit.\n"
        )
    return (
        "\n\n## 确定性分组比较\n"
        "需要比较重复记录分组或值集合时，先从原始行确认分组字段；在反复拼接 shell 计数/差异流水线前，"
        "优先调用一次 mcp__evidence__compare_evidence，使用 group/value 命名捕获和两个已观察到的分组标识。"
        "独立比较合并一批。格式不支持、工具报错或覆盖不完整时，继续用原工具核查；修正一次仍失败就停止重试该比较并用原工具核查；其他任务不强制比较。"
        "核对 complete、observed 和原始行号；未观察到的分组不等于已观察到的空快照，集合差异不能证明物理删除、时间界限或原因。"
        "Agent 自己的 echo/print 标签只是笔记，不能作为源证据。必需 Skill、源码查证、不确定性和 150 轮上限均保留。\n"
    )
