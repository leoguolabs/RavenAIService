"""
读取并渲染 claude_agent_package_search 通用提示词。

结构对齐 ``app/agents/project_expert/prompts.py``：从
``prompts_config.yaml`` 的 ``claude_agent_package_search.generic`` 区块
加载（支持按 locale 的多语言变体、``zh`` 兜底），后台保存提示词时由
``prompts_config_service._invalidate_prompt_caches`` 清空缓存即时生效。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import yaml

from app.i18n.prompts import select_localized_body

_PROMPTS_CACHE: Dict[str, Any] = {}


def _load_config() -> Dict[str, Any]:
    if _PROMPTS_CACHE:
        return _PROMPTS_CACHE

    from app.config import settings
    from pathlib import Path
    import os

    raw = getattr(settings, "prompts_config_path", "app/prompts/prompts_config.yaml")
    if os.path.isabs(raw):
        path = Path(raw)
    else:
        project_root = Path(__file__).resolve().parents[3]
        path = (project_root / raw).resolve()

    content = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    _PROMPTS_CACHE.update(parsed or {})
    return _PROMPTS_CACHE


def get_prompts(locale: Optional[str] = None, *, packaging: bool = False) -> Tuple[str, str]:
    """Return the generic (system_prompt, user_prompt_template) for ``locale``.

    ``locale`` selects the per-language body, falling back to the default
    language (``zh``) when a variant is missing; a legacy flat-string body is
    returned unchanged.
    """
    config = _load_config()
    agent_config: Dict[str, Any] = config.get("claude_agent_package_search", {})
    variant_key = "packaging" if packaging else "generic"
    variant = agent_config.get(variant_key) or {}
    if packaging and not variant:
        # Older administrator-saved prompt files have only the search variant.
        # Never reintroduce its metadata-first workflow into package execution.
        from pathlib import Path
        bundled = Path(__file__).resolve().parents[2] / "prompts" / "prompts_config.yaml"
        variant = yaml.safe_load(bundled.read_text(encoding="utf-8"))["claude_agent_package_search"]["packaging"]

    system_prompt = select_localized_body(variant.get("system_prompt"), locale)
    user_prompt_template = select_localized_body(
        variant.get("user_prompt_template"), locale
    )

    return system_prompt, user_prompt_template


def render_user_prompt(
    user_prompt_template: str,
    *,
    task_id: str,
    workspace_dir: str = "",
    question: str,
    hints: str,
) -> str:
    replacements = {
        "task_id": task_id,
        "workspace_dir": workspace_dir,
        "question": question,
        "hints": hints or "",
    }
    rendered = user_prompt_template
    for key, value in replacements.items():
        rendered = rendered.replace(f"{{{key}}}", str(value))
    return rendered
