"""Preloaded task data must not coexist with mandatory duplicate read calls."""
from pathlib import Path

import pytest
import yaml

from app.agents.evidence_policy import use_snapshot_in_templates
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
