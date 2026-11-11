"""Unit tests for the Bug Fix Coding Agent (task 5.5).

Covers output-JSON extraction, merge_requests normalization, token redaction,
provider/API-base inference, and the system-prompt contract guarantees.
"""

from __future__ import annotations

import pytest

from app.agents.bug_fix import agent as bf_agent
from app.agents.bug_fix import git_tools
from app.agents.bug_fix import prompts


@pytest.fixture(autouse=True)
def dedicated_model(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "runtime_settings_path", str(tmp_path / "models.json"))
    monkeypatch.setattr(settings, "bug_fix_agent_provider", "anthropic")
    monkeypatch.setattr(settings, "bug_fix_agent_api_key", "test-dedicated-key")
    monkeypatch.setattr(settings, "bug_fix_agent_model", "advanced-test-model")


# ───────────────────── output JSON extraction ──────────────────────

def test_extract_final_json_from_fenced_block():
    text = (
        "Here is the result.\n"
        "```json\n"
        '{"status": "succeeded", "merge_requests": [{"branch_name": "b"}]}\n'
        "```\n"
    )
    parsed = bf_agent._extract_final_json(text)
    assert parsed["status"] == "succeeded"
    assert parsed["merge_requests"][0]["branch_name"] == "b"


def test_extract_final_json_takes_last_block():
    text = (
        "```json\n{\"status\": \"failed\", \"merge_requests\": []}\n```\n"
        "more text\n"
        "```json\n{\"status\": \"succeeded\", \"merge_requests\": [{\"branch_name\": \"x\"}]}\n```"
    )
    parsed = bf_agent._extract_final_json(text)
    assert parsed["status"] == "succeeded"


def test_extract_final_json_bare_object_fallback():
    text = 'prefix {"status": "succeeded", "merge_requests": []} suffix'
    parsed = bf_agent._extract_final_json(text)
    assert parsed["status"] == "succeeded"


def test_extract_final_json_returns_none_on_garbage():
    assert bf_agent._extract_final_json("no json here") is None


# ──────────────────── merge_requests normalization ─────────────────

def test_normalize_drops_items_without_branch():
    out = bf_agent._normalize_merge_requests(
        [{"title": "no branch"}, {"branch_name": "good"}]
    )
    assert len(out) == 1
    assert out[0]["branch_name"] == "good"


def test_normalize_redacts_tokens_in_url_and_description():
    out = bf_agent._normalize_merge_requests(
        [
            {
                "branch_name": "b",
                "mr_url": "https://oauth2:secret@host/foo/-/merge_requests/1",
                "description": "see https://oauth2:secret@host/foo.git",
            }
        ]
    )
    assert "secret" not in out[0]["mr_url"]
    assert "https://***@host" in out[0]["mr_url"]
    assert "secret" not in out[0]["description"]


def test_normalize_handles_non_list():
    assert bf_agent._normalize_merge_requests(None) == []


# ──────────────────── fix_outcomes normalization ───────────────────

def test_normalize_fix_outcomes_basic_and_coercion():
    out = bf_agent._normalize_fix_outcomes(
        [
            {"fix_index": 1, "title": "a", "outcome": "already_implemented",
             "reason": "done in 9b750d5"},
            # 无 outcome 但有 mr_url → created_mr；fix_index 字符串可强转
            {"fix_index": "2", "title": "b", "mr_url": "https://h/mr/1"},
            # 非法 outcome 且无 mr_url 不得伪装为无需修改
            {"title": "c", "outcome": "weird"},
            "not-a-dict",
        ]
    )
    assert len(out) == 3
    assert out[0]["outcome"] == "already_implemented" and out[0]["fix_index"] == 1
    assert out[1]["outcome"] == "created_mr" and out[1]["fix_index"] == 2
    assert out[2]["outcome"] == "failed"


def test_normalize_fix_outcomes_redacts_tokens():
    out = bf_agent._normalize_fix_outcomes(
        [
            {
                "outcome": "created_mr",
                "mr_url": "https://oauth2:secret@host/foo/-/merge_requests/1",
                "reason": "pushed to https://oauth2:secret@host/foo.git",
            }
        ]
    )
    assert "secret" not in out[0]["mr_url"]
    assert "secret" not in out[0]["reason"]


def test_normalize_fix_outcomes_non_list():
    assert bf_agent._normalize_fix_outcomes(None) == []


