"""Preloaded task data must not coexist with mandatory duplicate read calls."""
import json
from pathlib import Path

import pytest
import yaml

from app.agents.evidence_policy import snapshot_prompt, use_snapshot_in_templates
from app.agents.log_analysis.prompts import render_user_prompt


@pytest.mark.parametrize("agent", ["claude_agent_log_analysis", "claude_agent_project_expert"])
@pytest.mark.parametrize("locale", ["zh", "en"])
def test_builtin_snapshot_templates_preserve_investigation_and_question(agent, locale):
    config = yaml.safe_load((Path(__file__).parents[2] / "app/prompts/prompts_config.yaml").read_text())
    variant = config[agent]["generic"]
    system, user = use_snapshot_in_templates(variant["system_prompt"][locale], variant["user_prompt_template"][locale])
    assert "首先读取当前工作目录下的" not in system
    assert "First read `task.json`" not in system
    assert "Read 工具的第一次调用应为" not in system
    assert "The first Read tool call should" not in system
    assert "后端" in user if locale == "zh" else "server task snapshot" in user
    assert "缺少所需字段" in system if locale == "zh" else "missing required fields" in system
    # Only startup mechanics change, including for a question containing those
    # same instruction strings. User data is interpolated afterward.
    question = '首先读取当前工作目录下的 `task.json`。Read 工具的第一次调用应为 `{"file_path":"task.json"}`。'
    rendered = render_user_prompt(user, task_id="test", workspace_dir="/workspace", question=question, hints="")
    assert question in rendered
    for term in ("question_type", "root_cause_hypotheses"):
        assert term in system
    for term in ("clone_project_repo", "related_repos", "repo_info", "conversation_history_file"):
        assert system.count(term) == variant["system_prompt"][locale].count(term)
    if agent == "claude_agent_log_analysis":
        assert "task.json.attachments" in system
        assert "第 2–4 步" in user if locale == "zh" else "Steps 2–4" in user


def test_custom_template_text_and_fallback_are_unchanged():
    system, user = "Custom task requirements: keep all attachments.", "Question: {question}"
    assert use_snapshot_in_templates(system, user) == (system, user)


@pytest.mark.parametrize("locale, required_terms", [
    ("zh", ("先批量取证", "mcp__evidence__search_evidence", "mcp__evidence__read_evidence", "通用失败返回值不能证明执行了哪个分支", "不限制总调用数")),
    ("en", ("Batch-first evidence dispatch", "mcp__evidence__search_evidence", "mcp__evidence__read_evidence", "A generic failure return does not identify which branch ran", "does not cap total calls")),
])
def test_batch_plan_guidance_is_opt_in_and_preserves_snapshot(locale, required_terms):
    snapshot = {"task": {"question": "unchanged"}, "project_catalog": {"projects": [], "count": 0}}
    disabled = snapshot_prompt(snapshot, locale)
    enabled = snapshot_prompt(snapshot, locale, batch_plan=True)

    assert "Batch-first evidence dispatch" not in disabled
    assert "先批量取证" not in disabled
    assert all(term in enabled for term in required_terms)
    assert '"question": "unchanged"' in enabled
    assert "JSON" in enabled


@pytest.mark.parametrize("locale, required_terms", [
    ("zh", ("聚焦式证据批次", "最多 4 项", "limit 8–12", "未观察到", "不限制总调用数", "150 轮上限")),
    ("en", ("Focused evidence batches", "at most 4 requests", "limit 8-12", "not observed in the inspected scope", "no total-call", "150-turn limit")),
])
def test_batch_plan_v3_is_opt_in_and_preserves_uncertainty(locale, required_terms):
    snapshot = {"task": {"question": "unchanged"}, "project_catalog": {"projects": [], "count": 0}}
    disabled = snapshot_prompt(snapshot, locale)
    enabled = snapshot_prompt(snapshot, locale, batch_plan_v3=True)
    legacy = snapshot_prompt(snapshot, locale, batch_plan=True)

    assert all(term in enabled for term in required_terms)
    assert "Focused evidence batches" not in disabled
    assert "聚焦式证据批次" not in disabled
    assert "Batch-first evidence dispatch" in legacy or "先批量取证" in legacy
    assert "Batch-first evidence dispatch" not in enabled
    assert "先批量取证" not in enabled
    assert '"question": "unchanged"' in enabled
    assert "JSON" in enabled


@pytest.mark.parametrize("locale, required_terms", [
    ("zh", ("前置合并证据检索", "最多 8 项", "不固定为少量样本", "产生路径和新鲜度", "枚举", "当前状态保留未知", "不能证明事件从未发生", "总调用数不设上限", "150 轮上限")),
    ("en", ("Front-loaded evidence batches", "up to 8 requests", "not a fixed small sample", "producer", "enums", "current state unknown", "an event never happened", "no total-call cap", "150-turn cap")),
])
def test_batch_plan_v4_is_opt_in_and_front_loads_known_checks(locale, required_terms):
    snapshot = {"task": {"question": "unchanged"}, "project_catalog": {"projects": [], "count": 0}}
    disabled = snapshot_prompt(snapshot, locale)
    enabled = snapshot_prompt(snapshot, locale, batch_plan_v4=True)
    legacy = snapshot_prompt(snapshot, locale, batch_plan=True)

    assert all(term in enabled for term in required_terms)
    assert "Front-loaded evidence batches" not in disabled
    assert "前置合并证据检索" not in disabled
    assert "Batch-first evidence dispatch" in legacy or "先批量取证" in legacy
    assert "聚焦式证据批次" not in enabled and "先批量取证" not in enabled
    assert '"question": "unchanged"' in enabled
    assert "JSON" in enabled


@pytest.mark.parametrize("locale, heading, reuse", [
    ("zh", "## 前置合并证据检索", "按已证实的事实复用证据"),
    ("en", "## Front-loaded evidence batches", "Reuse established facts"),
])
def test_v4_execution_guidance_follows_snapshot_data_without_changing_it(locale, heading, reuse):
    snapshot = {"task": {"question": "unchanged"}, "project_catalog": {"projects": ["catalog-tail-marker"]}}
    enabled = snapshot_prompt(snapshot, locale, batch_plan_v4=True)
    assert enabled.index(heading) > enabled.index("catalog-tail-marker")
    assert enabled.count(heading) == 1 and reuse in enabled
    assert json.loads(enabled.split("```json\n", 1)[1].split("\n```", 1)[0]) == snapshot


@pytest.mark.parametrize("startup, fallback, replacement", [
    (
        "读取当前工作目录的 task.json。",
        '需要读任务时使用 Read({"file_path":"task.json"})。',
        "仅在缺少所需字段时读取 `task.json`",
    ),
    (
        "Read task.json in the current workspace.",
        'When the task file is needed, use Read({"file_path":"task.json"}).',
        "read `task.json` only for missing required fields",
    ),
])
def test_compact_workflow_reuses_snapshot_and_keeps_task_fallback(startup, fallback, replacement):
    system = f"{fallback}\n{startup}\nVerify all attachments and repo_info; keep the output schema."
    resolved, user = use_snapshot_in_templates(system, "Question: {question}")
    assert startup not in resolved
    assert replacement in resolved
    assert fallback in resolved
    assert resolved.endswith("Verify all attachments and repo_info; keep the output schema.")
    # A user may quote the same instruction as data; interpolation still comes
    # after policy resolution and must not rewrite their question.
    rendered = render_user_prompt(user, task_id="test", workspace_dir="/workspace", question=startup, hints="")
    assert startup in rendered
