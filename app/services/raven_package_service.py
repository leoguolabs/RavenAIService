"""Raven rebuild package management service.

The legacy Node package service stored package metadata in a JSON array and
package files on disk. This service keeps that storage contract so existing
volumes can be reused by the unified FastAPI backend.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Generator, Iterable, Optional

try:  # POSIX deployments use flock; other platforms retain process-local safety.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

from fastapi import HTTPException, UploadFile, status

try:
    from packaging.version import InvalidVersion, Version, parse as _parse_version
except ImportError:  # pragma: no cover - packaging is a hard dep of project
    Version = None  # type: ignore[assignment]
    InvalidVersion = Exception  # type: ignore[assignment]
    _parse_version = None  # type: ignore[assignment]

from app.config import settings
from app.i18n.messages import t
from app.utils.storage_utils import get_free_bytes

logger = logging.getLogger(__name__)

# Special filter value selecting packages without a project association.
UNASSOCIATED_PROJECT = "__unassociated__"

# Software uploads/scans share one allow-list; configuration-manager output
# remains independently restricted to the formats its builder generates.
PACKAGE_UPLOAD_SUFFIXES = (".tgz", ".tar.gz", ".upkg")

# ─────────────── Editable package metadata limits & validation ───────────────
# These bound the only two fields the metadata-edit flow may change so a direct
# API caller cannot bloat the JSON store. They are intentionally generous; the
# UI never needs the full range.
PACKAGE_DESCRIPTION_MAX_LEN = 4000
PACKAGE_TAG_MAX_COUNT = 30
PACKAGE_TAG_MAX_LEN = 64

# Sentinel marking an editable field as "not supplied" so a caller can clear a
# field to empty (``description=None`` / ``tags=[]``) without it being confused
# with "leave unchanged".
_UNSET: Any = object()


class _RepositoryMutex:
    """Re-entrant repository lock shared by all service instances in-process.

    The process-local ``RLock`` is required because ``flock`` behaviour for two
    independently opened descriptors in one process differs between POSIX
    implementations.  The outermost acquisition additionally takes an
    exclusive advisory file lock, so separate API/worker processes serialize
    the same JSON repository.  Nested service calls reuse the descriptor owned
    by the current thread and therefore cannot deadlock themselves.
    """

    _UNSUPPORTED_ERRNOS = {
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }

    def __init__(self, lock_file: Path) -> None:
        self.lock_file = lock_file
        self._thread_lock = threading.RLock()
        self._local = threading.local()
        self._warned_fallback = False

    @contextmanager
    def acquire(self) -> Generator[None, None, None]:
        with self._thread_lock:
            depth = int(getattr(self._local, "depth", 0))
            if depth == 0:
                self._local.fd = self._acquire_file_lock()
            self._local.depth = depth + 1
            try:
                yield
            finally:
                remaining = int(self._local.depth) - 1
                self._local.depth = remaining
                if remaining == 0:
                    fd = getattr(self._local, "fd", None)
                    try:
                        if fd is not None and fcntl is not None:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        if fd is not None:
                            os.close(fd)
                        self._local.fd = None

    def _acquire_file_lock(self) -> Optional[int]:
        if fcntl is None:
            self._warn_fallback("当前平台不支持 fcntl.flock")
            return None

        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            os.close(fd)
            if exc.errno in self._UNSUPPORTED_ERRNOS:
                self._warn_fallback(f"文件系统不支持 flock: {exc}")
                return None
            raise
        return fd

    def _warn_fallback(self, reason: str) -> None:
        if self._warned_fallback:
            return
        self._warned_fallback = True
        logger.warning("Raven 包仓库跨进程文件锁不可用，将退化为进程内互斥：%s", reason)


_REPOSITORY_MUTEXES: dict[str, _RepositoryMutex] = {}
_REPOSITORY_MUTEXES_GUARD = threading.Lock()


def _repository_mutex_for(metadata_file: Path) -> _RepositoryMutex:
    """Return one shared mutex for a canonical metadata lock-file path."""

    lock_file = metadata_file.parent / f".{metadata_file.name}.lock"
    key = str(lock_file.resolve(strict=False))
    with _REPOSITORY_MUTEXES_GUARD:
        mutex = _REPOSITORY_MUTEXES.get(key)
        if mutex is None:
            mutex = _RepositoryMutex(lock_file)
            _REPOSITORY_MUTEXES[key] = mutex
        return mutex


class MetadataValidationError(ValueError):
    """Raised when an editable metadata field fails normalization.

    Carries a stable ``code`` so the API layer can map it to a localized 400
    message, plus optional ``params`` for message interpolation (e.g. ``max``).
    """

    def __init__(self, code: str, **params: Any) -> None:
        super().__init__(code)
        self.code = code
        self.params = params


def normalize_description(value: Any) -> str:
    """Trim a description, allow clearing to empty, cap at the max length.

    ``None`` clears the field to an empty string. Non-string values and
    over-length input raise :class:`MetadataValidationError`.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MetadataValidationError("metadata_description_invalid")
    text = value.strip()
    if len(text) > PACKAGE_DESCRIPTION_MAX_LEN:
        raise MetadataValidationError(
            "metadata_description_too_long", max=PACKAGE_DESCRIPTION_MAX_LEN
        )
    return text