# ───────────────────────── provider inference ──────────────────────

def test_infer_provider_gitlab(monkeypatch):
    monkeypatch.setattr(git_tools.settings, "bug_fix_git_provider", None)
    assert git_tools.infer_provider("https://gitlab.example.com/a/b.git") == "gitlab"


def test_infer_provider_github(monkeypatch):
    monkeypatch.setattr(git_tools.settings, "bug_fix_git_provider", None)
    assert git_tools.infer_provider("https://github.com/a/b.git") == "github"


def test_infer_provider_config_override(monkeypatch):
    monkeypatch.setattr(git_tools.settings, "bug_fix_git_provider", "gitlab")
    assert git_tools.infer_provider("https://unknown.host/a/b.git") == "gitlab"


def test_infer_api_base_gitlab(monkeypatch):
    monkeypatch.setattr(git_tools.settings, "bug_fix_git_api_base", None)
    base = git_tools.infer_api_base("https://gitlab.example.com/a/b.git", "gitlab")
    assert base == "https://gitlab.example.com/api/v4"


def test_infer_api_base_github_dotcom(monkeypatch):
    monkeypatch.setattr(git_tools.settings, "bug_fix_git_api_base", None)
    base = git_tools.infer_api_base("https://github.com/a/b.git", "github")
    assert base == "https://api.github.com"


def test_gitlab_project_path_strips_git_suffix():
    assert git_tools._gitlab_project_path("https://h/group/proj.git") == "group/proj"


# ─────────────────────── system prompt contract ────────────────────

def test_system_prompt_enforces_minimal_change_and_no_default_branch():
    system, _ = prompts.get_prompts()
    assert "最小改动" in system
    assert "默认分支" in system
    # multi-MR split guidance
    assert "merge_requests" in system
    # never auto-merge
    assert "合并" in system


def test_allowed_tools_include_write_capable_tools():
    for tool in ("Edit", "Write", "Bash", "Read", "Grep", "Glob"):
        assert tool in bf_agent.ALLOWED_TOOLS


def test_run_sync_uses_fresh_event_loop_for_consecutive_tasks():
    """同一 Celery 子进程连续执行任务时，不得复用上次已关闭的 event loop。"""
    import asyncio

    seen_loops = []

    class _Agent(bf_agent.BugFixCodingAgent):
        async def run(self, ctx):
            seen_loops.append(asyncio.get_running_loop())
            return {"status": "succeeded", "task_id": ctx.task_id}

    agent = _Agent()
    try:
        first = agent.run_sync(_make_ctx("sync-1"))
        second = agent.run_sync(_make_ctx("sync-2"))
    finally:
        # 旧实现失败时会把 closed loop 留在线程 policy 中，避免污染后续测试。
        asyncio.set_event_loop(None)

    assert first["task_id"] == "sync-1"
    assert second["task_id"] == "sync-2"
    assert len(seen_loops) == 2
    assert seen_loops[0] is not seen_loops[1]
    assert all(loop.is_closed() for loop in seen_loops)


def test_system_prompt_defines_fix_outcomes_contract():
    """契约必须要求逐项结局，并给出 already_implemented「无需改动」的处理路径。"""
    system, _ = prompts.get_prompts()
    assert "fix_outcomes" in system
    assert "already_implemented" in system
    # 已实现的项不应制造空 MR
    assert "空 MR" in system or "空提交" in system


def test_result_helper_redacts_error_tokens():
    res = bf_agent._result(
        status="failed",
        merge_requests=[],
        error="clone failed for https://oauth2:secret@host/x.git",
    )
    assert "secret" not in res["error"]


# ───────────────────────── prompt logging ──────────────────────────

