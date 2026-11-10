"""AskUserQuestion —— Agent 主动向用户澄清不清晰指令的跨 Agent 公共能力。

用户偏好「指令不清晰时允许 Agent 向我提问」是**全局**开关（见前端
``settings.clarification.enabledLabel``），因此本模块不属于任何单个 agent：
device / log_analysis / project_expert / package_search 四个面向对话的 agent
都从这里获得同一套工具、同一段提示词、同一份用户偏好解析逻辑。

与 ``device_agent/permissions.py`` 的工具审批（HITL）同构：调用即阻塞 agent
loop，发出 ``clarification_request`` trace 事件，等待 HTTP 端点
``POST /chat/clarifications/{request_id}/resolve`` 把用户答案写回
:class:`PermissionBroker` 的 Future，随后把答案作为工具返回值回喂模型继续。

设计要点（详见 openspec design.md Decision 1/2/6/9）：

- **触发权归模型**：是否调用 ``AskUserQuestion`` 完全由模型决定；不清晰才问。
- **复用 broker**：同一 run 内 permission 与 clarification 共享一个
  :class:`PermissionBroker`；``request_id`` 全 UUID，互不串扰。broker 按
  ``run_id`` 注册进 ``chat_run_service``，resolve 端点据此定位——所有 agent
  （含通过 ``register_external_job`` 挂靠的 workspace agent）走同一张表。
- **跨 loop**：workspace agent 跑在 ``asyncio.to_thread`` 起的独立 loop 中，
  broker 的 settle 逻辑已按跨 loop 情形处理，本模块无需额外关心。
- **多问题**：一次调用支持 1..N 个问题，每问 2–4 预设选项 + 隐式自由输入。
- **超时**：等待时长由 ``settings.agent_clarification_timeout_seconds`` 决定
  （代码常量，默认 5 分钟）；超时行为 ``on_timeout ∈ {cancel, continue}``
  决定取消本轮 run 还是让模型继续。
- **每轮上限**：``max_rounds`` 限制单次 run 的提问次数，达上限后直接返回提示。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.agents.hitl_broker import PermissionBroker
from app.agents.log_analysis.trace import (
    CLARIFICATION_REQUEST,
    CLARIFICATION_RESOLVED,
    SeqCounter,
    build_event,
    safe_emit,
)

logger = logging.getLogger(__name__)

ASK_SERVER_NAME = "ask"
ASK_TOOL_NAME = "AskUserQuestion"
ASK_SDK_NAME = f"mcp__{ASK_SERVER_NAME}__{ASK_TOOL_NAME}"

# Claude Code ships a built-in tool of the same bare name. It is not wired to
# RavenAI's broker or SSE card, so a headless run that calls it waits forever on
# a terminal UI that does not exist. Every agent must disallow it and expose
# only the qualified in-process MCP tool above.
BUILTIN_ASK_TOOL_NAME = ASK_TOOL_NAME

EmitFn = Callable[[Any], None]
CancelRunFn = Callable[[], None]

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_ROUNDS = 5


# ─────────────────────── Preferences ───────────────────────────────


@dataclass(frozen=True)
class ClarificationPrefs:
    """用户「Agent 澄清提问」偏好的规范化视图。

    三个字段与 ``User.clarification_*`` 一一对应；``timeout_seconds`` 是代码
    常量（非用户可改）。所有 agent 必须经由 :meth:`from_user` /
    :meth:`from_mapping` 读取，避免各处 ``getattr`` 默认值漂移。
    """

    enabled: bool = True
    max_rounds: int = DEFAULT_MAX_ROUNDS
    on_timeout: str = "cancel"
    timeout_seconds: float = float(DEFAULT_TIMEOUT_SECONDS)

    @staticmethod
    def _timeout_from_settings() -> float:
        try:
            from app.config import settings
        except Exception:  # pragma: no cover - config always importable in app
            return float(DEFAULT_TIMEOUT_SECONDS)
        value = getattr(settings, "agent_clarification_timeout_seconds", None)
        if not value:
            # Legacy key kept for deployments whose .env still sets it.
            value = getattr(
                settings,
                "device_agent_clarification_timeout_seconds",
                DEFAULT_TIMEOUT_SECONDS,
            )
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(DEFAULT_TIMEOUT_SECONDS)

    @classmethod
    def from_user(cls, user: Any) -> "ClarificationPrefs":
        """从 ``User`` 记录构造；``None``（匿名用户）取默认值（开启）。"""
        return cls(
            enabled=bool(getattr(user, "clarification_enabled", True)),
            max_rounds=int(
                getattr(user, "clarification_max_rounds", DEFAULT_MAX_ROUNDS) or 0
            ),
            on_timeout=str(
                getattr(user, "clarification_on_timeout", "cancel") or "cancel"
            ),
            timeout_seconds=cls._timeout_from_settings(),
        )

    @classmethod
    def from_mapping(cls, data: Optional[Dict[str, Any]]) -> "ClarificationPrefs":
        """从已序列化的偏好字典构造（跨线程/跨任务传递时使用）。"""
        data = data or {}
        return cls(
            enabled=bool(data.get("enabled", True)),
            max_rounds=int(data.get("max_rounds", DEFAULT_MAX_ROUNDS) or 0),
            on_timeout=str(data.get("on_timeout", "cancel") or "cancel"),
            timeout_seconds=float(
                data.get("timeout_seconds") or cls._timeout_from_settings()
            ),
        )

    @classmethod
    def disabled(cls) -> "ClarificationPrefs":
        """非交互入口（Celery 批处理等）——没有人能作答，一律不提问。"""
        return cls(enabled=False)

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_rounds": self.max_rounds,
            "on_timeout": self.on_timeout,
            "timeout_seconds": self.timeout_seconds,
        }

    @property
    def active(self) -> bool:
        """真正会向用户提问：既要开关打开，也要还允许至少提问 1 次。"""
        return bool(self.enabled) and int(self.max_rounds) > 0


class MandatoryClarificationError(RuntimeError):
    """A server-enforced clarification could not be completed safely.

    Unlike the model-facing ``AskUserQuestion`` tool, callers must treat this
    exception as a terminal gate: no protected side effect may run after it.
    ``code`` is stable for API/tests while the human-readable message can be
    localized by the caller.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ─────────────────────── Prompt guidance ───────────────────────────

