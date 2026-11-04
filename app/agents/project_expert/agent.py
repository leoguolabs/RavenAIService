"""
Claude Agent SDK 项目专家 Agent。

与 ``LogAnalysisAgent`` 同构，但去掉附件日志分析这一环：工作区初始包含
``repo/`` + ``task.json``，不解压归档、不校验 metadata.json；运行中可以通过
工作区绑定工具增加 ``related_repos/<project_code>/``。项目身份来自用户显式
选择的项目仓库（写入 ``task.json.repo_info``）。

主入口：
  ProjectExpertAgent().run(ctx)       — async, returns dict
  ProjectExpertAgent().run_sync(ctx)  — sync wrapper (供后台线程调用)

两个入口都可选注入 ``trace_emitter``，在 SDK loop 内部按消息粒度向外
推送 ``AgentTraceEvent`` —— 与 Log Analysis 完全一致。

trace 层与 ``lookup_project_repo`` MCP 工具均 **复用** log_analysis 的
实现，不重复造轮子。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:  # avoid importing the SDK-dependent module at import time
    from app.agents.clarification import ClarificationBinding

# 复用 log_analysis 的 trace 层（纯 SDK 消息 → AgentTraceEvent 转换，无日志语义）。
from app.agents.log_analysis.trace import (
    AgentTraceEvent,
    CANCELLED,
    DEFAULT_EXCERPT_MAX_BYTES,
    ERROR,
    RUN_COMPLETE,
    RUN_START,
    STEP_END,
    SYSTEM_NOTICE,
    build_event,
    coerce_excerpt,
    derive_tool_trace,
    mask_tokens,
    summarize,
)
# 复用 log_analysis agent 的 trace 状态机与结果抽取逻辑。
from app.agents.log_analysis.agent import (
    AgentCancelled,
    _RunState,
    _cancelled_result,
    _close_any_active_steps,
    _emit_cancel_requested,
    _emit_for_message,
    _extract_fenced_json,
    _log_workflow,
    _normalize_question_type,
    _strip_confidence_fields,
    _validate_result_schema,
    extract_recoverable_result_fields,
)
from app.agents.project_expert.workspace import WorkspaceContext
from app.agents.log_analysis.mcp_tools import (
    PROJECT_DISCOVERY_MCP_TOOL,
    PROJECT_REPO_CLONE_MCP_TOOL,
    PROJECT_REPO_LOOKUP_MCP_TOOL,
    build_project_fit_guidance,
)

logger = logging.getLogger(__name__)


PROJECT_REPO_MCP_TOOL = PROJECT_REPO_LOOKUP_MCP_TOOL

ALLOWED_TOOLS = [
    "Bash",
    "Read",
    "Grep",
    "Glob",
    "Skill",  # 允许模型调用通过 setting_sources 加载的用户自定义 Skill
    PROJECT_DISCOVERY_MCP_TOOL,
    PROJECT_REPO_MCP_TOOL,
    PROJECT_REPO_CLONE_MCP_TOOL,
]

# Agent 唯一键，与 skills_service.SUPPORTED_AGENTS 对应。
AGENT_KEY = "project_expert"


TraceEmitter = Callable[[AgentTraceEvent], None]


def _empty_result(model: str, *, status: str, error_kind: Optional[str], **extra: Any) -> Dict[str, Any]:
    base = {
        "engine": "claude-agent-sdk",
        "model": model,
        "schema_version": 3,
        "status": status,
        "error_kind": error_kind,
        "question_type": "other",
        "answer": "",
        "summary": "",
        "severity": "info",
        "root_cause_hypotheses": [],
        "recommended_actions": [],
        "related_keywords": [],
    }
    base.update(extra)
    return base


class ProjectExpertAgent:
    """Wraps the Claude Agent SDK query() loop for project source-code Q&A."""

    async def run(
        self,
        ctx: WorkspaceContext,
        cancel_event: Optional[threading.Event] = None,
        trace_emitter: Optional[TraceEmitter] = None,
        clarification_binding: Optional["ClarificationBinding"] = None,
    ) -> Dict[str, Any]:
        """Run the agent loop and return the structured result dict.

        Args:
            ctx: project-expert workspace context (paths + metadata).
            cancel_event: optional ``threading.Event`` checked between SDK
                messages; when set the agent emits ``cancel_requested`` then
                terminates with a ``cancelled`` result.
            trace_emitter: optional synchronous callback invoked once per
                ``AgentTraceEvent`` (used by the chat service for SSE).
            clarification_binding: optional AskUserQuestion wiring supplied by
                the chat service. ``None`` disables asking (no SSE subscriber
                exists to answer).
        """
        from app.agents.anthropic_client import PROVIDER_PROFILES, build_options
        from app.agents.clarification import BUILTIN_ASK_TOOL_NAME
        from app.agents.project_expert.prompts import get_prompts, render_user_prompt
        from app.agents.skill_prompting import (
            build_plain_text_skill_answer_fields,
            build_skill_availability_prompt,
        )
        from app.config import settings

        try:
            from claude_agent_sdk import query  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "claude-agent-sdk is required. Install with: pip install claude-agent-sdk>=0.1"
            ) from exc

        system_prompt, user_prompt_template = get_prompts(locale=ctx.locale)
        system_prompt += (
            "\n\n## 当前运行工作区\n"
            f"本次运行的当前工作目录是 `{ctx.temp_dir}`。"
            f"`task.json` 的真实路径是 `{ctx.task_json_path}`，"
            f"源码目录是 `{ctx.repo_dir}`。"
            "读取文件和搜索时只使用这些路径或它们的相对路径 "
            "(`task.json`、`repo/...`)。"
            "第一次 Read 调用请使用 `{\"file_path\":\"task.json\"}`。"
            "如果 `task.json` 中有 `conversation_history_file`，请在判断追问或项目切换的"
            "指代时读取它；其中过去项目的回答不能作为当前项目源码的证据。"
            "本工作区没有 `logs/` 目录，也没有 metadata.json，不要去搜索它们。"
            "如果路径不确定，先用 `pwd` / `ls -la` 确认当前目录。\n"
        )

        task_data: Dict[str, Any] = {}
        try:
            task_data = json.loads(Path(ctx.task_json_path).read_text(encoding="utf-8"))
        except Exception:
            pass

        # Resolve effective model before run for logging & result.
        # Endpoint selection happens here, not at build_options time: RUN_START
        # below names the model and is already streamed to the browser by the
        # time the first SDK message arrives, so it cannot be corrected later.
        from app.agents.routed_query import routed_query
        from app.services import model_router

        from app.services.chat_image_store import native_image_input_required
        endpoints = model_router.candidates(agent_kind=AGENT_KEY, require_mcp=True,
            require_image=native_image_input_required(ctx.temp_dir))
        chosen = endpoints[0] if endpoints else None
        if chosen is not None:
            provider = chosen.provider
            profile = chosen.profile
            effective_model = chosen.model
        else:
            provider = settings.anthropic_provider
            profile = PROVIDER_PROFILES.get(provider)
            effective_model = settings.anthropic_model or (profile.default_model if profile else "unknown")
        supports_mcp = bool(profile and profile.supports_mcp_server_tools)
        # Every candidate is MCP-capable (require_mcp above), so the tool set and
        # the system prompt below are identical across slots and safe to build once.
        served: Dict[str, str] = {"model": effective_model, "provider": str(provider)}

        allowed_tools = list(ALLOWED_TOOLS)
        mcp_servers = None
        if supports_mcp:
            from app.agents.log_analysis.mcp_tools import get_mcp_server
            primary_repo_info = (
                task_data.get("repo_info") if isinstance(task_data, dict) else None
            )
            primary_project_code = (
                primary_repo_info.get("project_code")
                if isinstance(primary_repo_info, dict)
                else None
            )
            mcp_servers = {
                "project_repo": get_mcp_server(
                    workspace_dir=ctx.temp_dir,
                    primary_project_code=primary_project_code,
                    agent_key=AGENT_KEY,
                )
            }
        else:
            allowed_tools = [
                name for name in allowed_tools if not name.startswith("mcp__")
            ]
            system_prompt += (
                "\n\n## 运行时约束\n"
                f"当前 provider `{provider}` 不支持 MCP server 工具。"
                "本次运行中项目目录、仓库查询和工作区克隆 MCP 工具不可用，"
                "因此不能在当前会话追加其他项目仓库。"
                "请直接使用 `task.json` 中 `repo_info.repo_url` 克隆仓库。"
            )
            logger.info(
                "ProjectExpertAgent: provider=%s does not support MCP; using repo_info.repo_url only",
                provider,
            )

        evidence_snapshot = None
        if settings.agent_evidence_efficiency_enabled is True and supports_mcp:
            from app.agents.evidence_policy import prepare_snapshot
            from app.agents.evidence_tools import TOOL_NAMES, get_evidence_mcp_server

            evidence_snapshot = await prepare_snapshot(task_data)
            if evidence_snapshot is not None:
                mcp_servers["evidence"] = get_evidence_mcp_server(ctx.temp_dir)
                allowed_tools.extend(TOOL_NAMES)
                from app.agents.evidence_policy import use_snapshot_in_templates

                system_prompt, user_prompt_template = use_snapshot_in_templates(system_prompt, user_prompt_template)

        user_prompt = render_user_prompt(
            user_prompt_template,
            task_id=ctx.task_id,
            workspace_dir=ctx.temp_dir,
            question=ctx.metadata.get("question") or task_data.get("question", ""),
            hints=ctx.metadata.get("hints") or task_data.get("hints", ""),
        )

        # 物化全部启用的 Skill 到 cwd/.claude/skills/<name>/，配合
        # setting_sources=["project"] 让 SDK 按官方约定发现 Skill。
        # 相关性判定交给模型在推理中完成：提示词只给出 name+description 菜单，
        # 模型按需调用 Skill 工具加载，后端不再预筛候选集。
        repo_info = task_data.get("repo_info") if isinstance(task_data, dict) else None
        project_code: Optional[str] = None
        project_name: Optional[str] = None
        project_card: Optional[str] = None
        repo_url_value: Optional[str] = None
        if isinstance(repo_info, dict):
            project_code = repo_info.get("project_code") or None
            project_name = repo_info.get("project_name") or None
            project_card = repo_info.get("project_card") or None
            repo_url_value = repo_info.get("repo_url") or None

        # 「未关联代码仓库」的项目：repo_url 为空。此时不要尝试克隆仓库，
        # 改为依赖项目级系统提示词与已启用的 Skill 来回答。
        if not (repo_url_value and str(repo_url_value).strip()):
            system_prompt += (
                "\n\n## 未关联代码仓库\n"
                "本项目没有关联的代码仓库（repo_info.repo_url 为空）。"
                "不要为当前项目执行 lookup 或手工 clone，`repo/` 目录是空的。"
                "如果项目适配性检查发现另一个已注册项目明确匹配，仍可通过"
                "工作区克隆工具把它加入 `related_repos/`；否则请基于当前项目级"
                "系统提示词、已启用的 Skill 以及用户上下文回答。\n"
            )

        # 项目级附加系统提示词：像 Skill 一样分级处理——在通用（Agent 级）系统
        # 提示词之后叠加该项目的专属约束。无配置时返回空串。
        try:
            from app.services import project_prompt_service

            project_prompt_addendum = project_prompt_service.build_project_prompt_addendum(
                project_code, AGENT_KEY, project_name=project_name
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ProjectExpertAgent: failed to load project prompt: %s", exc)
            project_prompt_addendum = ""
        if project_prompt_addendum:
            system_prompt += project_prompt_addendum
            logger.info(
                "ProjectExpertAgent: applied project-level system prompt project_code=%s chars=%d",
                project_code,
                len(project_prompt_addendum),
            )

        available_skills: List[str] = []
        materialized_skills: List[str] = []
        skill_overviews: List[Dict[str, str]] = []
        try:
            from app.agents.skill_prompting import build_skill_relevance_query
            from app.services import skills_service

            all_skill_overviews = skills_service.enabled_skill_overviews(
                AGENT_KEY,
                project_code=project_code,
            )
            available_skills = [item["name"] for item in all_skill_overviews]
            skill_query = build_skill_relevance_query(
                question=ctx.metadata.get("question") or task_data.get("question", ""),
                hints=ctx.metadata.get("hints") or task_data.get("hints", ""),
                attachments=(
                    ctx.metadata.get("attachments")
                    or task_data.get("attachments")
                ),
            )
            materialized_skills = skills_service.materialize_relevant_enabled_skills(
                AGENT_KEY,
                ctx.temp_dir,
                query_text=skill_query,
                project_code=project_code,
            )
            if materialized_skills:
                selected_names = set(materialized_skills)
                skill_overviews = [
                    item
                    for item in all_skill_overviews
                    if item["name"] in selected_names
                ]
                logger.info(
                    "ProjectExpertAgent: materialized %d skill(s): %s",
                    len(materialized_skills),
                    ", ".join(materialized_skills),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ProjectExpertAgent: failed to materialize skills: %s", exc)

        # Required response rules must survive optional-Skill failures and
        # execute on every invocation, including resumed/follow-up workspaces.
        from app.agents.required_skill_policy import (
            load_required_skill_prompt,
            required_skill_names,
        )

        required_names = required_skill_names(AGENT_KEY)
        required_skill_prompt, required_skill_evidence = load_required_skill_prompt(
            AGENT_KEY, ctx.temp_dir,
        )
        available_skills = list(dict.fromkeys([*available_skills, *required_names]))
        materialized_skills = list(dict.fromkeys([*materialized_skills, *required_names]))
        optional_skills = [name for name in materialized_skills if name not in required_names]
        optional_overviews = [item for item in skill_overviews if item["name"] not in required_names]

        if optional_skills:
            skill_availability_prompt = build_skill_availability_prompt(
                optional_overviews or optional_skills,
                final_output_contract="第 5 步的围栏 JSON schema",
            )
            system_prompt += skill_availability_prompt
            user_prompt += skill_availability_prompt

        system_prompt += build_project_fit_guidance(
            workflow_name="项目专家",
            project_name=project_name,
            project_code=project_code,
            project_card=project_card,
            catalog_available=supports_mcp,
            catalog_preloaded=evidence_snapshot is not None,
            locale=ctx.locale,
            selected_project_is_target=True,
        )
        if ctx.locale == "en":
            system_prompt += (
                "\n\n## Project Expert source boundary\n"
                "The selected project in task.json.repo_info is the target of this turn. "
                "For claims about its implementation, clone and use only verified repositories "
                "from the same product/project series, based on project cards and identities; "
                "do not infer series membership from a similar component name alone. "
                "If a needed component has no registered same-series repository, say explicitly "
                "that its implementation cannot be verified. You may inspect a different-series "
                "repository for an explicit comparison or as a reference, but name that project, "
                "label every such finding as reference only, and never attribute its behavior "
                "to the selected series. If the series relationship is unclear, ask for "
                "clarification before cloning. Prior chat answers are context, not source evidence."
            )
        else:
            system_prompt += (
                "\n\n## 项目专家的源码边界\n"
                "`task.json.repo_info` 中的所选项目是本轮分析对象。对该项目实现作结论时，"
                "只能把项目卡片和身份确认属于同一产品/项目系列的仓库作为实现证据；"
                "不能仅凭组件名称相似推断同系列。若所需组件没有已注册的同系列仓库，"
                "必须明确告知用户该组件的实现无法核实。其他系列仓库只能在用户明确要求比较时，"
                "或确有参考价值时作为参考读取；必须标明参考项目和来源，明确这是参考，"
                "不能把参考项目的行为写成所选项目的实现。系列归属不明确时先澄清，"
                "不要猜测后克隆。历史对话中的旧回答仅用于理解问题，不能替代当前源码证据。"
            )

        setting_sources = ["project"] if materialized_skills else None

        # Append the blunt response-language directive last so the answer
        # language is decoupled from the (possibly Chinese) source code/input.
        from app.i18n.prompts import response_language_directive

        system_prompt += "\n\n" + response_language_directive(ctx.locale)

        # ``state`` first: the ask tool must share this run's seq space, or the
        # frontend's (run_id, seq) deduper drops the question card.
        state = _RunState(task_id=ctx.task_id, emitter=trace_emitter)

        # --- AskUserQuestion clarification (optional) -----------------------
        clarification = (
            clarification_binding.setup(
                emit=state.emit,
                seq_counter=state.seq_counter,
                task_id=ctx.task_id,
                locale=ctx.locale,
                workflow_agent=True,
            )
            if clarification_binding is not None
            else None
        )
        if clarification is not None:
            mcp_servers, allowed_tools = clarification.apply(mcp_servers, allowed_tools)
            system_prompt += "\n\n" + clarification.prompt_addendum

        system_prompt += required_skill_prompt
        evidence_hooks = None
        evidence_review = None
        if evidence_snapshot is not None:
            from app.agents.evidence_policy import execution_hooks, snapshot_prompt
            from app.agents.evidence_review import FinalEvidenceReview

            system_prompt += snapshot_prompt(evidence_snapshot, ctx.locale)
            if settings.agent_evidence_final_review_enabled is True:
                evidence_review = FinalEvidenceReview(ctx.locale, ctx.task_id, settings.anthropic_max_turns)
            evidence_hooks = execution_hooks(
                ctx.locale, ctx.task_id, ctx.temp_dir,
                source_context=settings.agent_evidence_source_context_enabled is True,
                final_review=evidence_review,
            )

        def _make_options(endpoint: Optional[Any]) -> Any:
            return build_options(
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                cwd=ctx.temp_dir,
                # Claude Code's built-in AskUserQuestion is not wired to the
                # RavenAI broker/SSE card and would block forever; only the
                # qualified MCP tool above is valid here.
                disallowed_tools=[BUILTIN_ASK_TOOL_NAME],
                permission_mode="bypassPermissions",
                mcp_servers=mcp_servers,
                setting_sources=setting_sources,
                endpoint=endpoint,
                **({"hooks": evidence_hooks} if evidence_hooks else {}),
            )

        def _on_endpoint(endpoint: Any) -> None:
            served["model"] = endpoint.model
            served["provider"] = endpoint.provider

        start = time.monotonic()

        _log_workflow(ctx.task_id, "run_start", model=effective_model)
        state.emit(
            build_event(
                RUN_START,
                task_id=ctx.task_id,
                seq_counter=state.seq_counter,
                model=effective_model,
                provider=str(provider),
                available_skills=list(available_skills),
                loaded_skills=list(materialized_skills),
            )
        )
        if available_skills:
            state.emit(
                build_event(
                    SYSTEM_NOTICE,
                    task_id=ctx.task_id,
                    seq_counter=state.seq_counter,
                    kind="skills_available",
                    detail=", ".join(available_skills),
                    available_skills=list(available_skills),
                    loaded_skills=list(materialized_skills),
                )
            )

        for evidence in required_skill_evidence:
            state.emit(
                build_event(
                    SYSTEM_NOTICE,
                    task_id=ctx.task_id,
                    seq_counter=state.seq_counter,
                    kind="required_skill_loaded",
                    detail=f"{evidence['name']}: complete rules loaded for this response",
                    **evidence,
                )
            )

        try:
            async for message in routed_query(
                prompt=user_prompt,
                make_options=_make_options,
                agent_kind=AGENT_KEY,
                candidates=endpoints,
                on_endpoint=_on_endpoint,
            ):
                if cancel_event is not None and cancel_event.is_set():
                    _log_workflow(ctx.task_id, "cancelled", reason="cancel_event_set")
                    _emit_cancel_requested(state)
                    raise AgentCancelled()
                if evidence_review is not None:
                    evidence_review.observe(message)
                _emit_for_message(message, state=state)

        except AgentCancelled:
            duration = time.monotonic() - start
            _log_workflow(
                ctx.task_id,
                "run_complete",
                status="cancelled",
                duration_s=round(duration, 2),
                tokens_in=state.token_usage["input_tokens"],
                tokens_out=state.token_usage["output_tokens"],
            )
            _close_any_active_steps(state, reason="cancelled")
            trace_summary = summarize(state.trace_events)
            state.emit(
                build_event(
                    CANCELLED,
                    task_id=ctx.task_id,
                    seq_counter=state.seq_counter,
                    trace_summary=trace_summary,
                )
            )
            return _cancelled_result(
                model=served["model"],
                duration=duration,
                tool_trace=derive_tool_trace(state.trace_events),
                token_usage=state.token_usage,
                trace_events=list(state.trace_events),
                trace_summary=trace_summary,
            )
        except asyncio.TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            duration = time.monotonic() - start
            _close_any_active_steps(state, reason="error")
            trace_summary = summarize(state.trace_events)
            state.emit(
                build_event(
                    ERROR,
                    task_id=ctx.task_id,
                    seq_counter=state.seq_counter,
                    error_kind=type(exc).__name__,
                    message=str(exc),
                    trace_summary=trace_summary,
                )
            )
            logger.exception("ProjectExpertAgent: run failed: %s", exc)
            raise
        finally:
            # The ask tool can only fire inside the loop above; release the
            # broker registry slot and deny anything still pending.
            if clarification is not None:
                clarification.close()

        final_text = state.final_text
        duration = time.monotonic() - start
        _log_workflow(
            ctx.task_id,
            "run_complete",
            status="finished",
            tool_calls=sum(1 for ev in state.trace_events if ev.get("type") == STEP_END),
            duration_s=round(duration, 2),
            tokens_in=state.token_usage["input_tokens"],
            tokens_out=state.token_usage["output_tokens"],
        )

        trace_summary = summarize(state.trace_events)
        state.emit(
            build_event(
                RUN_COMPLETE,
                task_id=ctx.task_id,
                seq_counter=state.seq_counter,
                trace_summary=trace_summary,
                final_text=coerce_excerpt(mask_tokens(final_text), DEFAULT_EXCERPT_MAX_BYTES * 4),
            )
        )

        parsed = _extract_fenced_json(final_text)
        tool_trace = derive_tool_trace(state.trace_events)
        common_extra = {
            "tool_trace": tool_trace,
            "trace_events": list(state.trace_events),
            "trace_summary": trace_summary,
            "raw": final_text,
            "duration_seconds": round(duration, 2),
            "token_usage": state.token_usage,
            "loaded_skills": list(materialized_skills),
        }

        if parsed is None:
            wrapped_skill_answer = build_plain_text_skill_answer_fields(
                final_text,
                optional_skills,
            )
            if wrapped_skill_answer:
                logger.warning(
                    "ProjectExpertAgent: no fenced JSON in skill result; wrapped plain text answer"
                )
                return {
                    "engine": "claude-agent-sdk",
                    "model": served["model"],
                    "provider": served["provider"],
                    "schema_version": 3,
                    **wrapped_skill_answer,
                    **common_extra,
                }
            logger.warning("ProjectExpertAgent: no fenced JSON in result, schema_mismatch")
            return _empty_result(
                served["model"], status="schema_mismatch", error_kind=None, **common_extra
            )

        if not _validate_result_schema(parsed):
            recovered = extract_recoverable_result_fields(final_text)
            if recovered:
                logger.warning(
                    "ProjectExpertAgent: result JSON incomplete; recovered user-facing answer"
                )
                recovered_status = recovered.get("status", "ok")
                recovered_error_kind = recovered.get("error_kind")
                if recovered_error_kind:
                    recovered_status = "error"
                return {
                    "engine": "claude-agent-sdk",
                    "model": served["model"],
                    "provider": served["provider"],
                    "schema_version": 3,
                    "status": recovered_status,
                    "error_kind": recovered_error_kind,
                    "question_type": recovered.get("question_type", "other"),
                    "answer": recovered.get("answer", ""),
                    "summary": recovered.get("summary", ""),
                    "severity": recovered.get("severity", "info"),
                    "root_cause_hypotheses": _strip_confidence_fields(
                        recovered.get("root_cause_hypotheses", [])
                    ),
                    "recommended_actions": recovered.get("recommended_actions", []),
                    "related_keywords": recovered.get("related_keywords", []),
                    "parse_warning": "incomplete_json_recovered",
                    **common_extra,
                }

            logger.warning("ProjectExpertAgent: result JSON missing required fields, schema_mismatch")
            return _empty_result(
                served["model"], status="schema_mismatch", error_kind=None, **common_extra
            )

        status = parsed.get("status", "ok")
        error_kind = parsed.get("error_kind")
        if error_kind:
            status = "error"

        question_type = _normalize_question_type(parsed.get("question_type"))
        answer = parsed.get("answer", "") or ""
        summary = parsed.get("summary", "") or ""
        if not answer and summary:
            answer = summary

        return {
            "engine": "claude-agent-sdk",
            "model": served["model"],
            "provider": served["provider"],
            "schema_version": 3,
            "status": status,
            "error_kind": error_kind,
            "question_type": question_type,
            "answer": answer,
            "summary": summary,
            "severity": parsed.get("severity", "info"),
            "root_cause_hypotheses": _strip_confidence_fields(
                parsed.get("root_cause_hypotheses", [])
            ),
            "recommended_actions": parsed.get("recommended_actions", []),
            "related_keywords": parsed.get("related_keywords", []),
            **common_extra,
        }

    def run_sync(
        self,
        ctx: WorkspaceContext,
        cancel_event: Optional[threading.Event] = None,
        trace_emitter: Optional[TraceEmitter] = None,
        clarification_binding: Optional["ClarificationBinding"] = None,
    ) -> Dict[str, Any]:
        """Synchronous wrapper (for background threads). Applies request timeout.

        Runs on a fresh event loop in the calling thread, i.e. not the FastAPI
        loop that serves the clarification-resolve endpoint; ``PermissionBroker``
        settles futures across loops so ``clarification_binding`` still works.
        """
        from app.config import settings

        timeout = settings.anthropic_request_timeout_seconds
        try:
            return asyncio.run(
                asyncio.wait_for(
                    self.run(
                        ctx,
                        cancel_event=cancel_event,
                        trace_emitter=trace_emitter,
                        clarification_binding=clarification_binding,
                    ),
                    timeout=float(timeout),
                )
            )
        except asyncio.TimeoutError:
            logger.error("ProjectExpertAgent: timed out after %ds", timeout)
            return _empty_result(
                "unknown",
                status="error",
                error_kind="timeout",
                severity="error",
                tool_trace=[],
                trace_events=[],
                trace_summary={
                    "thought_duration_seconds": float(timeout),
                    "tool_call_count": 0,
                    "thinking_chars": 0,
                },
                raw=f"Agent timed out after {timeout} seconds",
                duration_seconds=float(timeout),
                token_usage={"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
            )
