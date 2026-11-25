"""
日志分析工作区准备与清理。

职责：
- 为每个 Celery 任务在隔离临时目录下解压日志归档
- 创建 task.json（仅含非敏感字段）
- 解压大小保护（ai_analysis_max_extract_bytes）
- 任务结束后幂等清理临时目录
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from app.config import settings
from app.i18n import DEFAULT as I18N_DEFAULT
from app.tools.archive_tool import (  # noqa: F401 – re-exported for callers
    SUPPORTED_ARCHIVE_EXTS,
    SUPPORTED_SPREADSHEET_EXTS,
    SUPPORTED_TEXT_EXTS,
    detect_upload_kind,
)

logger = logging.getLogger(__name__)


# ─────────────────────── Exceptions ────────────────────────────────

class WorkspaceError(Exception):
    """工作区相关错误基类。"""


class WorkspaceExtractTooLarge(WorkspaceError):
    """解压总大小超出限制。"""


class MissingArchiveError(WorkspaceError):
    """LogRecord 缺少 archive_path 字段或文件不存在。"""


class MissingMetadataJsonError(WorkspaceError):
    """解压后的 logs/ 树中找不到 metadata.json。"""


class UnsupportedUploadFormatError(WorkspaceError):
    """上传的附件无法解压或作为受支持的单文件分析。"""


# ─────────────────────── Data Structures ───────────────────────────

@dataclass
class WorkspaceContext:
    task_id: str
    temp_dir: str           # 绝对路径，如 /base/clone_dirs/<task_id>/
    logs_dir: str           # temp_dir/logs/
    repo_dir: str           # temp_dir/repo/
    task_json_path: str     # temp_dir/task.json
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Active locale for this run (drives prompt selection + the response-language
    # directive). Resolved from the request/owner at enqueue time; defaults to
    # the system default so legacy callers keep working.
    locale: str = I18N_DEFAULT


# ─────────────────────── Helpers ───────────────────────────────────

def _find_metadata_json(logs_dir: Path) -> Optional[Path]:
    """在 logs/ 树中查找第一个 metadata.json（任意子目录）。"""
    for p in logs_dir.rglob("metadata.json"):
        return p
    return None


def _safe_output_path(dest: Path, name: str) -> Path:
    target = (dest / name).resolve()
    base = dest.resolve()
    if base != target and base not in target.parents:
        raise WorkspaceError(f"Unsafe archive member path: {name}")
    return target


def _extract_tar(archive_path: Path, dest: Path, max_bytes: int) -> None:
    extracted = 0
    with tarfile.open(archive_path, "r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            extracted += member.size
            if extracted > max_bytes:
                raise WorkspaceExtractTooLarge(
                    f"Extraction aborted: cumulative size {extracted} bytes "
                    f"exceeds limit {max_bytes} bytes"
                )
            tf.extract(member, path=dest, set_attrs=False)


def _extract_zip(archive_path: Path, dest: Path, max_bytes: int) -> None:
    extracted = 0
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            extracted += info.file_size
            if extracted > max_bytes:
                raise WorkspaceExtractTooLarge(
                    f"Extraction aborted: cumulative size {extracted} bytes "
                    f"exceeds limit {max_bytes} bytes"
                )
            zf.extract(info, path=dest)


def _extract_7z(archive_path: Path, dest: Path, max_bytes: int) -> None:
    try:
        import py7zr
    except ImportError as exc:
        raise WorkspaceError("py7zr is required to extract .7z archives") from exc

    extracted = 0
    with py7zr.SevenZipFile(archive_path, mode="r") as sz:
        for name, bio in sz.read().items():
            if bio is None:
                continue
            data = bio.read()
            extracted += len(data)
            if extracted > max_bytes:
                raise WorkspaceExtractTooLarge(
                    f"Extraction aborted: cumulative size {extracted} bytes "
                    f"exceeds limit {max_bytes} bytes"
                )
            out = dest / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)


def _validate_extracted_output(dest: Path, max_bytes: int) -> None:
    extracted = 0
    for path in dest.rglob("*"):
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise WorkspaceError(f"Failed to inspect extracted path: {path}") from exc
        if dest.resolve() != resolved and dest.resolve() not in resolved.parents:
            raise WorkspaceError(f"Unsafe extracted path: {path}")
        if path.is_symlink():
            raise WorkspaceError(f"Unsafe symlink in archive output: {path}")
        if not path.is_file():
            continue
        extracted += path.stat().st_size
        if extracted > max_bytes:
            raise WorkspaceExtractTooLarge(
                f"Extraction aborted: cumulative size {extracted} bytes "
                f"exceeds limit {max_bytes} bytes"
            )


def _extract_rar_with_unar(archive_path: Path, dest: Path, max_bytes: int) -> None:
    if shutil.which("lsar") is None or shutil.which("unar") is None:
        raise WorkspaceError("unar/lsar is required as a fallback to extract this .rar archive")

    try:
        listing = subprocess.run(
            ["lsar", "-json", str(archive_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        payload = json.loads(listing.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"Failed to inspect .rar archive with lsar: {exc}") from exc

    extracted = 0
    for item in payload.get("lsarContents", []) or []:
        name = item.get("XADFileName") or item.get("name") or ""
        if not name or name.endswith("/") or item.get("XADIsDirectory"):
            continue
        _safe_output_path(dest, name)
        extracted += int(item.get("XADFileSize") or 0)
        if extracted > max_bytes:
            raise WorkspaceExtractTooLarge(
                f"Extraction aborted: cumulative size {extracted} bytes "
                f"exceeds limit {max_bytes} bytes"
            )

    try:
        subprocess.run(
            [
                "unar",
                "-quiet",
                "-force-overwrite",
                "-no-directory",
                "-output-directory",
                str(dest),
                str(archive_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.SubprocessError as exc:
        stderr = getattr(exc, "stderr", "") or ""
        detail = f": {stderr.strip()}" if stderr.strip() else ""
        raise WorkspaceError(f"Failed to extract .rar archive with unar{detail}") from exc

    _validate_extracted_output(dest, max_bytes)


def _extract_rar_with_bsdtar(archive_path: Path, dest: Path, max_bytes: int) -> None:
    """Last-resort .rar backend, using libarchive's independent RAR5 reader.

    unar/XADMaster has a decompression bug on some RAR5 archives with large
    files: instead of erroring on the bad entry, it silently stops output
    partway through (short of the header's declared size) — surfaced upstream
    as "Failed the read enough data" (rarfile, which also shells out to unar)
    and "Attempted to read more data than was available" (the unar fallback
    above). bsdtar/libarchive implements RAR5 decoding independently of
    unrar/unar and has been confirmed to extract those same entries intact,
    so it's tried as a final fallback before giving up.
    """
    if shutil.which("bsdtar") is None:
        raise WorkspaceError("bsdtar is required as a final fallback to extract this .rar archive")

    try:
        subprocess.run(
            [
                "bsdtar",
                "-x",
                "--no-same-owner",
                "--no-same-permissions",
                "-f",
                str(archive_path),
                "-C",
                str(dest),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.SubprocessError as exc:
        stderr = getattr(exc, "stderr", "") or ""
        detail = f": {stderr.strip()}" if stderr.strip() else ""
        raise WorkspaceError(f"Failed to extract .rar archive with bsdtar{detail}") from exc

    _validate_extracted_output(dest, max_bytes)


def _reset_extract_dir(dest: Path) -> None:
    """Clear partial output before retrying extraction with a different tool.

    A failed rarfile/unar attempt can abort mid-entry and leave a truncated
    file behind rather than cleaning up; without this the next tool's
    (correct) output could end up mixed with leftover corrupt files instead
    of replacing them outright.
    """
    shutil.rmtree(str(dest), ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)


def _extract_rar(archive_path: Path, dest: Path, max_bytes: int) -> None:
    rar_error: Optional[BaseException] = None
    try:
        import rarfile
    except ImportError as exc:
        rar_error = exc
    else:
        extracted = 0
        try:
            with rarfile.RarFile(archive_path, mode="r") as rf:
                for info in rf.infolist():
                    name = info.filename
                    if not name or name.endswith("/") or info.isdir():
                        continue
                    is_symlink = getattr(info, "is_symlink", None)
                    if callable(is_symlink) and is_symlink():
                        continue
                    file_size = int(getattr(info, "file_size", 0) or 0)
                    extracted += file_size
                    if extracted > max_bytes:
                        raise WorkspaceExtractTooLarge(
                            f"Extraction aborted: cumulative size {extracted} bytes "
                            f"exceeds limit {max_bytes} bytes"
                        )
                    out = _safe_output_path(dest, name)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with rf.open(info) as src, out.open("wb") as dst:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            dst.write(chunk)
            return
        except WorkspaceExtractTooLarge:
            raise
        except rarfile.Error as exc:
            rar_error = exc
            logger.warning(
                "rarfile failed to extract %s; trying unar fallback: %s",
                archive_path,
                exc,
            )
            _reset_extract_dir(dest)

    unar_error: Optional[BaseException] = None
    try:
        _extract_rar_with_unar(archive_path, dest, max_bytes)
        return
    except WorkspaceExtractTooLarge:
        raise
    except WorkspaceError as exc:
        unar_error = exc
        logger.warning(
            "unar failed to extract %s; trying bsdtar fallback: %s",
            archive_path,
            exc,
        )
        _reset_extract_dir(dest)

    try:
        _extract_rar_with_bsdtar(archive_path, dest, max_bytes)
    except WorkspaceExtractTooLarge:
        raise
    except WorkspaceError as exc:
        details = "; ".join(
            f"{label}: {err}"
            for label, err in (("rarfile", rar_error), ("unar", unar_error), ("bsdtar", exc))
            if err is not None
        )
        raise WorkspaceError(f"Failed to extract .rar archive: {details}") from exc


def _extract_archive(archive_path: Path, dest: Path, max_bytes: int) -> None:
    suffix = "".join(archive_path.suffixes).lower()
    if suffix in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar"):
        _extract_tar(archive_path, dest, max_bytes)
    elif suffix == ".zip":
        _extract_zip(archive_path, dest, max_bytes)
    elif suffix == ".7z":
        _extract_7z(archive_path, dest, max_bytes)
    elif suffix == ".rar":
        _extract_rar(archive_path, dest, max_bytes)
    else:
        # Fallback: try tarfile (covers .gz and other compressed tars) then zip
        try:
            _extract_tar(archive_path, dest, max_bytes)
        except tarfile.TarError:
            _extract_zip(archive_path, dest, max_bytes)


def _place_single_file(src: Path, dest_dir: Path, max_bytes: int, *, preferred_name: str = "") -> Path:
    """Copy a single uploaded file into ``dest_dir`` under a safe name.

    No decompression happens — the file is analyzed as-is. The size guard
    mirrors the archive path so a single oversized attachment is rejected the
    same way an oversized archive is.
    """
    size = src.stat().st_size
    if size > max_bytes:
        raise WorkspaceExtractTooLarge(
            f"Upload aborted: file size {size} bytes exceeds limit {max_bytes} bytes"
        )
    # Strip any directory component a caller-supplied name might carry; fall back
    # to the stored archive_path basename.
    name = Path(preferred_name or src.name).name or src.name
    out = _safe_output_path(dest_dir, name)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)
    return out


def _attachment_dirname(index: int, filename: str) -> str:
    """Return a stable, traversal-safe directory name for one upload."""
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(filename).name).strip("._")
    if not safe_name:
        safe_name = "attachment"
    return f"{index:02d}_{safe_name[:120]}"


def _record_archive_path(log_record: Any) -> Path:
    archive_path_str = getattr(log_record, "archive_path", None) or getattr(
        log_record, "file_path", None
    )
    if not archive_path_str:
        raise MissingArchiveError(
            f"LogRecord id={getattr(log_record, 'id', '?')} has no archive_path"
        )
    archive_path = Path(archive_path_str)
    if not archive_path.exists():
        raise MissingArchiveError(f"Archive file not found: {archive_path}")
    return archive_path


# ─────────────────────── Public API ────────────────────────────────

def populate_logs_dir(
    archive_path: Path,
    logs_dir: Path,
    *,
    max_bytes: Optional[int] = None,
    preferred_name: str = "",
) -> tuple[str, Optional[Path]]:
    """把一次日志上传（压缩包或单文件）落到 ``logs_dir``。

    与 ``prepare`` 的解压/落盘逻辑一致，供其它 Agent 工作区（如 Bug Fix）
    复用，以获得与日志分析工作区相同的 ``logs/`` 内容。

    Returns:
        (upload_kind, attachment_path)：``attachment_path`` 仅在单文件
        （text/spreadsheet/pcap）时非 None。

    Raises:
        UnsupportedUploadFormatError / WorkspaceExtractTooLarge / WorkspaceError
    """
    if max_bytes is None:
        max_bytes = settings.ai_analysis_max_extract_bytes
    upload_kind = detect_upload_kind(str(archive_path))
    attachment_path: Optional[Path] = None
    if upload_kind == "archive":
        _extract_archive(archive_path, logs_dir, max_bytes)
    elif upload_kind in ("text", "spreadsheet", "pcap"):
        attachment_path = _place_single_file(
            archive_path, logs_dir, max_bytes, preferred_name=preferred_name
        )
    else:
        raise UnsupportedUploadFormatError(
            f"Upload {archive_path.name!r} is not a supported archive, "
            "plain-text log, spreadsheet or packet capture"
        )
    return upload_kind, attachment_path


def prepare(log_record: Any, *, require_metadata: bool = True) -> WorkspaceContext:
    """准备任务工作区：解压日志归档，创建 task.json，不包含仓库 URL / token。

    Args:
        log_record: LogRecord ORM 对象（含 id, archive_path, question 等字段）
        require_metadata: 是否强制要求归档内包含 metadata.json。当调用方
            已经通过其它方式（如显式选择项目仓库）提供项目身份时，可置为
            False 以跳过该校验。

    Returns:
        WorkspaceContext

    Raises:
        MissingArchiveError: archive_path 为空或文件不存在
        WorkspaceExtractTooLarge: 解压超限
        MissingMetadataJsonError: require_metadata=True 且解压后找不到 metadata.json
    """
    archive_path_str = getattr(log_record, "archive_path", None) or getattr(log_record, "file_path", None)
    if not archive_path_str:
        raise MissingArchiveError(
            f"LogRecord id={getattr(log_record, 'id', '?')} has no archive_path"
        )

    archive_path = Path(archive_path_str)
    if not archive_path.exists():
        raise MissingArchiveError(
            f"Archive file not found: {archive_path}"
        )

    task_id = str(uuid.uuid4())
    base_dir = Path(settings.code_repo_clone_base_dir)
    temp_dir = base_dir / task_id
    logs_dir = temp_dir / "logs"
    repo_dir = temp_dir / "repo"

    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        repo_dir.mkdir(parents=True, exist_ok=True)

        max_bytes = settings.ai_analysis_max_extract_bytes
        try:
            upload_kind, attachment_path = populate_logs_dir(
                archive_path,
                logs_dir,
                max_bytes=max_bytes,
                preferred_name=getattr(log_record, "original_filename", None) or "",
            )
        except WorkspaceExtractTooLarge:
            shutil.rmtree(str(logs_dir), ignore_errors=True)
            raise

        # Verify metadata.json exists (unless caller opted out)
        if require_metadata:
            meta_path = _find_metadata_json(logs_dir)
            if meta_path is None:
                raise MissingMetadataJsonError(
                    f"No metadata.json found under {logs_dir}"
                )

        # Write task.json with non-sensitive fields only
        task_data = {
            "log_id": getattr(log_record, "id", None),
            "question": getattr(log_record, "issue_description", None) or getattr(log_record, "question", None) or "",
            "hints": getattr(log_record, "hints", None) or "",
            "project_id": getattr(log_record, "project_id", None),
            "upload_kind": upload_kind,
        }
        if attachment_path is not None:
            # ``attachment_path`` comes back resolved (absolute) from
            # ``_safe_output_path``, while ``temp_dir`` is built from a possibly
            # relative settings value. Resolve ``temp_dir`` so ``relative_to``
            # compares two absolute paths instead of raising on the mismatch.
            attachment_rel = attachment_path.relative_to(temp_dir.resolve()).as_posix()
            task_data["attachments"] = [
                {
                    "filename": attachment_path.name,
                    "path": attachment_rel,
                    "kind": upload_kind,
                }
            ]
        task_json_path = temp_dir / "task.json"
        task_json_path.write_text(json.dumps(task_data, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info(
            "Workspace prepared: task_id=%s temp_dir=%s archive=%s",
            task_id, temp_dir, archive_path,
        )
        ctx = WorkspaceContext(
            task_id=task_id,
            temp_dir=str(temp_dir),
            logs_dir=str(logs_dir),
            repo_dir=str(repo_dir),
            task_json_path=str(task_json_path),
        )
        ctx.metadata["upload_kind"] = upload_kind
        if attachment_path is not None:
            ctx.metadata["attachments"] = task_data["attachments"]
        return ctx

    except (MissingArchiveError, WorkspaceExtractTooLarge, MissingMetadataJsonError):
        shutil.rmtree(str(temp_dir), ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(str(temp_dir), ignore_errors=True)
        raise


def prepare_many(
    log_records: Sequence[Any], *, require_metadata: bool = True
) -> WorkspaceContext:
    """Prepare one workspace containing multiple independently-isolated uploads.

    Each upload is materialized below ``logs/<index>_<filename>/``. This keeps
    identically-named files and archive members from overwriting one another,
    while preserving one recursive ``logs/`` tree for the Agent to inspect.
    ``task.json.attachments`` is the authoritative manifest for all uploads.
    """
    records = list(log_records)
    if not records:
        raise MissingArchiveError("No log attachments were provided")

    archive_paths = [_record_archive_path(record) for record in records]
    task_id = str(uuid.uuid4())
    base_dir = Path(settings.code_repo_clone_base_dir)
    temp_dir = base_dir / task_id
    logs_dir = temp_dir / "logs"
    repo_dir = temp_dir / "repo"

    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        repo_dir.mkdir(parents=True, exist_ok=True)

        max_bytes = settings.ai_analysis_max_extract_bytes
        attachments = []
        upload_kinds = []

        for index, (record, archive_path) in enumerate(
            zip(records, archive_paths), start=1
        ):
            original_filename = (
                getattr(record, "original_filename", None) or archive_path.name
            )
            attachment_root = logs_dir / _attachment_dirname(
                index, original_filename
            )
            attachment_root.mkdir(parents=True, exist_ok=False)

            used_bytes = sum(
                path.stat().st_size
                for path in logs_dir.rglob("*")
                if path.is_file()
            )
            remaining_bytes = max_bytes - used_bytes
            if remaining_bytes <= 0:
                raise WorkspaceExtractTooLarge(
                    f"Extraction aborted: cumulative size {used_bytes} bytes "
                    f"exceeds limit {max_bytes} bytes"
                )

            upload_kind, attachment_path = populate_logs_dir(
                archive_path,
                attachment_root,
                max_bytes=remaining_bytes,
                preferred_name=original_filename,
            )
            _validate_extracted_output(logs_dir, max_bytes)
            upload_kinds.append(upload_kind)

            manifest_path = attachment_path or attachment_root
            manifest_rel = manifest_path.resolve().relative_to(
                temp_dir.resolve()
            ).as_posix()
            attachments.append(
                {
                    "log_id": getattr(record, "id", None),
                    "filename": Path(original_filename).name,
                    "path": manifest_rel,
                    "kind": upload_kind,
                }
            )

        if require_metadata and _find_metadata_json(logs_dir) is None:
            raise MissingMetadataJsonError(
                f"No metadata.json found under {logs_dir}"
            )

        primary = records[0]
        task_data = {
            "log_id": getattr(primary, "id", None),
            "log_ids": [getattr(record, "id", None) for record in records],
            "question": getattr(primary, "issue_description", None)
            or getattr(primary, "question", None)
            or "",
            "hints": getattr(primary, "hints", None) or "",
            "project_id": getattr(primary, "project_id", None),
            "upload_kind": "multiple",
            "upload_kinds": upload_kinds,
            "attachments": attachments,
        }
        task_json_path = temp_dir / "task.json"
        task_json_path.write_text(
            json.dumps(task_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.info(
            "Multi-attachment workspace prepared: task_id=%s temp_dir=%s "
            "attachment_count=%d",
            task_id,
            temp_dir,
            len(attachments),
        )
        return WorkspaceContext(
            task_id=task_id,
            temp_dir=str(temp_dir),
            logs_dir=str(logs_dir),
            repo_dir=str(repo_dir),
            task_json_path=str(task_json_path),
            metadata={
                "upload_kind": "multiple",
                "upload_kinds": upload_kinds,
                "attachments": attachments,
            },
        )
    except Exception:
        shutil.rmtree(str(temp_dir), ignore_errors=True)
        raise


def cleanup(ctx: WorkspaceContext) -> None:
    """幂等删除临时工作区目录。"""
    temp = Path(ctx.temp_dir)
    if temp.exists():
        shutil.rmtree(str(temp), ignore_errors=True)
        logger.info("Workspace cleaned up: %s", ctx.temp_dir)