_CLARIFICATION_GUIDANCE: Dict[str, str] = {
    "zh": (
        "## 何时向用户提问（mcp__ask__AskUserQuestion）\n"
        "当且仅当出现以下情况时，调用 `mcp__ask__AskUserQuestion` 向用户澄清：\n"
        "- 执行该动作所必需的关键参数缺失；\n"
        "- 指令存在多种合理且后果不同的解读；\n"
        "- 操作目标对象/范围不明确，猜错代价较高。\n"
        "必须使用完整工具名 `mcp__ask__AskUserQuestion`；不要调用 Claude CLI 内置的"
        "同名 `AskUserQuestion` 工具，后者未接入本产品的提问卡片。\n"
        "能够根据上下文合理推断时，不要打断用户，直接继续。\n"
        "提问时：把需要澄清的点尽量在一次调用里问全（每个问题给 2–4 个预设选项，"
        "并配简短说明）；本轮最多可提问 {max_rounds} 次，达上限后请基于已知信息自行决断。"
    ),
    "en": (
        "## When to ask the user (mcp__ask__AskUserQuestion)\n"
        "Call `mcp__ask__AskUserQuestion` to clarify only when:\n"
        "- a required parameter for the action is missing;\n"
        "- the instruction has multiple reasonable interpretations with different outcomes;\n"
        "- the target/scope is ambiguous and guessing wrong is costly.\n"
        "Always use the full name `mcp__ask__AskUserQuestion`; never use Claude CLI's "
        "built-in `AskUserQuestion`, which is not connected to RavenAI's clarification card.\n"
        "If you can reasonably infer intent from context, do NOT interrupt — just proceed.\n"
        "When you do ask, batch everything you need into a single call (2–4 preset options "
        "with short descriptions per question). You may ask at most {max_rounds} time(s) this "
        "run; once the cap is hit, decide using the information you have."
    ),
}

