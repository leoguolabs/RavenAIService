from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _init_git_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Raven Test"], check=True)
    (path / "README.md").write_text("project evidence\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "fixture"], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workspace(path: Path) -> Path:
    path.mkdir()
    (path / "repo").mkdir()
    (path / "task.json").write_text(
        json.dumps(
            {
                "question": "cross project",
                "repo_info": {"project_code": "primary"},
            }
        ),
        encoding="utf-8",
    )
    return path


def _project(source: Path, code: str = "secondary", token: str = "secret-token") -> dict:
    return {
        "project_code": code,
        "project_name": code.title(),
        "project_card": f"{code} project scope",
        "repo_url": str(source),
        "clone_url": str(source),
        "default_branch": "main",
        "token": token,
    }


def test_clone_related_repo_is_safe_persisted_and_reused(monkeypatch, tmp_path):
    from app.agents.log_analysis import mcp_tools

    source = tmp_path / "source"
    expected_commit = _init_git_repo(source)
    workspace = _workspace(tmp_path / "workspace")
    real_run = subprocess.run
    clone_envs = []
    clone_calls = 0

    def recording_run(command, **kwargs):
        nonlocal clone_calls
        if command[:2] == ["git", "clone"]:
            clone_calls += 1
            clone_envs.append(kwargs.get("env", {}))
        return real_run(command, **kwargs)

    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/raven-ssh-agent/agent.sock")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    monkeypatch.setattr(mcp_tools.subprocess, "run", recording_run)

    first = mcp_tools._clone_project_repo_sync(
        project=_project(source),
        workspace_dir=str(workspace),
        primary_project_code="primary",
        max_related_repos=2,
        timeout_seconds=30,
    )
    second = mcp_tools._clone_project_repo_sync(
        project=_project(source),
        workspace_dir=str(workspace),
        primary_project_code="primary",
        max_related_repos=2,
        timeout_seconds=30,
    )

    assert first["status"] == "ok"
    assert first["path"] == "related_repos/secondary"
    assert first["commit_sha"] == expected_commit
    assert first["reused"] is False
    assert second["reused"] is True
    assert clone_calls == 1
    assert clone_envs[0]["SSH_AUTH_SOCK"] == "/run/raven-ssh-agent/agent.sock"
    assert clone_envs[0]["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"

    serialized = json.dumps(first)
    for forbidden in ("repo_url", "clone_url", "secret-token"):
        assert forbidden not in serialized

    manifest = json.loads((workspace / "task.json").read_text(encoding="utf-8"))
    assert manifest["repo_info"] == {"project_code": "primary"}
    assert manifest["related_repos"] == [
        {
            "project_code": "secondary",
            "project_name": "Secondary",
            "project_card": "secondary project scope",
            "path": "related_repos/secondary",
            "default_branch": "main",
            "branch": "main",
            "commit_sha": expected_commit,
            "reused": True,
        }
    ]
    assert "secret-token" not in json.dumps(manifest)


def test_primary_project_clone_keeps_repo_compatibility(tmp_path):
    from app.agents.log_analysis import mcp_tools

    source = tmp_path / "source"
    expected_commit = _init_git_repo(source)
    workspace = _workspace(tmp_path / "workspace")

    payload = mcp_tools._clone_project_repo_sync(
        project=_project(source, code="primary"),
        workspace_dir=str(workspace),
        primary_project_code="primary",
        max_related_repos=1,
        timeout_seconds=30,
    )

    assert payload["path"] == "repo"
    assert payload["commit_sha"] == expected_commit
    assert (workspace / "repo" / ".git").is_dir()
    manifest = json.loads((workspace / "task.json").read_text(encoding="utf-8"))
    assert "related_repos" not in manifest


def test_clone_and_reuse_never_expose_auth_in_git_config(tmp_path, monkeypatch):
    from app.agents.log_analysis import mcp_tools
    source = tmp_path / 'source'
    _init_git_repo(source)
    workspace = _workspace(tmp_path / 'workspace')
    secret_url = 'http://oauth2:fixture-secret@git.example/repo.git'
    project = {**_project(source), 'clone_url': secret_url}
    real_run = subprocess.run

    def clone_locally(command, **kwargs):
        if command[:2] == ['git', 'clone']:
            result = real_run([*command[:-2], str(source), command[-1]], **kwargs)
            real_run(['git', '-C', command[-1], 'remote', 'set-url', 'origin', secret_url], check=True)
            return result
        return real_run(command, **kwargs)

    monkeypatch.setattr(mcp_tools.subprocess, 'run', clone_locally)
    for attempt in range(2):
        if attempt:
            real_run(['git', '-C', str(workspace / 'related_repos/secondary'), 'remote', 'set-url', 'origin', secret_url], check=True)
        result = mcp_tools._clone_project_repo_sync(
            project=project, workspace_dir=str(workspace), primary_project_code='primary',
            max_related_repos=2, timeout_seconds=30,
        )
        assert result['status'] == 'ok'
        checkout = Path(result['absolute_path'])
        assert 'fixture-secret' not in (checkout / '.git/config').read_text()
        remote = real_run(['git', '-C', str(checkout), 'remote', '-v'], check=True, capture_output=True, text=True).stdout
        assert 'http://git.example/repo.git' in remote
        assert 'fixture-secret' not in remote


def test_package_server_exposes_only_discovery_and_safe_clone(tmp_path, monkeypatch):
    from app.agents.log_analysis import mcp_tools
    import claude_agent_sdk
    monkeypatch.setattr(mcp_tools, '_get_servers', lambda: (None, None))
    monkeypatch.setattr(mcp_tools, '_base_tools', [SimpleNamespace(name='discover_projects'), SimpleNamespace(name='lookup_project_repo')])
    monkeypatch.setattr(claude_agent_sdk, 'create_sdk_mcp_server', lambda **kwargs: kwargs)
    server = mcp_tools.get_mcp_server(workspace_dir=str(tmp_path), agent_key='package_search')
    assert [t.name for t in server['tools']] == ['discover_projects', 'clone_project_repo']


def test_related_repo_limit_and_conflict_preserve_existing_data(tmp_path):
    from app.agents.log_analysis import mcp_tools

    source = tmp_path / "source"
    _init_git_repo(source)
    workspace = _workspace(tmp_path / "workspace")
    first = mcp_tools._clone_project_repo_sync(
        project=_project(source, code="first"),
        workspace_dir=str(workspace),
        primary_project_code="primary",
        max_related_repos=1,
        timeout_seconds=30,
    )
    limited = mcp_tools._clone_project_repo_sync(
        project=_project(source, code="second"),
        workspace_dir=str(workspace),
        primary_project_code="primary",
        max_related_repos=1,
        timeout_seconds=30,
    )
    conflict_dir = workspace / "related_repos" / "conflict"
    conflict_dir.mkdir()
    marker = conflict_dir / "keep.txt"
    marker.write_text("do not delete", encoding="utf-8")
    conflict = mcp_tools._clone_project_repo_sync(
        project=_project(source, code="conflict"),
        workspace_dir=str(workspace),
        primary_project_code="primary",
        max_related_repos=2,
        timeout_seconds=30,
    )

    assert first["status"] == "ok"
    assert limited["error"] == "related_repo_limit"
    assert conflict["error"] == "target_conflict"
    assert marker.read_text(encoding="utf-8") == "do not delete"


def test_failed_clone_cleans_partial_and_masks_credentials(monkeypatch, tmp_path):
    from app.agents.log_analysis import mcp_tools

    workspace = _workspace(tmp_path / "workspace")
    project = {
        "project_code": "failed",
        "project_name": "Failed",
        "project_card": "failure fixture",
        "repo_url": "https://git.example/private.git",
        "clone_url": "https://oauth2:super-secret@git.example/private.git",
        "default_branch": "main",
        "token": "super-secret",
    }

    def failed_run(command, **_kwargs):
        partial = Path(command[-1])
        partial.mkdir(parents=True)
        (partial / "partial.txt").write_text("partial", encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            128,
            stdout="",
            stderr=f"fatal: authentication failed for {project['clone_url']}",
        )

    monkeypatch.setattr(mcp_tools.subprocess, "run", failed_run)
    payload = mcp_tools._clone_project_repo_sync(
        project=project,
        workspace_dir=str(workspace),
        primary_project_code="primary",
        max_related_repos=2,
        timeout_seconds=30,
    )

    assert payload["error"] == "authentication_failed"
    serialized = json.dumps(payload)
    assert "super-secret" not in serialized
    assert project["clone_url"] not in serialized
    assert not (workspace / "related_repos" / "failed").exists()
    assert not list((workspace / "related_repos").glob(".failed.partial-*"))


def test_containment_rejects_paths_outside_workspace(tmp_path):
    from app.agents.log_analysis.mcp_tools import _contained_path

    workspace = _workspace(tmp_path / "workspace")
    with pytest.raises(ValueError, match="escapes"):
        _contained_path(workspace, tmp_path / "outside")


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_key", ["project_expert", "package_search"])
async def test_clone_payload_requires_agent_binding(monkeypatch, tmp_path, agent_key):
    from app.agents.log_analysis import mcp_tools
    from app.models.database import db_manager
    from app.services import project_repo_service

    workspace = _workspace(tmp_path / "workspace")
    repo = SimpleNamespace(
        project_code="secondary",
        project_name="Secondary",
        project_card="secondary scope",
        repo_url="ssh://git@example/repo.git",
        default_branch="main",
        git_token=None,
        enabled=True,
    )

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(db_manager, "session_factory", lambda: SessionContext())
    monkeypatch.setattr(
        project_repo_service,
        "get_by_project_code",
        AsyncMock(return_value=repo),
    )
    monkeypatch.setattr(
        project_repo_service,
        "supports_agent",
        AsyncMock(return_value=False),
    )

    payload = await mcp_tools.clone_project_repo_payload(
        project_code="secondary",
        workspace_dir=str(workspace),
        primary_project_code="primary",
        agent_key=agent_key,
    )

    assert payload == {
        "error": "agent_not_enabled",
        "project_code": "secondary",
        "agent_key": agent_key,
    }
    assert not (workspace / "related_repos").exists()
