from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.project_expert.workspace import WorkspaceContext


@dataclass
class FakeToolUse:
    name: str
    input: Any
    id: Optional[str] = None


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


def _repo(**overrides):
    data = {
        "id": 7,
        "project_code": "foo",
        "project_name": "Foo Service",
        "project_card": "Foo service authentication and account APIs",
        "repo_url": "https://gitlab.example/foo.git",
        "default_branch": "main",
        "enabled": True,
        "git_token": "secret-token",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _result_json(answer: str = "见 repo/app/main.py:10") -> str:
    payload = {
        "status": "ok",
        "question_type": "qa",
        "answer": answer,
        "summary": answer,
        "severity": "info",
        "root_cause_hypotheses": [],
        "recommended_actions": [],
        "related_keywords": [],
    }
    return f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


async def _fake_query_with_clone(*_args, **_kwargs) -> AsyncIterator[Any]:
    yield FakeAssistantMessage(
        tool_uses=[
            FakeToolUse(
                name="mcp__project_repo__lookup_project_repo",
                input={"project_code": "foo"},
                id="lookup-1",
            )
        ]
    )
    yield FakeToolResultMessage(
        tool_results=[
            FakeToolResult(
                tool_use_id="lookup-1",
                content=[
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "project_code": "foo",
                                "clone_url": "https://oauth2:secret-token@gitlab.example/foo.git",
                                "default_branch": "main",
                            }
                        ),
                    }
                ],
            )
        ]
    )
    yield FakeAssistantMessage(
        tool_uses=[
            FakeToolUse(
                name="Bash",
                input={
                    "command": "git clone --depth 1 https://oauth2:secret-token@gitlab.example/foo.git repo"
                },
                id="clone-1",
            )
        ]
    )
    yield FakeToolResultMessage(
        tool_results=[FakeToolResult(tool_use_id="clone-1", content="Cloning into 'repo'...")]
    )
    yield FakeResultMessage(result=_result_json())


async def _fake_query_reuse_existing_repo(*_args, **_kwargs) -> AsyncIterator[Any]:
    yield FakeAssistantMessage(
        tool_uses=[
            FakeToolUse(
                name="Bash",
                input={"command": "git -C repo status --short && rg auth repo"},
                id="reuse-1",
            )
        ]
    )
    yield FakeToolResultMessage(
        tool_results=[FakeToolResult(tool_use_id="reuse-1", content="repo/app/main.py:10: auth")]
    )
    yield FakeResultMessage(result=_result_json("复用 repo/.git 后定位到 repo/app/main.py:10"))


async def _fake_query_unescaped_answer_quotes(*_args, **_kwargs) -> AsyncIterator[Any]:
    yield FakeResultMessage(result=(
        "基于对灵犀10操作维护项目源码的调查，以下是关于重构的回答：\n"
        "```json\n"
        "{\n"
        '  "status": "ok",\n'
        '  "question_type": "qa",\n'
        '  "answer": "在灵犀10（LX10）操作维护这个项目中，**"重构"有明确的专有含义：它指的是「在轨软件重构」**，即通过地面指令远程替换/重新加载星载基带处理器上运行的不同软件版本。根据 `repo/README.md:71-78`，它包括基带软件、核心网版本、DVB馈电版本、Ka相控阵软件和客户定制启动脚本的重构。",\n'
        '  "summary": "重构在该项目中主要指在轨软件重构。",\n'
        '  "severity": "info",\n'
        '  "root_cause_hypotheses": [],\n'
        '  "recommended_actions": [],\n'
        '  "related_keywords": ["重构", "在轨软件重构"]\n'
        "}\n"
        "```"
    ))


async def _fake_query_plain_skill_answer(*_args, **_kwargs) -> AsyncIterator[Any]:
    yield FakeResultMessage(result="TURQUOISE-MONGOOSE-9")