# Investigative agents (log analysis / project expert / package search) run a
# mandatory multi-step workflow that ends in a fenced-JSON contract. Without an
# explicit carve-out the model treats "always complete the workflow" as an
# instruction to never pause, so a bare prompt like "请定位问题" gets answered by
# guessing an interpretation instead of asking. This addendum states the
# precedence and names the concrete ambiguity that matters for these agents.
_WORKFLOW_CLARIFICATION_ADDENDUM: Dict[str, str] = {
    "zh": (
        "\n本条规则**优先于**下文的强制工作流：当用户的问题本身不清晰时，"
        "先提问澄清，再开始（或继续）工作流；不要为了走完流程而先猜一个解读。"
        "澄清发生在流程中间也没关系——拿到答案后从当前步骤继续即可，"
        "最终仍按规定输出围栏 JSON。\n"
        "对本智能体而言，以下情形属于「问题不清晰」，应当提问：\n"
        "- 用户只给出「请定位问题」「分析一下」「看看有没有问题」这类没有指明"
        "现象、模块或时间范围的笼统诉求，而材料中存在多处彼此独立、"
        "严重程度不同的可疑点，无法判断用户关心哪一个；\n"
        "- 用户描述的现象在材料中找不到对应线索，需要用户补充复现步骤、"
        "发生时间或具体报错；\n"
        "- 材料包含多个附件/模块/进程，而用户没有指明要针对哪一个。\n"
        "反之，如果材料中的问题指向单一且明确（例如只有一处错误，或用户已经"
        "说明了现象），就不要提问，直接给出结论。"
    ),
    "en": (
        "\nThis rule takes **precedence over** the mandatory workflow below: when the "
        "user's question itself is unclear, ask first, then start (or resume) the "
        "workflow — do not pick an interpretation just to keep the workflow moving. "
        "Clarifying mid-workflow is fine; resume from the current step once answered and "
        "still finish with the required fenced JSON.\n"
        "For this agent, the following count as an unclear question and should be asked about:\n"
        '- the user gives only a blanket request such as "locate the problem" / "take a '
        'look" with no symptom, module or time range, while the material contains several '
        "independent suspicious findings of differing severity, so which one they care "
        "about cannot be determined;\n"
        "- the symptom the user describes has no matching evidence in the material, and "
        "reproduction steps, a time window or the exact error text are needed;\n"
        "- the material spans multiple attachments / modules / processes and the user did "
        "not say which one to target.\n"
        "Conversely, when the material points at a single unambiguous problem (only one "
        "error, or the user already stated the symptom), do NOT ask — just answer."
    ),
}


def clarification_guidance(
    locale: Optional[str] = None,
    *,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    workflow_agent: bool = False,
) -> str:
    """返回 AskUserQuestion 使用指引（按 locale），供 system prompt 末尾追加。

    仅在澄清生效（:attr:`ClarificationPrefs.active`）时由调用方拼接；
    禁用澄清时不应出现。

    Args:
        locale: ``zh`` / ``en``；其它值回退到 ``zh``。
        max_rounds: 本轮提问上限，写进提示词让模型自己收敛。
        workflow_agent: 目标 agent 带强制多步工作流（log_analysis /
            project_expert / package_search）时置 True，追加优先级说明，
            否则「必须走完工作流」会把提问压制掉。
    """
    lang = (locale or "zh").strip().lower()
    if lang not in _CLARIFICATION_GUIDANCE:
        lang = "zh"
    body = _CLARIFICATION_GUIDANCE[lang]
    try:
        body = body.format(max_rounds=max_rounds)
    except (KeyError, IndexError):
        pass
    if workflow_agent:
        body += _WORKFLOW_CLARIFICATION_ADDENDUM[lang]
    return body


# ─────────────────────── Schema ────────────────────────────────────

_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "description": "要向用户澄清的一个或多个问题。",
            "items": {
                "type": "object",
                "properties": {
                    "header": {
                        "type": "string",
                        "description": "极短标签（≤12 字），用于 UI chip 展示。",
                    },
                    "question": {
                        "type": "string",
                        "description": "完整的问题文本。",
                    },
                    "multiSelect": {
                        "type": "boolean",
                        "description": "是否允许多选；缺省 false（单选）。",
                    },
                    "options": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "description": "2–4 个预设选项；用户也可不选预设、自行输入。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "选项简短文本。",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "选项含义/影响说明。",
                                },
                            },
                            "required": ["label", "description"],
                        },
                    },
                },
                "required": ["header", "question", "options"],
            },
        }
    },
    "required": ["questions"],
}

_TOOL_DESCRIPTION = (
    "当用户指令不清晰（缺少关键参数、存在多种合理解读、目标对象/范围不明确）时，"
    "向用户提出一个或多个澄清问题，每个问题给出 2–4 个预设选项；用户也可自行输入。"
    "能够合理推断时不要调用本工具。调用会阻塞，直到用户作答后返回其选择。"
)


# ─────────────────────── Helpers ───────────────────────────────────


