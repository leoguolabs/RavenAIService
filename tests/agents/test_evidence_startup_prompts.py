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