def test_run_logs_full_prompts(monkeypatch, caplog):
    """run() 必须把系统/用户提示词完整落日志，供事后评估修复准确性。"""
    import asyncio
    import logging

    import claude_agent_sdk
    from app.agents import anthropic_client
    from app.agents.bug_fix.workspace import BugFixWorkspaceContext

    async def _fake_query(*, prompt, options):
        class _Msg:
            content = '```json\n{"status": "failed", "merge_requests": []}\n```'

        yield _Msg()

    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)
    monkeypatch.setattr(anthropic_client, "build_options", lambda **kw: object())

    ctx = BugFixWorkspaceContext(
        task_id="t-1",
        temp_dir="/tmp/ws",
        repo_dir="/tmp/ws/repo",
        task_json_path="/tmp/ws/task.json",
        default_branch="main",
    )
    with caplog.at_level(logging.INFO, logger="app.agents.bug_fix.agent"):
        asyncio.run(bf_agent.BugFixCodingAgent().run(ctx))

    prompt_logs = [r.message for r in caplog.records if "prompt task=t-1" in r.message]
    system_logs = [m for m in prompt_logs if "kind=system" in m]
    user_logs = [m for m in prompt_logs if "kind=user" in m]
    assert len(system_logs) == 1 and len(user_logs) == 1
    # 系统提示词全文（含工作区附录）在日志中
    assert "最小改动原则" in system_logs[0]
    assert "/tmp/ws" in system_logs[0]
    # 用户提示词全文（已渲染变量）在日志中
    assert "t-1" in user_logs[0]
    assert "main" in user_logs[0]


def test_run_logs_workflow_events(monkeypatch, caplog):
    """run() 必须按内容块落 workflow 中间日志，否则长运行无法与卡死区分。"""
    import asyncio
    import logging

    import claude_agent_sdk
    from app.agents import anthropic_client
    from app.agents.bug_fix.workspace import BugFixWorkspaceContext

    class _Thinking:
        thinking = "先读 task.json 定位根因"

    class _ToolUse:
        name = "Bash"
        input = {"command": "git checkout -b bugfix/ai-t-2-1"}

    class _ToolResult:
        tool_use_id = "toolu_01"
        is_error = False
        content = "Switched to a new branch"

    class _Text:
        text = "已创建修复分支"

    class _AssistantMsg:
        content = [_Thinking(), _ToolUse(), _ToolResult(), _Text()]

    class _ResultMsg:
        content = None
        result = '```json\n{"status": "failed", "merge_requests": []}\n```'

    async def _fake_query(*, prompt, options):
        yield _AssistantMsg()
        yield _ResultMsg()

    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)
    monkeypatch.setattr(anthropic_client, "build_options", lambda **kw: object())

    ctx = BugFixWorkspaceContext(
        task_id="t-2",
        temp_dir="/tmp/ws",
        repo_dir="/tmp/ws/repo",
        task_json_path="/tmp/ws/task.json",
        default_branch="main",
    )
    with caplog.at_level(logging.INFO, logger="app.agents.bug_fix.agent"):
        asyncio.run(bf_agent.BugFixCodingAgent().run(ctx))

    workflow = [r.message for r in caplog.records if "workflow task=t-2" in r.message]
    assert any("event=thinking" in m and "task.json" in m for m in workflow)
    assert any("event=tool_call" in m and "tool=Bash" in m for m in workflow)
    assert any("event=tool_result" in m and "status=ok" in m for m in workflow)
    assert any("event=assistant_text" in m for m in workflow)
    assert any("event=result" in m for m in workflow)


def _make_ctx(task_id: str):
    from app.agents.bug_fix.workspace import BugFixWorkspaceContext

    return BugFixWorkspaceContext(
        task_id=task_id,
        temp_dir="/tmp/ws",
        repo_dir="/tmp/ws/repo",
        task_json_path="/tmp/ws/task.json",
        default_branch="main",
    )


def test_run_returns_structured_failed_on_max_turns(monkeypatch):
    """SDK 超回合抛裸异常时，run() 必须返回 error_kind=max_turns_exceeded 的结构化结果。"""
    import asyncio

    import claude_agent_sdk
    from app.agents import anthropic_client

    async def _fake_query(*, prompt, options):
        class _Msg:
            content = "开始修复"

        yield _Msg()
        raise Exception(
            "Claude Code returned an error result: Reached maximum number of turns (60)"
        )

    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)
    monkeypatch.setattr(anthropic_client, "build_options", lambda **kw: object())

    result = asyncio.run(bf_agent.BugFixCodingAgent().run(_make_ctx("t-3")))
    assert result["status"] == "failed"
    assert result["error_kind"] == "max_turns_exceeded"
    assert result["merge_requests"] == []