def _normalize_questions(raw: Any) -> List[Dict[str, Any]]:
    """把模型传入的 ``questions`` 规整成稳定结构；非法项被丢弃。"""
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        header = str(item.get("header") or "").strip()
        multi = bool(item.get("multiSelect") or False)
        opts_raw = item.get("options")
        options: List[Dict[str, str]] = []
        if isinstance(opts_raw, list):
            for opt in opts_raw:
                if not isinstance(opt, dict):
                    continue
                label = str(opt.get("label") or "").strip()
                if not label:
                    continue
                options.append(
                    {
                        "label": label,
                        "description": str(opt.get("description") or "").strip(),
                    }
                )
        out.append(
            {
                "header": header,
                "question": question,
                "multiSelect": multi,
                "options": options,
                **(
                    {"question_key": str(item.get("question_key"))}
                    if item.get("question_key") is not None
                    else {}
                ),
                **(
                    {"allow_custom": bool(item["allow_custom"])}
                    if "allow_custom" in item
                    else {}
                ),
                **(
                    {"required": bool(item["required"])}
                    if "required" in item
                    else {}
                ),
            }
        )
    return out


def _wrap_text(text: str) -> Dict[str, Any]:
    """SDK 工具返回形态：``{"content":[{"type":"text","text":...}]}``。"""
    return {"content": [{"type": "text", "text": text}]}


def _format_answers(questions: List[Dict[str, Any]], answers: Any) -> str:
    """把用户答案渲染为结构化、人类可读的文本块回喂模型。"""
    by_index: Dict[int, Dict[str, Any]] = {}
    if isinstance(answers, list):
        for ans in answers:
            if not isinstance(ans, dict):
                continue
            try:
                idx = int(ans.get("question_index"))
            except (TypeError, ValueError):
                continue
            by_index[idx] = ans

    lines: List[str] = ["用户对澄清问题的回答："]
    for i, q in enumerate(questions):
        lines.append(f"{i + 1}. {q.get('question', '')}")
        ans = by_index.get(i) or {}
        selected = ans.get("selected_labels")
        labels = (
            [str(s).strip() for s in selected if str(s).strip()]
            if isinstance(selected, list)
            else []
        )
        custom = str(ans.get("custom_text") or "").strip()
        if labels:
            lines.append(f"   - 选择：{', '.join(labels)}")
        if custom:
            lines.append(f"   - 补充：{custom}")
        if not labels and not custom:
            lines.append("   - （未作答）")
    return "\n".join(lines)


