"""Snapshot replay must surface pending AskUserQuestion clarifications."""

from __future__ import annotations

import time

from app.services.chat_run_service import (
    ChatRunJob,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_RUNNING,
    chat_run_service,
)


def _job() -> ChatRunJob:
    return ChatRunJob(
        run_id="run-clar",
        session_id="sess-clar",
        user_id=None,
        owner_scope="anon:clar",
        agent_kind="device",
        status=RUN_STATUS_RUNNING,
        started_at=time.monotonic(),
        user_message="restart it",
        request_payload={},
    )


def test_snapshot_includes_pending_clarifications():
    job = _job()
    job.pending_clarifications["req-1"] = {
        "type": "clarification_request",
        "request_id": "req-1",
        "paused": True,
        "questions": [{"question": "which?", "options": [{"label": "a"}, {"label": "b"}]}],
    }
    payload = chat_run_service._snapshot_payload(job)  # noqa: SLF001
    assert "pending_clarifications" in payload
    assert len(payload["pending_clarifications"]) == 1
    assert payload["pending_clarifications"][0]["request_id"] == "req-1"
    assert payload["pending_clarifications"][0]["paused"] is True

    job.mark_status(RUN_STATUS_CANCELLED)
    assert chat_run_service._snapshot_payload(job)["pending_clarifications"] == []


def test_snapshot_empty_pending_clarifications_by_default():
    payload = chat_run_service._snapshot_payload(_job())  # noqa: SLF001
    assert payload["pending_clarifications"] == []
