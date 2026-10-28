"""兼容完整裸 JSON，以及旧版本误包装为 Skill 文本的历史分析结果。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


class DuplicateResultField(ValueError):
    """An ambiguous object cannot be repaired by selecting one of its values."""


def unique_json_object(pairs: List[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateResultField(key)
        result[key] = value
    return result


def parse_complete_analysis_result(text: str) -> Optional[Dict[str, Any]]:
    """Only accept a complete, unambiguous analysis object, not arbitrary JSON."""
    try:
        parsed = json.loads(text, object_pairs_hook=unique_json_object)
    except (ValueError, TypeError):
        return None
    if (
        isinstance(parsed, dict)
        and isinstance(parsed.get("status"), str)
        and any(isinstance(parsed.get(key), str) and parsed[key].strip() for key in ("answer", "summary"))
    ):
        return parsed
    return None


def normalize_saved_analysis_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Restore display fields on read without rewriting history or dispatch signals.

    Old versions treated unfenced JSON as a plain Skill answer. Restrict repair
    to that exact wrapper and retain raw/trace/attribution and bug-fix state.
    """
    if result.get("parse_warning") != "plain_text_skill_answer_wrapped":
        return result
    raw = result.get("raw")
    answer = result.get("answer")
    if not isinstance(raw, str) or not isinstance(answer, str) or answer.strip() != raw.strip():
        return result
    parsed = parse_complete_analysis_result(raw)
    if parsed is None:
        return result

    normalized = dict(result)
    for key in ("question_type", "answer", "summary", "severity"):
        value = parsed.get(key)
        if isinstance(value, str):
            normalized[key] = value
        elif key in {"answer", "summary"}:
            normalized[key] = ""
    if not normalized["answer"].strip():
        normalized["answer"] = normalized["summary"]
    for key in ("root_cause_hypotheses", "recommended_actions", "related_keywords"):
        value = parsed.get(key)
        normalized[key] = value if isinstance(value, list) else []
    error_kind = parsed.get("error_kind")
    normalized["error_kind"] = error_kind if isinstance(error_kind, str) else None
    normalized["status"] = "error" if normalized["error_kind"] else parsed["status"]
    normalized["parse_warning"] = "legacy_structured_result_unwrapped"
    return normalized
