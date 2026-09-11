"""Unified Raven package API.

These endpoints replace the legacy Node package service while preserving the
same `/packages`, `/upload`, `/download`, and `/search` API shapes.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.users import get_optional_user, get_request_locale
from app.i18n.messages import t
from app.models.database import get_db
from app.services.raven_package_service import (
    MetadataValidationError,
    normalize_description,
    normalize_tags,
    raven_package_service,
    validate_project_code,
)

logger = logging.getLogger(__name__)

# Hard limit for /packages/agent-search query length per spec.
PACKAGE_SEARCH_QUERY_MAX_LEN = 1000

router = APIRouter()


async def _record_package_search_metrics(
    result: Any, user: Any, project_repo_id: Optional[int] = None
) -> None:
    """Best-effort AI usage recording for a package-search run. Never raises.

    ``result`` is the agent result dict (non-stream) or the synthetic dict built
    from the streaming ``final`` event; both expose ``model``/``usage`` and the
    recommended/relevant id lists used for the sanitized ``result_count``.

    ``project_repo_id`` is the mandatory project the search was bound to; it is
    recorded so the run is attributed to its project in the metrics dashboard
    (without it every API-driven search shows as "未归属项目").
    """
    try:
        if not isinstance(result, dict):
            return
        from app.services import metrics_service

        recommended = result.get("recommended_package_ids")
        result_count = len(recommended) if isinstance(recommended, list) else 0
        session_id = result.get("session_id")
        anchor = session_id or uuid.uuid4().hex
        user_id = (
            str(user.id) if user is not None and getattr(user, "id", None) else None
        )
        await metrics_service.record_agent_run_usage(
            source="package_search_agent",
            agent_kind="package_search",
            result=result,
            user_id=user_id,
            session_id=session_id,
            project_repo_id=(
                str(project_repo_id) if project_repo_id is not None else None
            ),
            idempotency_key=f"ai_usage:package_search:{anchor}",
            extra_metadata={"result_count": result_count},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("packages: agent-search metrics record skipped: %s", exc)


async def _record_package_activity(
    *,
    action: str,
    project_code: Optional[str],
    status_value: str = "success",
    count: int = 1,
) -> None:
    """Best-effort package activity recording (upload/download/etc). Never raises.

    Persists a low-sensitivity ``package_activity`` event (action + project_code)
    and bumps the Prometheus ``raven_package_activity_total`` counter. The
    Prometheus counter carries no project label (low-cardinality constraint);
    the project association lives only in the persisted event metadata. No file
    contents, paths, or package ids are stored.
    """
    try:
        from app.services import metrics_service
        from app.utils import metrics as prom

        code = str(project_code or "") or "unassociated"
        await metrics_service.record_business_event(
            event_type="package_activity",
            source=f"package_{action}",
            idempotency_key=f"package_activity:{action}:{uuid.uuid4().hex}",
            status=status_value,
            metadata={"project_code": code, "result_count": count},
        )
        for _ in range(max(1, count)):
            prom.record_package_activity(action=action, status=status_value)
    except Exception as exc:  # noqa: BLE001
        logger.debug("packages: activity metrics record skipped: %s", exc)


def _ok(data: Any = None, message: str = "ok", **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"success": True, "message": message}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return payload


async def _can_edit_package_metadata(db: Any, package: dict[str, Any], user: Any) -> bool:
    """Return whether ``user`` may edit ``package`` description/tags.

    Authorization: global ``role == "admin"`` may edit any existing package;
    otherwise the package's ``projectCode`` must resolve to an enabled
    ``project_repo`` of which the user is a member. Anonymous users (``user is
    None``) are never authorized. Never raises — used both for the detail-page
    capability flag and (via :func:`_require_package_metadata_edit`) enforcement.
    """
    if user is None:
        return False
    if str(getattr(user, "role", "") or "") == "admin":
        return True
    code = str(package.get("projectCode") or "").strip()
    if not code:
        return False

    from app.services import project_repo_member_service, project_repo_service

    repo = await project_repo_service.get_by_project_code(db, code)
    if repo is None or not getattr(repo, "enabled", True):
        return False
    return await project_repo_member_service.is_member(db, repo.id, str(user.id))


async def _require_package_metadata_edit(
    db: Any, package: dict[str, Any], user: Any, locale: str
) -> None:
    """Enforce metadata-edit authorization, raising 401/403 as appropriate.

    Anonymous callers get 401; authenticated non-members (including packages
    with no resolvable enabled project for non-admins) get 403.
    """
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=t("auth.not_logged_in", locale),
        )
    if not await _can_edit_package_metadata(db, package, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=t("package.metadata_forbidden", locale),
        )


def _metadata_fields(
    version: Optional[str] = None,
    projectCode: Optional[str] = None,
    description: Optional[str] = None,
    isPatch: Optional[str] = None,
    tags: Optional[str] = None,
    components: Optional[str] = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in {
        "version": version,
        "projectCode": projectCode,
        "description": description,
        "isPatch": isPatch,
        "tags": tags,
        "components": components,
    }.items():
        if value is not None:
            fields[key] = value
    return fields


def _parse_package_info(
    packageInfo: Optional[str], locale: str = "zh"
) -> Optional[dict[str, Any]]:
    if not packageInfo:
        return None
    try:
        parsed = json.loads(packageInfo)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.invalid_package_info_json", locale, error=exc),
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.package_info_not_object", locale),
        )
    return parsed


@router.get("/packages")
async def list_packages(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1),
    search: str = "",
    projectCode: str = "",
    type: str = "",
    tags: str = "",
    version: str = "",
    isPatch: str = "",
    sortBy: str = "createdAt",
    sortOrder: str = "desc",
) -> dict[str, Any]:
    # ``type`` is a deprecated alias kept for old clients; it is interpreted
    # as a projectCode filter and loses to an explicit ``projectCode``.
    packages, pagination = raven_package_service.filter_packages(
        {
            "page": page,
            "limit": limit,
            "search": search,
            "projectCode": projectCode or type,
            "tags": tags,
            "version": version,
            "isPatch": isPatch,
            "sortBy": sortBy,
            "sortOrder": sortOrder,
        }
    )
    return _ok({"packages": packages, "pagination": pagination})


@router.get("/packages/stats/overview")
async def package_stats() -> dict[str, Any]:
    packages = raven_package_service.get_all_packages()
    by_project: dict[str, int] = {}
    for package in packages:
        project_code = str(package.get("projectCode") or "") or "unassociated"
        by_project[project_code] = by_project.get(project_code, 0) + 1
    recent = sorted(packages, key=lambda pkg: str(pkg.get("createdAt") or ""), reverse=True)[:5]
    return _ok(
        {
            "totalPackages": len(packages),
            "packagesByProject": by_project,
            "recentPackages": [
                {
                    "id": pkg.get("id"),
                    "name": pkg.get("name"),
                    "version": pkg.get("version"),
                    "createdAt": pkg.get("createdAt"),
                }
                for pkg in recent
            ],
        }
    )


@router.post("/packages/scan")
async def scan_packages(
    locale: str = Depends(get_request_locale),
) -> dict[str, Any]:
    added = raven_package_service.scan_uploads_directory()
    return _ok({"added": added}, message=t("package.scan_complete", locale))


@router.get("/packages/{package_id}")
async def get_package(
    package_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
) -> dict[str, Any]:
    package = raven_package_service.get_package(package_id)
    if not package:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=t("package.not_found", locale),
        )
    # ``canEditMetadata`` is a presentation-only hint so the detail page can
    # show edit controls to eligible users; the PATCH endpoint re-checks.
    data = dict(package)
    data["canEditMetadata"] = await _can_edit_package_metadata(db, package, current_user)
    return _ok(data)


@router.patch("/packages/{package_id}/metadata")
async def update_package_metadata(
    package_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_optional_user),
    locale: str = Depends(get_request_locale),
) -> dict[str, Any]:
    """Update only the Raven package ``description`` and/or ``tags``.

    Body: ``{description?: string | null, tags?: string[]}`` with at least one
    field present. Authorized for global admins and members of the package's
    enabled project. Non-editable package asset fields are never touched.
    """
    package = raven_package_service.get_package(package_id)
    if not package:
        # 401 for anonymous callers takes priority so we don't leak existence,
        # but an authenticated caller hitting a missing package gets 404.
        if current_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=t("auth.not_logged_in", locale),
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=t("package.not_found", locale),
        )

    await _require_package_metadata_edit(db, package, current_user, locale)

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.body_not_object", locale),
        )

    has_description = "description" in body
    has_tags = "tags" in body
    if not has_description and not has_tags:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.metadata_patch_empty", locale),
        )

    updates: dict[str, Any] = {}
    try:
        if has_description:
            updates["description"] = normalize_description(body.get("description"))
        if has_tags:
            updates["tags"] = normalize_tags(body.get("tags"))
    except MetadataValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t(f"package.{exc.code}", locale, **exc.params),
        ) from exc

    saved = raven_package_service.update_package_metadata(package_id, **updates)
    if saved is None:
        # Race: package removed between load and write.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=t("package.not_found", locale),
        )
    return _ok(saved, message=t("package.metadata_update_success", locale))


@router.delete("/packages/{package_id}")
async def delete_package(
    package_id: str,
    locale: str = Depends(get_request_locale),
) -> dict[str, Any]:
    if not raven_package_service.delete_package(package_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=t("package.not_found_or_delete_failed", locale),
        )
    return _ok(message=t("package.delete_success", locale))


@router.post("/upload")
async def upload_package(
    file: UploadFile = File(...),
    packageInfo: Optional[str] = Form(None),
    version: Optional[str] = Form(None),
    projectCode: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    isPatch: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    components: Optional[str] = Form(None),
    locale: str = Depends(get_request_locale),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Validate the project before touching the disk so an invalid project
    # never leaves a stored file behind.
    await validate_project_code(db, projectCode, locale)
    file_path, size, sha256 = await raven_package_service.store_upload(file)
    try:
        package = raven_package_service.build_package_info(
            file_path=file_path,
            size=size,
            sha256=sha256,
            metadata_fields=_metadata_fields(version, projectCode, description, isPatch, tags, components),
            package_info=_parse_package_info(packageInfo, locale),
        )
        saved = raven_package_service.add_or_update_package(package)
        await _record_package_activity(
            action="upload", project_code=saved.get("projectCode")
        )
        return _ok(message=t("package.upload_success", locale), package=saved)
    except Exception:
        raven_package_service.cleanup_file(file_path)
        raise


@router.post("/upload/batch")
async def upload_package_batch(
    request: Request,
    locale: str = Depends(get_request_locale),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    form = await request.form()
    files = list(form.getlist("file")) or list(form.getlist("files"))
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.no_files_uploaded", locale),
        )

    await validate_project_code(db, form.get("projectCode"), locale)
    fields = _metadata_fields(
        version=form.get("version"),
        projectCode=form.get("projectCode"),
        description=form.get("description"),
        isPatch=form.get("isPatch"),
        tags=form.get("tags"),
        components=form.get("components"),
    )
    results = []
    errors = []
    for upload in files:
        if not hasattr(upload, "filename") or not hasattr(upload, "read"):
            continue
        try:
            file_path, size, sha256 = await raven_package_service.store_upload(upload)
            package = raven_package_service.build_package_info(file_path, size, sha256, metadata_fields=fields)
            saved = raven_package_service.add_or_update_package(package)
            results.append(saved)
            await _record_package_activity(
                action="upload", project_code=saved.get("projectCode")
            )
        except Exception as exc:
            errors.append({"filename": getattr(upload, "filename", ""), "error": str(exc)})

    return _ok(
        message=t("package.batch_upload_success", locale, count=len(results)),
        packages=results,
        errors=errors or None,
    )


@router.get("/upload/progress/{upload_id}")
async def upload_progress(upload_id: str) -> dict[str, Any]:
    return {"uploadId": upload_id, "progress": 100, "status": "completed"}


@router.get("/download/stats")
async def download_stats() -> dict[str, Any]:
    packages = raven_package_service.get_all_packages()
    by_project: dict[str, int] = {}
    for package in packages:
        project_code = str(package.get("projectCode") or "") or "unassociated"
        by_project[project_code] = by_project.get(project_code, 0) + 1
    return _ok({"totalDownloads": 0, "popularPackages": packages[:5], "downloadsByProject": by_project})


@router.post("/download/batch")
async def download_batch(
    request: Request,
    locale: str = Depends(get_request_locale),
) -> FileResponse:
    body = await request.json()
    package_ids = body.get("packageIds")
    if not isinstance(package_ids, list) or not package_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.ids_required", locale),
        )
    packages = [pkg for pkg_id in package_ids if (pkg := raven_package_service.get_package(str(pkg_id)))]
    if not packages:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=t("package.no_valid_packages", locale),
        )
    zip_path = raven_package_service.build_zip(packages)
    filename = f"packages-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    await _record_package_activity(
        action="download_batch", project_code=None, count=len(packages)
    )
    return FileResponse(
        str(zip_path),
        filename=filename,
        media_type="application/zip",
        background=BackgroundTask(raven_package_service.cleanup_file, zip_path),
    )


@router.get("/download/project/{project_code}")
async def download_by_project(
    project_code: str,
    version: Optional[str] = None,
    locale: str = Depends(get_request_locale),
) -> FileResponse:
    packages = [
        pkg for pkg in raven_package_service.get_all_packages()
        if pkg.get("projectCode") == project_code and (not version or pkg.get("version") == version)
    ]
    if not packages:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=t("package.none_for_criteria", locale),
        )
    await _record_package_activity(
        action="download_project", project_code=project_code, count=len(packages)
    )
    if len(packages) == 1:
        return _package_file_response(packages[0], locale)
    zip_path = raven_package_service.build_zip(packages, prefix=f"{project_code}-packages")
    filename = f"{project_code}-packages-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return FileResponse(
        str(zip_path),
        filename=filename,
        media_type="application/zip",
        background=BackgroundTask(raven_package_service.cleanup_file, zip_path),
    )


@router.get("/download/{package_id}")
async def download_package(
    package_id: str,
    locale: str = Depends(get_request_locale),
) -> FileResponse:
    package = raven_package_service.get_package(package_id)
    if not package:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=t("package.not_found", locale),
        )
    response = _package_file_response(package, locale)
    await _record_package_activity(
        action="download", project_code=package.get("projectCode")
    )
    return response


def _package_file_response(package: dict[str, Any], locale: str = "zh") -> FileResponse:
    file_path = raven_package_service.package_file(package)
    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=t("package.file_not_found", locale),
        )
    return FileResponse(
        str(file_path),
        filename=str(package.get("name") or Path(file_path).name),
        media_type=(
            "application/octet-stream"
            if file_path.suffix.lower() == ".upkg"
            else "application/gzip"
        ),
    )


def _validate_search_query(raw: Any, locale: str = "zh") -> str:
    """Coerce + validate the agent-search ``query`` field.

    Raises HTTPException 400 when:
    - missing or non-string;
    - empty / whitespace-only;
    - longer than ``PACKAGE_SEARCH_QUERY_MAX_LEN`` characters.
    """
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.query_required", locale),
        )
    text = raw.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.query_empty", locale),
        )
    if len(text) > PACKAGE_SEARCH_QUERY_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.query_too_long", locale, max=PACKAGE_SEARCH_QUERY_MAX_LEN),
        )
    return text


@router.post("/packages/agent-search")
async def agent_search_packages(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_optional_user),
):
    """Claude Agent SDK driven Raven package search (project-bound).

    Body: ``{query: string, project_repo_id: int, session_id?: string, stream?: bool}``.

    ``project_repo_id`` is mandatory — the run is bound to that project: the
    package metadata MCP tools are server-side scoped to it and the agent may
    clone its repository. Each request prepares an isolated workspace and
    cleans it up afterwards (no session-level reuse on this endpoint).

    - ``stream=false`` (default): blocking JSON response with the structured
      recommendation, tool trace, model & usage.
    - ``stream=true``: ``text/event-stream`` SSE feed of
      ``AgentTraceEvent`` dicts (matching ``docs/agent_trace_protocol.md``)
      terminated by a synthetic ``final`` event whose ``data`` is the same
      payload as the non-stream response.
    """
    from app.agents.package_search import workspace as pkg_workspace
    from app.agents.package_search.agent import PackageSearchAgent
    from app.i18n.deps import LOCALE_HEADER, resolve_locale

    locale = resolve_locale(
        header_locale=request.headers.get(LOCALE_HEADER),
        accept_language=request.headers.get("Accept-Language"),
        user=current_user,
    )

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.body_not_object", locale),
        )
    query = _validate_search_query(body.get("query"), locale)
    session_id = body.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.session_id_not_string", locale),
        )
    use_stream = bool(body.get("stream") or False)

    project_repo_id = body.get("project_repo_id")
    if not isinstance(project_repo_id, int) or isinstance(project_repo_id, bool):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "reason": "project_repo_required",
                "message": t("package.project_repo_required", locale),
            },
        )

    from app.services import project_repo_service

    repo = await project_repo_service.get_by_id(db, project_repo_id)
    if (
        repo is None
        or not getattr(repo, "enabled", True)
        or not await project_repo_service.supports_agent(db, repo, "package_search")
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "reason": "project_repo_required",
                "message": t(
                    "package.project_invalid", locale, code=str(project_repo_id)
                ),
            },
        )

    ctx = pkg_workspace.prepare(
        project_repo=repo, question=query, session_id=session_id
    )
    ctx.locale = locale
    agent = PackageSearchAgent()

    if not use_stream:
        try:
            result = await agent.run(ctx)
        finally:
            pkg_workspace.cleanup(ctx)
        await _record_package_search_metrics(result, current_user, project_repo_id)
        return {
            "answer": result["answer"],
            "recommended_package_ids": result["recommended_package_ids"],
            "relevant_package_ids": result["relevant_package_ids"],
            "notes": result.get("notes"),
            "tool_trace": result["tool_trace"],
            "model": result["model"],
            "usage": result["usage"],
        }

    async def _sse():
        final_event: Optional[dict] = None
        try:
            async for event in agent.stream(ctx):
                if isinstance(event, dict) and event.get("type") == "final":
                    final_event = event
                yield f"event: {event.get('type', 'message')}\n"
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        except Exception as exc:  # noqa: BLE001
            err = {
                "type": "error",
                "error_kind": type(exc).__name__,
                "message": str(exc),
            }
            yield f"event: error\n"
            yield f"data: {json.dumps(err, ensure_ascii=False, default=str)}\n\n"
        finally:
            pkg_workspace.cleanup(ctx)
            # Record metrics once the stream terminates. The synthetic ``final``
            # event carries the same model/usage payload as the non-stream result.
            if isinstance(final_event, dict):
                synthetic = dict(final_event.get("data") or {})
                synthetic.setdefault(
                    "session_id", final_event.get("task_id") or session_id
                )
                await _record_package_search_metrics(
                    synthetic, current_user, project_repo_id
                )

    return StreamingResponse(_sse(), media_type="text/event-stream")