def normalize_tags(value: Any) -> list[str]:
    """Normalize tags to a unique, ordered list of trimmed non-empty strings.

    Input must be a list of strings. Each tag is trimmed; empty tags are
    dropped; duplicates (by exact trimmed value) are removed keeping first-seen
    order. Over-length tags or too many tags raise
    :class:`MetadataValidationError`.
    """
    if not isinstance(value, list):
        raise MetadataValidationError("metadata_tags_invalid")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise MetadataValidationError("metadata_tags_invalid")
        tag = item.strip()
        if not tag:
            continue
        if len(tag) > PACKAGE_TAG_MAX_LEN:
            raise MetadataValidationError(
                "metadata_tag_too_long", max=PACKAGE_TAG_MAX_LEN
            )
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    if len(out) > PACKAGE_TAG_MAX_COUNT:
        raise MetadataValidationError(
            "metadata_tags_too_many", max=PACKAGE_TAG_MAX_COUNT
        )
    return out


def _normalize_project_code(package: dict[str, Any]) -> dict[str, Any]:
    """Idempotent lazy migration: ensure ``projectCode`` on a package record.

    Legacy records carry a hardcoded ``packageType``; its value becomes the
    ``projectCode``. The original ``packageType`` key is kept untouched so a
    rolled-back deployment can still read the same metadata file. The read
    path never writes the file just for this normalization — the result is
    persisted along with the next regular write.
    """
    if "projectCode" not in package:
        package["projectCode"] = str(package.get("packageType") or "")
    return package


def _abs_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(settings.base_dir) / path


def _json_or_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_list(value: Any) -> list[Any]:
    parsed = _json_or_value(value)
    if parsed is None or parsed == "":
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, tuple):
        return list(parsed)
    if isinstance(parsed, str):
        return [item.strip() for item in parsed.split(",") if item.strip()]
    return [parsed]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def validate_project_code(db: Any, project_code: Any, locale: str = "zh") -> Any:
    """Validate that ``project_code`` maps to a registered, enabled project.

    Returns the matching ``ProjectRepo`` record. Raises HTTPException 400 when
    the code is missing, unregistered, or the project is disabled. Shared by
    the upload APIs so the package-project association is always backed by the
    project registry.
    """
    code = str(project_code or "").strip()
    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.project_code_required", locale),
        )
    from app.services import project_repo_service

    # 配置管理员依赖项目配置/Skills，而不是代码仓库；无仓库项目同样有效。
    # 是否启用 package_search 仍由 supports_agent 在下一步强制校验。
    repo = await project_repo_service.get_by_project_code(db, code, require_repo=False)
    if repo is None or not await project_repo_service.supports_agent(
        db, repo, "package_search"
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("package.project_invalid", locale, code=code),
        )
    return repo


