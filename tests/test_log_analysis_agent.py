"""Integration tests for LogAnalysisAgent (mock claude_agent_sdk.query)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def isolate_mock_sdk_from_endpoint_credentials():
    # These tests exercise Agent result/trace handling, not endpoint selection.
    # Their partial Settings mocks must not be parsed as a real API key pool.
    with patch("app.services.model_router.candidates", return_value=[]):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("fenced", [False, True])
@pytest.mark.parametrize("skills", [[], ["optional-skill"]])
async def test_complete_result_keeps_structured_evidence_and_uncertainty(workspace_ctx, fenced, skills):
    from app.agents.log_analysis.agent import LogAnalysisAgent

    expected = {
        "status": "ok", "question_type": "root_cause", "severity": "warn",
        "answer": "Progress was observed; completion is unknown. Example: ```json {} ```\n\n"
                  "| Board | Time source |\n|---|---|\n| A | TOD |\n\n"
                  "```mermaid\nflowchart LR\n A[Source] --> B[Board]\n```",
        "summary": "Completion is unknown.",
        "root_cause_hypotheses": [{"hypothesis": "Driver failure", "evidence": ["log:test.log:17"]}],
        "recommended_actions": ["Collect the missing terminal record"],
        "related_keywords": ["driver"], "requires_code_fix": False, "proposed_fixes": [],
    }

    async def query(*args, **kwargs):
        body = json.dumps(expected)
        yield FakeResultMessage(result=f"```json\n{body}\n```" if fenced else body)

    sdk = MagicMock()
    sdk.query = query
    with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
         patch("app.services.skills_service.materialize_relevant_enabled_skills", return_value=skills), \
         patch.dict("sys.modules", {"claude_agent_sdk": sdk}), \
         patch("app.config.settings", MagicMock(anthropic_provider="yinhe", anthropic_model="test", anthropic_request_timeout_seconds=60)):
        result = await LogAnalysisAgent().run(workspace_ctx)
    for key, value in expected.items():
        assert result[key] == value
    assert "parse_warning" not in result


def test_unfenced_non_analysis_or_incomplete_json_is_not_a_complete_result():
    from app.agents.log_analysis.agent import _extract_fenced_json

    assert _extract_fenced_json('{"rows": [1, 2]}') is None
    assert _extract_fenced_json('{"status": "ok", "answer": "unfinished"') is None


@pytest.mark.parametrize("body", [
    '{"status":"ok","severity":"info","answer":"original","answer":"retry fragment"}',
    '{"status":"ok","severity":"info","answer":"original","summary":"first","summary":"second",',
    '{"status":"ok","severity":"info","answer":"original","root_cause_hypotheses":[{"hypothesis":"first","hypothesis":"second"}]}',
    '{"status":"ok","severity":"info","answer":"original","root_cause_hypotheses":[{"hypothesis":"first","hypothesis":"second"},',
])
def test_duplicate_result_fields_are_never_recovered(body):
    from app.agents.log_analysis.agent import _extract_fenced_json, extract_recoverable_result_fields

    for text in (body, f"```json\n{body}\n```"):
        assert _extract_fenced_json(text) is None
        assert extract_recoverable_result_fields(text) == {}


def test_repeated_field_names_inside_answer_text_are_not_duplicate_keys():
    from app.agents.log_analysis.agent import _extract_fenced_json

    expected = {"status": "ok", "severity": "info", "answer": 'The log contains "summary": twice: "summary":.'}
    assert _extract_fenced_json(f"```json\n{json.dumps(expected)}\n```") == expected


@pytest.mark.parametrize("field", ["answer", "summary"])
def test_mismatched_quote_cannot_swallow_later_result_fields(field):
    from app.agents.log_analysis.agent import _extract_fenced_json, extract_recoverable_result_fields

    prefix = '{"status":"ok","severity":"warn",'
    if field == "summary":
        prefix += '"answer":"A complete answer",'
    broken = prefix + f'"{field}":"First line\nSecond line\', "related_keywords":[],"recommended_actions":[]}}'
    text = f"```json\n{broken}\n```"
    assert _extract_fenced_json(text) is None
    assert extract_recoverable_result_fields(text) == {}


def test_valid_json_can_quote_apostrophes_and_result_field_examples():
    from app.agents.log_analysis.agent import _extract_fenced_json

    expected = {"status":"ok","severity":"info","answer":'Example: \' , "summary": "source text"'}
    assert _extract_fenced_json(f"```json\n{json.dumps(expected)}\n```") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("fenced", [False, True])
@pytest.mark.parametrize("body", [
    '{"status":"ok","severity":"info","answer":"original","summary":"first","summary":"retry fragment"}',
    '{"status":"ok","severity":"info","answer":"First line\nSecond line\', "summary":"Retry fragment"}',
])
async def test_corrupt_result_cannot_fall_back_to_plain_skill_answer(workspace_ctx, fenced, body):
    from app.agents.log_analysis.agent import LogAnalysisAgent

    terminal = f"```json\n{body}\n```" if fenced else body
    calls = []

    async def query(*args, **kwargs):
        calls.append(True)
        yield FakeResultMessage(result=terminal)

    sdk = MagicMock()
    sdk.query = query
    with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), \
         patch("app.services.skills_service.materialize_relevant_enabled_skills", return_value=["optional-test-skill"]), \
         patch.dict("sys.modules", {"claude_agent_sdk": sdk}), \
         patch("app.config.settings", MagicMock(anthropic_provider="yinhe", anthropic_model="test", anthropic_request_timeout_seconds=60)):
        result = await LogAnalysisAgent().run(workspace_ctx)
    assert result["status"] == "schema_mismatch"
    assert result["answer"] == ""
    assert result["raw"] == terminal
    assert len(calls) == 1


# ─────────────────────── Fake SDK Message Types ────────────────────

@dataclass
class FakeToolUse:
    name: str
    input: Any


@dataclass
class FakeAssistantMessage:
    tool_uses: List[FakeToolUse] = field(default_factory=list)
    usage: Optional[Any] = None


@dataclass
class FakeToolResult:
    tool_use_id: str
    content: Any


@dataclass
class FakeToolResultMessage:
    tool_results: List[FakeToolResult] = field(default_factory=list)


@dataclass
class FakeResultMessage:
    result: str


def _make_good_result_json() -> str:
    payload = {
        "status": "ok",
        "summary": "Found issue in module X",
        "severity": "error",
        "root_cause_hypotheses": [
            {"hypothesis": "Null pointer in handler", "evidence": ["repo:src/main.c:42"]}
        ],
        "recommended_actions": ["Fix null check"],
        "related_keywords": ["null", "handler"],
    }
    return f"```json\n{json.dumps(payload)}\n```"


async def _fake_query_ok(*args, **kwargs) -> AsyncIterator[Any]:
    """Simulates: AssistantMessage(lookup_project_repo) → ToolResultMessage → AssistantMessage(git clone) → ResultMessage"""
    yield FakeAssistantMessage(
        tool_uses=[FakeToolUse(name="mcp__project_repo__lookup_project_repo", input={"project_code": "foo"})]
    )
    yield FakeToolResultMessage(
        tool_results=[FakeToolResult(
            tool_use_id="1",
            content=[{"type": "text", "text": json.dumps({
                "project_code": "foo",
                "project_name": "Foo",
                "repo_url": "https://gitlab.example/foo.git",
                "clone_url": "https://oauth2:secret-token@gitlab.example/foo.git",
                "default_branch": "main",
                "auth_required": True,
            })}],
        )]
    )
    yield FakeAssistantMessage(
        tool_uses=[FakeToolUse(name="Bash", input={"command": "git clone $CLONE_URL repo"})]
    )
    yield FakeToolResultMessage(
        tool_results=[FakeToolResult(tool_use_id="2", content="Cloning into 'repo'...")]
    )
    yield FakeResultMessage(result=_make_good_result_json())


async def _fake_query_schema_mismatch(*args, **kwargs) -> AsyncIterator[Any]:
    yield FakeResultMessage(result="I analyzed the logs and found some issues but cannot format as JSON.")


async def _fake_query_truncated_json_prefix(*args, **kwargs) -> AsyncIterator[Any]:
    yield FakeResultMessage(result=(
        "```json\n"
        "{\n"
        '  "status": "ok",\n'
        '  "question_type": "root_cause",\n'
        '  "answer": "## 根因分析\\n\\n```\\nlog:log/Irun_oam.log:11503\\n```\\n已定位到两路TOD交替注入。",\n'
        '  "summary": "两路TOD时间源不一致导致校准分裂",\n'
        '  "severity": "critical",\n'
        '  "root_cause_hypotheses": [\n'
        '    {"hypothesis": "M3广播两组TOD", "evidence": ["log:log/Irun_oam.log:11503"]},\n'
        '    {"hypothesis": "第二条被截断", "evidence": ["repo:Code_CPUPl'
    ))


async def _fake_query_truncated_after_answer(*args, **kwargs) -> AsyncIterator[Any]:
    yield FakeResultMessage(result=(
        "我已掌握完整的证据链。现在输出最终分析结果。\n"
        "```json\n"
        "{\n"
        '  "status": "ok",\n'
        '  "question_type": "root_cause",\n'
        '  "answer": "## 重构失败原因分析\\n\\n根因是版本缓存未在重构成功后失效。",\n'
        '  "recommended_actions": [\n'
        '    "重构成功后清理 g_upfVersionCached",\n'
        '    "审查 xw_upf_reconfig.sh 的幂等性'
    ))


async def _fake_query_not_registered(*args, **kwargs) -> AsyncIterator[Any]:
    yield FakeAssistantMessage(
        tool_uses=[FakeToolUse(name="mcp__project_repo__lookup_project_repo", input={"project_code": "foo"})]
    )
    yield FakeToolResultMessage(
        tool_results=[FakeToolResult(tool_use_id="1", content='{"error": "not_found", "project_code": "foo"}')]
    )
    # Agent retries with project_name
    yield FakeAssistantMessage(
        tool_uses=[FakeToolUse(name="mcp__project_repo__lookup_project_repo", input={"project_code": "Foo Project"})]
    )
    yield FakeToolResultMessage(
        tool_results=[FakeToolResult(tool_use_id="2", content='{"error": "not_found", "project_code": "foo project"}')]
    )
    error_result = json.dumps({
        "status": "error",
        "error_kind": "project_repo_not_registered",
        "summary": "Project not registered",
        "severity": "error",
        "root_cause_hypotheses": [],
        "recommended_actions": [],
        "related_keywords": [],
    })
    yield FakeResultMessage(result=f"```json\n{error_result}\n```")


# ─────────────────────── Fixtures ──────────────────────────────────

@pytest.fixture
def workspace_ctx():
    import tempfile, os, json as _json
    from app.agents.log_analysis.workspace import WorkspaceContext

    tmp = tempfile.mkdtemp()
    task_json = os.path.join(tmp, "task.json")
    _json.dump({"log_id": 1, "question": "What failed?", "project_id": None, "hints": ""}, open(task_json, "w"))
    os.makedirs(os.path.join(tmp, "repo"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "logs"), exist_ok=True)

    ctx = WorkspaceContext(
        task_id="test-task-id",
        temp_dir=tmp,
        logs_dir=os.path.join(tmp, "logs"),
        repo_dir=os.path.join(tmp, "repo"),
        task_json_path=task_json,
    )
    ctx.metadata = {"project_code": "generic", "question": "What failed?"}
    yield ctx

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def _patch_build_options():
    fake_options = MagicMock()
    return patch("app.agents.anthropic_client.build_options", return_value=fake_options)


def _patch_mcp_server():
    return patch("app.agents.log_analysis.mcp_tools.get_mcp_server", return_value=MagicMock())


def _patch_prompts():
    system = "You are a test agent."
    user_tmpl = "Question: {question} log_type: {log_type} task_id: {task_id} hints: {hints}"
    return patch("app.agents.log_analysis.prompts.get_prompts", return_value=(system, user_tmpl))


def _patch_skills():
    return patch("app.services.skills_service.materialize_enabled_skills", return_value=[])


# ─────────────────────── Tests ─────────────────────────────────────

def test_render_user_prompt_includes_workspace_dir():
    from app.agents.log_analysis.prompts import render_user_prompt

    prompt = render_user_prompt(
        'Workspace root: {workspace_dir}\nRead: {"file_path":"task.json"}\nQuestion: {question}',
        task_id="task-1",
        workspace_dir="/app/temp/code_repos/task-1",
        question="What failed?",
        project_code="generic",
        hints="",
    )

    assert "Workspace root: /app/temp/code_repos/task-1" in prompt
    assert '{"file_path":"task.json"}' in prompt
    assert "/data/task" not in prompt


class TestLogAnalysisAgentRun:
    @pytest.mark.asyncio
    async def test_successful_run_parses_json(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_ok
        fake_sdk.ClaudeAgentOptions = MagicMock

        with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            result = await LogAnalysisAgent().run(workspace_ctx)

        assert result["status"] == "ok"
        assert result["engine"] == "claude-agent-sdk"
        assert result["schema_version"] == 3
        assert result["summary"] == "Found issue in module X"
        assert result["severity"] == "error"
        assert len(result["root_cause_hypotheses"]) == 1

    @pytest.mark.asyncio
    async def test_run_logs_workflow_events(self, workspace_ctx, caplog):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_ok
        fake_sdk.ClaudeAgentOptions = MagicMock

        with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            with caplog.at_level("INFO"):
                await LogAnalysisAgent().run(workspace_ctx)

        workflow_logs = [record.message for record in caplog.records if "LogAnalysisAgent workflow" in record.message]
        assert any("event=run_start" in message for message in workflow_logs)
        assert any("event=tool_call" in message and "lookup_project_repo" in message for message in workflow_logs)
        assert any("event=tool_result" in message for message in workflow_logs)
        assert any("event=run_complete" in message for message in workflow_logs)

    @pytest.mark.asyncio
    async def test_tool_trace_masks_token(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_ok

        with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            result = await LogAnalysisAgent().run(workspace_ctx)

        # tool_trace should not contain plaintext token
        trace_str = json.dumps(result["tool_trace"])
        assert "secret-token" not in trace_str, "Token must be masked in tool_trace"
        assert "https://***@" in trace_str or "oauth2:secret" not in trace_str

    @pytest.mark.asyncio
    async def test_schema_mismatch_when_no_json(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_schema_mismatch

        with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            result = await LogAnalysisAgent().run(workspace_ctx)

        assert result["status"] == "schema_mismatch"
        assert "cannot format" in result["raw"]

    @pytest.mark.asyncio
    async def test_truncated_fenced_json_prefix_is_recovered(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_truncated_json_prefix

        with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            result = await LogAnalysisAgent().run(workspace_ctx)

        assert result["status"] == "ok"
        assert result["question_type"] == "root_cause"
        assert "两路TOD" in result["answer"]
        assert "```" in result["answer"]
        assert result["summary"] == "两路TOD时间源不一致导致校准分裂"
        assert result["severity"] == "critical"
        assert result["root_cause_hypotheses"] == [
            {"hypothesis": "M3广播两组TOD", "evidence": ["log:log/Irun_oam.log:11503"]}
        ]
        assert result["recommended_actions"] == []
        assert result["related_keywords"] == []

    @pytest.mark.asyncio
    async def test_truncated_json_with_answer_but_missing_optional_fields_is_recovered(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_truncated_after_answer

        with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            result = await LogAnalysisAgent().run(workspace_ctx)

        assert result["status"] == "ok"
        assert result["parse_warning"] == "incomplete_json_recovered"
        assert result["question_type"] == "root_cause"
        assert "版本缓存未在重构成功后失效" in result["answer"]
        assert result["severity"] == "info"
        assert result["recommended_actions"] == ["重构成功后清理 g_upfVersionCached"]

    @pytest.mark.asyncio
    async def test_project_repo_not_registered(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_not_registered

        with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            result = await LogAnalysisAgent().run(workspace_ctx)

        assert result["status"] == "error"
        assert result["error_kind"] == "project_repo_not_registered"

    @pytest.mark.asyncio
    async def test_uses_context_question_over_task_json_question(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        prompts: List[str] = []

        async def _fake_query_capture(*args, **kwargs) -> AsyncIterator[Any]:
            prompts.append(kwargs.get("prompt") or args[0])
            yield FakeResultMessage(result=_make_good_result_json())

        workspace_ctx.metadata["question"] = "克隆代码仓库并告诉我最新两次修改"

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_capture

        with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            result = await LogAnalysisAgent().run(workspace_ctx)

        assert result["status"] == "ok"
        assert prompts
        assert "克隆代码仓库" in prompts[0]
        assert "What failed?" not in prompts[0]

    @pytest.mark.asyncio
    async def test_deepseek_does_not_register_mcp_server(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_schema_mismatch

        with _patch_build_options() as mock_build_options, \
             _patch_mcp_server() as mock_get_mcp_server, \
             _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            await LogAnalysisAgent().run(workspace_ctx)

        mock_get_mcp_server.assert_called_once()
        assert mock_get_mcp_server.call_args.kwargs == {
            "workspace_dir": workspace_ctx.temp_dir,
            "primary_project_code": None,
            "agent_key": "log_analysis",
        }
        kwargs = mock_build_options.call_args.kwargs
        assert kwargs["mcp_servers"] is not None
        assert "mcp__project_repo__lookup_project_repo" in kwargs["allowed_tools"]
        assert "mcp__project_repo__discover_projects" in kwargs["allowed_tools"]
        assert "mcp__project_repo__clone_project_repo" in kwargs["allowed_tools"]
        assert "项目适配性检查（最高优先级）" in kwargs["system_prompt"]
        assert "当前系统还没有适合回答这个问题的项目" in kwargs["system_prompt"]
        assert "在当前工作区克隆匹配项目" in kwargs["system_prompt"]

    @pytest.mark.asyncio
    async def test_unsupported_provider_disables_multi_project_tools(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_schema_mismatch

        with _patch_build_options() as mock_build_options, \
             _patch_mcp_server() as mock_get_mcp_server, \
             _patch_prompts(), _patch_skills(), \
             patch("app.services.model_router.candidates", return_value=[]), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="legacy-model",
                 anthropic_provider="legacy-no-tools",
                 anthropic_request_timeout_seconds=600,
             )):
            await LogAnalysisAgent().run(workspace_ctx)

        mock_get_mcp_server.assert_not_called()
        kwargs = mock_build_options.call_args.kwargs
        assert kwargs["mcp_servers"] is None
        assert all(not name.startswith("mcp__") for name in kwargs["allowed_tools"])
        assert "不能在当前任务追加其他项目仓库" in kwargs["system_prompt"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("locale", ["en", "zh"])
    async def test_run_threads_locale_into_prompts_and_directive(self, workspace_ctx, locale):
        """End-to-end (4.4): the run honours ``ctx.locale`` — it selects the
        per-language prompt body via ``get_prompts(locale=...)`` and appends the
        matching response-language directive to the system prompt handed to
        ``build_options``."""
        from app.agents.log_analysis.agent import LogAnalysisAgent
        from app.i18n.prompts import response_language_directive

        workspace_ctx.locale = locale

        captured_locale = {}
        captured_prompt = {}

        def _spy_get_prompts(*args, **kwargs):
            captured_locale["value"] = kwargs.get("locale")
            return ("You are a test agent.", "{question}")

        async def _spy_query(*args, **kwargs):
            captured_prompt["value"] = kwargs.get("prompt")
            async for message in _fake_query_ok(*args, **kwargs):
                yield message

        fake_sdk = MagicMock()
        fake_sdk.query = _spy_query
        fake_sdk.ClaudeAgentOptions = MagicMock

        with _patch_build_options() as mock_build_options, _patch_mcp_server(), \
             patch("app.agents.log_analysis.prompts.get_prompts", side_effect=_spy_get_prompts), \
             _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            await LogAnalysisAgent().run(workspace_ctx)

        # Locale flowed into prompt selection.
        assert captured_locale["value"] == locale
        # Required Skill instructions can follow the language directive; both
        # remain in the actual prompt delivered to the SDK.
        system_prompt = mock_build_options.call_args.kwargs["system_prompt"]
        assert response_language_directive(locale) in system_prompt
        assert captured_prompt["value"].endswith(response_language_directive(locale))
        # The other locale's directive must NOT be present.
        other = "zh" if locale == "en" else "en"
        assert response_language_directive(other) not in system_prompt
        assert response_language_directive(other) not in captured_prompt["value"]

    @pytest.mark.asyncio
    async def test_bash_tool_is_unrestricted_for_temp_workspace(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_schema_mismatch

        with _patch_build_options() as mock_build_options, \
             _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            await LogAnalysisAgent().run(workspace_ctx)

        kwargs = mock_build_options.call_args.kwargs
        assert kwargs["permission_mode"] == "bypassPermissions"
        assert "Bash" in kwargs["allowed_tools"]
        assert not any(tool.startswith("Bash(") for tool in kwargs["allowed_tools"])
        assert "hooks" not in kwargs


class TestRuntimeTooling:
    def test_dockerfile_installs_agent_cli_tools(self):
        from pathlib import Path

        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        for package in ("git", "openssh-client", "ripgrep", "jq"):
            assert package in dockerfile

    def test_ssh_compose_overlay_keeps_private_key_out_of_agent_containers(self):
        from pathlib import Path

        import yaml

        compose = yaml.safe_load(
            Path("docker-compose.ssh.yml").read_text(encoding="utf-8")
        )
        services = compose["services"]

        assert "setpriv --reuid=1000" in services["ssh-agent"]["command"][0]
        assert any(
            "/run/raven-ssh/id_ed25519:ro" in volume
            for volume in services["ssh-agent"]["volumes"]
        )

        for name in ("backend", "worker", "worker-bugfix"):
            service = services[name]
            assert service["environment"]["SSH_AUTH_SOCK"].endswith("/agent.sock")
            assert (
                "StrictHostKeyChecking=yes"
                in service["environment"]["GIT_SSH_COMMAND"]
            )
            assert any(
                "ssh_agent_runtime:/run/raven-ssh-agent:ro" == volume
                for volume in service["volumes"]
            )
            assert not any("id_ed25519" in volume for volume in service["volumes"])


class TestRunSync:
    def test_run_sync_timeout(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        async def _slow_run(self, _, cancel_event=None, trace_emitter=None, clarification_binding=None):
            await asyncio.sleep(9999)

        with patch.object(LogAnalysisAgent, "run", _slow_run), \
             patch("app.config.settings", MagicMock(
                 anthropic_request_timeout_seconds=0.01,
             )):
            result = LogAnalysisAgent().run_sync(workspace_ctx)

        assert result["status"] == "error"
        assert result["error_kind"] == "timeout"


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_event_aborts_between_messages(self, workspace_ctx):
        """Agent should observe cancel_event between SDK messages and return cancelled."""
        from app.agents.log_analysis.agent import LogAnalysisAgent
        import threading

        cancel_event = threading.Event()

        async def _fake_query_drip(*args, **kwargs):
            # First message comes through normally.
            yield FakeAssistantMessage(
                tool_uses=[FakeToolUse(name="Read", input={"path": "x"})]
            )
            # User cancels between messages.
            cancel_event.set()
            yield FakeToolResultMessage(
                tool_results=[FakeToolResult(tool_use_id="1", content="data")]
            )
            yield FakeResultMessage(result=_make_good_result_json())

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_drip

        with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=3600,
             )):
            result = await LogAnalysisAgent().run(workspace_ctx, cancel_event=cancel_event)

        assert result["status"] == "cancelled"
        assert result["error_kind"] == "cancelled"
        # The first tool_call should have been recorded before cancel was observed.
        assert any(entry.get("name") == "Read" for entry in result["tool_trace"])

    def test_run_sync_passes_cancel_event(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent
        import threading

        received: dict = {}

        async def _capture_run(self, ctx, cancel_event=None, trace_emitter=None, clarification_binding=None):
            received["evt"] = cancel_event
            received["emitter"] = trace_emitter
            return {
                "engine": "claude-agent-sdk",
                "model": "fake",
                "schema_version": 3,
                "status": "ok",
                "question_type": "other",
                "answer": "",
                "summary": "",
                "severity": "info",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
                "tool_trace": [],
                "raw": "",
                "duration_seconds": 0.0,
                "token_usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
            }

        evt = threading.Event()
        with patch.object(LogAnalysisAgent, "run", _capture_run), \
             patch("app.config.settings", MagicMock(anthropic_request_timeout_seconds=10)):
            LogAnalysisAgent().run_sync(workspace_ctx, cancel_event=evt)

        assert received["evt"] is evt


class TestFastFailCeleryTask:
    def _make_log_record(self, archive_path=None):
        r = MagicMock()
        r.id = 1
        r.archive_path = archive_path
        r.is_deleted = False
        r.metadata_json = None
        r.file_path = "/tmp/test.log"
        return r

    def _make_session(self, log_record):
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = log_record
        return session

    def test_missing_archive_returns_error_kind(self):
        from app.tasks.ai_analysis import run_ai_analysis_task

        log_record = self._make_log_record(archive_path=None)

        with patch("app.tasks.ai_analysis.SessionLocal") as MockSession, \
             patch("app.tasks.ai_analysis.settings") as mock_settings, \
             patch("app.tasks.ai_analysis.current_task") as mock_task:
            mock_settings.max_retry_attempts = 0
            mock_settings.anthropic_model = "deepseek-v4-pro"
            mock_settings.anthropic_request_timeout_seconds = 600
            mock_task.request.id = "test-task"
            session = self._make_session(log_record)
            MockSession.return_value = session

            result = run_ai_analysis_task.run(log_record.id, "test query")

        assert result["error_kind"] == "missing_archive"

    def test_missing_metadata_json_returns_error_kind(self):
        import tarfile
        import tempfile
        import io
        from pathlib import Path
        from app.tasks.ai_analysis import run_ai_analysis_task

        tmpdir = tempfile.mkdtemp()
        archive = Path(tmpdir) / "test.tar.gz"

        with tarfile.open(str(archive), "w:gz") as tf:
            data = b"no metadata here"
            info = tarfile.TarInfo(name="app.log")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        log_record = self._make_log_record(archive_path=str(archive))

        with patch("app.tasks.ai_analysis.SessionLocal") as MockSession, \
             patch("app.tasks.ai_analysis.settings") as mock_settings, \
             patch("app.tasks.ai_analysis.current_task") as mock_task, \
             patch("app.config.settings") as cfg_settings:
            mock_settings.max_retry_attempts = 0
            mock_settings.anthropic_model = "deepseek-v4-pro"
            mock_settings.anthropic_request_timeout_seconds = 600
            mock_task.request.id = "test-task"
            cfg_settings.code_repo_clone_base_dir = tmpdir
            cfg_settings.ai_analysis_max_extract_bytes = 100 * 1024 * 1024
            session = self._make_session(log_record)
            MockSession.return_value = session

            result = run_ai_analysis_task.run(log_record.id, "test query")

        assert result["error_kind"] == "missing_metadata_json"

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ───────────────── code-fix signal (bug-fix dispatch) ─────────────────


class TestCodeFixSignal:
    """Unit tests for the `requires_code_fix` / `proposed_fixes` signal."""

    def test_code_defect_populates_signal(self):
        from app.agents.log_analysis.agent import _normalize_code_fix_signal

        parsed = {
            "requires_code_fix": True,
            "proposed_fixes": [
                {
                    "title": "为 handler 增加空指针检查",
                    "description": "在 src/main.c 的 handle() 入口判空",
                    "rationale": "根因是 req 为 NULL 时解引用崩溃",
                    "suspected_files": ["repo:src/main.c"],
                    "suspected_symbols": ["handle"],
                }
            ],
        }
        requires, fixes = _normalize_code_fix_signal(parsed)
        assert requires is True
        assert len(fixes) == 1
        assert fixes[0]["title"] == "为 handler 增加空指针检查"
        assert fixes[0]["rationale"].startswith("根因")
        assert fixes[0]["suspected_files"] == ["repo:src/main.c"]
        assert fixes[0]["suspected_symbols"] == ["handle"]

    def test_non_code_issue_clears_signal(self):
        from app.agents.log_analysis.agent import _normalize_code_fix_signal

        parsed = {"requires_code_fix": False, "proposed_fixes": []}
        requires, fixes = _normalize_code_fix_signal(parsed)
        assert requires is False
        assert fixes == []

    def test_legacy_response_defaults_safely(self):
        from app.agents.log_analysis.agent import _normalize_code_fix_signal

        # Legacy responses omit both fields entirely.
        requires, fixes = _normalize_code_fix_signal({"status": "ok"})
        assert requires is False
        assert fixes == []

    def test_requires_true_but_no_valid_fixes_does_not_trigger(self):
        from app.agents.log_analysis.agent import _normalize_code_fix_signal

        # A stray boolean with no usable fixes must not request a fix.
        requires, fixes = _normalize_code_fix_signal(
            {"requires_code_fix": True, "proposed_fixes": []}
        )
        assert requires is False
        assert fixes == []

    def test_malformed_fixes_are_dropped(self):
        from app.agents.log_analysis.agent import _normalize_proposed_fixes

        fixes = _normalize_proposed_fixes(
            [
                {"title": "ok", "description": "d", "rationale": "r"},
                {"title": "missing rationale", "description": "d"},
                {"description": "no title", "rationale": "r"},
                "not a dict",
                {"title": "  ", "description": "d", "rationale": "r"},
            ]
        )
        assert len(fixes) == 1
        assert fixes[0]["title"] == "ok"
        # Optional fields omitted when absent.
        assert "suspected_files" not in fixes[0]

    @pytest.mark.asyncio
    async def test_successful_run_surfaces_signal(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        async def _fake_query_with_fix(*args, **kwargs):
            payload = {
                "status": "ok",
                "question_type": "root_cause",
                "answer": "空指针导致崩溃",
                "summary": "空指针导致崩溃",
                "severity": "error",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
                "requires_code_fix": True,
                "proposed_fixes": [
                    {"title": "判空", "description": "加空指针检查", "rationale": "根因是空指针"}
                ],
            }
            yield FakeResultMessage(result=f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```")

        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_with_fix
        fake_sdk.ClaudeAgentOptions = MagicMock

        with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            result = await LogAnalysisAgent().run(workspace_ctx)

        assert result["requires_code_fix"] is True
        assert len(result["proposed_fixes"]) == 1
        assert result["proposed_fixes"][0]["title"] == "判空"

    @pytest.mark.asyncio
    async def test_legacy_run_defaults_signal(self, workspace_ctx):
        from app.agents.log_analysis.agent import LogAnalysisAgent

        # _fake_query_ok emits a legacy payload with no signal fields.
        fake_sdk = MagicMock()
        fake_sdk.query = _fake_query_ok
        fake_sdk.ClaudeAgentOptions = MagicMock

        with _patch_build_options(), _patch_mcp_server(), _patch_prompts(), _patch_skills(), \
             patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}), \
             patch("app.config.settings", MagicMock(
                 anthropic_model="deepseek-v4-pro",
                 anthropic_provider="deepseek",
                 anthropic_request_timeout_seconds=600,
             )):
            result = await LogAnalysisAgent().run(workspace_ctx)

        assert result["requires_code_fix"] is False
        assert result["proposed_fixes"] == []