def _normalize_complete_answers(
    questions: List[Dict[str, Any]], answers: Any
) -> List[Dict[str, Any]]:
    """Validate that every mandatory question has exactly one complete answer."""
    if not isinstance(answers, list):
        raise MandatoryClarificationError(
            "missing_answers", "配置管理员未收到完整的打包确认答案。"
        )

    by_index: Dict[int, Dict[str, Any]] = {}
    for raw in answers:
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw.get("question_index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(questions) or index in by_index:
            raise MandatoryClarificationError(
                "invalid_answers", "打包确认答案包含无效或重复的问题序号。"
            )
        selected_raw = raw.get("selected_labels")
        selected = (
            [str(value).strip() for value in selected_raw if str(value).strip()]
            if isinstance(selected_raw, list)
            else []
        )
        custom = str(raw.get("custom_text") or "").strip()
        if custom and questions[index].get("allow_custom") is False:
            raise MandatoryClarificationError(
                "invalid_answers", "该确认问题不接受自定义答案，请选择已有选项。"
            )
        if not selected and not custom:
            raise MandatoryClarificationError(
                "missing_answers", "项目与每个上传文件都必须由用户逐项确认。"
            )
        if not bool(questions[index].get("multiSelect")) and len(selected) > 1:
            raise MandatoryClarificationError(
                "invalid_answers", "单选确认问题不能选择多个选项。"
            )
        allowed = {
            str(option.get("label") or "").strip()
            for option in questions[index].get("options") or []
            if isinstance(option, dict)
        }
        if selected and any(label not in allowed for label in selected):
            raise MandatoryClarificationError(
                "invalid_answers", "打包确认答案引用了不存在的选项。"
            )
        by_index[index] = {
            "question_index": index,
            "selected_labels": selected,
            "custom_text": custom or None,
            **(
                {"question_key": questions[index]["question_key"]}
                if questions[index].get("question_key") is not None
                else {}
            ),
        }

    if len(by_index) != len(questions):
        raise MandatoryClarificationError(
            "missing_answers", "项目与每个上传文件都必须由用户逐项确认。"
        )
    return [by_index[index] for index in range(len(questions))]


async def request_mandatory_clarification(
    questions: List[Dict[str, Any]],
    *,
    broker: PermissionBroker,
    emit: EmitFn,
    seq_counter: SeqCounter,
    task_id: str,
    run_id: str,
    session_id: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    cancel_run: Optional[CancelRunFn] = None,
    event_fields: Optional[Dict[str, Any]] = None,
    publication_review: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Ask a non-optional, programmatic confirmation through the existing UI.

    This is deliberately separate from ``AskUserQuestion``.  It is invoked by
    the service before a protected build/publication, so it does not depend on
    model tool use and ignores the user's preference for *optional* agent
    clarification.  The caller must bind the returned answers to an immutable
    plan and must stop on every :class:`MandatoryClarificationError`.
    """
    normalized = _normalize_questions(questions)
    if not normalized or len(normalized) != len(questions):
        raise MandatoryClarificationError(
            "invalid_questions", "无法生成完整的打包确认问题。"
        )
    if not run_id:
        raise MandatoryClarificationError(
            "missing_run_id", "打包确认缺少可回传答案的运行标识。"
        )

    request_id = str(uuid.uuid4())
    extra = dict(event_fields or {})
    # Register the broker request before exposing its ID on SSE.  A fast client
    # can resolve as soon as it sees the frame; emitting first creates a small
    # but real 404 race.
    try:
        future = broker.open_clarification(request_id)
    except RuntimeError as exc:
        raise MandatoryClarificationError(
            "broker_closed", "打包确认通道不可用，已阻止构建和发布。"
        ) from exc
    safe_emit(
        emit,
        build_event(
            CLARIFICATION_REQUEST,
            task_id=task_id,
            seq_counter=seq_counter,
            request_id=request_id,
            questions=normalized,
            run_id=run_id,
            session_id=session_id,
            mandatory=True,
            purpose="package_build_confirmation",
            **extra,
            **({"publication_review": publication_review} if publication_review is not None else {}),
        ),
    )

    timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else ClarificationPrefs._timeout_from_settings()
    )
    try:
        decision = await asyncio.wait_for(future, timeout=max(0.1, timeout))
    except asyncio.TimeoutError as exc:
        broker.cancel(request_id, reason="timeout")
        safe_emit(
            emit,
            build_event(
                CLARIFICATION_RESOLVED,
                task_id=task_id,
                seq_counter=seq_counter,
                request_id=request_id,
                outcome="cancelled",
                reason="timeout",
                run_id=run_id,
                session_id=session_id,
                mandatory=True,
                purpose="package_build_confirmation",
                **extra,
            ),
        )
        if cancel_run is not None:
            cancel_run()
        raise MandatoryClarificationError(
            "timeout", "用户未在限定时间内完成打包确认，已阻止构建和发布。"
        ) from exc
    except asyncio.CancelledError:
        broker.cancel(request_id, reason="cancelled")
        raise

    if not isinstance(decision, dict) or decision.get("decision") == "deny":
        reason = (
            str(decision.get("reason") or "cancelled")
            if isinstance(decision, dict)
            else "cancelled"
        )
        safe_emit(
            emit,
            build_event(
                CLARIFICATION_RESOLVED,
                task_id=task_id,
                seq_counter=seq_counter,
                request_id=request_id,
                outcome="cancelled",
                reason=reason,
                run_id=run_id,
                session_id=session_id,
                mandatory=True,
                purpose="package_build_confirmation",
                **extra,
            ),
        )
        raise MandatoryClarificationError(
            "cancelled", "打包确认已取消，未执行构建和发布。"
        )
    try:
        complete = _normalize_complete_answers(normalized, decision.get("answers"))
    except MandatoryClarificationError as exc:
        safe_emit(
            emit,
            build_event(
                CLARIFICATION_RESOLVED,
                task_id=task_id,
                seq_counter=seq_counter,
                request_id=request_id,
                outcome="rejected",
                reason=exc.code,
                run_id=run_id,
                session_id=session_id,
                mandatory=True,
                purpose="package_build_confirmation",
                **extra,
            ),
        )
        raise

    safe_emit(
        emit,
        build_event(
            CLARIFICATION_RESOLVED,
            task_id=task_id,
            seq_counter=seq_counter,
            request_id=request_id,
            outcome="answered",
            run_id=run_id,
            session_id=session_id,
            mandatory=True,
            purpose="package_build_confirmation",
            **extra,
        ),
    )
    return complete


# ─────────────────────── Tool factory ──────────────────────────────


def make_ask_user_question_tool(
    *,
    broker: PermissionBroker,
    timeout_seconds: float,
    on_timeout: str = "cancel",
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    cancel_run: Optional[CancelRunFn] = None,
    emit: Optional[EmitFn] = None,
    seq_counter: Optional[SeqCounter] = None,
    task_id: str = "",
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    tool_decorator: Callable[..., Any],
) -> Any:
    """构造 ``AskUserQuestion`` 的 SDK 工具 proxy。

    Args:
        broker: 与 resolve 端点共享的 per-run broker。
        timeout_seconds: 用户作答等待时长（秒）。
        on_timeout: ``"cancel"`` 取消本轮 run；``"continue"`` 让模型基于已知信息继续。
        max_rounds: 单次 run 最多发起的 ``AskUserQuestion`` 次数。
        cancel_run: 取消本轮 run 的回调（``cancel`` 模式超时时调用）；缺省时降级为继续。
        emit/seq_counter/task_id/run_id/session_id: trace 事件上下文。
        tool_decorator: ``claude_agent_sdk.tool`` 装饰器（由调用方注入，便于测试）。
    """

    # Per-run 提问计数（闭包内可变）。
    counter: Dict[str, int] = {"count": 0}
    normalized_on_timeout = (
        "continue" if str(on_timeout).strip().lower() == "continue" else "cancel"
    )

    def _emit_request(request_id: str, questions: List[Dict[str, Any]]) -> None:
        if emit is None or seq_counter is None:
            return
        safe_emit(
            emit,
            build_event(
                CLARIFICATION_REQUEST,
                task_id=task_id,
                seq_counter=seq_counter,
                request_id=request_id,
                questions=questions,
                run_id=run_id,
                session_id=session_id,
            ),
        )

    def _emit_resolved(
        request_id: str, outcome: str, *, reason: Optional[str] = None
    ) -> None:
        if emit is None or seq_counter is None:
            return
        safe_emit(
            emit,
            build_event(
                CLARIFICATION_RESOLVED,
                task_id=task_id,
                seq_counter=seq_counter,
                request_id=request_id,
                outcome=outcome,
                reason=reason,
                run_id=run_id,
                session_id=session_id,
            ),
        )

    @tool_decorator(ASK_TOOL_NAME, _TOOL_DESCRIPTION, _INPUT_SCHEMA)
    async def _ask(args):
        questions = _normalize_questions((args or {}).get("questions"))
        if not questions:
            return _wrap_text(
                "AskUserQuestion 未提供有效问题；请基于已知信息继续，不要重复调用。"
            )

        # 每轮提问上限：达上限后不再阻塞用户。
        if counter["count"] >= max(0, int(max_rounds)):
            logger.info(
                "AskUserQuestion: max rounds reached run_id=%s max=%s",
                run_id,
                max_rounds,
            )
            return _wrap_text(
                f"已达到本轮提问上限（{max_rounds} 次）。请基于已知信息自行决断，"
                "不要再调用 AskUserQuestion。"
            )
        counter["count"] += 1

        request_id = str(uuid.uuid4())
        try:
            future = broker.open_clarification(request_id)
        except RuntimeError as exc:
            logger.warning("AskUserQuestion: broker closed: %s", exc)
            _emit_resolved(request_id, "cancelled", reason="broker_closed")
            return _wrap_text("澄清通道不可用；请基于已知信息继续。")
        _emit_request(request_id, questions)

        try:
            decision = await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            broker.cancel(request_id, reason="timeout")
            if normalized_on_timeout == "continue" or cancel_run is None:
                _emit_resolved(request_id, "timeout", reason="timeout")
                return _wrap_text(
                    "用户未在限定时间内回答澄清问题；请基于已知信息给出最合理的处理"
                    "或最佳猜测，继续完成任务。"
                )
            # cancel 模式：取消本轮 run。
            _emit_resolved(request_id, "cancelled", reason="timeout")
            try:
                cancel_run()
            except Exception as exc:  # noqa: BLE001
                logger.warning("AskUserQuestion: cancel_run failed: %s", exc)
            return _wrap_text("用户未在限定时间内回答；本轮已取消。")
        except asyncio.CancelledError:
            broker.cancel(request_id, reason="cancelled")
            raise

        answers = decision.get("answers") if isinstance(decision, dict) else None
        _emit_resolved(request_id, "answered")
        return _wrap_text(_format_answers(questions, answers))

    return _ask


def build_clarification_mcp_server(
    *,
    broker: PermissionBroker,
    timeout_seconds: float,
    on_timeout: str = "cancel",
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    cancel_run: Optional[CancelRunFn] = None,
    emit: Optional[EmitFn] = None,
    seq_counter: Optional[SeqCounter] = None,
    task_id: str = "",
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Tuple[Any, str]:
    """构造 ``ask`` in-process MCP server，仅含 ``AskUserQuestion`` 工具。

    Returns:
        ``(server, sdk_tool_name)``：server 传给
        ``ClaudeAgentOptions.mcp_servers={"ask": server}``；``sdk_tool_name`` 为
        ``mcp__ask__AskUserQuestion``，调用方追加进 ``allowed_tools``。
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    proxy = make_ask_user_question_tool(
        broker=broker,
        timeout_seconds=timeout_seconds,
        on_timeout=on_timeout,
        max_rounds=max_rounds,
        cancel_run=cancel_run,
        emit=emit,
        seq_counter=seq_counter,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        tool_decorator=tool,
    )
    server = create_sdk_mcp_server(name=ASK_SERVER_NAME, version="1.0.0", tools=[proxy])
    return server, ASK_SDK_NAME


# ─────────────────────── One-call setup ────────────────────────────


@dataclass
class ClarificationRuntime:
    """一次 run 内澄清能力的全部产物，由 :func:`setup_clarification` 生成。

    Agent 只需把三样东西并入自己的 SDK 配置：``mcp_server`` 挂到
    ``mcp_servers["ask"]``、``sdk_tool_name`` 追加进 ``allowed_tools``、
    ``prompt_addendum`` 拼到 system prompt 末尾。``close()` 在 ``finally``
    中调用即可解除 broker 注册并 deny 所有挂起请求。
    """

    broker: PermissionBroker
    mcp_server: Any
    sdk_tool_name: str
    prompt_addendum: str
    _close: Optional[Callable[[], None]] = None

    def apply(
        self,
        mcp_servers: Optional[Dict[str, Any]],
        allowed_tools: List[str],
    ) -> Tuple[Dict[str, Any], List[str]]:
        """把澄清工具并入调用方已有的 ``mcp_servers`` / ``allowed_tools``。"""
        servers = dict(mcp_servers or {})
        servers[ASK_SERVER_NAME] = self.mcp_server
        tools = list(allowed_tools)
        if self.sdk_tool_name not in tools:
            tools.append(self.sdk_tool_name)
        return servers, tools

    def close(self) -> None:
        try:
            self.broker.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("clarification: broker close failed: %s", exc)
        if self._close is not None:
            try:
                self._close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("clarification: unregister failed: %s", exc)


def setup_clarification(
    prefs: Optional[ClarificationPrefs],
    *,
    emit: Optional[EmitFn],
    seq_counter: Optional[SeqCounter],
    task_id: str,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    locale: Optional[str] = None,
    cancel_run: Optional[CancelRunFn] = None,
    broker: Optional[PermissionBroker] = None,
    register_broker: Optional[Callable[[str, PermissionBroker], None]] = None,
    unregister_broker: Optional[Callable[[str], None]] = None,
    workflow_agent: bool = False,
) -> Optional[ClarificationRuntime]:
    """澄清能力的唯一装配入口，四个对话 agent 共用。

    返回 ``None`` 表示本轮不提问（用户关闭了开关、``max_rounds`` 为 0，或没有
    可用于回传答案的 ``run_id``）——调用方此时不得暴露工具，也不得追加提示词，
    否则模型会调用一个永远无人应答的工具。

    Args:
        prefs: 用户偏好；``None`` 视为禁用（非交互入口的安全默认值）。
        emit/seq_counter/task_id: trace 事件上下文，与 agent 主事件序列共用同一
            个 ``seq_counter``，否则前端按 ``(run_id, seq)`` 去重会丢弃提问卡片。
        run_id: resolve 端点按它定位 broker；缺失则无法回传答案，直接禁用。
        cancel_run: ``on_timeout="cancel"`` 时调用；缺省降级为 ``continue`` 语义。
        broker: 已有 broker（DeviceAgent 与工具审批共用一个）；缺省时新建。
        register_broker/unregister_broker: 通常是
            ``chat_run_service.register_broker`` / ``unregister_broker``。
        workflow_agent: 见 :func:`clarification_guidance`。
    """
    prefs = prefs or ClarificationPrefs.disabled()
    if not prefs.active:
        return None
    if not run_id:
        logger.info(
            "clarification: disabled for task_id=%s — no run_id to resolve against",
            task_id,
        )
        return None

    owned_broker = broker is None
    broker = broker or PermissionBroker()

    if owned_broker and register_broker is not None:
        try:
            register_broker(run_id, broker)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "clarification: broker register failed run_id=%s: %s", run_id, exc
            )
            return None

    try:
        server, sdk_tool_name = build_clarification_mcp_server(
            broker=broker,
            timeout_seconds=prefs.timeout_seconds,
            on_timeout=prefs.on_timeout,
            max_rounds=int(prefs.max_rounds),
            cancel_run=cancel_run,
            emit=emit,
            seq_counter=seq_counter,
            task_id=task_id,
            run_id=run_id,
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001
        # An SDK-side failure here must never take the whole run down; the agent
        # simply proceeds without the ability to ask.
        logger.warning(
            "clarification: failed to build ask server run_id=%s: %s", run_id, exc
        )
        if owned_broker and unregister_broker is not None:
            try:
                unregister_broker(run_id)
            except Exception:  # noqa: BLE001
                pass
        return None

    close_cb: Optional[Callable[[], None]] = None
    if owned_broker and unregister_broker is not None:
        close_cb = lambda: unregister_broker(run_id)  # noqa: E731

    logger.info(
        "clarification: enabled run_id=%s max_rounds=%s on_timeout=%s timeout=%ss",
        run_id,
        prefs.max_rounds,
        prefs.on_timeout,
        prefs.timeout_seconds,
    )

    return ClarificationRuntime(
        broker=broker,
        mcp_server=server,
        sdk_tool_name=sdk_tool_name,
        prompt_addendum=clarification_guidance(
            locale, max_rounds=int(prefs.max_rounds), workflow_agent=workflow_agent
        ),
        _close=close_cb,
    )


@dataclass
class ClarificationBinding:
    """服务层 → agent 的澄清上下文载体。

    ``LogAnalysisAgent`` / ``ProjectExpertAgent`` / ``PackageSearchAgent`` 的
    ``run()`` 只接收这一个可选参数：服务层知道 run_id / owner / 用户偏好 /
    如何取消本轮，agent 知道 emit 与 seq_counter，两边在 :meth:`setup` 汇合。
    传 ``None``（Celery 等非交互入口的默认值）即完全不提问。
    """

    prefs: ClarificationPrefs
    run_id: str
    session_id: Optional[str] = None
    cancel_run: Optional[CancelRunFn] = None
    register_broker: Optional[Callable[[str, PermissionBroker], None]] = None
    unregister_broker: Optional[Callable[[str], None]] = None

    def setup(
        self,
        *,
        emit: Optional[EmitFn],
        seq_counter: Optional[SeqCounter],
        task_id: str,
        locale: Optional[str] = None,
        workflow_agent: bool = True,
    ) -> Optional[ClarificationRuntime]:
        return setup_clarification(
            self.prefs,
            emit=emit,
            seq_counter=seq_counter,
            task_id=task_id,
            run_id=self.run_id,
            session_id=self.session_id,
            locale=locale,
            cancel_run=self.cancel_run,
            register_broker=self.register_broker,
            unregister_broker=self.unregister_broker,
            workflow_agent=workflow_agent,
        )

    @classmethod
    def for_chat_run(
        cls,
        *,
        user: Any,
        run_id: str,
        session_id: Optional[str],
        cancel_run: Optional[CancelRunFn] = None,
    ) -> Optional["ClarificationBinding"]:
        """服务层标准构造：读用户偏好 + 挂到 ``chat_run_service`` 的 broker 表。

        Returns ``None`` when the preference is off, so callers can pass the
        result straight through without branching.
        """
        prefs = ClarificationPrefs.from_user(user)
        if not prefs.active or not run_id:
            return None
        from app.services.chat_run_service import chat_run_service

        return cls(
            prefs=prefs,
            run_id=run_id,
            session_id=session_id,
            cancel_run=cancel_run,
            register_broker=chat_run_service.register_broker,
            unregister_broker=chat_run_service.unregister_broker,
        )


__all__ = [
    "ASK_SERVER_NAME",
    "ASK_TOOL_NAME",
    "ASK_SDK_NAME",
    "BUILTIN_ASK_TOOL_NAME",
    "ClarificationBinding",
    "MandatoryClarificationError",
    "ClarificationPrefs",
    "ClarificationRuntime",
    "clarification_guidance",
    "make_ask_user_question_tool",
    "request_mandatory_clarification",
    "build_clarification_mcp_server",
    "setup_clarification",
]
