"""Prepare the explicit primary checkout before the first model request."""

import logging
import re

from app.agents.log_analysis.mcp_tools import clone_project_repo_payload

logger = logging.getLogger(__name__)


async def prepare_primary_repo(ctx, project_code: str) -> str:
    """Reuse the registered clone path and keep the SDK workflow on failure."""
    try:
        checkout = await clone_project_repo_payload(
            project_code=project_code,
            workspace_dir=ctx.temp_dir,
            primary_project_code=project_code,
            agent_key="log_analysis",
        )
    except Exception as exc:
        logger.warning("Primary checkout preparation failed: %s", type(exc).__name__)
        return ""

    commit = checkout.get("commit_sha")
    if (
        checkout.get("status") != "ok"
        or checkout.get("path") != "repo"
        or not isinstance(commit, str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", commit)
    ):
        return ""

    if ctx.locale == "en":
        return (
            "\n\n## Prepared primary checkout\n"
            f"The server verified the registered checkout at repo/ (HEAD {commit}). "
            "After the required project-fit check, reuse this checkout for the matching project. "
            "The primary clone step is already satisfied. Read task.json as usual. "
            "Verify any requested capture revision before using source as evidence. "
            "Investigate the relevant logs and source together where their paths are known. "
            "Additional projects still use the normal discovery and clone tools.\n"
        )
    return (
        "\n\n## 服务端已准备主仓库\n"
        f"服务端已核验注册仓库的 repo/ 检出（HEAD {commit}）。"
        "完成规定的项目适配性检查后，如项目匹配，直接复用该检出，主仓库克隆步骤已经完成。"
        "按原规则读取 task.json。问题涉及采集时的具体版本时，先核对检出版本，再以源码为证据。"
        "已知日志与源码位置时，可合并独立检查。追加项目仍使用原有发现和克隆工具。\n"
    )