def test_run_salvages_json_emitted_before_sdk_error(monkeypatch):
    """异常前已输出的最终 JSON（如 MR 已建好、收尾时才报错）必须被采信。"""
    import asyncio

    import claude_agent_sdk
    from app.agents import anthropic_client

    async def _fake_query(*, prompt, options):
        class _Msg:
            content = (
                '```json\n{"status": "succeeded", "merge_requests": ['
                '{"branch_name": "bugfix/ai-t-4-1", "title": "fix", '
                '"mr_url": "https://git.example.com/mr/1"}]}\n```'
            )

        yield _Msg()
        raise Exception(
            "Claude Code returned an error result: Reached maximum number of turns (60)"
        )

    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)
    monkeypatch.setattr(anthropic_client, "build_options", lambda **kw: object())

    result = asyncio.run(bf_agent.BugFixCodingAgent().run(_make_ctx("t-4")))
    assert result["status"] == "partial"
    assert len(result["merge_requests"]) == 1
    assert result["merge_requests"][0]["branch_name"] == "bugfix/ai-t-4-1"


def test_run_keeps_succeeded_when_all_fixes_already_implemented(monkeypatch):
    """0 MR 但每个拟修复项都 already_implemented → 判 succeeded，并透传 fix_outcomes。

    复现真实场景：某修复项在基线分支已实现（空 diff、无 MR），不应被误判为失败，
    也不应静默消失——结局要能在结果里被解释。
    """
    import asyncio

    import claude_agent_sdk
    from app.agents import anthropic_client

    async def _fake_query(*, prompt, options):
        class _Msg:
            content = (
                '```json\n{"status": "succeeded", "merge_requests": [], '
                '"fix_outcomes": [{"fix_index": 1, "title": "guard", '
                '"outcome": "already_implemented", "reason": "in 9b750d5"}]}\n```'
            )

        yield _Msg()

    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)
    monkeypatch.setattr(anthropic_client, "build_options", lambda **kw: object())

    result = asyncio.run(bf_agent.BugFixCodingAgent().run(_make_ctx("t-5")))
    assert result["status"] == "succeeded"
    assert result["merge_requests"] == []
    assert len(result["fix_outcomes"]) == 1
    assert result["fix_outcomes"][0]["outcome"] == "already_implemented"


def test_run_still_fails_when_zero_mr_without_clean_outcomes(monkeypatch):
    """0 MR 且没有「全部无需改动」的结局解释 → 仍判失败（防止无产出被伪装成功）。"""
    import asyncio

    import claude_agent_sdk
    from app.agents import anthropic_client

    async def _fake_query(*, prompt, options):
        class _Msg:
            content = '```json\n{"status": "succeeded", "merge_requests": []}\n```'

        yield _Msg()

    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)
    monkeypatch.setattr(anthropic_client, "build_options", lambda **kw: object())

    result = asyncio.run(bf_agent.BugFixCodingAgent().run(_make_ctx("t-6")))
    assert result["status"] == "failed"
    assert result["error_kind"] == "no_merge_requests"


def test_review_requires_complete_decisions():
    proposals = [{"title": "a"}, {"title": "b"}]
    out, error = bf_agent._validate_outcomes([
        {"fix_index": 1, "outcome": "rejected", "reason": "src/a.c:42 contradicts the diagnosis"}
    ], proposals, [])
    assert error == "incomplete_fix_outcomes"
    assert [o["outcome"] for o in out] == ["rejected", "failed"]


def test_rejected_review_requires_evidence_and_unique_indices():
    for outcomes in ([{"fix_index": 1, "outcome": "rejected"}],
                     [{"fix_index": 1, "outcome": "rejected", "reason": "proof"}] * 2):
        _, error = bf_agent._validate_outcomes(outcomes, [{"title": "a"}], [])
        assert error == "incomplete_fix_outcomes"


def test_independent_model_does_not_fall_back(monkeypatch):
    import asyncio
    from app.config import settings
    monkeypatch.setattr(settings, "bug_fix_agent_api_key", None)
    monkeypatch.setattr(settings, "anthropic_api_key", "general-key")
    result = asyncio.run(bf_agent.BugFixCodingAgent().run(_make_ctx("no-model")))
    assert result["error_kind"] == "bug_fix_model_unconfigured"


