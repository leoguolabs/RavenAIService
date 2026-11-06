"""
Admin endpoints for prompt configuration and repo settings management.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from app.models.database import get_db
from app.security.admin_auth import ADMIN_TOKEN_HEADER, ADMIN_TOKEN_PREFIX, auth_manager
from app.security.admin_dependency import (
    AdminPrincipal,
    resolve_admin_identity,
    resolve_admin_principal,
)
from app.services import (
    model_settings_service,
    project_prompt_service,
    project_repo_member_service,
    project_repo_service,
    skills_service,
)
from app.services.user_service import user_service
from app.services.prompts_config_service import (
    load_prompts_config,
    update_prompt_entries,
    update_prompts_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])


class AdminAuthData(BaseModel):
    username: str
    token: str
    expires_at: datetime
    ttl_minutes: int


class AdminAuthResponse(BaseModel):
    success: bool = True
    data: AdminAuthData
    message: str = "登录成功"


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=256)


class MeResponse(BaseModel):
    success: bool = True
    data: dict
    message: str = "ok"


class PromptsSummary(BaseModel):
    log_type_keys: list[str] = Field(default_factory=list)
    has_default_plan: bool = False
    has_default_summary: bool = False
    function_keys: list[str] = Field(default_factory=list)
    editable_prompt_count: int = 0


class PromptEntryData(BaseModel):
    id: str
    function_key: str
    function_name: str
    function_description: Optional[str] = None
    agent_key: str
    agent_name: str
    agent_description: Optional[str] = None
    prompt_key: str
    prompt_label: str
    prompt_type: str
    path: list[str]
    content: str
    locale: Optional[str] = None


class PromptsConfigData(BaseModel):
    path: str
    content: str
    updated_at: datetime
    size: int
    checksum: str
    summary: PromptsSummary
    prompts: list[PromptEntryData] = Field(default_factory=list)


class PromptsConfigResponse(BaseModel):
    success: bool = True
    data: PromptsConfigData
    message: str = "ok"


class UpdatePromptsRequest(BaseModel):
    content: Optional[str] = None
    prompts: Optional[list[dict[str, str]]] = None
    expected_checksum: Optional[str] = None
    force: bool = False


bearer_scheme = HTTPBearer(auto_error=False)


async def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> str:
    """Validate bearer token for global-admin-only routes.

    Returns the admin username. Accepts a legacy admin token or a
    ``role == "admin"`` user token; rejects everyone else.
    """
    return await resolve_admin_identity(credentials, request=request)


# ``require_admin`` already enforces global-admin-only access. Expose an alias so
# global-only routes can document intent explicitly.
require_global_admin = require_admin


async def require_admin_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> AdminPrincipal:
    """Admit global admins or project-member admins (any enabled membership)."""
    return await resolve_admin_principal(credentials, request=request)


async def require_project_admin_by_repo_id(
    repo_id: int,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> AdminPrincipal:
    """Authorize access to a project repo by numeric id.

    Global admins are always allowed. Project-member admins are allowed only for
    enabled projects they belong to; otherwise the project is treated as
    non-existent (404) to avoid leaking project existence.
    """
    principal = await resolve_admin_principal(credentials, request=request)
    if principal.is_global_admin:
        return principal
    if repo_id not in principal.allowed_project_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    return principal


async def require_project_admin_by_code(
    project_code: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> AdminPrincipal:
    """Authorize access to project-scoped resources by ``project_code``.

    Global admins keep existing behavior, including referencing project codes
    that do not yet have a repo row (pre-provisioning). Project-member admins
    must reference an enabled project they belong to; otherwise 404. The lookup
    uses the principal's allowed (normalized) project codes, so no extra DB query
    is required.
    """
    principal = await resolve_admin_principal(credentials, request=request)
    if principal.is_global_admin:
        return principal
    normalized = (project_code or "").strip().lower()
    if normalized not in principal.allowed_project_codes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    return principal


@router.post("/auth/login", response_model=AdminAuthResponse)
async def admin_login(payload: LoginRequest) -> AdminAuthResponse:
    token, expires_at = auth_manager.verify_credentials(
        payload.username, payload.password
    )
    return AdminAuthResponse(
        data=AdminAuthData(
            username=payload.username,
            token=token,
            expires_at=datetime.fromtimestamp(expires_at),
            ttl_minutes=auth_manager.token_ttl_minutes(),
        )
    )


@router.post("/auth/logout", response_model=MeResponse)
async def admin_logout() -> MeResponse:
    # Client-side token drop; we remain stateless on the server
    return MeResponse(message="已退出登录", data={})


@router.get("/auth/me", response_model=MeResponse)
async def auth_me(
    principal: AdminPrincipal = Depends(require_admin_principal),
) -> MeResponse:
    return MeResponse(
        data={
            "username": principal.username,
            "access_level": principal.access_level,
            "allowed_nav_keys": principal.allowed_nav_keys,
            "allowed_project_ids": principal.allowed_project_ids,
            "allowed_project_codes": principal.allowed_project_codes,
        }
    )


@router.get("/prompts/config", response_model=PromptsConfigResponse)
async def get_prompts_config(
    _username: str = Depends(require_admin),
) -> PromptsConfigResponse:
    data = load_prompts_config()
    return PromptsConfigResponse(
        data=PromptsConfigData(**data),
        message="读取成功",
    )


@router.put("/prompts/config", response_model=PromptsConfigResponse)
async def save_prompts_config(
    payload: UpdatePromptsRequest,
    _username: str = Depends(require_admin),
) -> PromptsConfigResponse:
    if payload.prompts is not None:
        data = update_prompt_entries(
            prompt_updates=payload.prompts,
            expected_checksum=payload.expected_checksum,
            force=payload.force,
        )
    elif payload.content is not None:
        data = update_prompts_config(
            new_content=payload.content,
            expected_checksum=payload.expected_checksum,
            force=payload.force,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either prompts or content is required.",
        )
    return PromptsConfigResponse(
        data=PromptsConfigData(**data),
        message="保存成功",
    )


# ─────────────────── Model Settings (runtime, Admin-editable) ──────────────

class ModelSettingsResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]
    message: str = "ok"


class UpdateModelSettingsRequest(BaseModel):
    """Partial update — omitted / null fields are left unchanged.

    Secrets (``*_api_key``) are only updated when a non-empty value is sent;
    an empty string keeps the existing key. Non-secret text fields may be set
    to an empty string to fall back to the provider default.
    """

    anthropic_provider: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    anthropic_api_keys: Optional[List[str]] = None
    anthropic_base_url: Optional[str] = None
    anthropic_model: Optional[str] = None
    anthropic_small_fast_model: Optional[str] = None
    anthropic_max_tokens: Optional[int] = None
    bug_fix_agent_provider: Optional[str] = None
    bug_fix_agent_api_key: Optional[str] = None
    bug_fix_agent_base_url: Optional[str] = None
    bug_fix_agent_model: Optional[str] = None
    bug_fix_agent_small_fast_model: Optional[str] = None
    bug_fix_agent_max_tokens: Optional[int] = None
    bug_fix_agent_max_turns: Optional[int] = None
    bug_fix_agent_request_timeout_seconds: Optional[int] = None
    anthropic_backup_enabled: Optional[bool] = None
    anthropic_backup_provider: Optional[str] = None
    anthropic_backup_api_key: Optional[str] = None
    anthropic_backup_base_url: Optional[str] = None
    anthropic_backup_model: Optional[str] = None
    anthropic_backup_small_fast_model: Optional[str] = None
    model_router_enabled: Optional[bool] = None
    model_router_first_token_deadline_ms: Optional[int] = None
    model_router_slow_ttft_ms: Optional[int] = None
    model_router_window_size: Optional[int] = None
    model_router_trip_threshold: Optional[int] = None
    model_router_min_samples: Optional[int] = None
    model_router_hard_failure_trip: Optional[int] = None
    model_router_cooldown_seconds: Optional[int] = None
    model_router_sample_ttl_seconds: Optional[int] = None
    ocr_enabled: Optional[bool] = None
    ocr_api_key: Optional[str] = None
    ocr_base_url: Optional[str] = None
    ocr_model: Optional[str] = None
    ocr_provider: Optional[str] = None


# This request model is a hand-maintained mirror of ``model_settings_service._SPECS``,
# and FastAPI drops unknown body keys silently — so a spec added without a field
# here would be accepted by the API and then vanish before ``save()`` ever sees it.
# Fail at import instead of debugging a setting that "won't stick".
assert (
    set(UpdateModelSettingsRequest.model_fields) == model_settings_service.OVERRIDABLE_KEYS
), (
    "UpdateModelSettingsRequest and model_settings_service._SPECS disagree: "
    f"{sorted(set(UpdateModelSettingsRequest.model_fields) ^ model_settings_service.OVERRIDABLE_KEYS)}"
)


@router.get("/model-settings", response_model=ModelSettingsResponse)
async def get_model_settings(
    _username: str = Depends(require_admin),
) -> ModelSettingsResponse:
    return ModelSettingsResponse(data=model_settings_service.describe(), message="读取成功")


class AgentRuntimeUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: Optional[bool] = None
    executor: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = Field(default=None, repr=False)
    model: Optional[str] = None
    agent_overrides: Optional[Dict[str, Any]] = None


@router.get("/agent-runtime")
async def get_agent_runtime(_username: str = Depends(require_admin)):
    from app.services import agent_runtime_service
    return {"success": True, "data": agent_runtime_service.describe()}


@router.put("/agent-runtime")
async def update_agent_runtime(payload: AgentRuntimeUpdate, _username: str = Depends(require_admin)):
    from app.services import agent_runtime_service
    try:
        data = agent_runtime_service.save(payload.model_dump(exclude_none=True))
    except ValueError as exc:
        # Pydantic's default exception string includes input values, possibly keys.
        errors = exc.errors(include_input=False, include_url=False) if hasattr(exc, "errors") else []
        detail = "; ".join(str(error["msg"]) for error in errors) or "Invalid runtime configuration / 执行配置无效"
        raise HTTPException(status_code=400, detail=detail) from exc
    return {"success": True, "data": data}


@router.get("/agent-runtime/models")
async def get_agent_runtime_models(_username: str = Depends(require_admin)):
    from app.services import agent_runtime_service
    try:
        return {"success": True, "data": await agent_runtime_service.models()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Cannot read Magpie models / 无法读取 Magpie 模型") from exc


@router.put("/model-settings", response_model=ModelSettingsResponse)
async def update_model_settings(
    payload: UpdateModelSettingsRequest,
    _username: str = Depends(require_admin),
) -> ModelSettingsResponse:
    try:
        data = model_settings_service.save(payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return ModelSettingsResponse(data=data, message="保存成功")


@router.delete("/model-settings", response_model=ModelSettingsResponse)
async def reset_model_settings(
    _username: str = Depends(require_admin),
) -> ModelSettingsResponse:
    """Clear all Admin overrides; every model key reverts to its ``.env`` value."""
    data = model_settings_service.reset()
    return ModelSettingsResponse(data=data, message="已恢复为环境变量默认值")


class TestModelSettingsRequest(BaseModel):
    """Probe an endpoint with the form's current values.

    Every field is optional: omitted values fall back to the saved effective
    config, which is how the API key can stay server-side while the admin tests
    a new base URL or model.
    """

    target: str = "anthropic"  # "anthropic" | "anthropic_backup" | "ocr"
    provider: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    api_keys: Optional[List[str]] = None


@router.post("/model-settings/test", response_model=ModelSettingsResponse)
async def test_model_settings(
    payload: TestModelSettingsRequest,
    _username: str = Depends(require_admin),
) -> ModelSettingsResponse:
    """Send one minimal completion request and report the outcome.

    A failing upstream is a successful *test* (HTTP 200 with ``ok: false``) —
    only a malformed request is a 400.
    """
    try:
        data = await model_settings_service.test_connection(
            payload.model_dump(exclude_none=True)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return ModelSettingsResponse(
        data=data,
        message="连接测试通过" if data.get("ok") else "连接测试失败",
    )


# ─────────────────── Project Repo Registry ────────────────────────

class ProjectRepoData(BaseModel):
    id: int
    project_code: str
    project_name: str
    repo_url: str
    default_branch: str
    git_token_set: bool
    has_repo: bool = False
    enabled_agent_keys: List[str] = Field(default_factory=list)
    project_card: str
    enabled: bool
    member_count: int = 0
    created_at: datetime
    updated_at: datetime


class ProjectRepoListResponse(BaseModel):
    success: bool = True
    data: List[ProjectRepoData]
    message: str = "ok"


class ProjectRepoResponse(BaseModel):
    success: bool = True
    data: ProjectRepoData
    message: str = "ok"


class CreateProjectRepoRequest(BaseModel):
    project_code: str = Field(..., min_length=1, max_length=128)
    project_name: str = Field(..., min_length=1, max_length=256)
    # 允许空字符串：仅用于日志分类、无关联 Git 仓库的项目
    repo_url: str = Field(default="")
    default_branch: str = Field(default="main", max_length=128)
    git_token: Optional[str] = None
    project_card: str = Field(
        ...,
        min_length=1,
        max_length=project_repo_service.PROJECT_CARD_MAX_LENGTH,
    )
    enabled: bool = True
    enabled_agent_keys: Optional[List[str]] = None

    @field_validator("project_card")
    @classmethod
    def validate_project_card(cls, value: str) -> str:
        return project_repo_service.normalize_project_card(value)


class UpdateProjectRepoRequest(BaseModel):
    project_name: Optional[str] = None
    repo_url: Optional[str] = None
    default_branch: Optional[str] = None
    git_token: Optional[str] = None
    project_card: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=project_repo_service.PROJECT_CARD_MAX_LENGTH,
    )
    enabled: Optional[bool] = None
    enabled_agent_keys: Optional[List[str]] = None

    @field_validator("project_card")
    @classmethod
    def validate_explicit_project_card(cls, value: Optional[str]) -> Optional[str]:
        # Omitted fields retain the existing card.  An explicitly supplied
        # null/blank value must never clear this required project contract.
        if value is None:
            raise ValueError("项目卡片不能为空")
        return project_repo_service.normalize_project_card(value)


class TestConnectionResponse(BaseModel):
    success: bool = True
    data: dict
    message: str = "ok"


def _seed_default_prompts(project_code: str, has_repo: bool) -> None:
    """为项目播种默认的项目级提示词（目前仅项目专家；幂等、不抛错）。

    ``has_repo`` 决定播种「代码工作流」还是「无仓库」变体。播种失败不应阻断
    项目创建/更新，因此异常仅记录日志。
    """
    try:
        seeded = project_prompt_service.seed_project_default_prompts(
            project_code, has_repo=has_repo
        )
        if seeded:
            logger.info(
                "seeded default project prompts for project=%s has_repo=%s agents=%s",
                project_code,
                has_repo,
                ",".join(seeded),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("seed default project prompts failed for %s: %s", project_code, exc)


class ProjectAgentInfo(BaseModel):
    key: str
    name: str
    display_name: str
    framework: str
    requires_repo: bool = False
    description: Optional[str] = None


class ProjectAgentListResponse(BaseModel):
    success: bool = True
    data: List[ProjectAgentInfo]
    message: str = "ok"


def _repo_to_data(
    repo, member_count: int = 0, enabled_agent_keys: Optional[List[str]] = None
) -> ProjectRepoData:
    return ProjectRepoData(
        id=repo.id,
        project_code=repo.project_code,
        project_name=repo.project_name,
        repo_url=repo.repo_url,
        default_branch=repo.default_branch,
        git_token_set=bool(repo.git_token),
        has_repo=project_repo_service.has_repo(repo),
        enabled_agent_keys=enabled_agent_keys
        if enabled_agent_keys is not None
        else project_repo_service.default_agent_keys_for_repo(repo),
        project_card=repo.project_card,
        enabled=repo.enabled,
        member_count=member_count,
        created_at=repo.created_at,
        updated_at=repo.updated_at,
    )


@router.get("/project-agents", response_model=ProjectAgentListResponse)
async def list_project_agents(
    _principal: AdminPrincipal = Depends(require_admin_principal),
) -> ProjectAgentListResponse:
    """列出可在项目创建/编辑时启用的项目型 Agent。"""
    return ProjectAgentListResponse(
        data=[
            ProjectAgentInfo(**item)
            for item in project_repo_service.list_project_agents()
        ]
    )


@router.get("/project-repos", response_model=ProjectRepoListResponse)
async def list_project_repos(
    include_disabled: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    principal: AdminPrincipal = Depends(require_admin_principal),
    db=Depends(get_db),
) -> ProjectRepoListResponse:
    if principal.is_global_admin:
        repos = await project_repo_service.list_repos(
            db, include_disabled=include_disabled, offset=offset, limit=limit
        )
    else:
        # Project-member admins only ever see their own enabled projects,
        # regardless of include_disabled.
        allowed = set(principal.allowed_project_ids)
        all_enabled = await project_repo_service.list_repos(
            db, include_disabled=False, offset=0, limit=10_000
        )
        scoped = [r for r in all_enabled if r.id in allowed]
        repos = scoped[offset : offset + limit]
    counts = await project_repo_member_service.count_members_bulk(
        db, [r.id for r in repos]
    )
    agent_keys_by_repo = await project_repo_service.list_agent_keys_bulk(db, repos)
    return ProjectRepoListResponse(
        data=[
            _repo_to_data(r, counts.get(r.id, 0), agent_keys_by_repo.get(r.id, []))
            for r in repos
        ]
    )


@router.post("/project-repos", response_model=ProjectRepoResponse, status_code=status.HTTP_201_CREATED)
async def create_project_repo(
    payload: CreateProjectRepoRequest,
    _username: str = Depends(require_admin),
    db=Depends(get_db),
) -> ProjectRepoResponse:
    try:
        repo = await project_repo_service.create(
            db,
            project_code=payload.project_code,
            project_name=payload.project_name,
            repo_url=payload.repo_url,
            default_branch=payload.default_branch,
            git_token=payload.git_token,
            project_card=payload.project_card,
            enabled=payload.enabled,
        )
        agent_keys = await project_repo_service.replace_agent_keys(
            db, repo, payload.enabled_agent_keys
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"创建失败: {exc}",
        ) from exc
    # 为「项目专家」播种默认的项目级提示词：关联了代码仓库的项目播种代码工作流，
    # 未关联仓库的项目播种「无仓库」约束（日志分析 / 重构包管理员不播种，保持为空）。
    _seed_default_prompts(repo.project_code, project_repo_service.has_repo(repo))
    return ProjectRepoResponse(
        data=_repo_to_data(repo, enabled_agent_keys=agent_keys),
        message="创建成功",
    )


@router.get("/project-repos/{repo_id}", response_model=ProjectRepoResponse)
async def get_project_repo(
    repo_id: int,
    _principal: AdminPrincipal = Depends(require_project_admin_by_repo_id),
    db=Depends(get_db),
) -> ProjectRepoResponse:
    repo = await project_repo_service.get_by_id(db, repo_id)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    member_count = await project_repo_member_service.count_members(db, repo.id)
    agent_keys = await project_repo_service.list_agent_keys(db, repo)
    return ProjectRepoResponse(data=_repo_to_data(repo, member_count, agent_keys))


@router.put("/project-repos/{repo_id}", response_model=ProjectRepoResponse)
async def update_project_repo(
    repo_id: int,
    payload: UpdateProjectRepoRequest,
    principal: AdminPrincipal = Depends(require_project_admin_by_repo_id),
    db=Depends(get_db),
) -> ProjectRepoResponse:
    # Project-member admins may only edit safe project fields. Ownership/security
    # fields (enabled, git_token) remain global-admin-only.
    if not principal.is_global_admin and (
        payload.enabled is not None
        or payload.git_token is not None
        or payload.enabled_agent_keys is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="项目成员管理员不可修改启用状态、Git 凭据或项目 Agent",
        )
    repo = await project_repo_service.get_by_id(db, repo_id)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    had_repo = project_repo_service.has_repo(repo)
    try:
        repo = await project_repo_service.update(
            db,
            repo,
            project_name=payload.project_name,
            repo_url=payload.repo_url,
            default_branch=payload.default_branch,
            git_token=payload.git_token,
            project_card=payload.project_card,
            enabled=payload.enabled,
        )
        if payload.enabled_agent_keys is not None:
            agent_keys = await project_repo_service.replace_agent_keys(
                db, repo, payload.enabled_agent_keys
            )
        elif had_repo != project_repo_service.has_repo(repo):
            agent_keys = await project_repo_service.reconcile_agent_keys(db, repo)
        else:
            agent_keys = await project_repo_service.list_agent_keys(db, repo)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"更新失败: {exc}",
        ) from exc
    # 项目在「有仓库 ↔ 无仓库」之间切换时，为项目专家切换默认项目级提示词
    # （仅当该层仍是未改动的默认值；管理员的自定义内容不会被覆盖）。
    if had_repo != project_repo_service.has_repo(repo):
        _seed_default_prompts(repo.project_code, project_repo_service.has_repo(repo))
    return ProjectRepoResponse(
        data=_repo_to_data(repo, enabled_agent_keys=agent_keys),
        message="更新成功",
    )


@router.delete("/project-repos/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_repo(
    repo_id: int,
    force: bool = Query(default=False, description="为 true 时即便存在关联日志也强制删除"),
    _username: str = Depends(require_admin),
    db=Depends(get_db),
) -> None:
    from sqlalchemy import func, select, update as sa_update
    from app.models.log import LogRecord

    repo = await project_repo_service.get_by_id(db, repo_id)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")

    # 统计引用该项目的日志数量
    affected = (
        await db.execute(
            select(func.count(LogRecord.id)).where(LogRecord.project_id == repo_id)
        )
    ).scalar() or 0

    if affected and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "affected_logs": int(affected),
                "message": "该项目有关联的日志记录。使用 force=true 进行删除。",
            },
        )

    # 强制删除：先将关联日志的 project_id 置空（兼容未启用 FK 级联的方言）
    if affected:
        await db.execute(
            sa_update(LogRecord)
            .where(LogRecord.project_id == repo_id)
            .values(project_id=None)
        )

    await project_repo_service.delete(db, repo)


@router.post("/project-repos/{repo_id}/test-connection", response_model=TestConnectionResponse)
async def test_project_repo_connection(
    repo_id: int,
    _principal: AdminPrincipal = Depends(require_project_admin_by_repo_id),
    db=Depends(get_db),
) -> TestConnectionResponse:
    result = await project_repo_service.test_connection(db, repo_id)
    return TestConnectionResponse(
        success=result["success"],
        data=result,
        message=result["message"],
    )


# ─────────────────── Project Repo Members ─────────────────────────

class ProjectMemberData(BaseModel):
    id: str
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None


class ProjectMemberListResponse(BaseModel):
    success: bool = True
    data: List[ProjectMemberData]
    message: str = "ok"


class AddProjectMemberRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=36)


def _user_to_member(user) -> ProjectMemberData:
    return ProjectMemberData(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
    )


@router.get(
    "/project-repos/{repo_id}/members",
    response_model=ProjectMemberListResponse,
)
async def list_project_members(
    repo_id: int,
    _username: str = Depends(require_admin),
    db=Depends(get_db),
) -> ProjectMemberListResponse:
    repo = await project_repo_service.get_by_id(db, repo_id)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    members = await project_repo_member_service.list_members(db, repo_id)
    return ProjectMemberListResponse(data=[_user_to_member(u) for u in members])


@router.post(
    "/project-repos/{repo_id}/members",
    response_model=ProjectMemberListResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_project_member(
    repo_id: int,
    payload: AddProjectMemberRequest,
    _username: str = Depends(require_admin),
    db=Depends(get_db),
) -> ProjectMemberListResponse:
    repo = await project_repo_service.get_by_id(db, repo_id)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    user = await user_service.get_by_id(db, payload.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    # 幂等加入
    await project_repo_member_service.add_member(db, repo_id, payload.user_id)
    members = await project_repo_member_service.list_members(db, repo_id)
    return ProjectMemberListResponse(
        data=[_user_to_member(u) for u in members], message="添加成功"
    )


@router.delete(
    "/project-repos/{repo_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_project_member(
    repo_id: int,
    user_id: str,
    _username: str = Depends(require_admin),
    db=Depends(get_db),
) -> None:
    repo = await project_repo_service.get_by_id(db, repo_id)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    await project_repo_member_service.remove_member(db, repo_id, user_id)


# ─────────────────── Agent Skills Management ──────────────────────

class AgentInfo(BaseModel):
    key: str
    name: str
    framework: str
    description: Optional[str] = None


class AgentListResponse(BaseModel):
    success: bool = True
    data: List[AgentInfo]
    message: str = "ok"


class SkillData(BaseModel):
    id: str
    name: str
    description: str = ""
    enabled: bool = True
    required: bool = False
    source_filename: str = ""
    size_bytes: int = 0
    installed_at: Optional[str] = None
    updated_at: Optional[str] = None


class SkillListResponse(BaseModel):
    success: bool = True
    data: List[SkillData]
    message: str = "ok"


class SkillResponse(BaseModel):
    success: bool = True
    data: SkillData
    message: str = "ok"


class UpdateSkillRequest(BaseModel):
    enabled: bool


class SkillFilesResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]
    message: str = "ok"


class SkillFileContent(BaseModel):
    path: str
    size: int
    encoding: str
    content: Optional[str] = None
    truncated: bool = False


class SkillFileContentResponse(BaseModel):
    success: bool = True
    data: SkillFileContent
    message: str = "ok"


def _ensure_known_agent(agent_key: str) -> None:
    if agent_key not in skills_service.SUPPORTED_AGENTS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown agent_key: {agent_key}",
        )


@router.get("/agents", response_model=AgentListResponse)
async def list_skill_agents(
    _username: str = Depends(require_admin),
) -> AgentListResponse:
    """列出支持加载 Skill 的 Agent。"""
    return AgentListResponse(
        data=[AgentInfo(**item) for item in skills_service.list_agents()]
    )


@router.get("/agents/{agent_key}/skills", response_model=SkillListResponse)
async def list_agent_skills(
    agent_key: str,
    _username: str = Depends(require_admin),
) -> SkillListResponse:
    _ensure_known_agent(agent_key)
    try:
        items = skills_service.list_skills(agent_key)
    except skills_service.UnknownAgentError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return SkillListResponse(data=[SkillData(**item) for item in items])


@router.post(
    "/agents/{agent_key}/skills",
    response_model=SkillResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_agent_skill(
    agent_key: str,
    file: UploadFile = File(..., description="Skill zip 包"),
    overwrite: bool = Query(default=False),
    _username: str = Depends(require_admin),
) -> SkillResponse:
    """上传 zip 格式的 Skill 包并安装到指定 Agent。"""
    _ensure_known_agent(agent_key)

    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只支持上传 .zip 格式的 Skill 包",
        )

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传内容为空")

    try:
        entry = skills_service.install_skill(
            agent_key,
            zip_bytes=payload,
            source_filename=filename,
            overwrite=overwrite,
        )
    except skills_service.SkillConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except skills_service.SkillValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except skills_service.SkillError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return SkillResponse(data=SkillData(**entry), message="上传成功")


@router.patch("/agents/{agent_key}/skills/{skill_id}", response_model=SkillResponse)
async def update_agent_skill(
    agent_key: str,
    skill_id: str,
    payload: UpdateSkillRequest,
    _username: str = Depends(require_admin),
) -> SkillResponse:
    _ensure_known_agent(agent_key)
    try:
        entry = skills_service.set_skill_enabled(
            agent_key, skill_id, enabled=payload.enabled
        )
    except skills_service.SkillConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except skills_service.SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return SkillResponse(data=SkillData(**entry), message="更新成功")


@router.get(
    "/agents/{agent_key}/skills/{skill_id}/files",
    response_model=SkillFilesResponse,
)
async def list_agent_skill_files(
    agent_key: str,
    skill_id: str,
    _username: str = Depends(require_admin),
) -> SkillFilesResponse:
    """列出某个 Skill 目录的文件树，用于左侧导航预览。"""
    _ensure_known_agent(agent_key)
    try:
        data = skills_service.list_skill_files(agent_key, skill_id)
    except skills_service.SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except skills_service.SkillValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return SkillFilesResponse(data=data)


@router.get(
    "/agents/{agent_key}/skills/{skill_id}/file",
    response_model=SkillFileContentResponse,
)
async def get_agent_skill_file(
    agent_key: str,
    skill_id: str,
    path: str = Query(..., description="Skill 目录下的相对路径"),
    _username: str = Depends(require_admin),
) -> SkillFileContentResponse:
    """读取 Skill 目录下指定文件的内容（文本类型才返回正文）。"""
    _ensure_known_agent(agent_key)
    try:
        data = skills_service.read_skill_file(agent_key, skill_id, path)
    except skills_service.SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except skills_service.SkillValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return SkillFileContentResponse(data=SkillFileContent(**data))


@router.delete(
    "/agents/{agent_key}/skills/{skill_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_agent_skill(
    agent_key: str,
    skill_id: str,
    _username: str = Depends(require_admin),
) -> None:
    _ensure_known_agent(agent_key)
    try:
        skills_service.delete_skill(agent_key, skill_id)
    except skills_service.SkillConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except skills_service.SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Project-level Skill endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/project-repos/{project_code}/skills",
    response_model=SkillListResponse,
)
async def list_project_skills(
    project_code: str,
    _principal: AdminPrincipal = Depends(require_project_admin_by_code),
) -> SkillListResponse:
    items = skills_service.list_project_skills(project_code)
    return SkillListResponse(data=[SkillData(**item) for item in items])


@router.post(
    "/project-repos/{project_code}/skills",
    response_model=SkillResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_project_skill(
    project_code: str,
    file: UploadFile = File(..., description="Skill zip 包"),
    overwrite: bool = Query(default=False),
    _principal: AdminPrincipal = Depends(require_project_admin_by_code),
) -> SkillResponse:
    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只支持上传 .zip 格式的 Skill 包",
        )

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传内容为空")

    try:
        entry = skills_service.install_project_skill(
            project_code,
            zip_bytes=payload,
            source_filename=filename,
            overwrite=overwrite,
        )
    except skills_service.SkillConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except skills_service.SkillValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except skills_service.SkillError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return SkillResponse(data=SkillData(**entry), message="上传成功")


@router.patch(
    "/project-repos/{project_code}/skills/{skill_id}",
    response_model=SkillResponse,
)
async def update_project_skill(
    project_code: str,
    skill_id: str,
    payload: UpdateSkillRequest,
    _principal: AdminPrincipal = Depends(require_project_admin_by_code),
) -> SkillResponse:
    try:
        entry = skills_service.set_project_skill_enabled(
            project_code, skill_id, enabled=payload.enabled
        )
    except skills_service.SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return SkillResponse(data=SkillData(**entry), message="更新成功")


@router.delete(
    "/project-repos/{project_code}/skills/{skill_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_project_skill(
    project_code: str,
    skill_id: str,
    _principal: AdminPrincipal = Depends(require_project_admin_by_code),
) -> None:
    try:
        skills_service.delete_project_skill(project_code, skill_id)
    except skills_service.SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/project-repos/{project_code}/skills/{skill_id}/files",
    response_model=SkillFilesResponse,
)
async def list_project_skill_files(
    project_code: str,
    skill_id: str,
    _principal: AdminPrincipal = Depends(require_project_admin_by_code),
) -> SkillFilesResponse:
    try:
        data = skills_service.list_project_skill_files(project_code, skill_id)
    except skills_service.SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except skills_service.SkillValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return SkillFilesResponse(data=data)


@router.get(
    "/project-repos/{project_code}/skills/{skill_id}/file",
    response_model=SkillFileContentResponse,
)
async def get_project_skill_file(
    project_code: str,
    skill_id: str,
    path: str = Query(..., description="Skill 目录下的相对路径"),
    _principal: AdminPrincipal = Depends(require_project_admin_by_code),
) -> SkillFileContentResponse:
    try:
        data = skills_service.read_project_skill_file(project_code, skill_id, path)
    except skills_service.SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except skills_service.SkillValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return SkillFileContentResponse(data=SkillFileContent(**data))


# ---------------------------------------------------------------------------
# Project-level system prompt endpoints
# ---------------------------------------------------------------------------
#
# 让系统提示词像 Skill 一样分级处理：Agent 级基础提示词来自 prompts_config.yaml，
# 这里维护按 project_code 隔离的「项目级追加提示词」（可为空）。Agent 运行前会
# 把它叠加到基础系统提示词之后。


class ProjectSystemPromptData(BaseModel):
    project_code: str
    # ``None`` 表示项目共享层；否则为该 Agent 的专属层（含已播种的代码工作流）。
    agent_key: Optional[str] = None
    content: str
    exists: bool
    size_bytes: int = 0
    updated_at: Optional[datetime] = None


class ProjectSystemPromptResponse(BaseModel):
    success: bool = True
    data: ProjectSystemPromptData
    message: str = "ok"


class ProjectSystemPromptPreviewLayerData(BaseModel):
    key: str
    label: str
    exists: bool
    size_bytes: int = 0
    updated_at: Optional[datetime] = None


class ProjectSystemPromptPreviewData(BaseModel):
    project_code: str
    project_name: str
    agent_key: str
    locale: Optional[str] = None
    base_prompt: str
    project_addendum: str
    content: str
    base_chars: int
    addendum_chars: int
    total_chars: int
    layers: List[ProjectSystemPromptPreviewLayerData] = Field(default_factory=list)


class ProjectSystemPromptPreviewResponse(BaseModel):
    success: bool = True
    data: ProjectSystemPromptPreviewData
    message: str = "ok"


class UpdateProjectSystemPromptRequest(BaseModel):
    # 允许空字符串：空内容等价于清除项目级追加提示词。
    content: str = Field(default="", max_length=project_prompt_service.MAX_PROJECT_PROMPT_CHARS)


@router.get(
    "/project-repos/{project_code}/system-prompt",
    response_model=ProjectSystemPromptResponse,
)
async def get_project_system_prompt(
    project_code: str,
    agent: Optional[str] = Query(
        default=None,
        description=(
            "为空读取项目共享层（对所有 Agent 生效）；传入 agent_key "
            "（project_expert / log_analysis / package_search）读取该 Agent "
            "的专属层（含代码工作流）。"
        ),
    ),
    _principal: AdminPrincipal = Depends(require_project_admin_by_code),
) -> ProjectSystemPromptResponse:
    try:
        data = project_prompt_service.get_project_prompt(project_code, agent)
    except project_prompt_service.ProjectPromptValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return ProjectSystemPromptResponse(data=ProjectSystemPromptData(**data), message="读取成功")


@router.get(
    "/project-repos/{project_code}/system-prompt/preview",
    response_model=ProjectSystemPromptPreviewResponse,
)
async def preview_project_system_prompt(
    project_code: str,
    agent: str = Query(
        ...,
        description="要预览的 Agent key（project_expert / log_analysis / package_search）。",
    ),
    locale: Optional[str] = Query(default=None, description="提示词语言，如 zh / en。"),
    _principal: AdminPrincipal = Depends(require_project_admin_by_code),
    db=Depends(get_db),
) -> ProjectSystemPromptPreviewResponse:
    repo = await project_repo_service.get_by_project_code(db, project_code)
    project_name = repo.project_name if repo else project_code
    try:
        data = project_prompt_service.build_project_system_prompt_preview(
            project_code,
            agent,
            project_name=project_name,
            locale=locale,
        )
    except project_prompt_service.ProjectPromptValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return ProjectSystemPromptPreviewResponse(
        data=ProjectSystemPromptPreviewData(**data),
        message="读取成功",
    )


@router.put(
    "/project-repos/{project_code}/system-prompt",
    response_model=ProjectSystemPromptResponse,
)
async def update_project_system_prompt(
    project_code: str,
    payload: UpdateProjectSystemPromptRequest,
    agent: Optional[str] = Query(
        default=None,
        description="为空写入项目共享层；传入 agent_key 写入该 Agent 的专属层。",
    ),
    _principal: AdminPrincipal = Depends(require_project_admin_by_code),
) -> ProjectSystemPromptResponse:
    try:
        data = project_prompt_service.set_project_prompt(project_code, payload.content, agent)
    except project_prompt_service.ProjectPromptValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return ProjectSystemPromptResponse(data=ProjectSystemPromptData(**data), message="保存成功")
