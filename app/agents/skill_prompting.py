"""Prompt helpers for Claude Agent SDK Skill activation."""

from __future__ import annotations

import json
import re
from pathlib import PurePath
from typing import Any, Dict, Iterable, List, Optional


_FILENAME_KEYS = {
    "filename",
    "filenames",
    "original_filename",
    "original_name",
    "relative_path",
    "path",
}


def _collect_filename_evidence(value: Any, output: List[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in _FILENAME_KEYS:
                _collect_filename_evidence(nested, output)
            elif isinstance(nested, (dict, list, tuple)):
                _collect_filename_evidence(nested, output)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _collect_filename_evidence(nested, output)
        return
    if isinstance(value, str) and value.strip():
        # Preserve basename and suffix without copying workspace directories
        # into prompts or relevance logs.
        name = PurePath(value.strip()).name
        if name:
            output.append(name)


def build_skill_relevance_query(
    *,
    question: Any,
    hints: Any = "",
    attachments: Any = None,
    extra_text: Iterable[Any] = (),
) -> str:
    """Compose request-owned text and attachment names for Skill selection."""
    parts: List[str] = []
    for value in (question, hints, *extra_text):
        if value is not None and str(value).strip():
            parts.append(str(value).strip())
    filenames: List[str] = []
    _collect_filename_evidence(attachments, filenames)
    parts.extend(filenames)
    return "\n".join(parts)


def build_skill_availability_prompt(
    skills: Iterable[Any],
    *,
    final_output_contract: str,
) -> str:
    """Build a user-prompt addendum advertising the materialized skills.

    Materializing a Skill only makes it discoverable to the SDK. Relevance
    judgement is left to the model at inference time: it reads the name +
    description menu below and loads each Skill on demand via the ``Skill``
    tool — at any point in the run, not just at the start.

    The addendum frames Skills as an *auxiliary reference*: methodology and
    domain knowledge are followed, but any concrete claim about the code or
    system (paths, symbols, field names, enum values, line numbers, flow) must
    be verified against the real ``repo/`` source and ``logs/`` before it is
    used as an answer. When a Skill's details conflict with the actual code,
    the code wins — this prevents stale Skill content from skewing answers.

    *skills* accepts ``{"name", "description"}`` dicts or bare name strings.
    """
    entries: List[tuple[str, str]] = []
    seen = set()
    for raw in skills:
        if isinstance(raw, dict):
            name = str(raw.get("name") or "").strip()
            description = str(raw.get("description") or "").strip()
        else:
            name = str(raw or "").strip()
            description = ""
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append((name, description))
    if not entries:
        return ""

    bullets = "\n".join(
        f"- `{name}`：{description}" if description else f"- `{name}`"
        for name, description in entries
    )
    example = json.dumps({"skill": entries[0][0]}, ensure_ascii=False)
    paths = "\n".join(
        f"- `{name}` 文件：`.claude/skills/{name}/`" for name, _ in entries
    )
    return (
        "\n\n## 可用的 Skill（按需加载）\n"
        "下列 Skill 已物化到当前工作区，作为**辅助参考**。请根据名称与"
        "描述自行判断哪些与当前问题或子任务相关：\n"
        f"{bullets}\n\n"
        "使用规则：\n"
        "- 决定使用某个 Skill 之前，必须先调用 `Skill` 工具读取它的完整"
        f"指令（输入形如 `{example}`），再按指令执行；\n"
        "- 推理中途发现需要某个 Skill 时，可以随时补充加载，不限于开场；\n"
        "- 与当前请求无关的 Skill 不要加载，避免浪费上下文。\n\n"
        "### Skill 与真实代码的关系（非常重要）\n"
        "Skill 是**辅助参考**，不是代码事实的权威来源。它的价值在于提供"
        "领域知识、排查思路、术语解释与经验规律，帮你判断「该看哪里、"
        "怎么分析、要注意什么」——而不是替你断定代码里到底是什么。\n"
        "- **一切关于代码与系统的具体事实，一律以工作区里的真实源代码"
        "（`repo/`）与真实日志（`logs/`，如有）为准。** Skill 中凡涉及"
        "文件路径、函数/符号/模块名、字段名、枚举或常量取值、配置项、"
        "行号、接口签名、流程或调用关系等具体描述，都必须先到真实代码"
        "/日志中 `Grep`/`Read` 核对，再据此作答，不得直接照搬 Skill 的"
        "措辞当作结论；\n"
        "- 当 Skill 的描述与实际代码或日志冲突、或明显已与当前版本脱节时，"
        "**以实际代码与日志为准**，据此修正结论，丢弃 Skill 里过时或对不上"
        "的细节；必要时可在 `answer` 中用一句话点明「Skill 的描述与当前"
        "代码不一致，已以代码为准」；\n"
        "- 上面「以代码为准」只针对*事实判断*。Skill 给出的分析方法、检查"
        "清单与下面关于输出格式/措辞的要求仍应遵循（除非与真实代码直接"
        "矛盾）。\n\n"
        "如果 Skill 指令引用了相对路径脚本、模板或资源文件，必须相对下面"
        "的物化目录解析路径；例如 `<skill-dir>/scripts/...`，不要假设这些"
        "文件位于当前工作目录根部：\n"
        f"{paths}\n\n"
        f"最终输出仍必须遵守{final_output_contract}。如果某个 Skill 要求"
        "输出一个特定字符串、或要求“不要解释/不要添加额外文字”，请把该"
        "字符串原样放入最终 JSON 的 `answer` 和 `summary` 字段；不要输出"
        "裸文本，也不要省略 JSON 围栏。"
    )


def build_plain_text_skill_answer_fields(
    final_text: str,
    skill_names: Iterable[str],
) -> Optional[Dict[str, Any]]:
    """Wrap a non-JSON skill answer into the agent's structured schema fields."""
    answer = (final_text or "").strip()
    if not answer:
        return None
    # Callers have already tried parsing structured results. A rejected result
    # must not become status=ok merely because an optional Skill was loaded.
    if answer.startswith("{") or re.search(r"```json\b", answer):
        return None

    names: List[str] = []
    seen = set()
    for raw in skill_names:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return None

    return {
        "status": "ok",
        "error_kind": None,
        "question_type": "qa",
        "answer": answer,
        "summary": answer,
        "severity": "info",
        "root_cause_hypotheses": [],
        "recommended_actions": [],
        "related_keywords": names,
        "parse_warning": "plain_text_skill_answer_wrapped",
    }