class RavenPackageService:
    def __init__(self) -> None:
        self.data_dir = _abs_path(settings.raven_data_dir)
        self.uploads_dir = _abs_path(settings.upload_dir)
        self.metadata_file = _abs_path(settings.raven_metadata_file)
        # Package files and the JSON index form one repository.  All service
        # instances that target this metadata file share a process-local
        # re-entrant mutex, backed by a POSIX flock for worker/API processes.
        self._repo_mutex = _repository_mutex_for(self.metadata_file)
        # mkdir is deferred to first repository access to avoid import-time
        # failures when configured paths are not writable.

    @contextmanager
    def _repository_lock(self) -> Generator[None, None, None]:
        # Tests and administrative reconfiguration may retarget an existing
        # service instance. Resolve by the current metadata path on every
        # outer call so the flock never remains bound to the old repository.
        mutex = _repository_mutex_for(self.metadata_file)
        self._repo_mutex = mutex
        with mutex.acquire():
            yield

    def load_packages(self) -> list[dict[str, Any]]:
        with self._repository_lock():
            if not self.metadata_file.exists():
                return []
            try:
                data = json.loads(self.metadata_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("读取 Raven 包元数据失败: %s", exc)
                return []
            if not isinstance(data, list):
                return []
            return [
                _normalize_project_code(pkg) for pkg in data if isinstance(pkg, dict)
            ]

    def save_packages(self, packages: list[dict[str, Any]]) -> None:
        with self._repository_lock():
            self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                packages, ensure_ascii=False, indent=2, default=str
            ).encode("utf-8")
            temp_path = self.metadata_file.parent / (
                f".{self.metadata_file.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                with temp_path.open("wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.metadata_file)
                # Best-effort directory fsync makes the rename durable on POSIX.
                try:
                    dir_fd = os.open(self.metadata_file.parent, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
            finally:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)

    def package_file(self, package: dict[str, Any]) -> Path:
        path = Path(str(package.get("path") or ""))
        return path if path.is_absolute() else self.uploads_dir / path

    def get_all_packages(self, prune_missing: bool = True) -> list[dict[str, Any]]:
        with self._repository_lock():
            packages = self.load_packages()
            if not prune_missing:
                return packages

            existing: list[dict[str, Any]] = []
            removed = 0
            for package in packages:
                if self.package_file(package).exists():
                    existing.append(package)
                else:
                    removed += 1

            if removed:
                self.save_packages(existing)
                logger.info("Raven 包元数据清理完成，移除不存在文件记录 %s 条", removed)
            return existing

    def get_package(self, package_id: str) -> Optional[dict[str, Any]]:
        return next(
            (pkg for pkg in self.get_all_packages() if pkg.get("id") == package_id),
            None,
        )

    def add_or_update_package(self, package: dict[str, Any]) -> dict[str, Any]:
        with self._repository_lock():
            packages = self.load_packages()
            target_path = str(package.get("path") or "")
            updated = False

            for index, existing in enumerate(packages):
                if existing.get("path") == target_path:
                    package["id"] = existing.get("id") or package.get("id")
                    packages[index] = {**existing, **package}
                    updated = True
                    break

            if not updated:
                packages.append(package)

            self.save_packages(packages)
            return package

    def update_package_metadata(
        self,
        package_id: str,
        *,
        description: Any = _UNSET,
        tags: Any = _UNSET,
    ) -> Optional[dict[str, Any]]:
        """Update only ``metadata.description`` and/or ``metadata.tags``.

        Pass a value to change a field, omit it to leave it untouched. All
        non-editable package fields (``path``, ``size``, ``metadata.sha256``,
        ``version``, ``projectCode``, ``metadata.isPatch``,
        ``metadata.components``, ``createdAt`` …) are preserved. The change is
        persisted to the metadata JSON file and the saved package is returned.
        Returns ``None`` when no package matches ``package_id``.

        Values are expected to be already normalized by the caller
        (:func:`normalize_description` / :func:`normalize_tags`).
        """
        with self._repository_lock():
            packages = self.load_packages()
            target_index = next(
                (i for i, pkg in enumerate(packages) if pkg.get("id") == package_id),
                None,
            )
            if target_index is None:
                return None

            package = packages[target_index]
            metadata = dict(package.get("metadata") or {})
            if description is not _UNSET:
                metadata["description"] = description
            if tags is not _UNSET:
                metadata["tags"] = list(tags)
            package["metadata"] = metadata
            packages[target_index] = package

            self.save_packages(packages)
            return package

    def delete_package(self, package_id: str) -> bool:
        with self._repository_lock():
            packages = self.load_packages()
            target = next(
                (pkg for pkg in packages if pkg.get("id") == package_id), None
            )
            if not target:
                return False

            file_path = self.package_file(target)
            if file_path.exists():
                file_path.unlink()
            self.save_packages([pkg for pkg in packages if pkg.get("id") != package_id])
            return True

    async def store_upload(self, file: UploadFile) -> tuple[Path, int, str]:
        if not file.filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件缺少文件名"
            )

        lower_name = file.filename.lower()
        if not lower_name.endswith(PACKAGE_UPLOAD_SUFFIXES):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="仅支持 .tgz、.tar.gz 和 .upkg / Only .tgz, .tar.gz and .upkg files are allowed",
            )

        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        dest = self._unique_upload_path(file.filename)
        sha256_hash = hashlib.sha256()
        total = 0
        limit = settings.upload_max_size_mb * 1024 * 1024

        try:
            with dest.open("wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"文件过大，超过 {settings.upload_max_size_mb}MB 限制",
                        )
                    free_bytes = get_free_bytes(self.uploads_dir)
                    if free_bytes - settings.disk_reserve_bytes < len(chunk):
                        raise HTTPException(
                            status_code=507, detail="磁盘空间不足，无法完成上传"
                        )
                    out.write(chunk)
                    sha256_hash.update(chunk)
                out.flush()
                os.fsync(out.fileno())
        except Exception:
            if dest.exists():
                dest.unlink()
            raise
        finally:
            self._release_upload_reservation(dest)
            await file.close()

        return dest, total, sha256_hash.hexdigest()

    def publish_built_package(
        self,
        file_path: Path,
        *,
        confirmed_plan: dict[str, Any],
        components: Optional[list[Any]] = None,
        description: str = "由配置管理员在用户逐项确认后生成",
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Atomically copy a confirmed full package into the Raven repository.

        This is an internal side-effect boundary for the Configuration Manager;
        it is intentionally not exposed as an Agent shell command or public MCP
        endpoint.  A caller must provide the plan produced by the mandatory
        confirmation gate.  The artifact is copied through a temporary file,
        fsynced, renamed, then registered in the atomically-written JSON index;
        any metadata failure rolls the copied artifact back.
        """
        from app.services.package_confirmation_service import verify_confirmed_plan

        source = Path(file_path)
        if not source.is_file() or source.is_symlink():
            raise ValueError("待发布整包不存在或不是普通文件")
        lower_name = source.name.lower()
        if not (lower_name.endswith(".tgz") or lower_name.endswith(".tar.gz")):
            raise ValueError("配置管理员只允许发布 .tgz/.tar.gz 整包")
        verify_confirmed_plan(confirmed_plan)

        project_code = str(
            confirmed_plan.get("project_code")
            or (confirmed_plan.get("project") or {}).get("project_code")
            or ""
        ).strip()
        version = str(confirmed_plan.get("version") or "0.0.0").strip()
        mode = str(
            confirmed_plan.get("mode") or confirmed_plan.get("package_mode") or "full"
        ).lower()
        if not project_code:
            raise ValueError("已确认计划缺少项目代号")

        size = source.stat().st_size
        limit = int(settings.upload_max_size_mb) * 1024 * 1024
        if size <= 0:
            raise ValueError("整包文件为空")
        if size > limit:
            raise ValueError(f"整包超过 {settings.upload_max_size_mb}MB 仓库限制")

        with self._repository_lock():
            self.uploads_dir.mkdir(parents=True, exist_ok=True)
            if (
                get_free_bytes(self.uploads_dir) - int(settings.disk_reserve_bytes)
                < size
            ):
                raise OSError("磁盘空间不足，无法发布整包")
            destination = self._unique_upload_path(source.name)
            partial = (
                self.uploads_dir / f".{destination.name}.{uuid.uuid4().hex}.partial"
            )
            digest = hashlib.sha256()
            try:
                with source.open("rb") as incoming, partial.open("xb") as outgoing:
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                        outgoing.write(chunk)
                        digest.update(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                os.replace(partial, destination)

                custom_fields = {
                    "packagingAgent": "configuration_manager",
                    "packagingPlanHash": str(confirmed_plan.get("plan_hash")),
                    "packagingConfirmationHash": str(
                        confirmed_plan.get("confirmation_hash")
                    ),
                    "packagingRunId": str(confirmed_plan.get("run_id") or ""),
                    "sourceInputs": [
                        {
                            "uploadId": item.get("upload_id"),
                            "name": item.get("original_name") or item.get("name"),
                            "sha256": item.get("sha256"),
                            "component": item.get("selected_component")
                            or item.get("component"),
                        }
                        for item in confirmed_plan.get("inputs") or []
                        if isinstance(item, dict) and item.get("include", True)
                    ],
                }
                package = {
                    "id": str(uuid.uuid4()),
                    "name": destination.name,
                    "path": str(destination),
                    "size": size,
                    "createdAt": _now_iso(),
                    "projectCode": project_code,
                    "version": version,
                    "metadata": {
                        "isPatch": mode in {"patch", "incremental"},
                        "components": self.normalize_components(
                            list(components or []), version
                        ),
                        "description": normalize_description(description),
                        "tags": normalize_tags(tags or ["configuration-manager"]),
                        "sha256": digest.hexdigest(),
                        "customFields": custom_fields,
                    },
                }
                self.add_or_update_package(package)
                return package
            except Exception:
                partial.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
                raise
            finally:
                self._release_upload_reservation(destination)

    def build_package_info(
        self,
        file_path: Path,
        size: int,
        sha256: str,
        metadata_fields: Optional[dict[str, Any]] = None,
        package_info: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        metadata_fields = metadata_fields or {}
        base = (
            package_info.copy()
            if package_info
            else self.extract_package_metadata(file_path, size, sha256)
        )

        if package_info:
            base["path"] = str(file_path)
            base["size"] = size
            base.setdefault("id", str(uuid.uuid4()))
            base.setdefault("name", file_path.name)
            base.setdefault("createdAt", _now_iso())
            base.setdefault("version", self.parse_version(file_path.name) or "0.0.0")
            base.setdefault("metadata", {})
        base.setdefault("projectCode", "")

        metadata = dict(base.get("metadata") or {})
        if "description" in metadata_fields:
            metadata["description"] = metadata_fields.get("description") or ""
        if "isPatch" in metadata_fields:
            metadata["isPatch"] = _as_bool(metadata_fields.get("isPatch"))
        if "tags" in metadata_fields:
            metadata["tags"] = [
                str(item) for item in _as_list(metadata_fields.get("tags"))
            ]
        if "components" in metadata_fields:
            metadata["components"] = self.normalize_components(
                _as_list(metadata_fields.get("components")), base.get("version")
            )
        metadata["sha256"] = sha256
        metadata.setdefault("customFields", {})

        if metadata_fields.get("version"):
            base["version"] = str(metadata_fields["version"])
        if metadata_fields.get("projectCode"):
            base["projectCode"] = str(metadata_fields["projectCode"])

        base["metadata"] = metadata
        return base

    def extract_package_metadata(
        self, file_path: Path, size: Optional[int] = None, sha256: Optional[str] = None
    ) -> dict[str, Any]:
        stat = file_path.stat()
        version = self.parse_version(file_path.name) or "0.0.0"
        return {
            "id": str(uuid.uuid4()),
            "name": file_path.name,
            "path": str(file_path),
            "size": size if size is not None else stat.st_size,
            "createdAt": datetime.fromtimestamp(
                stat.st_ctime, timezone.utc
            ).isoformat(),
            # Scanned orphan files stay unassociated; the project must be
            # assigned explicitly (no filename guessing).
            "projectCode": "",
            "version": version,
            "metadata": {
                "isPatch": "patch" in file_path.name.lower(),
                # 组件不再从文件名猜测（卫星协议栈/OAM 等）；扫描到的孤立包组件留空，
                # 由显式上传的 metadata 或后续编辑补全。
                "components": [],
                "description": "",
                "tags": [],
                "sha256": sha256 or self.calculate_hash(file_path),
                "customFields": {},
            },
        }

    def scan_uploads_directory(self) -> int:
        with self._repository_lock():
            packages = self.load_packages()
            known_paths = {str(pkg.get("path")) for pkg in packages}
            added = 0
            if not self.uploads_dir.exists():
                return 0
            for file_path in self.uploads_dir.iterdir():
                if not file_path.is_file():
                    continue
                lower = file_path.name.lower()
                if not lower.endswith(PACKAGE_UPLOAD_SUFFIXES):
                    continue
                # Upload/publication creates the final target with O_EXCL and
                # keeps this marker until the bytes are fully fsynced.  Do not
                # index a transient zero/partial file.
                if self._upload_reservation_path(file_path).exists():
                    continue
                if str(file_path) in known_paths:
                    continue
                packages.append(self.extract_package_metadata(file_path))
                added += 1
            if added:
                self.save_packages(packages)
            return added

    def filter_packages(
        self, params: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        packages = self.get_all_packages()
        search = str(params.get("search") or "").lower()
        project_code = str(params.get("projectCode") or "")
        version = str(params.get("version") or "")
        tags = str(params.get("tags") or "").lower()
        is_patch = params.get("isPatch")

        if search:
            packages = [
                pkg
                for pkg in packages
                if search in str(pkg.get("name", "")).lower()
                or search in str(pkg.get("version", "")).lower()
                or search in str(pkg.get("metadata", {}).get("description", "")).lower()
            ]
        if project_code == UNASSOCIATED_PROJECT:
            packages = [pkg for pkg in packages if not pkg.get("projectCode")]
        elif project_code:
            packages = [
                pkg for pkg in packages if pkg.get("projectCode") == project_code
            ]
        if version:
            packages = [
                pkg for pkg in packages if str(pkg.get("version", "")) == version
            ]
        if tags:
            packages = [
                pkg
                for pkg in packages
                if any(
                    tags in str(tag).lower()
                    for tag in pkg.get("metadata", {}).get("tags", [])
                )
            ]
        if is_patch not in (None, ""):
            expected = _as_bool(is_patch)
            packages = [
                pkg
                for pkg in packages
                if _as_bool(pkg.get("metadata", {}).get("isPatch")) == expected
            ]

        sort_by = str(params.get("sortBy") or "createdAt")
        reverse = str(params.get("sortOrder") or "desc").lower() == "desc"
        packages.sort(key=lambda pkg: self._sort_value(pkg, sort_by), reverse=reverse)

        page = max(int(params.get("page") or 1), 1)
        limit = max(int(params.get("limit") or 10), 1)
        total = len(packages)
        start = (page - 1) * limit
        end = start + limit
        return packages[start:end], {
            "currentPage": page,
            "totalPages": (total + limit - 1) // limit,
            "totalItems": total,
            "itemsPerPage": limit,
        }

    def build_zip(
        self, packages: Iterable[dict[str, Any]], prefix: str = "packages"
    ) -> Path:
        temp = NamedTemporaryFile(prefix=f"{prefix}-", suffix=".zip", delete=False)
        temp_path = Path(temp.name)
        temp.close()
        with zipfile.ZipFile(
            temp_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for package in packages:
                file_path = self.package_file(package)
                if file_path.exists():
                    archive.write(
                        file_path, arcname=str(package.get("name") or file_path.name)
                    )
        return temp_path

    def calculate_hash(self, file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def parse_version(self, filename: str) -> Optional[str]:
        match = re.search(r"[Vv]?(\d+(?:\.\d+)*)", filename)
        return match.group(1) if match else None

    def normalize_components(
        self, components: list[Any], version: Any = None
    ) -> list[Any]:
        normalized = []
        for component in components:
            if isinstance(component, dict):
                normalized.append(component)
            else:
                normalized.append({"name": str(component), "version": version or ""})
        return normalized

    def _unique_upload_path(self, filename: str) -> Path:
        """Atomically reserve and return a collision-free repository target.

        A marker and the final pathname are both created with ``O_EXCL``.
        Every writer therefore observes a distinct target even when multiple
        service instances/processes publish the same filename at once.  The
        marker also lets the repository scanner ignore an in-progress upload.
        Callers must release it with :meth:`_release_upload_reservation`.
        """

        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename).name
        initial = self.uploads_dir / safe_name
        stem = (
            initial.name[:-7]
            if initial.name.lower().endswith(".tar.gz")
            else initial.stem
        )
        suffix = (
            ".tar.gz" if initial.name.lower().endswith(".tar.gz") else initial.suffix
        )

        # Atomic creation is sufficient for name allocation, but use the
        # repository mutex as well so local store/publish/scan transitions have
        # one consistent ordering.
        with self._repository_lock():
            for attempt in range(256):
                if attempt == 0:
                    candidate = initial
                else:
                    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                    candidate = self.uploads_dir / (
                        f"{stem}_{timestamp}_{uuid.uuid4().hex[:8]}{suffix}"
                    )
                marker = self._upload_reservation_path(candidate)
                marker_fd: Optional[int] = None
                target_fd: Optional[int] = None
                try:
                    marker_fd = os.open(
                        marker,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                    )
                    target_fd = os.open(
                        candidate,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o640,
                    )
                    return candidate
                except FileExistsError:
                    if marker_fd is not None:
                        marker.unlink(missing_ok=True)
                    continue
                except Exception:
                    if marker_fd is not None:
                        marker.unlink(missing_ok=True)
                    raise
                finally:
                    if target_fd is not None:
                        os.close(target_fd)
                    if marker_fd is not None:
                        os.close(marker_fd)
            raise FileExistsError("无法为 Raven 包分配唯一上传文件名")

    def _upload_reservation_path(self, destination: Path) -> Path:
        return destination.parent / f".{destination.name}.uploading"

    def _release_upload_reservation(self, destination: Path) -> None:
        self._upload_reservation_path(destination).unlink(missing_ok=True)

    def _sort_value(self, package: dict[str, Any], key: str) -> Any:
        if key == "createdAt":
            value = package.get("createdAt") or ""
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return datetime.min.replace(tzinfo=timezone.utc)
        return package.get(key) or ""

    def cleanup_file(self, path: Path) -> None:
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    # ─────────────────── Agent 检索专用：数据访问 API ───────────────────

    @staticmethod
    def compare_versions(a: str, b: str) -> int:
        """Compare two version strings using SemVer semantics.

        Returns -1 if a < b, 0 if equal, 1 if a > b. Falls back to
        string comparison when either side is not parseable.
        """
        sa, sb = str(a or ""), str(b or "")
        if _parse_version is not None:
            try:
                va = _parse_version(sa)
                vb = _parse_version(sb)
                if va < vb:
                    return -1
                if va > vb:
                    return 1
                return 0
            except InvalidVersion:
                pass
        if sa < sb:
            return -1
        if sa > sb:
            return 1
        return 0

    @staticmethod
    def _is_prerelease(value: str) -> bool:
        if _parse_version is None:
            return False
        try:
            return bool(_parse_version(str(value or "")).is_prerelease)
        except InvalidVersion:
            return False

    @staticmethod
    def _component_names(package: dict[str, Any]) -> list[str]:
        meta = package.get("metadata") or {}
        out: list[str] = []
        for item in meta.get("components") or []:
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    out.append(str(name))
            else:
                out.append(str(item))
        return out

    def iter_brief(self, packages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Project an iterable of packages to PackageBrief shape.

        Brief = {id, name, version, projectCode, isPatch, createdAt,
        components, tags, size}. Excludes sha256 and disk path.
        """
        out: list[dict[str, Any]] = []
        for pkg in packages:
            meta = pkg.get("metadata") or {}
            out.append(
                {
                    "id": pkg.get("id"),
                    "name": pkg.get("name"),
                    "version": pkg.get("version"),
                    "projectCode": str(pkg.get("projectCode") or ""),
                    "isPatch": bool(meta.get("isPatch")),
                    "createdAt": pkg.get("createdAt"),
                    "components": self._component_names(pkg),
                    "tags": [str(t) for t in (meta.get("tags") or [])],
                    "size": pkg.get("size"),
                }
            )
        return out

    def _clamp_limit(self, limit: Optional[int], max_limit: Optional[int]) -> int:
        hard_cap = int(
            max_limit if max_limit is not None else settings.package_search_max_limit
        )
        if hard_cap < 1:
            hard_cap = 1
        default = int(settings.package_search_default_limit)
        try:
            value = int(limit) if limit is not None else default
        except (TypeError, ValueError):
            value = default
        if value < 1:
            value = 1
        return min(value, hard_cap)

    def _scoped_packages(self, project_code: Optional[str]) -> list[dict[str, Any]]:
        """All packages, optionally narrowed to one project.

        ``None`` means no scoping; ``UNASSOCIATED_PROJECT`` selects packages
        without a project association.
        """
        packages = self.get_all_packages()
        if project_code is None:
            return packages
        if project_code == UNASSOCIATED_PROJECT:
            return [p for p in packages if not p.get("projectCode")]
        return [p for p in packages if p.get("projectCode") == project_code]

    def query_packages(
        self,
        filters: Optional[dict[str, Any]] = None,
        sort: Optional[dict[str, Any]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        max_limit: Optional[int] = None,
        project_code: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Generic structured query against the package metadata store.

        Returns ``(brief_items, total_before_paging)``. Filters supported:
        ``is_patch``, ``tags`` (list), ``component`` (single name).
        Sort accepts ``{"by": "createdAt"|"version"|"name", "order": "asc"|"desc"}``.
        ``project_code`` scopes the query to a single project.
        """
        packages = self._scoped_packages(project_code)
        filters = filters or {}

        is_patch = filters.get("is_patch")
        if is_patch is not None:
            want = _as_bool(is_patch)
            packages = [
                p
                for p in packages
                if _as_bool((p.get("metadata") or {}).get("isPatch")) == want
            ]

        tags = filters.get("tags")
        if tags:
            wanted = {str(t).lower() for t in tags}
            packages = [
                p
                for p in packages
                if wanted.issubset(
                    {str(t).lower() for t in (p.get("metadata") or {}).get("tags", [])}
                )
            ]

        component = filters.get("component")
        if component:
            needle = str(component).lower()
            packages = [
                p
                for p in packages
                if any(needle == c.lower() for c in self._component_names(p))
            ]

        sort = sort or {}
        sort_by = str(sort.get("by") or "createdAt")
        sort_order = str(sort.get("order") or "desc").lower()
        reverse = sort_order != "asc"

        if sort_by == "version":
            from functools import cmp_to_key

            packages.sort(
                key=cmp_to_key(
                    lambda a, b: self.compare_versions(
                        a.get("version", ""), b.get("version", "")
                    )
                ),
                reverse=reverse,
            )
        else:
            packages.sort(key=lambda p: self._sort_value(p, sort_by), reverse=reverse)

        total = len(packages)
        start = max(int(offset or 0), 0)
        effective_limit = self._clamp_limit(limit, max_limit)
        sliced = packages[start : start + effective_limit]
        return self.iter_brief(sliced), total

    def text_search(
        self,
        text: str,
        fields: Optional[list[str]] = None,
        limit: Optional[int] = None,
        max_limit: Optional[int] = None,
        project_code: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Literal substring match across the requested fields.

        Returns ``(items_with_matched_fields, total_before_paging)``.
        Each item is a PackageBrief plus ``matched_fields: list[str]``.
        ``project_code`` scopes the search to a single project.
        """
        needle = str(text or "").strip().lower()
        if not needle:
            return [], 0
        allowed = {"name", "version", "description", "tags", "components"}
        targets = [f for f in (fields or list(allowed)) if f in allowed]
        if not targets:
            targets = list(allowed)

        matches: list[tuple[dict[str, Any], list[str]]] = []
        for pkg in self._scoped_packages(project_code):
            meta = pkg.get("metadata") or {}
            hit_fields: list[str] = []
            if "name" in targets and needle in str(pkg.get("name") or "").lower():
                hit_fields.append("name")
            if "version" in targets and needle in str(pkg.get("version") or "").lower():
                hit_fields.append("version")
            if (
                "description" in targets
                and needle in str(meta.get("description") or "").lower()
            ):
                hit_fields.append("description")
            if "tags" in targets and any(
                needle in str(t).lower() for t in (meta.get("tags") or [])
            ):
                hit_fields.append("tags")
            if "components" in targets and any(
                needle in c.lower() for c in self._component_names(pkg)
            ):
                hit_fields.append("components")
            if hit_fields:
                matches.append((pkg, hit_fields))

        total = len(matches)
        effective_limit = self._clamp_limit(limit, max_limit)
        sliced = matches[:effective_limit]

        items: list[dict[str, Any]] = []
        for pkg, hits in sliced:
            brief = self.iter_brief([pkg])[0]
            brief["matched_fields"] = hits
            items.append(brief)
        return items, total

    def version_filter(
        self,
        version_min: Optional[str] = None,
        version_max: Optional[str] = None,
        include_prerelease: bool = False,
        limit: Optional[int] = None,
        max_limit: Optional[int] = None,
        project_code: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """SemVer-aware version range filter.

        ``version_min`` is inclusive lower bound, ``version_max`` is inclusive
        upper bound. Both fall back to string comparison if a value is not
        parseable as a Version. ``project_code`` scopes to a single project.
        """
        candidates = self._scoped_packages(project_code)

        if not include_prerelease:
            candidates = [
                p for p in candidates if not self._is_prerelease(p.get("version", ""))
            ]

        if version_min is not None:
            candidates = [
                p
                for p in candidates
                if self.compare_versions(p.get("version", ""), version_min) >= 0
            ]
        if version_max is not None:
            candidates = [
                p
                for p in candidates
                if self.compare_versions(p.get("version", ""), version_max) <= 0
            ]

        from functools import cmp_to_key

        candidates.sort(
            key=cmp_to_key(
                lambda a, b: self.compare_versions(
                    a.get("version", ""), b.get("version", "")
                )
            ),
            reverse=True,
        )

        total = len(candidates)
        effective_limit = self._clamp_limit(limit, max_limit)
        return self.iter_brief(candidates[:effective_limit]), total

    def list_components(
        self, project_code: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Aggregate distinct components with usage counts.

        Returns list of ``{name, count, project_codes: [str]}`` sorted by
        count descending. ``project_code`` scopes to a single project.
        """
        packages = self._scoped_packages(project_code)

        agg: dict[str, dict[str, Any]] = {}
        for pkg in packages:
            code = str(pkg.get("projectCode") or "")
            for name in self._component_names(pkg):
                entry = agg.setdefault(
                    name, {"name": name, "count": 0, "project_codes": set()}
                )
                entry["count"] += 1
                if code:
                    entry["project_codes"].add(code)

        out = [
            {
                "name": v["name"],
                "count": v["count"],
                "project_codes": sorted(v["project_codes"]),
            }
            for v in agg.values()
        ]
        out.sort(key=lambda r: (-r["count"], r["name"]))
        return out

    def find_by_component(
        self,
        component_name: str,
        version: Optional[str] = None,
        limit: Optional[int] = None,
        max_limit: Optional[int] = None,
        project_code: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Find packages whose components include ``component_name``.

        Optional ``version`` matches against the component's own version
        (since components are recorded as ``{name, version}`` dicts).
        ``project_code`` scopes the lookup to a single project.
        """
        needle = str(component_name or "").lower()
        if not needle:
            return [], 0
        target_version = str(version).strip() if version else None

        matched: list[dict[str, Any]] = []
        for pkg in self._scoped_packages(project_code):
            meta = pkg.get("metadata") or {}
            for comp in meta.get("components") or []:
                if isinstance(comp, dict):
                    cname = str(comp.get("name") or "").lower()
                    cver = str(comp.get("version") or "")
                else:
                    cname = str(comp).lower()
                    cver = str(pkg.get("version") or "")
                if cname != needle:
                    continue
                if target_version is not None and cver != target_version:
                    continue
                matched.append(pkg)
                break

        total = len(matched)
        effective_limit = self._clamp_limit(limit, max_limit)
        return self.iter_brief(matched[:effective_limit]), total

    def stats_by(
        self, group_by: str, project_code: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Aggregate counts by one of ``version_major | tag | isPatch``.

        ``project_code`` scopes the aggregation to a single project.
        """
        valid = {"version_major", "tag", "isPatch"}
        if group_by not in valid:
            raise ValueError(
                f"group_by must be one of {sorted(valid)}, got {group_by!r}"
            )

        packages = self._scoped_packages(project_code)
        counts: dict[str, int] = {}

        if group_by == "version_major":
            for pkg in packages:
                ver = str(pkg.get("version") or "")
                major = ver.split(".", 1)[0] if ver else "unknown"
                if not major:
                    major = "unknown"
                counts[major] = counts.get(major, 0) + 1
        elif group_by == "tag":
            for pkg in packages:
                for tag in (pkg.get("metadata") or {}).get("tags") or []:
                    key = str(tag)
                    counts[key] = counts.get(key, 0) + 1
        elif group_by == "isPatch":
            for pkg in packages:
                key = (
                    "patch"
                    if _as_bool((pkg.get("metadata") or {}).get("isPatch"))
                    else "full"
                )
                counts[key] = counts.get(key, 0) + 1

        out = [{"key": k, "count": v} for k, v in counts.items()]
        out.sort(key=lambda r: (-r["count"], r["key"]))
        return out


raven_package_service = RavenPackageService()
