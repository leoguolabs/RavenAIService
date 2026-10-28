"""Regression coverage for JSON answers persisted by the old Skill fallback."""

import copy
import json
from datetime import datetime

import pytest

from app.agents.log_analysis.result_normalization import normalize_saved_analysis_result


def wrapped_result():
    payload = {
        "status": "ok", "question_type": "qa", "severity": "info",
        "answer": "## 时间来源\n\n```mermaid\nflowchart LR\n A[主控板] --> B[基带板]\n```\n\n"
                  "| 日志 | 时间来源 |\n|---|---|\n| board.log | TOD |",
        "summary": "主控板转发 TOD。", "related_keywords": ["TOD"],
        "root_cause_hypotheses": [], "recommended_actions": ["核对日志"],
        "requires_code_fix": True,
        "proposed_fixes": [{"title": "Historical suggestion"}],
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    return payload, {
        "schema_version": 3, "status": "ok", "question_type": "qa",
        "answer": raw, "summary": raw, "raw": raw,
        "parse_warning": "plain_text_skill_answer_wrapped",
        "related_keywords": ["optional-skill"],
        "requires_code_fix": False, "proposed_fixes": [],
        "query": "时间从哪里来？", "model": "test-model", "duration_seconds": 246,
        "triggered_by": {"source": "ai_chat"}, "trace_events": [{"type": "run_complete"}],
    }


def test_legacy_wrapper_restores_markdown_without_mutating_history_or_dispatch():
    payload, original = wrapped_result()
    before = copy.deepcopy(original)
    normalized = normalize_saved_analysis_result(original)
    for key in ("answer", "summary", "question_type", "severity", "related_keywords", "recommended_actions"):
        assert normalized[key] == payload[key]
    for key in ("raw", "query", "model", "duration_seconds", "triggered_by", "trace_events",
                "requires_code_fix", "proposed_fixes"):
        assert normalized[key] == original[key]
    assert normalized["parse_warning"] == "legacy_structured_result_unwrapped"
    assert original == before
    assert normalize_saved_analysis_result(normalized) is normalized


@pytest.mark.parametrize("raw", [
    "普通 Skill 回答", '{"rows": [1, 2]}',
    '{"status":"ok","answer":"cut off"',
    '{"status":"ok","answer":"first","answer":"second"}',
    '{"status":"ok","answer":"valid","extra":{"key":1,"key":2}}',
])
def test_ambiguous_incomplete_and_non_analysis_content_is_left_unchanged(raw):
    _, result = wrapped_result()
    result.update(raw=raw, answer=raw, summary=raw)
    assert normalize_saved_analysis_result(result) is result


@pytest.mark.parametrize("change", [{"parse_warning": None}, {"answer": "Already edited answer"}])
def test_only_exact_legacy_wrapper_is_repaired(change):
    _, result = wrapped_result()
    result.update(change)
    assert normalize_saved_analysis_result(result) is result


def test_summary_only_and_error_results_preserve_meaning():
    _, result = wrapped_result()
    raw = json.dumps({"status": "ok", "error_kind": "missing_evidence", "summary": "证据不足"})
    result.update(raw=raw, answer=raw, summary=raw)
    normalized = normalize_saved_analysis_result(result)
    assert normalized["answer"] == normalized["summary"] == "证据不足"
    assert normalized["status"] == "error"
    assert normalized["error_kind"] == "missing_evidence"


@pytest.mark.asyncio
async def test_log_detail_normalizes_latest_and_history_without_database_write():
    from app.models.log import LogMetadata, LogRecord, LogStatus
    from app.services.log_service import log_service

    payload, result = wrapped_result()
    metadata = LogMetadata(extra_fields={
        "ai_analysis_result": result,
        "ai_analysis_conversation": [dict(result, created_at="2026-10-28T13:19:22")],
    })
    record = LogRecord(
        id="format-regression", filename="board.log", original_filename="board.log",
        file_size=10, file_path="/tmp/board.log", status=LogStatus.COMPLETED,
        progress=100, download_count=0, created_at=datetime(2026, 10, 28),
        updated_at=datetime(2026, 10, 28), metadata_json=metadata.model_dump_json(),
    )
    before = record.metadata_json
    info = await log_service._db_to_pydantic(record, metadata)
    assert info.ai_analysis_result["answer"] == payload["answer"]
    assert info.ai_analysis_conversation[0]["answer"] == payload["answer"]
    assert info.ai_analysis_conversation[0]["created_at"] == "2026-10-28T13:19:22"
    assert record.metadata_json == before
