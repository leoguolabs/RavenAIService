"""Claude Agent SDK in-process MCP tools for registered project repositories.

``discover_projects`` 只返回可公开的项目卡片目录；``lookup_project_repo``
保留原有的仓库解析兼容接口；工作区绑定的 ``clone_project_repo`` 在服务端完成
鉴权与克隆，只向模型返回无凭据的项目身份、工作区路径和提交信息。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

_server = None  # full project server, lazily created
_discovery_server = None  # discovery-only view for GeneralAgent
_base_tools = None  # discover + lookup tool objects used by bound servers

PROJECT_DISCOVERY_MCP_TOOL = "mcp__project_repo__discover_projects"
PROJECT_REPO_LOOKUP_MCP_TOOL = "mcp__project_repo__lookup_project_repo"
PROJECT_REPO_CLONE_MCP_TOOL = "mcp__project_repo__clone_project_repo"


def build_project_fit_guidance(
    *,
    workflow_name: str,
    project_name: Optional[str],
    project_code: Optional[str],
    project_card: Optional[str],
    catalog_available: bool,
    locale: str = "zh",
    selected_project_is_target: bool = False,
    catalog_preloaded: bool = False,
) -> str:
    """Build the shared, high-priority project-fit policy for Agent prompts."""
    if locale == "en":
        selected = (
            f"Current project: {project_name or 'unknown'} ({project_code or 'unknown'})\n"
            f"Current project card: {project_card or 'Scope unavailable; do not infer a confident match'}"
        )
        if catalog_available:
            evidence_rule = (
                "Before cloning or using project-specific evidence, read the complete enabled catalog in the startup snapshot."
                if catalog_preloaded else
                "Before cloning or using project-specific evidence, call "
                "`mcp__project_repo__discover_projects` and read the complete enabled catalog."
            )
            catalog_rule = (
                "For every eligible additional project materially required by the question, call "
                "`mcp__project_repo__clone_project_repo` with its project_code, then inspect only the returned path."
            )
        else:
            evidence_rule = (
                "Project catalog and workspace-bound clone tools are unavailable for this provider; "
                "you can evaluate only the persisted current project card and repository."
            )
            catalog_rule = (
                "Do not claim that a specific alternative exists, that no project exists, or that multi-project analysis was performed."
            )
        mismatch_rule = (
            "- If the selected project is clearly unrelated, explain the mismatch and ask the user to select the correct project; do not silently answer for a different project.\n"
            if selected_project_is_target else
            "- If the current project is clearly unrelated and another card clearly matches, do not use the selected repository, project prompt, or project Skills as evidence. Clone the matching project in this workspace and answer from that checkout.\n"
        )
        return (
            "\n\n## Project fit and multi-project investigation (highest priority)\n"
            f"This is a prerequisite for {workflow_name}. {selected}\n"
            f"- {evidence_rule}\n"
            f"{mismatch_rule}"
            "- If the question genuinely spans multiple cards, clone only the additional projects materially required and cite every finding with its project and returned repository path.\n"
            "- If the current project fully covers the question, keep the existing single-project workflow and do not clone unrelated catalog entries.\n"
            "- If the complete catalog has no match, state that no suitable project is registered. If evidence is ambiguous, explain the ambiguity and ask for clarification instead of cloning speculative projects.\n"
            f"- {catalog_rule}\n"
            "- A related project's card and cloned source are evidence; the current session's project-specific prompt and Skills are not automatically transferred to that project."
        )

    selected = (
        f"当前项目：{project_name or '未知'}（{project_code or 'unknown'}）\n"
        f"当前项目卡片：{project_card or '项目范围未提供，不能据此作出确定匹配'}"
    )
    if catalog_available:
        evidence_rule = (
            "在克隆仓库、加载项目专属知识或作出领域结论之前，必须先阅读启动快照中完整的已启用项目卡片目录。"
            if catalog_preloaded else
            "在克隆仓库、加载项目专属知识或作出领域结论之前，必须先调用 "
            "`mcp__project_repo__discover_projects` 读取完整的已启用项目卡片目录。"
        )
        catalog_rule = (
            "每一个符合项目边界、确实需要追加的项目，都必须调用 `mcp__project_repo__clone_project_repo`，"
            "并只检查工具返回的工作区路径。"
        )
    else:
        evidence_rule = (
            "当前运行时无法调用项目目录工具；只能使用当前项目卡片判断当前项目是否明显不匹配。"
        )
        catalog_rule = (
            "不得声称某个替代项目一定存在，也不得断言整个系统没有合适项目。"
        )
    mismatch_rule = (
        "- 如果所选项目明确不匹配，应说明并请用户改选正确项目；不得悄悄改用其他项目作为本轮分析对象。\n"
        if selected_project_is_target else
        "- 如果当前项目明确不匹配、另一个项目卡片明确匹配，不得把当前仓库、项目提示词或项目 Skill 当作证据；应在当前工作区克隆匹配项目并从该代码检出中作答。\n"
    )
    return (
        "\n\n## 项目适配性检查（最高优先级）\n"
        f"这是 {workflow_name} 的前置安全检查。{selected}\n"
        f"- {evidence_rule}\n"
        f"{mismatch_rule}"
        "- 如果问题确实跨越多个项目卡片，只克隆完成问题所必需的追加项目；每条结论必须标明项目及工具返回的仓库路径。\n"
        "- 如果当前项目已完整覆盖问题，继续单项目流程，不要因为目录里存在其他项目就无关克隆。\n"
        "- 如果完整目录中没有任何项目卡片匹配，必须明确回答“当前系统还没有适合回答这个问题的项目”，不要勉强挑选最接近但无关的项目。\n"
        "- 如果存在多个可能项目或证据不足，明确说明不确定性并请用户补充信息，不得制造唯一结论或试探性克隆无关项目。\n"
        f"- {catalog_rule}\n"
        "- 追加项目的项目卡片和已克隆源码可以作为证据；当前会话的项目专属提示词与 Skill 不会自动转移到追加项目。"
    )


def build_clone_url(repo_url: str, token: Optional[str]) -> str:
    """Inject a git token into an HTTPS clone URL. Public — also used by
    workspace pre-resolution for providers that don't support MCP tools.
    """
    if not token:
        return repo_url
    try:
        parsed = urlparse(repo_url)
        if parsed.scheme in ("http", "https") and not parsed.username:
            netloc = f"oauth2:{token}@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass
    return repo_url


_build_clone_url = build_clone_url  # backward-compat alias


def _mask_clone_url(url: str) -> str:
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", url, flags=re.IGNORECASE)


def _tool_content(payload: dict) -> dict:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
        ]
    }


def _safe_project_slug(project_code: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", project_code.strip().lower())
    normalized = normalized.strip("._-")
    if normalized:
        return normalized[:120]
    digest = hashlib.sha256(project_code.encode("utf-8")).hexdigest()[:16]
    return f"project-{digest}"


def _contained_path(workspace: Path, candidate: Path) -> Path:
    workspace = workspace.resolve()
    resolved = candidate.resolve()
    if resolved == workspace or workspace not in resolved.parents:
        raise ValueError("path escapes the bound workspace")
    return resolved


def _sanitize_git_error(
    text: str,
    *,
    repo_url: str,
    clone_url: str,
    token: str,
) -> str:
    sanitized = str(text or "")
    for secret in (clone_url, repo_url, token):
        if secret:
            sanitized = sanitized.replace(secret, "***")
    sanitized = _mask_clone_url(sanitized)
    sanitized = " ".join(sanitized.split())
    return sanitized[:800] or "git clone failed without diagnostic output"


def _clone_error_kind(message: str) -> str:
    lowered = message.lower()
    if "host key verification failed" in lowered:
        return "host_key_verification_failed"
    if "permission denied" in lowered or "authentication failed" in lowered:
        return "authentication_failed"
    if "could not resolve host" in lowered or "connection timed out" in lowered:
        return "network_error"
    return "clone_failed"


def _git_checkout_identity(target: Path) -> tuple[str, str]:
    branch_result = subprocess.run(
        ["git", "-C", str(target), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    commit_result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if commit_result.returncode != 0:
        raise RuntimeError("unable to read cloned repository commit")
    return branch_result.stdout.strip(), commit_result.stdout.strip()


def _remove_checkout_credentials(target: Path) -> None:
    """Git clone stores its authenticated URL; never hand that to code tools."""
    result = subprocess.run(
        ["git", "-C", str(target), "config", "--local", "--get-regexp", r"^remote\..*\.(url|pushurl)$"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("Unable to inspect checkout remote configuration")
    for line in result.stdout.splitlines():
        key, url = line.split(None, 1)
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or "@" not in parsed.netloc:
            continue
        clean = urlunparse(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[1]))
        update = subprocess.run(
            ["git", "-C", str(target), "config", "--local", "--fixed-value", "--replace-all", key, clean, url],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if update.returncode:
            raise RuntimeError("Unable to remove checkout remote credentials")


def _persist_related_repo_manifest(
    *,
    workspace: Path,
    project: dict,
    target: Path,
    branch: str,
    commit_sha: str,
    reused: bool,
) -> None:
    task_path = _contained_path(workspace, workspace / "task.json")
    if not task_path.is_file():
        raise RuntimeError("task.json is missing from the bound workspace")

    data = json.loads(task_path.read_text(encoding="utf-8"))
    entries = data.get("related_repos")
    if not isinstance(entries, list):
        entries = []
    entry = {
        "project_code": project["project_code"],
        "project_name": project["project_name"],
        "project_card": project["project_card"],
        "path": target.relative_to(workspace).as_posix(),
        "default_branch": project["default_branch"],
        "branch": branch,
        "commit_sha": commit_sha,
        "reused": reused,
    }
    data["related_repos"] = [
        existing
        for existing in entries
        if not isinstance(existing, dict)
        or existing.get("project_code") != project["project_code"]
    ] + [entry]

    temp_path = _contained_path(
        workspace,
        task_path.with_name(f".{task_path.name}.tmp-{uuid.uuid4().hex}"),
    )
    try:
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temp_path, task_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _clone_project_repo_sync(
    *,
    project: dict,
    workspace_dir: str,
    primary_project_code: Optional[str],
    max_related_repos: int,
    timeout_seconds: int,
) -> dict:
    workspace = Path(workspace_dir).resolve()
    if not workspace.is_dir():
        return {"error": "invalid_workspace", "message": "bound workspace does not exist"}

    project_code = project["project_code"]
    is_primary = bool(
        primary_project_code
        and project_code.strip().lower() == primary_project_code.strip().lower()
    )
    if is_primary:
        target = _contained_path(workspace, workspace / "repo")
    else:
        related_root = _contained_path(workspace, workspace / "related_repos")
        target = _contained_path(
            workspace, related_root / _safe_project_slug(project_code)
        )

    if (target / ".git").is_dir():
        try:
            _remove_checkout_credentials(target)
            branch, commit_sha = _git_checkout_identity(target)
            if not is_primary:
                _persist_related_repo_manifest(
                    workspace=workspace,
                    project=project,
                    target=target,
                    branch=branch,
                    commit_sha=commit_sha,
                    reused=True,
                )
        except Exception as exc:  # noqa: BLE001
            return {"error": "invalid_checkout", "message": str(exc)[:300]}
        return {
            "status": "ok",
            "project_code": project_code,
            "project_name": project["project_name"],
            "project_card": project["project_card"],
            "path": target.relative_to(workspace).as_posix(),
            "absolute_path": str(target),
            "default_branch": project["default_branch"],
            "branch": branch,
            "commit_sha": commit_sha,
            "reused": True,
        }

    if target.exists() and (
        not target.is_dir() or any(target.iterdir())
    ):
        return {
            "error": "target_conflict",
            "project_code": project_code,
            "message": "target path contains non-Git data and was not modified",
        }

    if not is_primary:
        related_root.mkdir(parents=True, exist_ok=True)
        existing = sum(
            1
            for child in related_root.iterdir()
            if child.is_dir() and (child / ".git").is_dir()
        )
        if existing >= max(0, max_related_repos):
            return {
                "error": "related_repo_limit",
                "project_code": project_code,
                "message": f"workspace related repository limit reached ({max_related_repos})",
            }

    partial = _contained_path(
        workspace,
        target.with_name(f".{target.name}.partial-{uuid.uuid4().hex}"),
    )
    clone_url = project["clone_url"]
    repo_url = project["repo_url"]
    token = project["token"]
    command = ["git", "clone", "--depth", "1"]
    if project["default_branch"]:
        command.extend(["--branch", project["default_branch"]])
    command.extend(["--", clone_url, str(partial)])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1, timeout_seconds),
            env=os.environ.copy(),
        )
        if result.returncode != 0:
            message = _sanitize_git_error(
                "\n".join(part for part in (result.stderr, result.stdout) if part),
                repo_url=repo_url,
                clone_url=clone_url,
                token=token,
            )
            return {
                "error": _clone_error_kind(message),
                "project_code": project_code,
                "message": message,
                "exit_code": result.returncode,
            }

        _remove_checkout_credentials(partial)
        branch, commit_sha = _git_checkout_identity(partial)
        if target.exists():
            target.rmdir()  # only the empty placeholder created by workspace setup
        partial.replace(target)
        if not is_primary:
            _persist_related_repo_manifest(
                workspace=workspace,
                project=project,
                target=target,
                branch=branch,
                commit_sha=commit_sha,
                reused=False,
            )
        return {
            "status": "ok",
            "project_code": project_code,
            "project_name": project["project_name"],
            "project_card": project["project_card"],
            "path": target.relative_to(workspace).as_posix(),
            "absolute_path": str(target),
            "default_branch": project["default_branch"],
            "branch": branch,
            "commit_sha": commit_sha,
            "reused": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "error": "clone_timeout",
            "project_code": project_code,
            "message": f"git clone exceeded {max(1, timeout_seconds)} seconds",
        }
    except Exception as exc:  # noqa: BLE001
        message = _sanitize_git_error(
            str(exc), repo_url=repo_url, clone_url=clone_url, token=token
        )
        return {
            "error": "clone_failed",
            "project_code": project_code,
            "message": message,
        }
    finally:
        if partial.exists():
            shutil.rmtree(partial, ignore_errors=True)


async def clone_project_repo_payload(
    *,
    project_code: str,
    workspace_dir: str,
    primary_project_code: Optional[str],
    agent_key: str,
) -> dict:
    """Resolve and clone one registered project without exposing credentials."""
    from app.config import settings
    from app.models.database import db_manager
    from app.services import project_repo_service

    code = (project_code or "").strip()
    if not code:
        return {"error": "invalid_input", "message": "project_code is required"}
    if agent_key not in {"project_expert", "log_analysis", "package_search"}:
        return {"error": "agent_not_allowed", "project_code": code}

    async with db_manager.session_factory() as db:
        repo = await project_repo_service.get_by_project_code(
            db, code, require_repo=False
        )
        if repo is None:
            return {"error": "not_found", "project_code": code}
        if not project_repo_service.has_repo(repo):
            return {"error": "repo_not_configured", "project_code": repo.project_code}
        if not await project_repo_service.supports_agent(db, repo, agent_key):
            return {
                "error": "agent_not_enabled",
                "project_code": repo.project_code,
                "agent_key": agent_key,
            }
        token = repo.git_token or settings.code_repo_git_token or ""
        project = {
            "project_code": repo.project_code,
            "project_name": repo.project_name,
            "project_card": repo.project_card,
            "repo_url": repo.repo_url,
            "clone_url": build_clone_url(repo.repo_url, token or None),
            "default_branch": repo.default_branch,
            "token": token,
        }

    payload = await asyncio.to_thread(
        _clone_project_repo_sync,
        project=project,
        workspace_dir=workspace_dir,
        primary_project_code=primary_project_code,
        max_related_repos=settings.agent_related_repo_max_count,
        timeout_seconds=settings.agent_repo_clone_timeout_seconds,
    )
    if payload.get("error"):
        logger.warning(
            "clone_project_repo failed code=%s agent=%s error=%s",
            project["project_code"],
            agent_key,
            payload.get("error"),
        )
    else:
        logger.info(
            "clone_project_repo ready code=%s agent=%s path=%s reused=%s commit=%s",
            project["project_code"],
            agent_key,
            payload.get("path"),
            payload.get("reused"),
            payload.get("commit_sha"),
        )
    return payload


async def discover_projects_payload() -> dict:
    """Build the safe Agent-facing project catalog (public for focused tests)."""
    from app.models.database import db_manager
    from app.services import project_repo_service

    async with db_manager.session_factory() as db:
        return await project_repo_service.discover_projects(db)


def _get_servers():
    """Lazily create the full and discovery-only in-process MCP servers."""
    global _server, _discovery_server, _base_tools
    if _server is not None and _discovery_server is not None:
        return _server, _discovery_server

    try:
        from claude_agent_sdk import tool, create_sdk_mcp_server
    except ImportError as exc:
        raise RuntimeError(
            "claude-agent-sdk is required. Install with: pip install claude-agent-sdk>=0.1"
        ) from exc

    from app.config import settings

    @tool(
        "discover_projects",
        "List all enabled projects and their required project cards so you can "
        "judge which project fits a user's question. Returns only safe identity, "
        "scope, repository-availability, and enabled-Agent metadata; never returns "
        "repository URLs or credentials. Read the complete catalog before naming "
        "a matching project or claiming that no suitable project exists.",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    async def _discover_projects(_args):
        payload = await discover_projects_payload()
        logger.info(
            "discover_projects: count=%s truncated=%s",
            payload.get("count"),
            payload.get("truncated"),
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }

    @tool(
        "lookup_project_repo",
        "Resolve a git repository URL and default branch by project_code "
        "(and optional project_name fallback) from the admin-managed registry. "
        "Returns clone-ready URL (with auth token if required), default branch, and auth info.",
        {
            "type": "object",
            "properties": {
                "project_code": {"type": "string"},
                "project_name": {"type": "string"},
            },
            "required": ["project_code"],
        },
    )
    async def _lookup_project_repo(args):
        from app.models.database import db_manager
        from app.services import project_repo_service

        code = (args.get("project_code") or "").strip()

        if not code:
            return _tool_content(
                {"error": "invalid_input", "message": "project_code is required"}
            )

        async with db_manager.session_factory() as db:
            # 日志分析 Agent 对「未关联代码仓库」的项目不可见。
            repo = await project_repo_service.get_by_project_code(
                db, code, require_repo=True
            )

        if not repo:
            logger.info("lookup_project_repo: not_found code=%s", code)
            return _tool_content({"error": "not_found", "project_code": code})

        effective_token = repo.git_token or settings.code_repo_git_token or ""
        clone_url = _build_clone_url(repo.repo_url, effective_token or None)

        payload = {
            "project_code": repo.project_code,
            "project_name": repo.project_name,
            "repo_url": repo.repo_url,           # no token, for display
            "clone_url": clone_url,               # token injected, for actual git clone
            "default_branch": repo.default_branch,
            "auth_required": bool(effective_token),
        }
        logger.info(
            "lookup_project_repo: found code=%s repo_url=%s auth=%s",
            repo.project_code,
            repo.repo_url,
            bool(effective_token),
        )
        return _tool_content(payload)

    _base_tools = (_discover_projects, _lookup_project_repo)
    _server = create_sdk_mcp_server(
        name="project_repo",
        version="1.2.0",
        tools=list(_base_tools),
    )
    # ``allowed_tools`` is not a hard visibility boundary in every Claude Agent
    # SDK permission mode. GeneralAgent therefore receives a separate server
    # object that never exposes the credential-bearing lookup tool at all.
    _discovery_server = create_sdk_mcp_server(
        name="project_repo",
        version="1.2.0",
        tools=[_discover_projects],
    )
    return _server, _discovery_server


def get_mcp_server(
    *,
    workspace_dir: Optional[str] = None,
    primary_project_code: Optional[str] = None,
    agent_key: Optional[str] = None,
):
    """Return a full server, optionally bound to one Agent workspace.

    The unbound form preserves the discovery + lookup compatibility surface.
    Project-bound Agents MUST use the bound form to receive the safe clone tool.
    """
    base_server, _ = _get_servers()
    if workspace_dir is None:
        return base_server
    if not agent_key:
        raise ValueError("agent_key is required for a workspace-bound project server")

    try:
        from claude_agent_sdk import tool, create_sdk_mcp_server
    except ImportError as exc:
        raise RuntimeError(
            "claude-agent-sdk is required. Install with: pip install claude-agent-sdk>=0.1"
        ) from exc

    workspace = str(Path(workspace_dir).resolve())
    clone_lock = asyncio.Lock()

    @tool(
        "clone_project_repo",
        "Clone or reuse one enabled registered project inside this Agent's bound "
        "workspace. Use the project_code returned by discover_projects. The tool "
        "chooses a contained path, keeps repository credentials server-side, and "
        "returns the project's complete card plus safe checkout path/branch/commit. "
        "Use it for the selected project and for every additional project materially "
        "required by a cross-project investigation.",
        {
            "type": "object",
            "properties": {"project_code": {"type": "string"}},
            "required": ["project_code"],
            "additionalProperties": False,
        },
    )
    async def _clone_project_repo(args):
        async with clone_lock:
            payload = await clone_project_repo_payload(
                project_code=(args.get("project_code") or ""),
                workspace_dir=workspace,
                primary_project_code=primary_project_code,
                agent_key=agent_key,
            )
        return _tool_content(payload)

    return create_sdk_mcp_server(
        name="project_repo",
        version="1.2.0",
        # Configuration Manager uses server-side cloning exclusively. The
        # legacy lookup returns an authenticated URL and must not be visible.
        tools=[*[t for t in _base_tools if agent_key != "package_search" or t.name != "lookup_project_repo"], _clone_project_repo],
    )


def get_project_discovery_mcp_server():
    """Return a discovery-only project_repo server safe for GeneralAgent."""
    return _get_servers()[1]
