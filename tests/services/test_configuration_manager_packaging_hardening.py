"""Focused regressions for Configuration Manager publication authority boundaries."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from app.agents.package_search.agent import (
    PACKAGING_TASK_VALUE_KEYS,
    _has_confirmed_package_plan,
    _is_packaging_task,
)
from app.agents.package_search.workspace import prepare
from app.services.package_search_chat_service import (
    AgentJob,
    PackageSearchChatService,
)


def _seed_authority(ctx: Any) -> None:
    task = json.loads(Path(ctx.task_json_path).read_text(encoding="utf-8"))
    task.update({key: {"authority": key} for key in PACKAGING_TASK_VALUE_KEYS})
    task.update({"packaging_requested": True, "package_mode": "packaging"})
    Path(ctx.task_json_path).write_text(json.dumps(task), encoding="utf-8")
    for run in ("hardening-run", "stale-run"):
        plan = Path(ctx.temp_dir) / "package_plan" / run / "confirmed-plan.json"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(
            json.dumps({"confirmation_token": f"signed-{run}"}),
            encoding="utf-8",
        )
    ctx.metadata["confirmed_plan"] = {"confirmation_token": "in-memory"}
    ctx.metadata["inputs_manifest"] = {"inputs": []}


def test_pure_search_revokes_all_packaging_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_repo_clone_base_dir", str(tmp_path / "work"))
    ctx = prepare(
        project_repo=None,
        question="build",
        session_id="hardening-session",
    )
    service = PackageSearchChatService()
    _seed_authority(ctx)
    service._bind_question_and_hints(
        ctx,
        question="pure search",
        hints="",
        packaging_requested=False,
    )
    pure_search_task = json.loads(
        Path(ctx.task_json_path).read_text(encoding="utf-8")
    )
    assert not set(PACKAGING_TASK_VALUE_KEYS) & pure_search_task.keys()
    assert _is_packaging_task(pure_search_task) is False
    assert _has_confirmed_package_plan(pure_search_task) is False
    assert not (Path(ctx.temp_dir) / "package_plan").exists()


@pytest.mark.asyncio
async def test_packaging_run_finally_revokes_current_and_stale_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_repo_clone_base_dir", str(tmp_path / "work"))
    ctx = prepare(
        project_repo=None,
        question="build",
        session_id="hardening-session",
    )
    _seed_authority(ctx)
    service = PackageSearchChatService()
    job = AgentJob(
        session_id="hardening-session",
        task_id=ctx.task_id,
        context_meta={},
        question="build",
        user_id="user-1",
        remember=False,
        started_at=time.monotonic(),
        run_id="hardening-run",
        owner_scope="user:user-1",
        input_manifest={"inputs": [{"upload_id": "component-1"}]},
    )

    async def no_op(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    monkeypatch.setattr("app.agents.package_search.agent.PackageSearchAgent.run_sync",
                        lambda *args: {"status": "cancelled", "answer": "cancelled"})
    monkeypatch.setattr(service, "_persist_job_result", no_op)
    monkeypatch.setattr(service, "_finalize_chat_run", no_op)

    await service._run_job_async(job, ctx)

    completed_task = json.loads(
        Path(ctx.task_json_path).read_text(encoding="utf-8")
    )
    assert job.done is True
    assert job.error is None
    assert not set(PACKAGING_TASK_VALUE_KEYS) & completed_task.keys()
    assert _is_packaging_task(completed_task) is False
    assert _has_confirmed_package_plan(completed_task) is False
    assert not (Path(ctx.temp_dir) / "package_plan").exists()
    assert "confirmed_plan" not in ctx.metadata
    assert "inputs_manifest" not in ctx.metadata
