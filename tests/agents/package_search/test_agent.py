"""Tests for the rebuilt (project-bound) PackageSearchAgent.

We stub the SDK loop (``PackageSearchAgent._run_sdk_loop``) with a
hand-rolled async generator that yields fake ``AssistantMessage`` /
``ResultMessage`` objects, then assert:

- the agent emits ``run_start`` / ``step_start`` / ``step_end`` /
  ``run_complete`` trace events in the right order;
- fenced JSON is parsed and IDs are validated against the real
  ``RavenPackageService.get_package`` lookup **scoped to the run's
  project** — cross-project IDs are dropped like hallucinated ones;
- invalid / hallucinated IDs are filtered, with a warning entry
  appended to ``tool_trace``;
- when the model omits the fenced block, the warning says
  ``missing structured answer`` and the ID arrays are empty;
- when the fenced block exists but is malformed, the warning says
  ``unparsable structured answer`` and the ID arrays are empty;
- a set ``cancel_event`` terminates the loop at the next message
  boundary with a ``cancelled`` result + trace event.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from app.agents.package_search.agent import PackageSearchAgent
from app.agents.package_search.workspace import WorkspaceContext


PROJECT = "proj-a"


# ──────────────── fake SDK message blocks ────────────────


class _ToolUseBlock:
    def __init__(self, *, name: str, tool_input: dict, block_id: str = "tool-1") -> None:
        self.name = name
        self.input = tool_input
        self.id = block_id


class _ToolResultBlock:
    def __init__(self, *, tool_use_id: str, text: str, is_error: bool = False) -> None:
        self.tool_use_id = tool_use_id
        self.content = [{"type": "text", "text": text}]
        self.is_error = is_error


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _Message:
    def __init__(self, blocks: list, usage: Any = None) -> None:
        self.content = blocks
        if usage is not None:
            self.usage = usage


class _ResultMessage:
    """Fake terminal ResultMessage: carries the final answer text."""

    def __init__(self, result: str) -> None:
        self.content = None
        self.result = result


class _Usage:
    def __init__(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = 0


# ──────────────── fixtures ────────────────


@pytest.fixture
def stub_service(monkeypatch):
    """Stub ``raven_package_service.get_package`` so we don't depend on disk.

    ``pkg-real-*`` belong to the run's project; ``pkg-other`` is a real
    package of *another* project (must be intercepted by the agent).
    """
    from app.services import raven_package_service as svc_module

    catalog = {
        "pkg-real-1": {"id": "pkg-real-1", "projectCode": PROJECT},
        "pkg-real-2": {"id": "pkg-real-2", "projectCode": PROJECT},
        "pkg-real-3": {"id": "pkg-real-3", "projectCode": PROJECT},
        "pkg-other": {"id": "pkg-other", "projectCode": "proj-b"},
    }

    def fake_get_package(pid):
        return catalog.get(pid)

    monkeypatch.setattr(svc_module.raven_package_service, "get_package", fake_get_package)
    return catalog


@pytest.fixture
def stub_options(monkeypatch):
    """Bypass ``_build_options`` so the test does not touch ClaudeAgentOptions."""
    def fake_build(self, *, system_prompt, project_code, cwd, endpoint=None, clarification=None):
        return (object(), "fake-model", "fake-provider")

    monkeypatch.setattr(PackageSearchAgent, "_build_options", fake_build)


def _make_ctx(tmp_path, *, project_code: str = PROJECT, question: str = "q") -> WorkspaceContext:
    return WorkspaceContext(
        task_id="task-1",
        temp_dir=str(tmp_path),
        repo_dir=str(tmp_path / "repo"),
        task_json_path=str(tmp_path / "task.json"),
        project_code=project_code,
        metadata={"question": question, "hints": ""},
    )


def _make_agent(messages: List[Any]) -> PackageSearchAgent:
    """Return an agent whose ``_run_sdk_loop`` yields the canned messages."""
    agent = PackageSearchAgent()

    async def fake_loop(self, prompt, options):
        for m in messages:
            yield m

    agent._run_sdk_loop = fake_loop.__get__(agent, PackageSearchAgent)  # type: ignore[method-assign]
    return agent


# ──────────────── tests ────────────────


def test_run_parses_fenced_json_and_validates_ids(stub_service, stub_options, tmp_path):
    answer_text = (
        "Here are the matches.\n"
        "```json\n"
        '{"recommended_package_ids": ["pkg-real-1"],'
        ' "relevant_package_ids": ["pkg-real-1", "pkg-real-2"],'
        ' "notes": "best matches by name"}\n'
        "```\n"
    )
    messages = [
        _Message([
            _ToolUseBlock(name="mcp__package_search__list_packages",
                          tool_input={"limit": 5}, block_id="tu1"),
        ], usage=_Usage(input_tokens=10, output_tokens=5)),
        _Message([
            _ToolResultBlock(tool_use_id="tu1", text='{"total": 1, "items": []}'),
        ]),
        _ResultMessage(answer_text),
    ]
    agent = _make_agent(messages)
    result = asyncio.run(agent.run(_make_ctx(tmp_path, question="find katx")))

    assert result["status"] == "ok"
    assert result["recommended_package_ids"] == ["pkg-real-1"]
    assert result["relevant_package_ids"] == ["pkg-real-1", "pkg-real-2"]
    assert result["notes"] == "best matches by name"
    assert result["model"] == "fake-model"
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 5
    assert result["session_id"] == "task-1"

    # tool_trace MUST contain one entry per step_start
    names = [entry.get("name") for entry in result["tool_trace"] if "name" in entry]
    assert names == ["mcp__package_search__list_packages"]

    # No warning entry when everything validates
    warnings = [e for e in result["tool_trace"] if e.get("type") == "warning"]
    assert warnings == []

    # Trace event ordering
    types = [e.get("type") for e in result["trace_events"]]
    assert types.index("run_start") < types.index("step_start")
    assert types.index("step_start") < types.index("step_end")
    assert types[-1] == "run_complete"


def test_run_accumulates_dict_usage_payload(stub_service, stub_options, tmp_path):
    """Claude-compatible providers may expose SDK usage as a plain dict."""
    answer_text = (
        "```json\n"
        '{"recommended_package_ids": ["pkg-real-1"], "relevant_package_ids": []}\n'
        "```\n"
    )
    messages = [
        _Message(
            [_TextBlock("thinking out loud")],
            usage={
                "prompt_tokens": 33,
                "completion_tokens": 7,
                "cache_read_input_tokens": 11,
                "cache_creation_input_tokens": 5,
            },
        ),
        _ResultMessage(answer_text),
    ]
    agent = _make_agent(messages)
    result = asyncio.run(agent.run(_make_ctx(tmp_path)))

    assert result["usage"] == {
        "input_tokens": 33,
        "output_tokens": 7,
        "cache_read_tokens": 11,
        "cache_write_tokens": 5,
    }


def test_run_filters_invalid_ids_and_emits_warning(stub_service, stub_options, tmp_path):
    answer_text = (
        "```json\n"
        '{"recommended_package_ids": ["pkg-real-1", "fake-id-x"],'
        ' "relevant_package_ids": ["pkg-real-2", "another-fake"]}\n'
        "```\n"
    )
    agent = _make_agent([_ResultMessage(answer_text)])
    result = asyncio.run(agent.run(_make_ctx(tmp_path)))

    assert result["recommended_package_ids"] == ["pkg-real-1"]
    assert result["relevant_package_ids"] == ["pkg-real-2"]
    warnings = [e for e in result["tool_trace"] if e.get("type") == "warning"]
    assert len(warnings) == 1
    assert "filtered 2 invalid ids" in warnings[0]["message"]


def test_run_intercepts_cross_project_ids(stub_service, stub_options, tmp_path):
    """A real package ID belonging to another project is dropped like a fake."""
    answer_text = (
        "```json\n"
        '{"recommended_package_ids": ["pkg-real-1", "pkg-other"],'
        ' "relevant_package_ids": ["pkg-other"]}\n'
        "```\n"
    )
    agent = _make_agent([_ResultMessage(answer_text)])
    result = asyncio.run(agent.run(_make_ctx(tmp_path, project_code=PROJECT)))

    assert result["recommended_package_ids"] == ["pkg-real-1"]
    assert result["relevant_package_ids"] == []
    warnings = [e for e in result["tool_trace"] if e.get("type") == "warning"]
    assert any("filtered 2 invalid ids" in w["message"] for w in warnings)


def test_run_warns_on_missing_fenced_block(stub_service, stub_options, tmp_path):
    agent = _make_agent([_ResultMessage("Sorry, I cannot answer.")])
    result = asyncio.run(agent.run(_make_ctx(tmp_path)))

    assert result["recommended_package_ids"] == []
    assert result["relevant_package_ids"] == []
    assert result["answer"] == "Sorry, I cannot answer."
    warnings = [e for e in result["tool_trace"] if e.get("type") == "warning"]
    assert warnings == [{"type": "warning", "message": "missing structured answer"}]


def test_run_warns_on_unparsable_fenced_block(stub_service, stub_options, tmp_path):
    answer_text = "```json\n{this is not valid JSON}\n```"
    agent = _make_agent([_ResultMessage(answer_text)])
    result = asyncio.run(agent.run(_make_ctx(tmp_path)))

    assert result["recommended_package_ids"] == []
    assert result["relevant_package_ids"] == []
    warnings = [e for e in result["tool_trace"] if e.get("type") == "warning"]
    assert warnings == [{"type": "warning", "message": "unparsable structured answer"}]


def test_run_warns_when_fields_wrong_type(stub_service, stub_options, tmp_path):
    """If ``recommended_package_ids`` is not a list, treat as unparsable."""
    answer_text = (
        "```json\n"
        '{"recommended_package_ids": "pkg-real-1",'
        ' "relevant_package_ids": null}\n'
        "```\n"
    )
    agent = _make_agent([_ResultMessage(answer_text)])
    result = asyncio.run(agent.run(_make_ctx(tmp_path)))

    assert result["recommended_package_ids"] == []
    assert result["relevant_package_ids"] == []
    warnings = [e for e in result["tool_trace"] if e.get("type") == "warning"]
    assert warnings[-1]["message"] == "unparsable structured answer"


def test_cancel_event_terminates_run(stub_service, stub_options, tmp_path):
    """A pre-set cancel_event stops the loop at the first message boundary."""
    messages = [
        _Message([
            _ToolUseBlock(name="mcp__package_search__list_packages",
                          tool_input={}, block_id="tu1"),
        ]),
        _ResultMessage("should never be reached"),
    ]
    agent = _make_agent(messages)
    cancel_event = threading.Event()
    cancel_event.set()
    result = asyncio.run(agent.run(_make_ctx(tmp_path), cancel_event=cancel_event))

    assert result["status"] == "cancelled"
    assert result["recommended_package_ids"] == []
    assert result["relevant_package_ids"] == []
    assert result["answer"] == ""
    types = [e.get("type") for e in result["trace_events"]]
    # Two-phase cancel: a system_notice first, then the terminal event.
    assert "system_notice" in types
    assert types[-1] == "cancelled"


def test_trace_emitter_receives_events(stub_service, stub_options, tmp_path):
    """Every trace event is also pushed to the injected trace_emitter."""
    answer_text = (
        "```json\n"
        '{"recommended_package_ids": [], "relevant_package_ids": []}\n'
        "```\n"
    )
    agent = _make_agent([_ResultMessage(answer_text)])
    seen: list[dict] = []
    result = asyncio.run(
        agent.run(_make_ctx(tmp_path), trace_emitter=seen.append)
    )
    assert [e.get("type") for e in seen] == [
        e.get("type") for e in result["trace_events"]
    ]
    assert seen[0]["type"] == "run_start"


@pytest.mark.parametrize("selected_by_relevance", [True, False])
def test_agent_materializes_skills_layers_prompts_and_reuses_trace_seq(
    stub_service,
    monkeypatch,
    tmp_path,
    selected_by_relevance,
):
    from app.agents.log_analysis.trace import SeqCounter
    from app.services import skills_service

    ctx = _make_ctx(tmp_path, question="请制作整包")
    Path(ctx.task_json_path).write_text(
        json.dumps(
            {
                "question": "请制作整包",
                "repo_info": {
                    "project_code": PROJECT,
                    "repo_url": "",
                },
                "packaging_requested": True,
                "inputs_manifest": "package_inputs/manifest.json",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    shared_counter = SeqCounter()
    assert shared_counter.next() == 1
    assert shared_counter.next() == 2
    ctx.metadata["trace_seq_counter"] = shared_counter

    materialize = MagicMock(return_value=["full-package-build"] if selected_by_relevance else [])
    materialize_packaging = MagicMock(return_value=["full-package-build"])
    overviews = MagicMock(
        return_value=[
            {
                "name": "full-package-build",
                "description": "分类并构建已确认的软件升级整包",
            },
            {"name": "package-audit", "description": "只读审计版本包"},
        ]
    )
    monkeypatch.setattr(
        skills_service, "materialize_relevant_enabled_skills", materialize
    )
    monkeypatch.setattr(skills_service, "materialize_enabled_skills", materialize_packaging)
    monkeypatch.setattr(skills_service, "enabled_skill_overviews", overviews)

    captured: dict[str, str] = {}

    def fake_build(
        self,
        *,
        system_prompt,
        project_code,
        cwd,
        endpoint=None,
        clarification=None,
    ):
        captured["system_prompt"] = system_prompt
        return object(), "fake-model", "fake-provider"

    monkeypatch.setattr(PackageSearchAgent, "_build_options", fake_build)
    agent = PackageSearchAgent()

    async def fake_loop(self, prompt, options):
        captured["user_prompt"] = prompt
        yield _ResultMessage(
            "```json\n"
            '{"recommended_package_ids": [], "relevant_package_ids": []}\n'
            "```"
        )

    agent._run_sdk_loop = fake_loop.__get__(agent, PackageSearchAgent)  # type: ignore[method-assign]
    clarification_binding = MagicMock()
    clarification_binding.setup.return_value = None
    result = asyncio.run(agent.run(ctx, clarification_binding=clarification_binding))

    assert clarification_binding.setup.call_args.kwargs["workflow_agent"] is False
    materialize.assert_called_once()
    assert materialize.call_args.args == ("package_search", ctx.temp_dir)
    assert materialize.call_args.kwargs["project_code"] == PROJECT
    assert "请制作整包" in materialize.call_args.kwargs["query_text"]
    if selected_by_relevance:
        materialize_packaging.assert_not_called()
    else:
        materialize_packaging.assert_called_once_with(
            "package_search", ctx.temp_dir, skill_names=["full-package-build"], project_code=PROJECT,
        )
    overviews.assert_called_once_with("package_search", project_code=PROJECT)
    assert "可用的 Skill（按需加载）" in captured["system_prompt"]
    assert "可用的 Skill（按需加载）" in captured["user_prompt"]
    assert "`full-package-build`：分类并构建已确认的软件升级整包" in (
        captured["user_prompt"]
    )
    assert "Agent 执行打包" in captured["system_prompt"]
    assert "Bash/Python" in captured["system_prompt"]
    assert "未关联代码仓库" in captured["system_prompt"]

    run_start = next(event for event in result["trace_events"] if event["type"] == "run_start")
    skills_notice = next(
        event
        for event in result["trace_events"]
        if event["type"] == "system_notice" and event.get("kind") == "skills_available"
    )
    assert run_start["seq"] == 3
    assert run_start["available_skills"] == ["full-package-build", "package-audit"]
    assert run_start["loaded_skills"] == ["full-package-build"]
    assert skills_notice["seq"] == 4
    assert skills_notice["available_skills"] == ["full-package-build", "package-audit"]
    assert skills_notice["loaded_skills"] == ["full-package-build"]
    assert result["loaded_skills"] == ["full-package-build"]
    assert shared_counter.value == max(event["seq"] for event in result["trace_events"])


def test_packaging_options_enable_code_execution_and_bound_publication(
    monkeypatch,
    tmp_path,
):
    from app.agents.anthropic_client import PROVIDER_PROFILES
    from app.agents.package_search.agent import (
        ALLOWED_TOOLS,
        PACKAGING_DISALLOWED_TOOLS,
    )
    from app.agents.package_search.agent_package_mcp import SDK_TOOL_NAME

    Path(tmp_path / "task.json").write_text(
        json.dumps(
            {
                "package_mode": "build",
                "confirmed_plan_path": "package_plan/confirmed-plan.json",
                "run_id": "run-1",
                "session_id": "session-1",
                "user_id": "user-1",
            }
        ),
        encoding="utf-8",
    )
    skill_dir = tmp_path / ".claude" / "skills" / "full-package-build"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: full-package-build\ndescription: build\n---\nbody\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_build_options(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        "app.agents.anthropic_client.build_options",
        fake_build_options,
    )
    monkeypatch.setattr(
        "app.agents.log_analysis.mcp_tools.get_mcp_server",
        lambda **kwargs: object(),
    )
    package_builder_server = object()
    get_package_builder_server = MagicMock(return_value=package_builder_server)
    monkeypatch.setattr(
        "app.agents.package_search.mcp_tools.get_mcp_server",
        lambda _project_code: object(),
    )
    monkeypatch.setattr(
        "app.agents.package_search.agent_package_mcp.get_mcp_server",
        get_package_builder_server,
    )
    endpoint = SimpleNamespace(
        provider="anthropic",
        profile=PROVIDER_PROFILES["anthropic"],
        model="claude-test",
    )

    options, model, provider = PackageSearchAgent()._build_options(
        system_prompt="system",
        project_code=PROJECT,
        cwd=str(tmp_path),
        endpoint=endpoint,
        packaging_publisher=sentinel,
    )

    assert options is sentinel
    assert (model, provider) == ("claude-test", "anthropic")
    assert set(PACKAGING_DISALLOWED_TOOLS) <= set(captured["disallowed_tools"])
    assert set(PACKAGING_DISALLOWED_TOOLS).isdisjoint(captured["allowed_tools"])
    assert "Skill" in captured["allowed_tools"]
    assert SDK_TOOL_NAME in captured["allowed_tools"]
    assert "Bash" in ALLOWED_TOOLS  # ordinary package search keeps Git access
    assert {"Bash", "Write", "Edit"} <= set(captured["allowed_tools"])
    assert {"Bash", "Write", "Edit"}.isdisjoint(captured["disallowed_tools"])
    assert captured["setting_sources"] == ["project"]
    assert captured["mcp_servers"]["agent_package"] is package_builder_server
    get_package_builder_server.assert_called_once_with(sentinel)


@pytest.mark.parametrize(
    ("task_data", "expected"),
    [
        ({"inputs_manifest": None}, False),
        ({"inputs_manifest": "package_inputs/manifest.json"}, True),
        ({"draft_plan_path": "package_plan/draft-plan.json"}, True),
        ({"confirmed_plan": {"plan_hash": "abc"}}, True),
        ({"package_plan": {"confirmed_path": "confirmed-plan.json"}}, True),
        ({"packaging_requested": True}, True),
    ],
)
def test_packaging_task_detection(task_data, expected):
    from app.agents.package_search.agent import _is_packaging_task

    assert _is_packaging_task(task_data) is expected


def test_stream_yields_events_and_final(stub_service, stub_options, tmp_path):
    answer_text = (
        "```json\n"
        '{"recommended_package_ids": ["pkg-real-1"], "relevant_package_ids": []}\n'
        "```\n"
    )
    messages = [
        _Message([
            _ToolUseBlock(name="mcp__package_search__list_packages",
                          tool_input={"limit": 5}, block_id="tu1"),
        ]),
        _Message([
            _ToolResultBlock(tool_use_id="tu1", text='{"total": 0, "items": []}'),
        ]),
        _ResultMessage(answer_text),
    ]
    agent = _make_agent(messages)
    ctx = _make_ctx(tmp_path)

    async def collect():
        out: list[dict] = []
        async for event in agent.stream(ctx):
            out.append(event)
        return out

    events = asyncio.run(collect())
    types = [e.get("type") for e in events]
    assert "run_start" in types
    assert "step_start" in types
    assert "step_end" in types
    assert "run_complete" in types
    assert types[-1] == "final"
    final = events[-1]
    assert final["data"]["recommended_package_ids"] == ["pkg-real-1"]
    assert final["data"]["relevant_package_ids"] == []


def test_packaging_prompts_skip_metadata_first_and_commit_history_gate():
    from app.agents.package_search.prompts import get_prompts
    system, user = get_prompts(locale="zh", packaging=True)
    assert "亲自调查并执行软件打包" in system
    assert "PublishPackage" in system
    assert "仅当提交记录不足以回答时才读源码" not in system
    assert "先判断问题是否仅凭包元数据工具即可回答" not in user