async def test_dedicated_endpoint_and_rejected_result(monkeypatch):
    import claude_agent_sdk
    from types import SimpleNamespace
    from app.agents import anthropic_client
    seen = {}
    def options(**kwargs):
        seen.update(kwargs)
        return object()
    async def query(**kwargs):
        yield SimpleNamespace(content='{"status":"succeeded","merge_requests":[],"fix_outcomes":[{"fix_index":1,"outcome":"rejected","reason":"Header and log values agree; proposal reverses correct behavior"}]}')
    monkeypatch.setattr(anthropic_client, "build_options", options)
    monkeypatch.setattr(claude_agent_sdk, "query", query)
    ctx = _make_ctx("review")
    ctx.metadata["proposed_fixes"] = [{"title": "Reverse enum"}]
    result = await bf_agent.BugFixCodingAgent().run(ctx)
    assert result["status"] == "succeeded"
    assert result["fix_outcomes"][0]["outcome"] == "rejected"
    assert seen["endpoint"].slot == "bug_fix"
    assert seen["endpoint"].api_key == "test-dedicated-key"
    assert result["model"] == "advanced-test-model"


async def test_terminal_sdk_error_without_exception(monkeypatch):
    import claude_agent_sdk
    from types import SimpleNamespace
    from app.agents import anthropic_client
    async def query(**kwargs):
        yield SimpleNamespace(is_error=True, result="API Error: 429 <ak-fixture-key> insufficient balance", content=None)
    monkeypatch.setattr(claude_agent_sdk, "query", query)
    monkeypatch.setattr(anthropic_client, "build_options", lambda **kw: object())
    result = await bf_agent.BugFixCodingAgent().run(_make_ctx("sdk-500"))
    assert result["status"] == "failed"
    assert result["error_kind"] == "upstream_error"
    assert "429 <***> insufficient balance" in result["error"]
    assert "ak-fixture-key" not in str(result)


@pytest.mark.parametrize("repo_url", [
    "ssh://git@gitlab.example.com:2225/team/repo.git", "git@gitlab.example.com:team/repo.git",
])
async def test_ssh_bug_fix_uses_worker_git_identity_without_provider_keys(monkeypatch, tmp_path, repo_url):
    from types import SimpleNamespace
    from app.agents import provider_query
    from app.agents.openai_agents_tools import WorkspaceTools

    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/fixture/agent.sock")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o StrictHostKeyChecking=yes")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")

    async def query(*, prompt, options):
        assert "-o merge_request.create" in options.system_prompt["append"]
        result = await WorkspaceTools(options).call("Bash", {"command": (
            'printf "%s\\n" "$SSH_AUTH_SOCK" "$GIT_SSH_COMMAND" "${OPENAI_API_KEY-unset}" "${ANTHROPIC_API_KEY-unset}"'
        )})
        assert result["stdout"].splitlines() == [
            "/run/fixture/agent.sock", "ssh -o StrictHostKeyChecking=yes", "unset", "unset",
        ]
        yield SimpleNamespace(content='{"status":"failed","error_kind":"fixture","merge_requests":[]}')

    monkeypatch.setattr(provider_query, "query", query)
    ctx = _make_ctx("ssh-repair")
    ctx.repo_url = repo_url
    ctx.temp_dir = str(tmp_path)
    result = await bf_agent.BugFixCodingAgent().run(ctx)
    assert result["error_kind"] == "fixture"


async def test_checkpoint_recovers_published_mr_and_marks_missing_item_failed(tmp_path, monkeypatch):
    import json
    import claude_agent_sdk
    from app.agents import anthropic_client
    checkpoint = {'merge_requests': [{'branch_name': 'bugfix/1', 'mr_url': 'https://host/mr/1'}],
                  'fix_outcomes': [{'fix_index': 1, 'outcome': 'created_mr', 'mr_url': 'https://host/mr/1'}]}
    (tmp_path / 'bug_fix_result.json').write_text(json.dumps(checkpoint))
    async def query(**kwargs):
        if False:
            yield None
        raise RuntimeError('Reached maximum number of turns (150)')
    monkeypatch.setattr(claude_agent_sdk, 'query', query)
    monkeypatch.setattr(anthropic_client, 'build_options', lambda **kw: object())
    ctx = _make_ctx('checkpoint')
    ctx.temp_dir = str(tmp_path)
    ctx.metadata['proposed_fixes'] = [{'title': 'a'}, {'title': 'b'}]
    result = await bf_agent.BugFixCodingAgent().run(ctx)
    assert result['status'] == 'partial'
    assert len(result['merge_requests']) == 1
    assert result['fix_outcomes'][1]['outcome'] == 'failed'
    assert result['error_kind'] == 'max_turns_exceeded'
    assert 'maximum number of turns' in result['error']