def _patch_agent_common(fake_query):
    from claude_agent_sdk import HookMatcher

    fake_sdk = SimpleNamespace(query=fake_query, HookMatcher=HookMatcher)
    return patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk})


def _make_ctx(tmp_path: Path) -> WorkspaceContext:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    task_json = tmp_path / "task.json"
    task_json.write_text(
        json.dumps(
            {
                "question": "鉴权在哪里实现？",
                "hints": "",
                "repo_info": {
                    "project_code": "foo",
                    "project_name": "Foo Service",
                    "project_card": "Foo service authentication and account APIs",
                    "repo_url": "https://gitlab.example/foo.git",
                    "default_branch": "main",
                    "source": "user_selected_project_repo",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return WorkspaceContext(
        task_id="task-1",
        temp_dir=str(tmp_path),
        repo_dir=str(repo_dir),
        task_json_path=str(task_json),
        metadata={"question": "鉴权在哪里实现？", "hints": ""},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("source_context,final_review", [(False, False), (True, False), (False, True), (True, True)])
async def test_evidence_snapshot_retains_project_expert_scope(tmp_path, available, source_context, final_review):
    from app.agents.project_expert.agent import ProjectExpertAgent
    from app.agents.evidence_tools import TOOL_NAMES

    ctx = _make_ctx(tmp_path)
    snapshot = {"task": {}, "project_catalog": {"count": 0, "projects": [], "truncated": False}}
    options = MagicMock(return_value=MagicMock())
    with _patch_agent_common(_fake_query_with_clone), \
         patch("app.agents.anthropic_client.build_options", options), \
         patch("app.services.model_router.candidates", return_value=[]), \
         patch("app.agents.log_analysis.mcp_tools.get_mcp_server", return_value={}), \
         patch("app.agents.evidence_tools.get_evidence_mcp_server", return_value={}), \
         patch("app.agents.evidence_policy.prepare_snapshot", AsyncMock(return_value=snapshot if available else None)), \
         patch("app.config.settings.agent_evidence_efficiency_enabled", True), \
         patch("app.config.settings.agent_evidence_source_context_enabled", source_context), \
         patch("app.config.settings.agent_evidence_final_review_enabled", final_review), \
         patch("app.config.settings.anthropic_provider", "yinhe"):
        result = await ProjectExpertAgent().run(ctx)
    assert result["status"] == "ok"
    actual = options.call_args.kwargs
    hooks = actual.get("hooks") or {}
    assert ("PostToolUse" in hooks) == (available and source_context)
    assert ("Stop" in hooks) == (available and final_review)
    if available and final_review:
        stop = actual["hooks"]["Stop"][0].hooks[0]
        assert (await stop({"hook_event_name": "Stop"}, None, {}))["decision"] == "block"
        assert await stop({"hook_event_name": "Stop"}, None, {}) == {}
    assert all((name in actual["allowed_tools"]) == available for name in TOOL_NAMES)
    assert "不能把参考项目的行为写成所选项目的实现" in actual["system_prompt"]
    assert "请用户改选正确项目" in actual["system_prompt"]
    assert "humanizer-zh" in actual["system_prompt"]


def _decode_sse_event(chunk: str) -> dict:
    assert chunk.startswith("data: ")
    return json.loads(chunk[len("data: "):].strip())


def test_workspace_contains_only_repo_and_task_json_without_token(monkeypatch, tmp_path):
    from app.agents.project_expert import workspace

    monkeypatch.setattr(workspace.settings, "code_repo_clone_base_dir", str(tmp_path))

    ctx = workspace.prepare(project_repo=_repo(), question="鉴权在哪里实现？", hints="hint")
    root = Path(ctx.temp_dir)
    task_data = json.loads(Path(ctx.task_json_path).read_text(encoding="utf-8"))

    assert sorted(item.name for item in root.iterdir()) == ["repo", "task.json"]
    assert Path(ctx.repo_dir).is_dir()
    assert not (root / "logs").exists()
    assert task_data["repo_info"]["source"] == "user_selected_project_repo"
    assert task_data["repo_info"]["project_code"] == "foo"
    assert task_data["repo_info"]["project_card"] == "Foo service authentication and account APIs"
    assert "secret-token" not in json.dumps(task_data, ensure_ascii=False)

    workspace.cleanup(ctx)
    workspace.cleanup(ctx)
    assert not root.exists()


@pytest.mark.asyncio
async def test_agent_uses_expected_tools_materializes_project_skills_and_masks_tokens(tmp_path):
    from app.agents.project_expert.agent import ALLOWED_TOOLS, ProjectExpertAgent

    ctx = _make_ctx(tmp_path)
    trace_events: list[dict] = []
    captured_prompt: dict[str, str] = {}
    build_options = MagicMock(return_value=MagicMock())

    async def fake_query_with_prompt_capture(*args, **kwargs) -> AsyncIterator[Any]:
        captured_prompt["prompt"] = kwargs.get("prompt") or (args[0] if args else "")
        async for message in _fake_query_with_clone(*args, **kwargs):
            yield message

    with _patch_agent_common(fake_query_with_prompt_capture), \
        patch("app.agents.anthropic_client.build_options", build_options), \
        patch(
            "app.services.skills_service.materialize_relevant_enabled_skills",
            return_value=["repo-reader"],
        ) as materialize, \
        patch(
            "app.services.skills_service.enabled_skill_overviews",
            return_value=[
                {"name": "repo-reader", "description": "读取仓库源码文件"},
                {"name": "db-helper", "description": "数据库维护"},
            ],
        ), \
        patch("app.agents.log_analysis.mcp_tools.get_mcp_server", return_value=MagicMock()), \
        patch("app.config.settings.anthropic_provider", "anthropic"):
        result = await ProjectExpertAgent().run(ctx, trace_emitter=trace_events.append)

    assert result["engine"] == "claude-agent-sdk"
    assert result["status"] == "ok"
    assert "repo/app/main.py:10" in result["answer"]
    materialize.assert_called_once()
    assert materialize.call_args.args[0] == "project_expert"
    assert materialize.call_args.args[1] == ctx.temp_dir
    assert "鉴权在哪里实现" in materialize.call_args.kwargs["query_text"]
    assert materialize.call_args.kwargs["project_code"] == "foo"

    kwargs = build_options.call_args.kwargs
    assert kwargs["allowed_tools"] == ALLOWED_TOOLS
    assert "mcp__project_repo__discover_projects" in kwargs["allowed_tools"]
    assert kwargs["cwd"] == ctx.temp_dir
    assert kwargs["setting_sources"] == ["project"]
    assert "可用的 Skill（按需加载）" in kwargs["system_prompt"]
    assert "可用的 Skill（按需加载）" in captured_prompt["prompt"]
    assert "`repo-reader`：读取仓库源码文件" in captured_prompt["prompt"]
    assert '"skill": "repo-reader"' in captured_prompt["prompt"]
    assert "最终输出仍必须遵守第 5 步的围栏 JSON schema" in captured_prompt["prompt"]
    run_start = next(ev for ev in trace_events if ev["type"] == "run_start")
    assert run_start["available_skills"] == ["repo-reader", "db-helper", "humanizer-zh"]
    assert run_start["loaded_skills"] == ["repo-reader", "humanizer-zh"]
    assert "db-helper" not in captured_prompt["prompt"]
    assert "项目适配性检查（最高优先级）" in kwargs["system_prompt"]
    assert "Foo service authentication and account APIs" in kwargs["system_prompt"]
    assert "当前系统还没有适合回答这个问题的项目" in kwargs["system_prompt"]
    assert "请用户改选正确项目" in kwargs["system_prompt"]
    assert "应在当前工作区克隆匹配项目并从该代码检出中作答" not in kwargs["system_prompt"]
    assert "其他系列仓库只能在用户明确要求比较时" in kwargs["system_prompt"]
    assert "不能把参考项目的行为写成所选项目的实现" in kwargs["system_prompt"]

    trace_text = json.dumps(result["trace_events"], ensure_ascii=False)
    assert "secret-token" not in trace_text
    assert "https://***@gitlab.example/foo.git" in trace_text


@pytest.mark.asyncio
async def test_custom_provider_registers_project_discovery_tool(tmp_path):
    """Regression for the production custom/yinhe provider capability gate."""
    from app.agents.project_expert.agent import ProjectExpertAgent

    ctx = _make_ctx(tmp_path)
    build_options = MagicMock(return_value=MagicMock())
    mcp_server = MagicMock()

    async def fake_query(*_args, **_kwargs) -> AsyncIterator[Any]:
        yield FakeResultMessage(result=_result_json())

    with _patch_agent_common(fake_query), \
        patch("app.agents.anthropic_client.build_options", build_options), \
        patch("app.services.skills_service.materialize_relevant_enabled_skills", return_value=[]), \
        patch(
            "app.agents.log_analysis.mcp_tools.get_mcp_server",
            return_value=mcp_server,
        ) as get_mcp_server, \
        patch("app.config.settings.anthropic_provider", "custom"), \
        patch("app.config.settings.anthropic_model", "yinhe-thinking"):
        result = await ProjectExpertAgent().run(ctx)

    assert result["status"] == "ok"
    get_mcp_server.assert_called_once_with(
        workspace_dir=ctx.temp_dir,
        primary_project_code="foo",
        agent_key="project_expert",
    )
    kwargs = build_options.call_args.kwargs
    assert kwargs["mcp_servers"] == {"project_repo": mcp_server}
    assert "mcp__project_repo__discover_projects" in kwargs["allowed_tools"]
    assert "mcp__project_repo__lookup_project_repo" in kwargs["allowed_tools"]
    assert "mcp__project_repo__clone_project_repo" in kwargs["allowed_tools"]
    assert "当前 provider `custom` 不支持 MCP" not in kwargs["system_prompt"]
    assert "必须先调用 `mcp__project_repo__discover_projects`" in kwargs["system_prompt"]
    assert "mcp__project_repo__clone_project_repo" in kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_unsupported_provider_disables_multi_project_tools(tmp_path):
    from app.agents.project_expert.agent import ProjectExpertAgent

    ctx = _make_ctx(tmp_path)
    build_options = MagicMock(return_value=MagicMock())

    async def fake_query(*_args, **_kwargs) -> AsyncIterator[Any]:
        yield FakeResultMessage(result=_result_json())

    with _patch_agent_common(fake_query), \
        patch("app.agents.anthropic_client.build_options", build_options), \
        patch("app.services.model_router.candidates", return_value=[]), \
        patch("app.services.skills_service.materialize_relevant_enabled_skills", return_value=[]), \
        patch("app.agents.log_analysis.mcp_tools.get_mcp_server") as get_mcp_server, \
        patch("app.config.settings.anthropic_provider", "legacy-no-tools"), \
        patch("app.config.settings.anthropic_model", "legacy-model"):
        result = await ProjectExpertAgent().run(ctx)

    assert result["status"] == "ok"
    get_mcp_server.assert_not_called()
    kwargs = build_options.call_args.kwargs
    assert kwargs["mcp_servers"] is None
    assert all(not name.startswith("mcp__") for name in kwargs["allowed_tools"])
    assert "不能在当前会话追加其他项目仓库" in kwargs["system_prompt"]
    assert "Do not claim" not in kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_agent_followup_reuses_existing_repo_without_clone(tmp_path):
    from app.agents.project_expert.agent import ProjectExpertAgent

    ctx = _make_ctx(tmp_path)
    (Path(ctx.repo_dir) / ".git").mkdir()

    with _patch_agent_common(_fake_query_reuse_existing_repo), \
        patch("app.agents.anthropic_client.build_options", return_value=MagicMock()), \
        patch("app.services.skills_service.materialize_relevant_enabled_skills", return_value=[]), \
        patch("app.agents.log_analysis.mcp_tools.get_mcp_server", return_value=MagicMock()):
        result = await ProjectExpertAgent().run(ctx)

    trace_text = json.dumps(result["trace_events"], ensure_ascii=False)
    assert "git clone" not in trace_text
    assert "git -C repo status" in trace_text
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_agent_recovers_grounded_answer_with_unescaped_inner_quotes(tmp_path):
    from app.agents.project_expert.agent import ProjectExpertAgent

    ctx = _make_ctx(tmp_path)

    with _patch_agent_common(_fake_query_unescaped_answer_quotes), \
        patch("app.agents.anthropic_client.build_options", return_value=MagicMock()), \
        patch("app.services.skills_service.materialize_relevant_enabled_skills", return_value=[]), \
        patch("app.agents.log_analysis.mcp_tools.get_mcp_server", return_value=MagicMock()):
        result = await ProjectExpertAgent().run(ctx)

    assert result["status"] == "ok"
    assert result["question_type"] == "qa"
    assert '**"重构"有明确的专有含义' in result["answer"]
    assert "repo/README.md:71-78" in result["answer"]
    assert "星载基带处理器" in result["answer"]
    assert result["answer"] != "在灵犀10（LX10）操作维护这个项目中，**"


@pytest.mark.asyncio
async def test_agent_wraps_plain_text_skill_answer(tmp_path):
    from app.agents.project_expert.agent import ProjectExpertAgent

    ctx = _make_ctx(tmp_path)

    with _patch_agent_common(_fake_query_plain_skill_answer), \
        patch("app.agents.anthropic_client.build_options", return_value=MagicMock()), \
        patch(
            "app.services.skills_service.materialize_relevant_enabled_skills",
            return_value=["skill-verifier"],
        ), \
        patch(
            "app.services.skills_service.enabled_skill_overviews",
            return_value=[{"name": "skill-verifier", "description": ""}],
        ), \
        patch("app.agents.log_analysis.mcp_tools.get_mcp_server", return_value=MagicMock()):
        result = await ProjectExpertAgent().run(ctx)

    assert result["status"] == "ok"
    assert result["question_type"] == "qa"
    assert result["answer"] == "TURQUOISE-MONGOOSE-9"
    assert result["summary"] == "TURQUOISE-MONGOOSE-9"
    assert result["parse_warning"] == "plain_text_skill_answer_wrapped"
    assert result["loaded_skills"] == ["skill-verifier", "humanizer-zh"]


@pytest.mark.asyncio
async def test_service_requires_project_repo_for_new_session():
    from app.services.project_expert_chat_service import ProjectExpertChatService

    service = ProjectExpertChatService()
    events = []
    async for chunk in service.stream(
        message="鉴权在哪里？",
        session_id="session-required",
        history_json=None,
        remember=False,
        project_repo_id=None,
        db=None,
        user=None,
    ):
        event = _decode_sse_event(chunk)
        events.append(event)
        if event.get("event") == "error":
            break

    assert events[-1]["event"] == "error"
    assert events[-1]["reason"] == "project_repo_required"
    assert "session-required" not in service._jobs


@pytest.mark.asyncio
async def test_service_switches_workspace_and_emits_context_on_project_switch(monkeypatch, tmp_path):
    from app.services.project_expert_chat_service import ProjectExpertChatService

    ctx = _make_ctx(tmp_path)
    new_root = tmp_path / "new-project"
    new_root.mkdir()
    new_ctx = _make_ctx(new_root)
    new_ctx.task_id = "task-2"
    meta = {
        "session_id": "session-followup",
        "task_id": ctx.task_id,
        "temp_dir": ctx.temp_dir,
        "repo_dir": ctx.repo_dir,
        "task_json_path": ctx.task_json_path,
        "project_repo_id": 1,
        "project_code": "foo",
        "project_name": "Foo Service",
    }

    class FastAgent:
        def run_sync(self, seen_ctx, _cancel_event=None, _trace_emitter=None, _clarification_binding=None):
            assert seen_ctx.temp_dir == new_ctx.temp_dir
            return {
                "engine": "claude-agent-sdk",
                "model": "fake-model",
                "schema_version": 3,
                "status": "ok",
                "error_kind": None,
                "question_type": "qa",
                "answer": "继续基于已克隆代码回答。",
                "summary": "ok",
                "severity": "info",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
                "tool_trace": [],
                "trace_events": [],
                "trace_summary": {},
                "raw": "ok",
                "duration_seconds": 0.0,
                "token_usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
            }

    service = ProjectExpertChatService()
    monkeypatch.setattr(service, "_load_context", lambda *_a, **_kw: (ctx, meta))
    async def resolve_repo(_db, project_repo_id):
        return _repo(id=project_repo_id, project_code="bar", project_name="Bar Service")

    monkeypatch.setattr(service, "_resolve_project_repo", resolve_repo)
    new_meta = {**meta, "task_id": new_ctx.task_id, "temp_dir": new_ctx.temp_dir,
                "repo_dir": new_ctx.repo_dir, "task_json_path": new_ctx.task_json_path,
                "project_repo_id": 2, "project_code": "bar", "project_name": "Bar Service"}
    monkeypatch.setattr(service, "_create_context_for_project", lambda **_kw: (new_ctx, new_meta))
    monkeypatch.setattr(service, "_save_context", lambda *_a, **_kw: None)
    monkeypatch.setattr(service, "_touch_context", lambda *_a, **_kw: None)
    monkeypatch.setattr(service, "_persist_exchange", lambda *_a, **_kw: None)

    async def fake_history_hint(**_kwargs):
        return ""

    monkeypatch.setattr(service, "_build_history_hint", fake_history_hint)
    monkeypatch.setattr(
        "app.services.project_expert_chat_service.ProjectExpertAgent",
        FastAgent,
    )
    monkeypatch.setattr(
        "app.services.project_expert_chat_service._AGENT_PROGRESS_INTERVAL_SECONDS",
        0.01,
    )

    events = []
    async for chunk in service.stream(
        message="继续看鉴权",
        session_id="session-followup",
        history_json=None,
        remember=False,
        project_repo_id=2,
        db=None,
        user=None,
    ):
        event = _decode_sse_event(chunk)
        events.append(event)
        if event.get("event") == "done":
            break

    notices = [
        event for event in events
        if event.get("event") == "agent_trace" and event.get("type") == "system_notice"
    ]
    assert any(event.get("kind") == "project_switched" for event in notices)
    assert any(event.get("event") == "project_expert_context" and event.get("project_repo_id") == 2 for event in events)
    assert events[-1]["event"] == "done"
    assert service.get_status("session-followup")["project_repo_id"] == 2
    assert service.get_status("session-followup")["status"] == "done"


@pytest.mark.asyncio
async def test_same_project_followup_keeps_workspace(monkeypatch, tmp_path):
    from app.services.project_expert_chat_service import ProjectExpertChatService

    ctx = _make_ctx(tmp_path)
    meta = {"project_repo_id": 1, "task_id": ctx.task_id, "project_code": "foo"}
    service = ProjectExpertChatService()
    monkeypatch.setattr(service, "_load_context", lambda *_a, **_kw: (ctx, meta))

    def unexpected_switch(**_kwargs):
        raise AssertionError("same project must reuse its workspace")

    monkeypatch.setattr(service, "_create_context_for_project", unexpected_switch)

    async def no_history(**_kwargs):
        return ""

    monkeypatch.setattr(service, "_build_history_hint", no_history)

    class FastAgent:
        def run_sync(self, seen_ctx, *_args):
            assert seen_ctx.temp_dir == ctx.temp_dir
            return {"status": "ok", "answer": "ok", "summary": "ok", "model": "fake-model"}

    monkeypatch.setattr("app.services.project_expert_chat_service.ProjectExpertAgent", FastAgent)
    events = [_decode_sse_event(chunk) async for chunk in service.stream(
        message="继续看", session_id="session-same", history_json=None,
        remember=False, project_repo_id=1, db=None, user=None,
    )]
    assert events[-1]["event"] == "done"
    assert service.get_status("session-same")["project_repo_id"] == 1


@pytest.mark.asyncio
async def test_project_switch_replaces_checkout_but_retains_full_transcript(monkeypatch, tmp_path):
    from app.agents.project_expert import workspace
    from app.services.project_expert_chat_service import ProjectExpertChatService

    monkeypatch.setattr(workspace.settings, "code_repo_clone_base_dir", str(tmp_path))
    service = ProjectExpertChatService()
    service.registry_dir = tmp_path / "contexts"
    old_ctx, _ = service._create_context_for_project(
        session_id="same-session", question="LX10 问题", repo=_repo(id=1), user=None,
    )
    (Path(old_ctx.repo_dir) / "old-source.txt").write_text("old", encoding="utf-8")
    (Path(old_ctx.temp_dir) / "related_repos").mkdir()

    new_ctx, meta = service._create_context_for_project(
        session_id="same-session", question="LX06 问题",
        repo=_repo(id=2, project_code="lx06_oam", project_name="LX06 OAM"), user=None,
    )
    assert new_ctx.temp_dir != old_ctx.temp_dir
    assert meta["project_repo_id"] == 2
    assert service._load_context("same-session", user=None)[0].temp_dir == new_ctx.temp_dir
    assert sorted(p.name for p in Path(new_ctx.temp_dir).iterdir()) == ["repo", "task.json"]
    assert not Path(old_ctx.temp_dir).exists()
    assert json.loads(Path(new_ctx.task_json_path).read_text(encoding="utf-8"))["repo_info"]["project_code"] == "lx06_oam"

    history = [{"role": "user", "content": f"历史问题 {n}"} for n in range(20)]
    hint = await service._build_history_hint(
        db=None, user=None, session_id="same-session",
        history_json=json.dumps(history, ensure_ascii=False), workspace_dir=new_ctx.temp_dir,
    )
    service._bind_question_and_hints(new_ctx, question="LX06 问题", hints=hint)
    transcript = json.loads((Path(new_ctx.temp_dir) / "conversation_history.json").read_text(encoding="utf-8"))
    assert len(transcript) == 20
    assert transcript[0]["content"] == "历史问题 0"
    assert "历史问题 0" not in hint
    assert "历史问题 19" in hint
    assert json.loads(Path(new_ctx.task_json_path).read_text(encoding="utf-8"))["conversation_history_file"] == "conversation_history.json"


@pytest.mark.asyncio
async def test_invalid_project_switch_keeps_existing_context(monkeypatch, tmp_path):
    from app.services.project_expert_chat_service import ProjectExpertChatService

    ctx = _make_ctx(tmp_path)
    meta = {"project_repo_id": 1, "task_id": ctx.task_id, "project_code": "foo"}
    service = ProjectExpertChatService()
    monkeypatch.setattr(service, "_load_context", lambda *_a, **_kw: (ctx, meta))

    async def unavailable(_db, _project_repo_id):
        return None

    monkeypatch.setattr(service, "_resolve_project_repo", unavailable)
    events = [_decode_sse_event(chunk) async for chunk in service.stream(
        message="改看另一个项目", session_id="session-invalid", history_json=None,
        remember=False, project_repo_id=2, db=None, user=None,
    )]
    assert events[-1]["reason"] == "project_repo_invalid"
    assert service._load_context("session-invalid", user=None)[0].temp_dir == ctx.temp_dir
    assert not service._jobs.get("session-invalid")


@pytest.mark.asyncio
async def test_project_switch_waits_for_active_run(monkeypatch, tmp_path):
    from app.services.project_expert_chat_service import AgentJob, ProjectExpertChatService

    service = ProjectExpertChatService()
    service._jobs["session-active"] = AgentJob(
        session_id="session-active", task_id="task-active", context_meta={},
        question="old", user_id=None, remember=False, started_at=time.monotonic(),
        project_repo_id=1,
    )
    events = [_decode_sse_event(chunk) async for chunk in service.stream(
        message="新项目问题", session_id="session-active", history_json=None,
        remember=False, project_repo_id=2, db=None, user=None,
    )]
    assert events[-1]["reason"] == "project_switch_while_running"
    assert service._jobs["session-active"].project_repo_id == 1


@pytest.mark.asyncio
async def test_service_cancel_and_result_polling(monkeypatch, tmp_path):
    from app.services.project_expert_chat_service import ProjectExpertChatService

    ctx = _make_ctx(tmp_path)
    meta = {
        "session_id": "session-cancel",
        "task_id": ctx.task_id,
        "temp_dir": ctx.temp_dir,
        "repo_dir": ctx.repo_dir,
        "task_json_path": ctx.task_json_path,
        "project_repo_id": 1,
    }
    captured = {}

    class CancellableAgent:
        def run_sync(self, _ctx, cancel_event=None, _trace_emitter=None, _clarification_binding=None):
            captured["event"] = cancel_event
            for _ in range(200):
                if cancel_event is not None and cancel_event.is_set():
                    break
                time.sleep(0.01)
            return {
                "engine": "claude-agent-sdk",
                "model": "fake-model",
                "schema_version": 3,
                "status": "cancelled",
                "error_kind": "cancelled",
                "question_type": "other",
                "answer": "本轮分析已被用户取消。",
                "summary": "cancelled",
                "severity": "info",
                "root_cause_hypotheses": [],
                "recommended_actions": [],
                "related_keywords": [],
                "tool_trace": [],
                "trace_events": [],
                "trace_summary": {},
                "raw": "cancelled",
                "duration_seconds": 0.0,
                "token_usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
            }

    service = ProjectExpertChatService()
    monkeypatch.setattr(service, "_load_context", lambda *_a, **_kw: (ctx, meta))
    monkeypatch.setattr(service, "_save_context", lambda *_a, **_kw: None)
    monkeypatch.setattr(service, "_touch_context", lambda *_a, **_kw: None)
    monkeypatch.setattr(service, "_persist_exchange", lambda *_a, **_kw: None)

    async def fake_history_hint(**_kwargs):
        return ""

    monkeypatch.setattr(service, "_build_history_hint", fake_history_hint)
    monkeypatch.setattr(
        "app.services.project_expert_chat_service.ProjectExpertAgent",
        CancellableAgent,
    )

    stream = service.stream(
        message="继续看鉴权",
        session_id="session-cancel",
        history_json=None,
        remember=False,
        project_repo_id=1,
        db=None,
        user=None,
    )
    events = []

    async def consume():
        async for chunk in stream:
            event = _decode_sse_event(chunk)
            events.append(event)
            if event.get("event") == "done":
                break

    consumer = asyncio.create_task(consume())
    for _ in range(50):
        if captured.get("event") is not None:
            break
        await asyncio.sleep(0.02)

    assert service.get_status("session-cancel")["status"] == "running"
    assert service.cancel("session-cancel") is True
    await asyncio.wait_for(consumer, timeout=5)

    status = service.get_status("session-cancel")
    assert status["status"] == "done"
    assert status["cancel_requested"] is True
    assert status["result"]["status"] == "cancelled"
    assert events[-1]["event"] == "done"


def test_project_expert_stream_endpoint_contract_has_no_file_parameter():
    from app.api.ai_chat import project_expert_stream_endpoint

    signature = inspect.signature(project_expert_stream_endpoint)
    assert "file" not in signature.parameters
    assert {"message", "session_id", "history", "remember", "project_repo_id"}.issubset(
        signature.parameters
    )
