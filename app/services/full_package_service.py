"""Deterministic, catalog-driven whole-package classification and building.

This module deliberately contains no chat, model, or repository side effects.  It
is the policy boundary shared by the Configuration Manager preflight and the
package-builder MCP server:

* a versioned JSON catalog is validated and hashed before it is used;
* uploaded files are inspected without extraction and classified from evidence;
* every draft can be converted to a hash-bound confirmed plan only after every
  mandatory answer is present; and
* package bytes can be produced only from a confirmed plan whose catalog and
  input hashes still match.

Repository publication is intentionally left to :mod:`raven_package_service`.
The builder writes one validated ``.tgz`` below a caller-owned workspace and
returns a JSON-serialisable result that the publisher can consume atomically.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence

from app.agents.log_analysis.workspace import (
    WorkspaceError,
    WorkspaceExtractTooLarge,
    _extract_archive,
    _validate_extracted_output,
)
from app.config import settings

logger = logging.getLogger(__name__)


DEFAULT_CATALOG_PATH = Path(__file__).with_name("resources") / "package-projects.json"
DEFAULT_MAX_ARCHIVE_MEMBERS = 10_000
DEFAULT_MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024
MANIFEST_NAME = "package-manifest.json"
SI_INI_NAME = "si.ini"
MAX_SI_INI_BYTES = 1024 * 1024
MAX_PACKAGE_MANIFEST_BYTES = 16 * 1024 * 1024

_SAFE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SAFE_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,5}(?:[-+][A-Za-z0-9.-]+)?$")
_ALLOWED_RECOGNITION_FIELDS = {
    "filename",
    "relative_path",
    "extension",
    "magic",
    "archive_member",
}
_ALLOWED_MATERIALIZATION = {"copy", "direct_include", "extract_match"}
_EXCLUDE_ANSWERS = {
    "exclude",
    "excluded",
    "skip",
    "ignore",
    "不纳入整包",
    "排除此文件",
    "排除",
    "取消",
}


# ───────────────────────────── Exceptions ──────────────────────────────


class FullPackageError(RuntimeError):
    """Base error for package classification and construction."""


class CatalogValidationError(FullPackageError, ValueError):
    """The JSON catalog is missing required data or contains unsafe rules."""


class ArchiveInspectionError(FullPackageError):
    """An archive cannot be safely inspected within configured limits."""


class PlanValidationError(FullPackageError, ValueError):
    """A draft or confirmed plan is incomplete, stale, or has been altered."""


class PackageBuildError(FullPackageError):
    """A confirmed component cannot be materialised or the artifact is invalid."""


# ───────────────────────── Serializable results ────────────────────────


def _plain(value: Any) -> Any:
    if isinstance(value, SerializableDict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


class SerializableDict(dict[str, Any]):
    """A normal mapping with explicit persistence helpers.

    Returning a mapping keeps the service convenient for existing chat code,
    while ``to_dict``/``serialize`` make the contract explicit for workspace
    manifests and tests.
    """

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(_plain(self))

    def serialize(self, *, indent: Optional[int] = 2) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":") if indent is None else None,
            indent=indent,
        )


class PackageCatalog(SerializableDict):
    """Validated catalog mapping carrying its canonical SHA-256 digest."""

    @property
    def digest(self) -> str:
        return str(self["catalog_digest"])

    @property
    def projects_by_code(self) -> dict[str, dict[str, Any]]:
        return {project["project_code"]: project for project in self["projects"]}


class ClassificationDraft(SerializableDict):
    pass


class ConfirmedPackagePlan(SerializableDict):
    pass


class BuildResult(SerializableDict):
    pass


# ───────────────────────── Canonical JSON / hashes ─────────────────────


def canonical_json(value: Any) -> str:
    """Return the single canonical JSON representation used by all hashes."""

    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_plain(value), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# ─────────────────────────── Catalog validation ────────────────────────


def _require_mapping(value: Any, location: str) -> MutableMapping[str, Any]:
    if not isinstance(value, MutableMapping):
        raise CatalogValidationError(f"{location} must be an object")
    return value


def _require_nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogValidationError(f"{location} must be a non-empty string")
    return value.strip()


def _safe_key(value: Any, location: str) -> str:
    key = _require_nonempty_string(value, location).lower()
    if not _SAFE_KEY_RE.fullmatch(key):
        raise CatalogValidationError(
            f"{location} must contain only lowercase letters, digits, '.', '_' or '-'"
        )
    return key


def _safe_flat_name(value: Any, location: str) -> str:
    name = _require_nonempty_string(value, location)
    if (
        name in {".", ".."}
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise CatalogValidationError(f"{location} must be a safe flat filename")
    return name


def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool):
        raise CatalogValidationError(f"{location} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError(f"{location} must be a positive integer") from exc
    if parsed <= 0:
        raise CatalogValidationError(f"{location} must be a positive integer")
    return parsed


def _validate_regex(pattern: Any, location: str) -> str:
    text = _require_nonempty_string(pattern, location)
    if len(text) > 2_000:
        raise CatalogValidationError(f"{location} is too long")
    try:
        re.compile(text, re.IGNORECASE)
    except re.error as exc:
        raise CatalogValidationError(f"{location} is not a valid regex: {exc}") from exc
    return text


def _normalise_recognition_rules(value: Any, location: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CatalogValidationError(f"{location} must be an array")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        rule = _require_mapping(raw, f"{location}[{index}]")
        field = _require_nonempty_string(
            rule.get("field"), f"{location}[{index}].field"
        ).lower()
        if field not in _ALLOWED_RECOGNITION_FIELDS:
            raise CatalogValidationError(
                f"{location}[{index}].field is unsupported: {field}"
            )
        pattern = _validate_regex(rule.get("pattern"), f"{location}[{index}].pattern")
        try:
            weight = float(rule.get("weight", 25))
        except (TypeError, ValueError) as exc:
            raise CatalogValidationError(
                f"{location}[{index}].weight must be numeric"
            ) from exc
        if not 0 < weight <= 100:
            raise CatalogValidationError(
                f"{location}[{index}].weight must be in (0, 100]"
            )
        result.append(
            {
                "field": field,
                "pattern": pattern,
                "weight": weight,
                "reason": str(rule.get("reason") or "").strip(),
            }
        )
    return result


def _normalise_version_rules(value: Any, location: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CatalogValidationError(f"{location} must be an array")
    result: list[dict[str, Any]] = []
    allowed_sources = {"filename", "relative_path", "archive_member"}
    allowed_transforms = {
        "dotted",
        "dotted_timestamp",
        "compact_decimal",
        "hex_bytes",
        "template",
    }
    for index, raw in enumerate(value):
        rule = _require_mapping(raw, f"{location}[{index}]")
        source = _require_nonempty_string(
            rule.get("source", "filename"), f"{location}[{index}].source"
        ).lower()
        transform = _require_nonempty_string(
            rule.get("transform", "dotted"), f"{location}[{index}].transform"
        ).lower()
        if source not in allowed_sources:
            raise CatalogValidationError(f"{location}[{index}].source is unsupported")
        if transform not in allowed_transforms:
            raise CatalogValidationError(f"{location}[{index}].transform is unsupported")
        normalised = {
            "source": source,
            "pattern": _validate_regex(
                rule.get("pattern"), f"{location}[{index}].pattern"
            ),
            "transform": transform,
        }
        if transform == "template":
            normalised["template"] = _require_nonempty_string(
                rule.get("template"), f"{location}[{index}].template"
            )
        result.append(normalised)
    return result


def validate_catalog(data: Mapping[str, Any]) -> PackageCatalog:
    """Validate and normalise a package catalog, returning its canonical hash."""

    if not isinstance(data, Mapping):
        raise CatalogValidationError("catalog must be a JSON object")
    raw = copy.deepcopy(dict(data))
    raw.pop("catalog_digest", None)
    schema_version = _require_nonempty_string(raw.get("schema_version"), "schema_version")
    if schema_version.split(".", 1)[0] != "1":
        raise CatalogValidationError(
            f"unsupported schema_version {schema_version!r}; expected major version 1"
        )
    catalog_version = _require_nonempty_string(raw.get("catalog_version"), "catalog_version")
    projects_raw = raw.get("projects")
    if not isinstance(projects_raw, list) or not projects_raw:
        raise CatalogValidationError("projects must be a non-empty array")

    normalised_projects: list[dict[str, Any]] = []
    seen_projects: set[str] = set()
    seen_registry_projects: set[str] = set()
    for p_index, project_raw in enumerate(projects_raw):
        location = f"projects[{p_index}]"
        project = _require_mapping(project_raw, location)
        project_code = _safe_key(project.get("project_code"), f"{location}.project_code")
        if project_code in seen_projects:
            raise CatalogValidationError(f"duplicate project_code: {project_code}")
        seen_projects.add(project_code)
        aliases_raw = project.get("aliases") or []
        if not isinstance(aliases_raw, list) or not all(
            isinstance(item, str) and item.strip() for item in aliases_raw
        ):
            raise CatalogValidationError(f"{location}.aliases must be an array of strings")
        registry_codes_raw = project.get("registry_project_codes", [])
        if not isinstance(registry_codes_raw, list):
            raise CatalogValidationError(f"{location}.registry_project_codes must be an array")
        registry_codes = [
            _safe_key(code, f"{location}.registry_project_codes[{index}]")
            for index, code in enumerate(registry_codes_raw)
        ]
        if len(set(registry_codes)) != len(registry_codes) or seen_registry_projects.intersection(registry_codes):
            raise CatalogValidationError("duplicate registry_project_codes mapping")
        seen_registry_projects.update(registry_codes)
        packet_attr = _positive_int(project.get("packet_attr"), f"{location}.packet_attr")
        patch_packet_attr = project.get("patch_packet_attr")
        if patch_packet_attr is not None:
            patch_packet_attr = _positive_int(
                patch_packet_attr, f"{location}.patch_packet_attr"
            )
        prefix = _safe_flat_name(project.get("package_prefix"), f"{location}.package_prefix")
        package_pattern = _require_nonempty_string(
            project.get(
                "package_name_pattern",
                "{package_prefix}-V{numeric_version}{patch_suffix}-{confirmation_short}.tgz",
            ),
            f"{location}.package_name_pattern",
        )
        # A format string is data, but path separators would let an override
        # escape the output directory after expansion.
        if "/" in package_pattern or "\\" in package_pattern or "\x00" in package_pattern:
            raise CatalogValidationError(
                f"{location}.package_name_pattern must produce a flat filename"
            )
        version_pattern = _validate_regex(
            project.get("package_version_pattern", r"^\d+\.\d+\.\d+\.\d+$"),
            f"{location}.package_version_pattern",
        )

        components_raw = project.get("components")
        if not isinstance(components_raw, list):
            raise CatalogValidationError(f"{location}.components must be an array")
        normalised_components: list[dict[str, Any]] = []
        seen_components: set[str] = set()
        seen_attrs: set[int] = set()
        seen_output_names: set[str] = set()
        for c_index, component_raw in enumerate(components_raw):
            c_location = f"{location}.components[{c_index}]"
            component = _require_mapping(component_raw, c_location)
            key = _safe_key(component.get("component_key"), f"{c_location}.component_key")
            if key in seen_components:
                raise CatalogValidationError(
                    f"duplicate component_key {key!r} in project {project_code}"
                )
            seen_components.add(key)
            recognition_only = bool(component.get("recognition_only", False))
            publishable = bool(component.get("publishable", not recognition_only))
            if recognition_only and publishable:
                raise CatalogValidationError(
                    f"{c_location} cannot be recognition_only and publishable"
                )

            file_attr: Optional[int] = None
            output_name: Optional[str] = None
            materialisation: Optional[dict[str, Any]] = None
            if publishable:
                file_attr = _positive_int(component.get("file_attr"), f"{c_location}.file_attr")
                if file_attr in seen_attrs:
                    raise CatalogValidationError(
                        f"duplicate file_attr {file_attr} in project {project_code}"
                    )
                seen_attrs.add(file_attr)
                output_name = _safe_flat_name(
                    component.get("output_name"), f"{c_location}.output_name"
                )
                if output_name.casefold() in {SI_INI_NAME.casefold(), MANIFEST_NAME.casefold()}:
                    raise CatalogValidationError(
                        f"{c_location}.output_name collides with package metadata"
                    )
                if output_name.casefold() in seen_output_names:
                    raise CatalogValidationError(
                        f"duplicate output_name {output_name!r} in project {project_code}"
                    )
                seen_output_names.add(output_name.casefold())
                raw_materialisation = _require_mapping(
                    component.get("materialization"), f"{c_location}.materialization"
                )
                kind = _require_nonempty_string(
                    raw_materialisation.get("type"),
                    f"{c_location}.materialization.type",
                ).lower()
                if kind not in _ALLOWED_MATERIALIZATION:
                    raise CatalogValidationError(
                        f"{c_location}.materialization.type is unsupported: {kind}"
                    )
                materialisation = {"type": kind}
                if kind == "extract_match":
                    patterns = raw_materialisation.get("patterns")
                    if not isinstance(patterns, list) or not patterns:
                        raise CatalogValidationError(
                            f"{c_location}.materialization.patterns must be non-empty"
                        )
                    materialisation["patterns"] = [
                        _validate_regex(pattern, f"{c_location}.materialization.patterns[{i}]")
                        for i, pattern in enumerate(patterns)
                    ]
                    excludes = raw_materialisation.get("exclude_patterns") or []
                    if not isinstance(excludes, list):
                        raise CatalogValidationError(
                            f"{c_location}.materialization.exclude_patterns must be an array"
                        )
                    materialisation["exclude_patterns"] = [
                        _validate_regex(pattern, f"{c_location}.materialization.exclude_patterns[{i}]")
                        for i, pattern in enumerate(excludes)
                    ]

            allow_prebuilt_extract = component.get("allow_prebuilt_extract", False)
            if not isinstance(allow_prebuilt_extract, bool):
                raise CatalogValidationError(f"{c_location}.allow_prebuilt_extract must be boolean")
            if allow_prebuilt_extract and (
                not publishable or not materialisation or materialisation["type"] != "extract_match"
            ):
                raise CatalogValidationError(
                    f"{c_location}.allow_prebuilt_extract requires a publishable extract_match component"
                )

            threshold = component.get("classification_threshold", 30)
            try:
                threshold = float(threshold)
            except (TypeError, ValueError) as exc:
                raise CatalogValidationError(
                    f"{c_location}.classification_threshold must be numeric"
                ) from exc
            if not 0 <= threshold <= 100:
                raise CatalogValidationError(
                    f"{c_location}.classification_threshold must be in [0, 100]"
                )
            normalised_components.append(
                {
                    "component_key": key,
                    "label": _require_nonempty_string(
                        component.get("label", key), f"{c_location}.label"
                    ),
                    "description": str(component.get("description") or "").strip(),
                    "file_attr": file_attr,
                    "output_name": output_name,
                    "publishable": publishable,
                    "recognition_only": recognition_only,
                    "recognition": _normalise_recognition_rules(
                        component.get("recognition"), f"{c_location}.recognition"
                    ),
                    "version_rules": _normalise_version_rules(
                        component.get("version_rules"), f"{c_location}.version_rules"
                    ),
                    "materialization": materialisation,
                    "allow_prebuilt_extract": allow_prebuilt_extract,
                    "classification_threshold": threshold,
                    "ambiguous_group": str(component.get("ambiguous_group") or "").strip(),
                    "default_version": str(component.get("default_version") or "V0.0.0.0").strip(),
                }
            )

        normalised_projects.append(
            {
                "project_code": project_code,
                "label": _require_nonempty_string(
                    project.get("label", project_code), f"{location}.label"
                ),
                "aliases": [str(item).strip() for item in aliases_raw],
                "registry_project_codes": registry_codes,
                "packet_attr": packet_attr,
                "patch_packet_attr": patch_packet_attr,
                "publisher": _require_nonempty_string(
                    project.get("publisher", "yinhe"), f"{location}.publisher"
                ),
                "package_prefix": prefix,
                "package_name_pattern": package_pattern,
                "package_version_pattern": version_pattern,
                "recognition": _normalise_recognition_rules(
                    project.get("recognition"), f"{location}.recognition"
                ),
                "prebuilt_recognition": _normalise_recognition_rules(
                    project.get("prebuilt_recognition"),
                    f"{location}.prebuilt_recognition",
                ),
                "version_rules": _normalise_version_rules(
                    project.get("version_rules"), f"{location}.version_rules"
                ),
                "components": normalised_components,
            }
        )

    for project in normalised_projects:
        if any(code in seen_projects and code != project["project_code"]
               for code in project["registry_project_codes"]):
            raise CatalogValidationError("registry_project_codes conflicts with another project_code")

    limits_raw = raw.get("limits") or {}
    limits = _require_mapping(limits_raw, "limits")
    normalised = {
        "schema_version": schema_version,
        "catalog_version": catalog_version,
        "limits": {
            "max_archive_members": _positive_int(
                limits.get("max_archive_members", DEFAULT_MAX_ARCHIVE_MEMBERS),
                "limits.max_archive_members",
            ),
            "max_extract_bytes": _positive_int(
                limits.get("max_extract_bytes", DEFAULT_MAX_EXTRACT_BYTES),
                "limits.max_extract_bytes",
            ),
        },
        "projects": normalised_projects,
    }
    digest = canonical_hash(normalised)
    normalised["catalog_digest"] = digest
    return PackageCatalog(normalised)


def load_catalog(source: Any = None) -> PackageCatalog:
    """Load a catalog from JSON path, JSON text, mapping, or the built-in file."""

    if isinstance(source, PackageCatalog):
        # Revalidate so a caller cannot mutate a previously returned dict and
        # retain its old trusted digest.
        return validate_catalog(source)
    if source is None:
        source = DEFAULT_CATALOG_PATH
    if isinstance(source, Mapping):
        return validate_catalog(source)
    if isinstance(source, (str, os.PathLike)):
        candidate = Path(source)
        if isinstance(source, os.PathLike) or candidate.exists():
            if candidate.is_dir():
                candidate = candidate / "references" / "package-projects.json"
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except OSError as exc:
                raise CatalogValidationError(f"cannot read catalog {candidate}: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise CatalogValidationError(f"catalog is not valid JSON: {exc}") from exc
            return validate_catalog(data)
        try:
            return validate_catalog(json.loads(str(source)))
        except json.JSONDecodeError as exc:
            raise CatalogValidationError(
                f"catalog path does not exist and value is not JSON: {source}"
            ) from exc
    raise CatalogValidationError(f"unsupported catalog source type: {type(source).__name__}")


# Compatibility aliases used by integration code and focused tests.
load_package_catalog = load_catalog
validate_package_catalog = validate_catalog


# ───────────────────────── Archive inspection ──────────────────────────


def _normalise_member_name(raw_name: Any) -> str:
    name = str(raw_name or "").replace("\\", "/")
    if "\x00" in name:
        raise ArchiveInspectionError("archive member contains a NUL byte")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ArchiveInspectionError(f"archive member is absolute: {raw_name}")
    while name.startswith("./"):
        name = name[2:]
    trimmed = name.rstrip("/")
    if not trimmed:
        return ""
    parts = PurePosixPath(trimmed).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchiveInspectionError(f"unsafe archive member path: {raw_name}")
    if len(trimmed) > 4_096:
        raise ArchiveInspectionError("archive member path is too long")
    return "/".join(parts)


def _archive_kind(path: Path) -> Optional[str]:
    try:
        with path.open("rb") as handle:
            header = handle.read(8)
    except OSError:
        return None
    if header.startswith(b"PK\x03\x04") or header.startswith(b"PK\x05\x06"):
        return "zip"
    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if header.startswith(b"Rar!\x1a\x07"):
        return "rar"
    try:
        if tarfile.is_tarfile(path):
            return "tar"
    except OSError:
        pass
    suffix = "".join(path.suffixes).lower()
    if suffix == ".zip":
        return "zip"
    if suffix == ".7z":
        return "7z"
    if suffix == ".rar":
        return "rar"
    if suffix in {".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz"}:
        return "tar"
    return None


def _bounded_details(
    raw_details: Iterable[tuple[str, int, bool, bool]],
    *,
    max_members: int,
    max_uncompressed_bytes: int,
) -> tuple[list[str], list[dict[str, Any]], int]:
    names: list[str] = []
    details: list[dict[str, Any]] = []
    total = 0
    for raw_name, raw_size, is_directory, is_link in raw_details:
        name = _normalise_member_name(raw_name)
        if not name:
            continue
        if is_link:
            raise ArchiveInspectionError(f"archive contains an unsafe link: {raw_name}")
        if is_directory:
            continue
        size = max(0, int(raw_size or 0))
        names.append(name)
        details.append({"name": name, "size": size})
        if len(names) > max_members:
            raise ArchiveInspectionError(
                f"archive has more than {max_members} file members"
            )
        total += size
        if total > max_uncompressed_bytes:
            raise ArchiveInspectionError(
                f"archive expands to {total} bytes, above limit {max_uncompressed_bytes}"
            )
    return names, details, total


def _inspect_zip(path: Path, max_members: int, max_bytes: int) -> tuple[list[str], list[dict[str, Any]], int, str]:
    with zipfile.ZipFile(path, "r") as archive:
        rows = []
        for info in archive.infolist():
            mode = (int(info.external_attr) >> 16) & 0xFFFF
            is_link = stat.S_IFMT(mode) == stat.S_IFLNK
            rows.append((info.filename, info.file_size, info.is_dir(), is_link))
    names, details, total = _bounded_details(
        rows, max_members=max_members, max_uncompressed_bytes=max_bytes
    )
    return names, details, total, "zipfile"


def _inspect_tar(path: Path, max_members: int, max_bytes: int) -> tuple[list[str], list[dict[str, Any]], int, str]:
    rows = []
    with tarfile.open(path, "r:*") as archive:
        for member in archive.getmembers():
            unsafe_type = member.isdev()
            skip_as_metadata = member.isdir()
            if member.issym() or member.islnk():
                target = str(member.linkname or "").replace("\\", "/")
                if (
                    not target
                    or target.startswith("/")
                    or re.match(r"^[A-Za-z]:", target)
                    or "\x00" in target
                ):
                    unsafe_type = True
                else:
                    base = PurePosixPath(member.name).parent if member.issym() else PurePosixPath()
                    stack: list[str] = []
                    for part in (base / PurePosixPath(target)).parts:
                        if part in {"", "."}:
                            continue
                        if part == "..":
                            if not stack:
                                unsafe_type = True
                                break
                            stack.pop()
                        else:
                            stack.append(part)
                # A safe internal link is legitimate archive metadata.  It is
                # not counted as an extracted payload and the shared workspace
                # extractor already skips non-regular TAR members.
                skip_as_metadata = not unsafe_type
            rows.append((member.name, member.size, skip_as_metadata, unsafe_type))
    names, details, total = _bounded_details(
        rows, max_members=max_members, max_uncompressed_bytes=max_bytes
    )
    return names, details, total, "tarfile"


def _inspect_7z(path: Path, max_members: int, max_bytes: int) -> tuple[list[str], list[dict[str, Any]], int, str]:
    try:
        import py7zr
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise ArchiveInspectionError("py7zr is unavailable") from exc
    rows = []
    try:
        with py7zr.SevenZipFile(path, mode="r") as archive:
            for info in archive.list():
                is_directory = bool(getattr(info, "is_directory", False))
                is_link_attr = getattr(info, "is_symlink", False)
                is_link = bool(is_link_attr() if callable(is_link_attr) else is_link_attr)
                raw_size = getattr(info, "uncompressed", None)
                if raw_size is None and not is_directory:
                    raise ArchiveInspectionError(
                        f"7z member has no declared uncompressed size: {getattr(info, 'filename', '')}"
                    )
                rows.append(
                    (
                        getattr(info, "filename", ""),
                        int(raw_size or 0),
                        is_directory,
                        is_link,
                    )
                )
    except Exception as exc:  # py7zr exposes several backend exception types
        raise ArchiveInspectionError(f"py7zr failed to inspect archive: {exc}") from exc
    names, details, total = _bounded_details(
        rows, max_members=max_members, max_uncompressed_bytes=max_bytes
    )
    return names, details, total, "py7zr"


def _inspect_rar_rarfile(path: Path, max_members: int, max_bytes: int) -> tuple[list[str], list[dict[str, Any]], int, str]:
    try:
        import rarfile
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise ArchiveInspectionError("rarfile is unavailable") from exc
    try:
        with rarfile.RarFile(path, mode="r") as archive:
            rows = []
            for info in archive.infolist():
                symlink_attr = getattr(info, "is_symlink", False)
                is_link = bool(symlink_attr() if callable(symlink_attr) else symlink_attr)
                rows.append(
                    (
                        info.filename,
                        int(getattr(info, "file_size", 0) or 0),
                        bool(info.isdir()),
                        is_link,
                    )
                )
    except Exception as exc:
        raise ArchiveInspectionError(f"rarfile failed to inspect archive: {exc}") from exc
    names, details, total = _bounded_details(
        rows, max_members=max_members, max_uncompressed_bytes=max_bytes
    )
    return names, details, total, "rarfile"


def _inspect_rar_lsar(path: Path, max_members: int, max_bytes: int) -> tuple[list[str], list[dict[str, Any]], int, str]:
    if shutil.which("lsar") is None:
        raise ArchiveInspectionError("lsar is unavailable")
    try:
        completed = subprocess.run(
            ["lsar", "-json", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        payload = json.loads(completed.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ArchiveInspectionError(f"lsar failed to inspect archive: {exc}") from exc
    rows = []
    for item in payload.get("lsarContents", []) or []:
        is_directory = bool(item.get("XADIsDirectory"))
        if not is_directory and item.get("XADFileSize") is None:
            raise ArchiveInspectionError(
                f"lsar did not report the size of RAR member: "
                f"{item.get('XADFileName') or item.get('name') or ''}"
            )
        rows.append(
            (
                item.get("XADFileName") or item.get("name") or "",
                int(item.get("XADFileSize") or 0),
                is_directory,
                bool(
                    item.get("XADIsLink")
                    or item.get("XADLinkDestination")
                    or item.get("XADIsSymbolicLink")
                ),
            )
        )
    names, details, total = _bounded_details(
        rows, max_members=max_members, max_uncompressed_bytes=max_bytes
    )
    return names, details, total, "lsar"


def _inspect_rar_bsdtar(path: Path, max_members: int, max_bytes: int) -> tuple[list[str], list[dict[str, Any]], int, str]:
    if shutil.which("bsdtar") is None:
        raise ArchiveInspectionError("bsdtar is unavailable")
    try:
        names_result = subprocess.run(
            ["bsdtar", "-tf", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        verbose_result = subprocess.run(
            ["bsdtar", "-tvf", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.SubprocessError as exc:
        raise ArchiveInspectionError(f"bsdtar failed to inspect archive: {exc}") from exc
    names = names_result.stdout.splitlines()
    verbose = verbose_result.stdout.splitlines()
    if len(names) != len(verbose):
        raise ArchiveInspectionError("bsdtar member and size listings disagree")
    rows = []
    for index, name in enumerate(names):
        line = verbose[index].lstrip()
        type_char = line[:1]
        fields = line.split(maxsplit=8)
        if len(fields) < 9:
            raise ArchiveInspectionError(
                f"bsdtar did not report a parseable member size: {name}"
            )
        try:
            declared_size = int(fields[4])
        except ValueError as exc:
            raise ArchiveInspectionError(
                f"bsdtar did not report a numeric member size: {name}"
            ) from exc
        rows.append(
            (
                name,
                declared_size,
                name.endswith("/") or type_char == "d",
                type_char not in {"-", "d"},
            )
        )
    member_names, details, total = _bounded_details(
        rows, max_members=max_members, max_uncompressed_bytes=max_bytes
    )
    return member_names, details, total, "bsdtar"


def inspect_archive(
    path: str | os.PathLike[str],
    *,
    max_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS,
    max_uncompressed_bytes: int = DEFAULT_MAX_EXTRACT_BYTES,
    strict: bool = False,
) -> SerializableDict:
    """List archive members without extracting them.

    ZIP, TAR and 7z use their Python backends.  RAR inspection follows the
    production extraction order (rarfile → lsar → bsdtar).  In best-effort mode
    backend/safety failures are returned in ``errors`` so classification can
    still ask the human about the file; ``strict=True`` raises immediately and
    is always used by the builder.
    """

    archive_path = Path(path)
    if not archive_path.is_file():
        raise ArchiveInspectionError(f"input file does not exist: {archive_path}")
    kind = _archive_kind(archive_path)
    result = SerializableDict(
        {
            "is_archive": kind is not None,
            "archive_type": kind,
            "backend": None,
            "members": [],
            "member_details": [],
            "member_count": 0,
            "total_uncompressed_bytes": 0,
            "errors": [],
        }
    )
    if kind is None:
        return result
    errors: list[str] = []
    try:
        if kind == "zip":
            inspected = _inspect_zip(archive_path, max_members, max_uncompressed_bytes)
        elif kind == "tar":
            inspected = _inspect_tar(archive_path, max_members, max_uncompressed_bytes)
        elif kind == "7z":
            inspected = _inspect_7z(archive_path, max_members, max_uncompressed_bytes)
        else:
            inspected = None
            for backend in (_inspect_rar_rarfile, _inspect_rar_lsar, _inspect_rar_bsdtar):
                try:
                    inspected = backend(archive_path, max_members, max_uncompressed_bytes)
                    break
                except ArchiveInspectionError as exc:
                    errors.append(str(exc))
            if inspected is None:
                raise ArchiveInspectionError("; ".join(errors) or "no RAR backend is available")
        names, details, total, backend_name = inspected
        result.update(
            {
                "backend": backend_name,
                "members": names,
                "member_details": details,
                "member_count": len(names),
                "total_uncompressed_bytes": total,
                "errors": errors,
            }
        )
    except (ArchiveInspectionError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        if strict:
            if isinstance(exc, ArchiveInspectionError):
                raise
            raise ArchiveInspectionError(str(exc)) from exc
        errors.append(str(exc))
        result["errors"] = errors
    return result


def list_archive_members(
    path: str | os.PathLike[str],
    *,
    max_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS,
    max_uncompressed_bytes: int = DEFAULT_MAX_EXTRACT_BYTES,
    strict: bool = False,
) -> list[str]:
    return list(
        inspect_archive(
            path,
            max_members=max_members,
            max_uncompressed_bytes=max_uncompressed_bytes,
            strict=strict,
        )["members"]
    )


# ───────────────────────── Version inference ───────────────────────────


def _format_version(value: str, transform: str, match: re.Match[str], template: str = "") -> Optional[str]:
    try:
        if transform == "template":
            groups = {key: val for key, val in match.groupdict().items() if val is not None}
            value = template.format(**groups)
        elif transform in {"dotted", "dotted_timestamp"}:
            value = match.groupdict().get("version") or match.group(1)
            timestamp = match.groupdict().get("timestamp")
            parts = value.replace("_", ".").split(".")
            if not all(part.isdigit() for part in parts):
                return None
            while len(parts) < 4:
                parts.append("0")
            value = ".".join(str(int(part)) for part in parts[:4])
            if timestamp:
                value += f".{timestamp}"
        elif transform == "compact_decimal":
            compact = match.groupdict().get("version") or match.group(1)
            if len(compact) != 4 or not compact.isdigit():
                return None
            value = ".".join(str(int(char)) for char in compact)
        elif transform == "hex_bytes":
            compact = match.groupdict().get("version") or match.group(1)
            if len(compact) != 8 or not re.fullmatch(r"[0-9A-Fa-f]{8}", compact):
                return None
            value = ".".join(str(int(compact[index : index + 2], 16)) for index in range(0, 8, 2))
    except (IndexError, KeyError, ValueError):
        return None
    value = str(value).strip().lstrip("Vv")
    return f"V{value}" if value else None


_GENERIC_VERSION_RULES = [
    {
        "source": "filename",
        "pattern": r"[Vv]?(?P<version>\d+\.\d+\.\d+\.\d+)[.-](?P<timestamp>\d{12})",
        "transform": "dotted_timestamp",
    },
    {
        "source": "filename",
        "pattern": r"[Vv](?P<version>[0-9A-Fa-f]{8})(?![0-9A-Fa-f])",
        "transform": "hex_bytes",
    },
    {
        "source": "filename",
        "pattern": r"[Vv]?(?P<version>\d+\.\d+\.\d+\.\d+)",
        "transform": "dotted",
    },
    {
        "source": "filename",
        "pattern": r"[Vv](?P<version>\d{4})(?!\d)",
        "transform": "compact_decimal",
    },
    {
        "source": "filename",
        "pattern": r"[Vv](?P<version>\d+\.\d+\.\d+)(?=$|[-_]|\.(?:tgz|tar|zip|gz|7z|rar|bin)$)",
        "transform": "dotted",
    },
    {
        "source": "filename",
        "pattern": r"[Vv](?P<version>\d+\.\d+)(?=$|[-_]|\.(?:tgz|tar|zip|gz|7z|rar|bin)$)",
        "transform": "dotted",
    },
]


def infer_versions(
    filename: str,
    *,
    relative_path: str = "",
    archive_members: Sequence[str] = (),
    rules: Optional[Sequence[Mapping[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Return ordered, de-duplicated version candidates and their evidence."""

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    effective_rules = list(rules or []) + _GENERIC_VERSION_RULES
    for rule in effective_rules:
        source = str(rule.get("source") or "filename")
        values = {
            "filename": [filename],
            "relative_path": [relative_path],
            "archive_member": list(archive_members),
        }.get(source, [])
        try:
            regex = re.compile(str(rule.get("pattern") or ""), re.IGNORECASE)
        except re.error:
            continue
        for candidate_source in values:
            match = regex.search(candidate_source or "")
            if not match:
                continue
            version = _format_version(
                match.group(0),
                str(rule.get("transform") or "dotted"),
                match,
                str(rule.get("template") or ""),
            )
            if not version or version in seen:
                continue
            seen.add(version)
            candidates.append(
                {
                    "version": version,
                    "source": source,
                    "matched": match.group(0),
                    "location": candidate_source,
                }
            )
    return candidates


def infer_version(
    filename: str,
    *,
    relative_path: str = "",
    archive_members: Sequence[str] = (),
    rules: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Optional[str]:
    candidates = infer_versions(
        filename,
        relative_path=relative_path,
        archive_members=archive_members,
        rules=rules,
    )
    return str(candidates[0]["version"]) if candidates else None


# ───────────────────────── Evidence classification ─────────────────────


def _compound_extension(name: str) -> str:
    lower = name.lower()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if lower.endswith(suffix):
            return suffix
    return Path(lower).suffix


def _magic_label(path: Path, archive_type: Optional[str]) -> str:
    if archive_type:
        return f"archive/{archive_type}"
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return "unknown"
    if header.startswith(b"\x7fELF"):
        return "application/elf"
    if header.startswith(b"!<arch>\n"):
        return "application/deb"
    if header.startswith(b"{\"") or header.lstrip().startswith((b"{", b"[")):
        return "application/json"
    return "application/octet-stream"


def _field_values(field: str, evidence: Mapping[str, Any]) -> list[str]:
    if field == "archive_member":
        return [str(item) for item in evidence.get("archive_members", [])]
    value = evidence.get(field, "")
    return [str(value)] if value is not None else []


def _score_rules(
    rules: Sequence[Mapping[str, Any]], evidence: Mapping[str, Any]
) -> tuple[float, list[dict[str, Any]]]:
    total = 0.0
    matches: list[dict[str, Any]] = []
    for rule in rules:
        regex = re.compile(str(rule["pattern"]), re.IGNORECASE)
        for value in _field_values(str(rule["field"]), evidence):
            found = regex.search(value)
            if not found:
                continue
            weight = float(rule["weight"])
            total += weight
            reason = str(rule.get("reason") or "").strip()
            matches.append(
                {
                    "field": rule["field"],
                    "matched": found.group(0),
                    "location": value,
                    "weight": weight,
                    "reason": reason
                    or f"{rule['field']} matched {found.group(0)!r}",
                }
            )
            # A member rule contributes at most once.  This avoids a large
            # archive inflating confidence just by repeating similar names.
            break
    return min(total, 100.0), matches


def _normalise_input(
    item: Mapping[str, Any],
    index: int,
    *,
    verify_hash: bool,
) -> dict[str, Any]:
    raw_path = item.get("path") or item.get("stored_path") or item.get("file_path")
    if not raw_path:
        raise PlanValidationError(f"input {index + 1} has no staged path")
    path = Path(str(raw_path)).expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise PlanValidationError(f"input file is missing or unsafe: {path}")
    stat_result = path.stat()
    actual_size = stat_result.st_size
    declared_size = item.get("size")
    if declared_size is not None and int(declared_size) != actual_size:
        raise PlanValidationError(
            f"input size changed for {item.get('original_name') or path.name}: "
            f"expected {declared_size}, got {actual_size}"
        )
    actual_hash = sha256_file(path) if verify_hash or not item.get("sha256") else str(item["sha256"])
    declared_hash = str(item.get("sha256") or "").lower()
    if declared_hash and verify_hash and declared_hash != actual_hash:
        raise PlanValidationError(
            f"input hash changed for {item.get('original_name') or path.name}"
        )
    original_name = Path(
        str(item.get("original_name") or item.get("filename") or path.name)
    ).name
    if not original_name:
        original_name = path.name
    upload_id = str(item.get("upload_id") or f"upload-{index + 1}-{actual_hash[:12]}").strip()
    if not upload_id or len(upload_id) > 256 or "\x00" in upload_id:
        raise PlanValidationError(f"input {index + 1} has an invalid upload_id")
    return {
        "upload_id": upload_id,
        "original_name": original_name,
        "path": str(path),
        "relative_path": str(item.get("relative_path") or item.get("original_relative_path") or ""),
        "size": actual_size,
        "sha256": actual_hash,
    }


def normalise_inputs(
    inputs: Sequence[Mapping[str, Any]], *, verify_hashes: bool = True
) -> list[dict[str, Any]]:
    if not isinstance(inputs, Sequence) or isinstance(inputs, (str, bytes)) or not inputs:
        raise PlanValidationError("at least one package input is required")
    result = [
        _normalise_input(item, index, verify_hash=verify_hashes)
        for index, item in enumerate(inputs)
    ]
    upload_ids = [item["upload_id"] for item in result]
    if len(upload_ids) != len(set(upload_ids)):
        raise PlanValidationError("upload_id values must be unique")
    return result


normalize_inputs = normalise_inputs


def _input_binding(item: Mapping[str, Any], *, include_mapping: bool = False) -> dict[str, Any]:
    result = {
        "upload_id": str(item.get("upload_id") or ""),
        "original_name": str(item.get("original_name") or ""),
        "path": str(Path(str(item.get("path") or "")).resolve()),
        "relative_path": str(item.get("relative_path") or ""),
        "size": int(item.get("size") or 0),
        "sha256": str(item.get("sha256") or "").lower(),
    }
    if include_mapping:
        selected = item.get("selected_components")
        if selected is None:
            selected = item.get("selected_component")
        if isinstance(selected, str):
            selected = [selected]
        result.update(
            {
                "include": bool(item.get("include")),
                "selected_component": sorted(str(value) for value in (selected or [])),
                "component_versions": {
                    str(key): str(value)
                    for key, value in sorted((item.get("component_versions") or {}).items())
                },
                "prebuilt": bool(item.get("prebuilt", False)),
            }
        )
    return result


def _draft_hash_payload(draft: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": str(draft.get("schema_version") or "1.0"),
        "packaging_requested": bool(draft.get("packaging_requested")),
        "catalog_digest": str(draft.get("catalog_digest") or ""),
        "project_code": str(draft.get("project_code") or ""),
        "version": str(draft.get("version") or ""),
        "mode": str(draft.get("mode") or ""),
        "inputs": [_input_binding(item) for item in draft.get("inputs", [])],
    }


def compute_draft_plan_hash(draft: Mapping[str, Any]) -> str:
    return canonical_hash(_draft_hash_payload(draft))


def _input_is_prebuilt(
    item: Mapping[str, Any], project: Mapping[str, Any], limits: Mapping[str, Any]
) -> bool:
    archive = inspect_archive(
        item["path"],
        max_members=int(limits["max_archive_members"]),
        max_uncompressed_bytes=int(limits["max_extract_bytes"]),
        strict=True,
    )
    if any(PurePosixPath(name).name == SI_INI_NAME for name in archive["members"]):
        return True
    score, _ = _score_rules(
        project["prebuilt_recognition"],
        {
            "filename": item.get("original_name", ""),
            "relative_path": item.get("relative_path") or item["path"],
            "archive_members": archive["members"],
        },
    )
    return bool(item.get("prebuilt")) or score >= 50


def _validated_prebuilt_component(
    item: Mapping[str, Any],
    project: Mapping[str, Any],
    component: Mapping[str, Any],
    limits: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the opted-in single-component TAR, without extracting it.

    Deliberately narrower than whole-package reuse: source metadata never becomes
    a payload, and an arbitrary manifest cannot introduce additional components.
    """
    if not component.get("allow_prebuilt_extract"):
        raise PlanValidationError("prebuilt package component extraction is not enabled")
    source = Path(item["path"])
    inspected = inspect_archive(
        source,
        max_members=int(limits["max_archive_members"]),
        max_uncompressed_bytes=int(limits["max_extract_bytes"]),
        strict=True,
    )
    if inspected["archive_type"] != "tar":
        raise PlanValidationError("prebuilt component extraction requires a TAR package")
    try:
        with tarfile.open(source, "r:*") as archive:
            members: dict[str, tarfile.TarInfo] = {}
            for member in archive.getmembers():
                name = _normalise_member_name(member.name)
                if member.isdir():
                    continue
                if not member.isfile() or name in members:
                    raise PlanValidationError("prebuilt package has duplicate or non-regular members")
                members[name] = member
            expected_name = str(component["output_name"])
            if set(members) != {SI_INI_NAME, expected_name}:
                raise PlanValidationError("prebuilt package must contain exactly si.ini and the component payload")
            metadata = _read_tar_member_limited(
                archive, members[SI_INI_NAME], limit=MAX_SI_INI_BYTES, label=SI_INI_NAME
            ).decode("utf-8")
            parsed: dict[str, str] = {}
            for raw_line in metadata.splitlines():
                line = raw_line.strip()
                if line == "---Release Note Starts---":
                    break
                if not line or line.startswith("#"):
                    continue
                match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*([^;\x00]+);", line)
                if match is None:
                    raise PlanValidationError("prebuilt si.ini contains a malformed field")
                key, value = match.group(1).lower(), match.group(2).strip()
                if key in parsed:
                    raise PlanValidationError("prebuilt si.ini contains duplicate fields")
                parsed[key] = value
            allowed_packet_attrs = {str(project["packet_attr"])}
            if project.get("patch_packet_attr"):
                allowed_packet_attrs.add(str(project["patch_packet_attr"]))
            if parsed.get("packetattr") not in allowed_packet_attrs:
                raise PlanValidationError("prebuilt PacketAttr does not match project catalog")
            if parsed.get("publisher") != project["publisher"]:
                raise PlanValidationError("prebuilt Publisher does not match project catalog")
            if parsed.get("filenuminpacket") != "1":
                raise PlanValidationError("prebuilt FileNumInPacket must declare exactly one payload")
            if parsed.get("filename_1") != expected_name:
                raise PlanValidationError("prebuilt FileName_1 does not match component catalog")
            if parsed.get("fileattr_1") != str(component["file_attr"]):
                raise PlanValidationError("prebuilt FileAttr_1 does not match component catalog")
            version = parsed.get("filever_1", "")
            if not version.startswith("V") or not _SAFE_VERSION_RE.fullmatch(version[1:]):
                raise PlanValidationError("prebuilt FileVer_1 is invalid")
            packet_version = parsed.get("packet_ver", "")
            if not packet_version.startswith("V") or not _SAFE_VERSION_RE.fullmatch(packet_version[1:]):
                raise PlanValidationError("prebuilt Packet_Ver is invalid")
            for key in parsed:
                indexed = re.fullmatch(r"file[a-z0-9_]*_(\d+)", key)
                if indexed and indexed.group(1) != "1":
                    raise PlanValidationError("prebuilt file fields disagree with declared count")
            payload = members[expected_name]
            if payload.size <= 0:
                raise PlanValidationError("prebuilt component payload is empty")
            for key in ("filesize_1", "filelength_1", "filelen_1"):
                if key in parsed and (not parsed[key].isdigit() or int(parsed[key]) != payload.size):
                    raise PlanValidationError("prebuilt component size mismatch")
            digests = {"md5": hashlib.md5(), "sha1": hashlib.sha1(), "sha256": hashlib.sha256()}
            handle = archive.extractfile(payload)
            if handle is None:
                raise PlanValidationError("prebuilt component payload cannot be read")
            total = 0
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                total += len(chunk)
                if total > payload.size:
                    raise PlanValidationError("prebuilt component size mismatch")
                for digest in digests.values():
                    digest.update(chunk)
            if total != payload.size:
                raise PlanValidationError("prebuilt component size mismatch")
            checksum_fields = {"filemd5_1": "md5", "filesha1_1": "sha1", "filesha256_1": "sha256"}
            for key, value in parsed.items():
                algorithm = checksum_fields.get(key)
                if key in {"filechecksum_1", "filehash_1"}:
                    algorithm = {32: "md5", 40: "sha1", 64: "sha256"}.get(len(value))
                    if algorithm is None:
                        raise PlanValidationError("prebuilt checksum format is unsupported")
                elif any(token in key for token in ("checksum", "hash", "md5", "sha", "crc", "digest")) and algorithm is None:
                    raise PlanValidationError("prebuilt checksum field is unsupported")
                if algorithm and value.lower() != digests[algorithm].hexdigest():
                    raise PlanValidationError("prebuilt component checksum mismatch")
            return {"version": version, "payload_name": expected_name, "size": total,
                    "sha256": digests["sha256"].hexdigest()}
    except (OSError, tarfile.TarError, UnicodeDecodeError, PackageBuildError) as exc:
        raise PlanValidationError(f"invalid prebuilt component package: {exc}") from exc


def classify_inputs(
    catalog: Any,
    inputs: Sequence[Mapping[str, Any]],
    *,
    project_hint: Optional[str] = None,
    verify_hashes: bool = True,
) -> ClassificationDraft:
    """Classify staged inputs and create an untrusted, confirmation-ready draft."""

    loaded = load_catalog(catalog)
    normalised_inputs = normalise_inputs(inputs, verify_hashes=verify_hashes)
    limits = loaded["limits"]
    hint = str(project_hint or "").strip().lower()
    project_scores: dict[str, float] = defaultdict(float)
    project_evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    classified_inputs: list[dict[str, Any]] = []

    for input_item in normalised_inputs:
        archive = inspect_archive(
            input_item["path"],
            max_members=int(limits["max_archive_members"]),
            max_uncompressed_bytes=int(limits["max_extract_bytes"]),
            strict=False,
        )
        evidence = {
            "filename": input_item["original_name"],
            "relative_path": input_item["relative_path"] or input_item["path"],
            "extension": _compound_extension(input_item["original_name"]),
            "magic": _magic_label(Path(input_item["path"]), archive["archive_type"]),
            "archive_members": archive["members"],
        }
        input_project_candidates: list[dict[str, Any]] = []
        all_component_candidates: list[dict[str, Any]] = []
        prebuilt_projects: set[str] = set()
        prebuilt_errors: list[str] = []

        for project in loaded["projects"]:
            code = project["project_code"]
            direct_score, direct_evidence = _score_rules(project["recognition"], evidence)
            if hint and code == hint:
                direct_score = min(100.0, direct_score + 20.0)
                direct_evidence.append(
                    {
                        "field": "project_hint",
                        "matched": hint,
                        "location": hint,
                        "weight": 20.0,
                        "reason": "前端预选项目（仅作为证据，仍需人工确认）",
                    }
                )
            prebuilt_score, prebuilt_evidence = _score_rules(
                project["prebuilt_recognition"], evidence
            )
            is_prebuilt = prebuilt_score >= 50 or any(
                PurePosixPath(name).name == SI_INI_NAME for name in archive["members"]
            )
            if is_prebuilt:
                prebuilt_projects.add(code)

            component_scores: list[float] = []
            for component in project["components"]:
                score, matches = _score_rules(component["recognition"], evidence)
                if score < float(component["classification_threshold"]):
                    continue
                component_scores.append(score)
                version_candidates = infer_versions(
                    input_item["original_name"],
                    relative_path=evidence["relative_path"],
                    archive_members=archive["members"],
                    rules=component["version_rules"],
                )
                prebuilt_component = None
                if is_prebuilt and component.get("allow_prebuilt_extract"):
                    try:
                        prebuilt_component = _validated_prebuilt_component(
                            input_item, project, component, limits
                        )
                    except FullPackageError as exc:
                        prebuilt_errors.append(f"{code}/{component['component_key']}: {exc}")
                    else:
                        version_candidates = [{
                            "version": prebuilt_component["version"],
                            "source": "si.ini", "matched": prebuilt_component["version"],
                            "location": SI_INI_NAME,
                        }]
                publishable = bool(component["publishable"]) and (
                    not is_prebuilt or prebuilt_component is not None
                )
                candidate = {
                    "project_code": code,
                    "component_key": component["component_key"],
                    "label": component["label"],
                    "confidence": round(score / 100.0, 4),
                    "score": round(score, 2),
                    "evidence": matches + (prebuilt_evidence if is_prebuilt else []),
                    "publishable": publishable,
                    "recognition_only": bool(component["recognition_only"]),
                    "prebuilt": is_prebuilt,
                    "prebuilt_extract": prebuilt_component is not None,
                    "ambiguous_group": component["ambiguous_group"],
                    "version": version_candidates[0]["version"] if version_candidates else None,
                    "version_candidates": version_candidates,
                }
                if not publishable:
                    candidate["unpublishable_reason"] = (
                        "prebuilt_package" if is_prebuilt else "recognition_only"
                    )
                all_component_candidates.append(candidate)

            component_contribution = min(sum(component_scores) * 0.35, 80.0)
            score = min(100.0, direct_score + component_contribution)
            combined_evidence = direct_evidence[:]
            if component_scores:
                combined_evidence.append(
                    {
                        "field": "component_evidence",
                        "matched": f"{len(component_scores)} component(s)",
                        "location": input_item["original_name"],
                        "weight": round(component_contribution, 2),
                        "reason": "组件识别规则支持该项目",
                    }
                )
            if score or len(loaded["projects"]) == 1:
                input_project_candidates.append(
                    {
                        "project_code": code,
                        "label": project["label"],
                        "confidence": round(score / 100.0, 4),
                        "score": round(score, 2),
                        "evidence": combined_evidence,
                    }
                )
                project_scores[code] += score
                project_evidence[code].extend(combined_evidence)

        all_component_candidates.sort(
            key=lambda item: (-float(item["score"]), item["project_code"], item["component_key"])
        )
        input_project_candidates.sort(
            key=lambda item: (-float(item["score"]), item["project_code"])
        )
        preferred_project = input_project_candidates[0]["project_code"] if input_project_candidates else None
        preferred = [
            candidate
            for candidate in all_component_candidates
            if candidate["project_code"] == preferred_project
            and candidate["publishable"]
            and candidate["confidence"] >= 0.55
        ]
        ambiguous_groups = {
            candidate["ambiguous_group"]
            for candidate in preferred
            if candidate.get("ambiguous_group")
        }
        selected = [
            candidate["component_key"]
            for candidate in preferred
            if not candidate.get("ambiguous_group")
        ]
        versions = infer_versions(
            input_item["original_name"],
            relative_path=evidence["relative_path"],
            archive_members=archive["members"],
        )
        classified_inputs.append(
            {
                **input_item,
                "file_type": evidence["magic"],
                "archive": archive.to_dict(),
                "project_candidates": input_project_candidates,
                "candidates": all_component_candidates,
                "selected_component": selected,
                "suggested_components": selected,
                "include": None,
                "prebuilt": bool(prebuilt_projects),
                "ambiguity": sorted(ambiguous_groups),
                "version": versions[0]["version"] if versions else None,
                "version_candidates": versions,
                "classification_errors": list(archive["errors"]) + prebuilt_errors,
            }
        )

    candidate_projects = []
    for project in loaded["projects"]:
        code = project["project_code"]
        candidate_projects.append(
            {
                "project_code": code,
                "label": project["label"],
                "confidence": round(min(project_scores.get(code, 0.0) / max(len(normalised_inputs), 1), 100.0) / 100.0, 4),
                "score": round(project_scores.get(code, 0.0), 2),
                "evidence": project_evidence.get(code, []),
            }
        )
    candidate_projects.sort(key=lambda item: (-float(item["score"]), item["project_code"]))
    proposed_project = candidate_projects[0]["project_code"] if candidate_projects else ""

    # Prefer a project-level version (for example LX10-V1.0.0.3 in the staged
    # relative path), then fall back to a consensus among component evidence.
    project_version_candidates: list[dict[str, Any]] = []
    project = loaded.projects_by_code.get(proposed_project)
    if project:
        for input_item in classified_inputs:
            project_version_candidates.extend(
                infer_versions(
                    input_item["original_name"],
                    relative_path=input_item["relative_path"] or input_item["path"],
                    archive_members=input_item["archive"]["members"],
                    rules=project["version_rules"],
                )
            )
    deduped_versions: list[dict[str, Any]] = []
    seen_versions: set[str] = set()
    for version in project_version_candidates:
        if version["version"] not in seen_versions:
            seen_versions.add(version["version"])
            deduped_versions.append(version)

    draft = ClassificationDraft(
        {
            "schema_version": "1.0",
            "packaging_requested": True,
            "catalog_digest": loaded.digest,
            "catalog_version": loaded["catalog_version"],
            "candidate_projects": candidate_projects,
            "project_candidates": candidate_projects,
            "project_code": proposed_project,
            "version": deduped_versions[0]["version"].lstrip("V") if deduped_versions else "",
            "version_candidates": deduped_versions,
            "mode": "full",
            "inputs": classified_inputs,
        }
    )
    draft["plan_hash"] = compute_draft_plan_hash(draft)
    draft["questions"] = build_confirmation_questions(draft, loaded)
    return draft


create_draft_plan = classify_inputs


# ───────────────────── Mandatory questions / confirmation ──────────────


def _option(label: str, description: str, value: Any, **extra: Any) -> dict[str, Any]:
    return {"label": label, "description": description, "value": value, **extra}


def build_confirmation_questions(
    draft: Mapping[str, Any], catalog: Any = None
) -> list[dict[str, Any]]:
    """Build one mandatory question for project/version/mode and every input."""

    if catalog is not None:
        load_catalog(catalog)  # validate the exact catalog used by the draft
    project_candidates = list(
        draft.get("candidate_projects") or draft.get("project_candidates") or []
    )
    project_options = [
        _option(
            str(item.get("label") or item.get("project_code")),
            f"识别置信度 {float(item.get('confidence') or 0):.0%}；仍需人工确认。",
            str(item.get("project_code")),
        )
        for item in project_candidates[:3]
    ]
    if len(project_options) < 2:
        project_options.append(
            _option("取消本次打包", "不确认项目，不会生成或上传任何整包。", "cancel")
        )
    questions: list[dict[str, Any]] = [
        {
            "question_key": "project",
            "header": "目标项目",
            "question": "请确认本次整包所属项目（初判仅作参考）。",
            "multiSelect": False,
            "required": True,
            "allow_custom": False,
            "options": project_options[:4],
        }
    ]

    version = str(draft.get("version") or "").lstrip("Vv")
    version_options = []
    if version:
        version_options.append(
            _option(f"V{version}", "采用识别到的整包版本。", version)
        )
    version_options.append(
        _option("手动输入版本", "输入项目 catalog 允许的版本号。", "custom")
    )
    if len(version_options) < 2:
        version_options.append(_option("取消本次打包", "不生成整包。", "cancel"))
    questions.append(
        {
            "question_key": "version",
            "header": "整包版本",
            "question": "请确认整包版本号。",
            "multiSelect": False,
            "required": True,
            "allow_custom": True,
            "options": version_options[:4],
        }
    )
    questions.append(
        {
            "question_key": "mode",
            "header": "整包类型",
            "question": "请确认生成全量包还是补丁包。",
            "multiSelect": False,
            "required": True,
            "allow_custom": False,
            "options": [
                _option("全量包", "使用项目的 PacketAttr。", "full"),
                _option("补丁包", "使用项目配置的 patch PacketAttr。", "patch"),
            ],
        }
    )

    proposed_project = str(draft.get("project_code") or "")
    for item in draft.get("inputs", []):
        upload_id = str(item.get("upload_id") or "")
        candidates = [
            candidate
            for candidate in item.get("candidates", [])
            if candidate.get("project_code") == proposed_project
        ]
        publishable = [candidate for candidate in candidates if candidate.get("publishable")]
        options: list[dict[str, Any]] = []

        # Preserve the important one-source-many suggestion as one option.
        suggested = [str(value) for value in item.get("suggested_components") or []]
        suggested_candidates = [
            candidate for candidate in publishable if candidate["component_key"] in suggested
        ]
        if len(suggested_candidates) > 1:
            labels = " + ".join(candidate["label"] for candidate in suggested_candidates)
            values = [candidate["component_key"] for candidate in suggested_candidates]
            options.append(
                _option(
                    labels,
                    "一个源文件同时提供这些组件；构建时只解压一次。",
                    values,
                    component_keys=values,
                )
            )
        for candidate in publishable:
            if len(options) >= 3:
                break
            options.append(
                _option(
                    str(candidate["label"]),
                    f"置信度 {float(candidate['confidence']):.0%}；"
                    + "；".join(
                        str(evidence.get("reason") or "")
                        for evidence in candidate.get("evidence", [])[:2]
                        if evidence.get("reason")
                    ),
                    candidate["component_key"],
                    component_keys=[candidate["component_key"]],
                )
            )
        if not options:
            options.append(
                _option(
                    "手动指定组件",
                    "没有可自动发布的候选；可输入该项目 catalog 中的组件 key。",
                    "custom",
                )
            )
        options = options[:3]
        options.append(
            _option("排除此文件", "明确确认该文件不进入本次整包。", "exclude")
        )
        questions.append(
            {
                "question_key": f"input:{upload_id}",
                "header": f"文件 {len(questions) - 2}",
                "question": f"请确认 {item.get('original_name')} 对应的组件，或明确排除。",
                "multiSelect": True,
                "required": True,
                "allow_custom": True,
                "options": options[:4],
                "upload_id": upload_id,
            }
        )
    return questions


def _answers_by_key(answers: Any, questions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    questions_by_key = {
        str(question.get("question_key") or ""): question for question in questions
    }

    def _from_resolution(answer: Mapping[str, Any], question: Mapping[str, Any]) -> Any:
        """Translate the frontend's display labels back to catalog-safe values."""

        selected_values = answer.get("selected_values")
        if isinstance(selected_values, list) and selected_values:
            resolved: Any = list(selected_values)
        elif selected_values is not None and selected_values != "":
            resolved = selected_values
        else:
            labels = answer.get("selected_labels")
            labels = labels if isinstance(labels, list) else []
            option_by_label = {
                str(option.get("label") or "").strip(): option
                for option in question.get("options", [])
                if isinstance(option, Mapping)
            }
            mapped: list[Any] = []
            for raw_label in labels:
                label = str(raw_label).strip()
                if not label:
                    continue
                option = option_by_label.get(label)
                value = option.get("value") if option is not None else label
                if isinstance(value, list):
                    mapped.extend(value)
                else:
                    mapped.append(value)
            if question.get("multiSelect"):
                resolved = mapped
            else:
                resolved = mapped[0] if mapped else None

        custom = str(answer.get("custom_text") or "").strip()
        selected_for_custom = resolved if isinstance(resolved, list) else [resolved]
        selected_custom = any(str(value or "").strip().lower() == "custom" for value in selected_for_custom)
        # A free-form response is authoritative only when no preset was chosen
        # or the explicit "custom" option was selected.  This prevents an
        # unrelated text note from overwriting a valid project/component label.
        if custom and (
            not any(str(value or "").strip() for value in selected_for_custom)
            or selected_custom
        ):
            return custom
        return resolved

    if isinstance(answers, Mapping):
        # Native programmatic shape: {"project": ..., "input:id": ...}.
        if "answers" in answers and isinstance(answers["answers"], (Mapping, list)):
            return _answers_by_key(answers["answers"], questions)
        normalised_mapping: dict[str, Any] = {}
        for key, value in answers.items():
            question = questions_by_key.get(str(key))
            if question is not None and isinstance(value, Mapping):
                normalised_mapping[str(key)] = _from_resolution(value, question)
            else:
                normalised_mapping[str(key)] = value
        return normalised_mapping
    result: dict[str, Any] = {}
    if not isinstance(answers, list):
        return result
    for item in answers:
        if not isinstance(item, Mapping):
            continue
        key = item.get("question_key")
        if key is None and item.get("question_index") is not None:
            try:
                key = questions[int(item["question_index"])]["question_key"]
            except (IndexError, KeyError, TypeError, ValueError):
                continue
        if not key:
            continue
        question = questions_by_key.get(str(key))
        if question is None:
            continue
        selected = item.get("value")
        if selected is None:
            selected = _from_resolution(item, question)
        result[str(key)] = selected
    return result


def _answer_scalar(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("value", "answer", "custom_text", "selected"):
            if value.get(key) is not None:
                return _answer_scalar(value[key])
        labels = value.get("selected_labels")
        if isinstance(labels, list) and labels:
            return str(labels[0]).strip()
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value or "").strip()


def _parse_component_answer(value: Any) -> tuple[bool, list[str], dict[str, str]]:
    versions: dict[str, str] = {}
    if isinstance(value, Mapping):
        include = value.get("include")
        raw_components = (
            value.get("components")
            if value.get("components") is not None
            else value.get("selected_component")
        )
        raw_versions = value.get("component_versions") or {}
        if isinstance(raw_versions, Mapping):
            versions = {str(key): str(val) for key, val in raw_versions.items()}
        if include is False:
            return False, [], versions
        value = raw_components if raw_components is not None else value.get("value")
    if isinstance(value, str):
        text = value.strip()
        if text.casefold() in {item.casefold() for item in _EXCLUDE_ANSWERS}:
            return False, [], versions
        components = [part.strip() for part in re.split(r"[,，+\s]+", text) if part.strip()]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        flat: list[str] = []
        for item in value:
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                flat.extend(str(part).strip() for part in item if str(part).strip())
            elif str(item).strip():
                flat.append(str(item).strip())
        if any(item.casefold() in {value.casefold() for value in _EXCLUDE_ANSWERS} for item in flat):
            if len(flat) > 1:
                raise PlanValidationError("exclude cannot be combined with component mappings")
            return False, [], versions
        components = flat
    else:
        components = []
    unique = list(dict.fromkeys(component.lower() for component in components if component))
    return bool(unique), unique, versions


def _confirmed_hash_payload(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": str(plan.get("schema_version") or "1.0"),
        "packaging_requested": bool(plan.get("packaging_requested")),
        "catalog_digest": str(plan.get("catalog_digest") or ""),
        "session_id": str(plan.get("session_id") or ""),
        "user_id": str(plan.get("user_id") or ""),
        "project_code": str(plan.get("project_code") or ""),
        "version": str(plan.get("version") or ""),
        "mode": str(plan.get("mode") or ""),
        "packet_attr": int(plan.get("packet_attr") or 0),
        "inputs": [
            _input_binding(item, include_mapping=True) for item in plan.get("inputs", [])
        ],
    }


def compute_confirmed_plan_hash(plan: Mapping[str, Any]) -> str:
    return canonical_hash(_confirmed_hash_payload(plan))


def confirm_plan(
    draft: Mapping[str, Any],
    answers: Any,
    *,
    session_id: str,
    user_id: str,
    run_id: str = "",
    catalog: Any = None,
    inputs: Optional[Sequence[Mapping[str, Any]]] = None,
    confirmed_at: Optional[str] = None,
) -> ConfirmedPackagePlan:
    """Resolve every mandatory answer into a hash-bound confirmed plan."""

    loaded = load_catalog(catalog)
    if not draft.get("packaging_requested"):
        raise PlanValidationError("draft is not a packaging request")
    if str(draft.get("catalog_digest") or "") != loaded.digest:
        raise PlanValidationError("catalog changed after classification; reclassify inputs")
    expected_draft_hash = compute_draft_plan_hash(draft)
    if str(draft.get("plan_hash") or "") != expected_draft_hash:
        raise PlanValidationError("draft plan hash is invalid")
    if not str(session_id).strip() or not str(user_id).strip():
        raise PlanValidationError("session_id and user_id are required for confirmation")

    draft_inputs = list(draft.get("inputs") or [])
    if inputs is not None:
        current = normalise_inputs(inputs, verify_hashes=True)
        if [_input_binding(item) for item in current] != [
            _input_binding(item) for item in draft_inputs
        ]:
            raise PlanValidationError("package inputs changed after classification")
    answer_map = _answers_by_key(answers, build_confirmation_questions(draft, loaded))
    required_keys = {"project", "version", "mode"} | {
        f"input:{item.get('upload_id')}" for item in draft_inputs
    }
    missing = sorted(
        key
        for key in required_keys
        if key not in answer_map or answer_map[key] is None or answer_map[key] == ""
    )
    if missing:
        raise PlanValidationError(f"missing mandatory confirmation answers: {', '.join(missing)}")

    project_code = _answer_scalar(answer_map["project"]).lower()
    project = loaded.projects_by_code.get(project_code)
    if project is None:
        raise PlanValidationError(f"unknown or cancelled project: {project_code}")
    version = _answer_scalar(answer_map["version"]).lstrip("Vv")
    if version.lower() in {"custom", "cancel"} or not _SAFE_VERSION_RE.fullmatch(version):
        raise PlanValidationError(f"invalid package version: {version!r}")
    if not re.fullmatch(str(project["package_version_pattern"]), version, re.IGNORECASE):
        raise PlanValidationError(
            f"package version {version!r} does not match project catalog"
        )
    mode_answer = _answer_scalar(answer_map["mode"]).lower()
    mode_aliases = {
        "full": "full",
        "全量包": "full",
        "normal": "full",
        "patch": "patch",
        "补丁包": "patch",
    }
    mode = mode_aliases.get(mode_answer)
    if mode is None:
        raise PlanValidationError(f"invalid package mode: {mode_answer!r}")
    if mode == "patch" and not project.get("patch_packet_attr"):
        raise PlanValidationError(f"project {project_code} does not support patch packages")
    packet_attr = int(
        project["patch_packet_attr"] if mode == "patch" else project["packet_attr"]
    )
    components_by_key = {
        component["component_key"]: component for component in project["components"]
    }
    confirmed_inputs: list[dict[str, Any]] = []
    globally_selected: dict[str, str] = {}
    included_count = 0

    for item in draft_inputs:
        upload_id = str(item.get("upload_id") or "")
        include, component_keys, versions = _parse_component_answer(
            answer_map[f"input:{upload_id}"]
        )
        is_prebuilt = bool(item.get("prebuilt"))
        if include:
            is_prebuilt = _input_is_prebuilt(item, project, loaded["limits"])
        selected_versions: dict[str, str] = {}
        for component_key in component_keys:
            component = components_by_key.get(component_key)
            if component is None:
                raise PlanValidationError(
                    f"unknown component {component_key!r} for project {project_code}"
                )
            if not component["publishable"] or component["recognition_only"]:
                raise PlanValidationError(
                    f"component {component_key!r} is recognition-only and cannot be packaged"
                )
            if component_key in globally_selected:
                raise PlanValidationError(
                    f"component {component_key!r} is mapped by both "
                    f"{globally_selected[component_key]} and {upload_id}"
                )
            globally_selected[component_key] = upload_id
            prebuilt_component = None
            if is_prebuilt:
                prebuilt_component = _validated_prebuilt_component(
                    item, project, component, loaded["limits"]
                )
                if versions.get(component_key) and str(versions[component_key]).lstrip("Vv") != (
                    prebuilt_component["version"].lstrip("Vv")
                ):
                    raise PlanValidationError("prebuilt component version must match FileVer_1")
            inferred = next(
                (
                    candidate.get("version")
                    for candidate in item.get("candidates", [])
                    if candidate.get("project_code") == project_code
                    and candidate.get("component_key") == component_key
                    and candidate.get("version")
                ),
                None,
            )
            component_version = str(
                (prebuilt_component or {}).get("version")
                or versions.get(component_key)
                or inferred
                or item.get("version")
                or component["default_version"]
            )
            if not component_version.startswith("V"):
                component_version = f"V{component_version}"
            selected_versions[component_key] = component_version
        if include:
            included_count += len(component_keys)
        confirmed_inputs.append(
            {
                **_input_binding(item),
                "include": include,
                "selected_components": sorted(component_keys),
                "selected_component": (
                    component_keys[0] if len(component_keys) == 1 else sorted(component_keys)
                ),
                "component_versions": selected_versions,
                "prebuilt": is_prebuilt,
            }
        )
    if included_count == 0:
        raise PlanValidationError("at least one publishable component must be included")

    timestamp = confirmed_at or datetime.now(timezone.utc).isoformat()
    confirmed = ConfirmedPackagePlan(
        {
            "schema_version": "1.0",
            "packaging_requested": True,
            "status": "confirmed",
            "catalog_digest": loaded.digest,
            "catalog_version": loaded["catalog_version"],
            "draft_plan_hash": expected_draft_hash,
            "session_id": str(session_id),
            "user_id": str(user_id),
            "run_id": str(run_id or ""),
            "project_code": project_code,
            "version": version,
            "mode": mode,
            "is_patch": mode == "patch",
            "packet_attr": packet_attr,
            "inputs": confirmed_inputs,
            "confirmed_at": timestamp,
        }
    )
    confirmed["plan_hash"] = compute_confirmed_plan_hash(confirmed)
    confirmed["confirmation_hash"] = canonical_hash(
        {
            "plan_hash": confirmed["plan_hash"],
            "confirmed_at": confirmed["confirmed_at"],
        }
    )
    return confirmed


create_confirmed_plan = confirm_plan


def validate_confirmed_plan(
    plan: Mapping[str, Any],
    catalog: Any = None,
    *,
    inputs: Optional[Sequence[Mapping[str, Any]]] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    verify_files: bool = True,
) -> ConfirmedPackagePlan:
    """Recompute every trust binding and return a normalised confirmed plan."""

    loaded = load_catalog(catalog)
    candidate = ConfirmedPackagePlan(copy.deepcopy(dict(plan)))
    if candidate.get("status") != "confirmed" or not candidate.get("packaging_requested"):
        raise PlanValidationError("package plan is not confirmed")
    if candidate.get("catalog_digest") != loaded.digest:
        raise PlanValidationError("confirmed plan catalog digest is stale")
    if session_id is not None and str(candidate.get("session_id")) != str(session_id):
        raise PlanValidationError("confirmed plan belongs to a different session")
    if user_id is not None and str(candidate.get("user_id")) != str(user_id):
        raise PlanValidationError("confirmed plan belongs to a different user")
    project = loaded.projects_by_code.get(str(candidate.get("project_code") or ""))
    if project is None:
        raise PlanValidationError("confirmed project no longer exists in catalog")
    expected_attr = project["patch_packet_attr"] if candidate.get("mode") == "patch" else project["packet_attr"]
    if not expected_attr or int(candidate.get("packet_attr") or 0) != int(expected_attr):
        raise PlanValidationError("confirmed PacketAttr does not match catalog")
    expected_plan_hash = compute_confirmed_plan_hash(candidate)
    if candidate.get("plan_hash") != expected_plan_hash:
        raise PlanValidationError("confirmed plan hash is invalid")
    expected_confirmation_hash = canonical_hash(
        {
            "plan_hash": expected_plan_hash,
            "confirmed_at": str(candidate.get("confirmed_at") or ""),
        }
    )
    if candidate.get("confirmation_hash") != expected_confirmation_hash:
        raise PlanValidationError("confirmation hash is invalid")

    current_inputs = normalise_inputs(candidate.get("inputs") or [], verify_hashes=verify_files)
    expected_bindings = [_input_binding(item) for item in candidate.get("inputs", [])]
    if [_input_binding(item) for item in current_inputs] != expected_bindings:
        raise PlanValidationError("confirmed input files changed")
    if inputs is not None:
        supplied = normalise_inputs(inputs, verify_hashes=verify_files)
        if [_input_binding(item) for item in supplied] != expected_bindings:
            raise PlanValidationError("current package manifest differs from confirmed plan")

    components = {item["component_key"]: item for item in project["components"]}
    seen: set[str] = set()
    included = 0
    for input_item in candidate.get("inputs", []):
        selected = input_item.get("selected_components")
        if selected is None:
            selected = input_item.get("selected_component") or []
        if isinstance(selected, str):
            selected = [selected]
        if bool(input_item.get("include")) != bool(selected):
            raise PlanValidationError(
                f"input {input_item.get('upload_id')} has inconsistent include/mapping state"
            )
        is_prebuilt = bool(input_item.get("prebuilt"))
        if selected and verify_files:
            is_prebuilt = _input_is_prebuilt(input_item, project, loaded["limits"])
        for key in selected:
            component = components.get(str(key))
            if component is None or not component["publishable"] or component["recognition_only"]:
                raise PlanValidationError(f"component {key!r} is not publishable")
            if key in seen:
                raise PlanValidationError(f"component {key!r} is selected more than once")
            if is_prebuilt:
                if not verify_files:
                    if not component.get("allow_prebuilt_extract"):
                        raise PlanValidationError("prebuilt input cannot be included")
                else:
                    verified = _validated_prebuilt_component(input_item, project, component, loaded["limits"])
                    if (input_item.get("component_versions") or {}).get(key) != verified["version"]:
                        raise PlanValidationError("confirmed prebuilt version differs from FileVer_1")
            seen.add(str(key))
            included += 1
    if included == 0:
        raise PlanValidationError("confirmed plan contains no components")
    return candidate


# ─────────────────────── Deterministic package build ───────────────────


def _component_sort_key(component: Mapping[str, Any]) -> tuple[int, str]:
    return int(component["file_attr"]), str(component["component_key"])


def _extract_7z_streaming(source: Path, destination: Path) -> None:
    """Ask py7zr to stream members directly to disk.

    The legacy log workspace's ``SevenZipFile.read()`` path materialises every
    member in memory.  Package inputs can be hundreds of MiB, so the builder
    uses ``extractall`` after the member/size/path preflight and validates the
    resulting tree immediately afterwards.
    """

    try:
        import py7zr
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise PackageBuildError("py7zr is required to extract .7z archives") from exc
    try:
        with py7zr.SevenZipFile(source, mode="r") as archive:
            archive.extractall(path=destination)
    except Exception as exc:
        raise PackageBuildError(f"failed to extract 7z archive {source.name}: {exc}") from exc


def _safe_extract_archive(
    source: Path,
    destination: Path,
    *,
    max_members: int,
    max_bytes: int,
) -> SerializableDict:
    inspection = inspect_archive(
        source,
        max_members=max_members,
        max_uncompressed_bytes=max_bytes,
        strict=True,
    )
    if not inspection["is_archive"]:
        raise PackageBuildError(f"extract_match source is not an archive: {source.name}")
    reserve = max(0, int(getattr(settings, "disk_reserve_bytes", 0) or 0))
    free = shutil.disk_usage(destination.parent).free
    declared = int(inspection["total_uncompressed_bytes"])
    if declared > max(0, free - reserve):
        raise PackageBuildError(
            f"not enough workspace disk for {source.name}: needs {declared} bytes "
            f"while preserving {reserve} bytes"
        )
    destination.mkdir(parents=True, exist_ok=False)
    try:
        if inspection["archive_type"] == "7z":
            _extract_7z_streaming(source, destination)
        else:
            _extract_archive(source, destination, max_bytes)
        _validate_extracted_output(destination, max_bytes)
        files = [path for path in destination.rglob("*") if path.is_file()]
        if len(files) > max_members:
            raise PackageBuildError(
                f"archive {source.name} extracted more than {max_members} files"
            )
        # Resolve every path after extraction too.  This catches a backend that
        # unexpectedly emitted a symlink despite the member preflight.
        root = destination.resolve()
        for path in destination.rglob("*"):
            resolved = path.resolve()
            if path.is_symlink() or (resolved != root and root not in resolved.parents):
                raise PackageBuildError(f"unsafe extracted path: {path}")
    except (WorkspaceError, WorkspaceExtractTooLarge, ArchiveInspectionError, PackageBuildError) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        if isinstance(exc, PackageBuildError):
            raise
        raise PackageBuildError(f"failed to safely extract {source.name}: {exc}") from exc
    except Exception as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise PackageBuildError(f"failed to safely extract {source.name}: {exc}") from exc
    return inspection


def _materialise_extract_match(
    extraction_root: Path,
    materialization: Mapping[str, Any],
    *,
    source_name: str,
    component_key: str,
) -> Path:
    includes = [re.compile(pattern, re.IGNORECASE) for pattern in materialization["patterns"]]
    excludes = [
        re.compile(pattern, re.IGNORECASE)
        for pattern in materialization.get("exclude_patterns", [])
    ]
    matches: list[Path] = []
    for path in extraction_root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(extraction_root).as_posix()
        if any(regex.search(relative) for regex in excludes):
            continue
        if any(regex.search(relative) for regex in includes):
            matches.append(path)
    matches.sort(key=lambda path: path.relative_to(extraction_root).as_posix())
    if not matches:
        raise PackageBuildError(
            f"{source_name}: no payload matched component {component_key}"
        )
    if len(matches) > 1:
        joined = ", ".join(
            path.relative_to(extraction_root).as_posix() for path in matches[:5]
        )
        raise PackageBuildError(
            f"{source_name}: component {component_key} matched multiple payloads: {joined}"
        )
    return matches[0]


def _collision_safe_name(desired: str, component_key: str, used: set[str]) -> str:
    candidate = Path(desired).name
    if candidate.casefold() not in used:
        used.add(candidate.casefold())
        return candidate
    suffixes = "".join(Path(candidate).suffixes)
    stem = candidate[: -len(suffixes)] if suffixes else candidate
    index = 1
    while True:
        discriminator = component_key if index == 1 else f"{component_key}-{index}"
        alternative = f"{stem}-{discriminator}{suffixes}"
        if alternative.casefold() not in used:
            used.add(alternative.casefold())
            return alternative
        index += 1


def _numeric_version(version: str) -> str:
    base = version.lstrip("Vv").split("-", 1)[0]
    parts = base.split(".")[:4]
    return "".join(str(int(part)) for part in parts if part.isdigit()) or re.sub(r"\W+", "", base)


def package_filename(project: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
    values = {
        "package_prefix": project["package_prefix"],
        "version": str(plan["version"]),
        "numeric_version": _numeric_version(str(plan["version"])),
        "mode": str(plan["mode"]),
        "patch_suffix": "-Patch" if plan["mode"] == "patch" else "",
        "confirmation_short": str(plan["confirmation_hash"])[:12],
        "project_code": str(plan["project_code"]),
    }
    try:
        name = str(project["package_name_pattern"]).format(**values)
    except (KeyError, ValueError) as exc:
        raise PackageBuildError(f"invalid package_name_pattern: {exc}") from exc
    if not name.lower().endswith(".tgz"):
        name += ".tgz"
    try:
        return _safe_flat_name(name, "generated package filename")
    except CatalogValidationError as exc:
        raise PackageBuildError(str(exc)) from exc


def generate_si_ini(
    project: Mapping[str, Any],
    plan: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
) -> str:
    ordered = sorted(components, key=_component_sort_key)
    lines = [
        f"Packet_Ver=V{str(plan['version']).lstrip('Vv')};",
        f"PacketAttr={int(plan['packet_attr'])};",
        f"Publisher={project['publisher']};",
        f"FileNumInPacket={len(ordered)};",
        "",
    ]
    for index, component in enumerate(ordered, start=1):
        version = str(component.get("version") or "V0.0.0.0")
        if not version.startswith("V"):
            version = f"V{version}"
        lines.extend(
            [
                f"FileName_{index}={component['output_name']};",
                f"FileAttr_{index}={int(component['file_attr'])};",
                f"FileVer_{index}={version};",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _add_deterministic_file(archive: tarfile.TarFile, path: Path, arcname: str) -> None:
    info = archive.gettarinfo(str(path), arcname=arcname)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o644
    with path.open("rb") as handle:
        archive.addfile(info, handle)


def _create_deterministic_tgz(source_dir: Path, output_path: Path, names: Sequence[str]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name in sorted(names):
                    _add_deterministic_file(archive, source_dir / name, name)
        raw.flush()
        os.fsync(raw.fileno())


def _parse_si_ini(content: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().removesuffix(";")
    return result


def _read_tar_member_limited(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    limit: int,
    label: str,
) -> bytes:
    if member.size > limit:
        raise PackageBuildError(f"{label} exceeds metadata limit {limit} bytes")
    extracted = archive.extractfile(member)
    if extracted is None:
        raise PackageBuildError(f"cannot read package member: {member.name}")
    data = extracted.read(limit + 1)
    if len(data) > limit:
        raise PackageBuildError(f"{label} exceeds metadata limit {limit} bytes")
    return data


def _stream_tar_member_hash(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    expected_size: int,
) -> tuple[int, str]:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise PackageBuildError(f"cannot read package member: {member.name}")
    total = 0
    digest = hashlib.sha256()
    while True:
        chunk = extracted.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > expected_size:
            raise PackageBuildError(f"component size exceeds manifest: {member.name}")
        digest.update(chunk)
    return total, digest.hexdigest()


def validate_full_package_artifact(
    artifact_path: str | os.PathLike[str],
    *,
    expected_manifest: Optional[Mapping[str, Any]] = None,
) -> SerializableDict:
    """Reopen a TGZ and verify its flat payload, si.ini, manifest, and hashes."""

    path = Path(artifact_path)
    try:
        with tarfile.open(path, "r:gz") as archive:
            members_by_name: dict[str, tarfile.TarInfo] = {}
            seen_casefold: set[str] = set()
            for member in archive.getmembers():
                name = _normalise_member_name(member.name)
                if not name:
                    continue
                if "/" in name or not member.isfile():
                    raise PackageBuildError(
                        f"whole package must be flat and regular-file only: {member.name}"
                    )
                folded = name.casefold()
                if folded in seen_casefold:
                    raise PackageBuildError(f"duplicate package member: {name}")
                seen_casefold.add(folded)
                members_by_name[name] = member

            si_member = members_by_name.get(SI_INI_NAME)
            manifest_member = members_by_name.get(MANIFEST_NAME)
            if si_member is None or manifest_member is None:
                raise PackageBuildError(
                    "whole package is missing si.ini or package-manifest.json"
                )
            si_bytes = _read_tar_member_limited(
                archive,
                si_member,
                limit=MAX_SI_INI_BYTES,
                label=SI_INI_NAME,
            )
            manifest_bytes = _read_tar_member_limited(
                archive,
                manifest_member,
                limit=MAX_PACKAGE_MANIFEST_BYTES,
                label=MANIFEST_NAME,
            )
            try:
                manifest = json.loads(manifest_bytes.decode("utf-8"))
                si_ini = si_bytes.decode("utf-8")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PackageBuildError(f"invalid package metadata encoding: {exc}") from exc
            if not isinstance(manifest, Mapping):
                raise PackageBuildError("embedded package manifest must be an object")
            if expected_manifest is not None and canonical_json(manifest) != canonical_json(
                expected_manifest
            ):
                raise PackageBuildError(
                    "embedded package manifest differs from build manifest"
                )
            parsed = _parse_si_ini(si_ini)
            raw_components = manifest.get("components") or []
            if not isinstance(raw_components, list) or not all(
                isinstance(component, Mapping) for component in raw_components
            ):
                raise PackageBuildError("manifest components must be an array of objects")
            try:
                components = sorted(raw_components, key=_component_sort_key)
            except (KeyError, TypeError, ValueError) as exc:
                raise PackageBuildError(f"manifest component ordering is invalid: {exc}") from exc
            try:
                declared_count = int(parsed.get("FileNumInPacket", "-1"))
            except ValueError as exc:
                raise PackageBuildError("si.ini FileNumInPacket is invalid") from exc
            if declared_count != len(components):
                raise PackageBuildError(
                    f"si.ini declares {declared_count} files but manifest has {len(components)}"
                )

            expected_names = {SI_INI_NAME, MANIFEST_NAME}
            for index, component in enumerate(components, start=1):
                try:
                    name = _safe_flat_name(
                        component["output_name"],
                        f"manifest.components[{index - 1}].output_name",
                    )
                    expected_size = int(component["size"])
                    expected_digest = str(component["sha256"])
                except (CatalogValidationError, KeyError, TypeError, ValueError) as exc:
                    raise PackageBuildError(f"invalid manifest component: {exc}") from exc
                if expected_size <= 0:
                    raise PackageBuildError(f"component payload is empty: {name}")
                expected_names.add(name)
                if parsed.get(f"FileName_{index}") != name:
                    raise PackageBuildError(
                        f"si.ini FileName_{index} does not match manifest"
                    )
                if parsed.get(f"FileAttr_{index}") != str(component["file_attr"]):
                    raise PackageBuildError(
                        f"si.ini FileAttr_{index} does not match catalog"
                    )
                expected_version = str(component.get("version") or "V0.0.0.0")
                if not expected_version.startswith("V"):
                    expected_version = f"V{expected_version}"
                if parsed.get(f"FileVer_{index}") != expected_version:
                    raise PackageBuildError(
                        f"si.ini FileVer_{index} does not match manifest"
                    )
                payload_member = members_by_name.get(name)
                if payload_member is None:
                    raise PackageBuildError(f"component payload is missing: {name}")
                if payload_member.size != expected_size:
                    raise PackageBuildError(f"component size mismatch: {name}")
                actual_size, actual_digest = _stream_tar_member_hash(
                    archive,
                    payload_member,
                    expected_size=expected_size,
                )
                if actual_size != expected_size:
                    raise PackageBuildError(f"component size mismatch: {name}")
                if actual_digest != expected_digest:
                    raise PackageBuildError(f"component hash mismatch: {name}")

            if set(members_by_name) != expected_names:
                extras = sorted(set(members_by_name) - expected_names)
                missing = sorted(expected_names - set(members_by_name))
                raise PackageBuildError(
                    f"package members differ; missing={missing}, extra={extras}"
                )
            si_digest = hashlib.sha256(si_bytes).hexdigest()
            if si_digest != manifest.get("si_ini_sha256"):
                raise PackageBuildError("si.ini hash does not match manifest")
            return SerializableDict(
                {
                    "valid": True,
                    "members": sorted(members_by_name),
                    "component_count": len(components),
                    "manifest": manifest,
                    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    "si_ini": si_ini,
                    "si_ini_sha256": si_digest,
                }
            )
    except PackageBuildError:
        raise
    except (OSError, tarfile.TarError, ArchiveInspectionError) as exc:
        raise PackageBuildError(f"cannot reopen package {path.name}: {exc}") from exc


def build_full_package(
    confirmed_plan: Mapping[str, Any],
    *,
    workspace_dir: str | os.PathLike[str],
    catalog: Any = None,
    output_dir: Optional[str | os.PathLike[str]] = None,
    max_extract_bytes: Optional[int] = None,
    max_archive_members: Optional[int] = None,
) -> BuildResult:
    """Build and reopen-validate a flat TGZ from a valid confirmed plan only."""

    loaded = load_catalog(catalog)
    plan = validate_confirmed_plan(confirmed_plan, loaded, verify_files=True)
    project = loaded.projects_by_code[plan["project_code"]]
    components_by_key = {
        component["component_key"]: component for component in project["components"]
    }
    workspace = Path(workspace_dir).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    destination = Path(output_dir).expanduser().resolve() if output_dir else workspace / "package_build"
    destination.mkdir(parents=True, exist_ok=True)
    extract_limit = int(
        max_extract_bytes
        or loaded["limits"].get("max_extract_bytes")
        or getattr(settings, "ai_analysis_max_extract_bytes", DEFAULT_MAX_EXTRACT_BYTES)
    )
    member_limit = int(
        max_archive_members or loaded["limits"].get("max_archive_members") or DEFAULT_MAX_ARCHIVE_MEMBERS
    )
    scratch = Path(tempfile.mkdtemp(prefix=".full-package-build-", dir=str(workspace)))
    stage = scratch / "flat"
    extracts = scratch / "extracts"
    stage.mkdir()
    extracts.mkdir()
    extraction_cache: dict[str, Path] = {}
    used_names = {SI_INI_NAME.casefold(), MANIFEST_NAME.casefold()}
    built_components: list[dict[str, Any]] = []
    partial_artifact: Optional[Path] = None

    try:
        for input_item in plan["inputs"]:
            if not input_item.get("include"):
                continue
            source = Path(input_item["path"])
            is_prebuilt = _input_is_prebuilt(input_item, project, loaded["limits"])
            selected = input_item.get("selected_components")
            if selected is None:
                selected = input_item.get("selected_component") or []
            for component_key in selected:
                component = components_by_key[component_key]
                materialization = component["materialization"]
                kind = materialization["type"]
                source_payload = source
                prebuilt_component = (
                    _validated_prebuilt_component(input_item, project, component, loaded["limits"])
                    if is_prebuilt else None
                )
                if kind == "extract_match":
                    extracted = extraction_cache.get(input_item["sha256"])
                    if extracted is None:
                        extracted = extracts / input_item["sha256"]
                        _safe_extract_archive(
                            source,
                            extracted,
                            max_members=member_limit,
                            max_bytes=extract_limit,
                        )
                        extraction_cache[input_item["sha256"]] = extracted
                    source_payload = _materialise_extract_match(
                        extracted,
                        materialization,
                        source_name=input_item["original_name"],
                        component_key=component_key,
                    )
                elif kind == "direct_include":
                    # Validate archive structure even though its bytes are copied
                    # verbatim; a traversal-bearing component archive must not be
                    # smuggled into a trusted whole package.
                    if _archive_kind(source):
                        inspect_archive(
                            source,
                            max_members=member_limit,
                            max_uncompressed_bytes=extract_limit,
                            strict=True,
                        )
                elif kind != "copy":  # defensive: catalog validation already rejects this
                    raise PackageBuildError(f"unsupported materialization type: {kind}")

                if prebuilt_component and (
                    source_payload.stat().st_size != prebuilt_component["size"]
                    or sha256_file(source_payload) != prebuilt_component["sha256"]
                ):
                    raise PackageBuildError("extracted prebuilt payload differs from validated source")

                output_name = _collision_safe_name(
                    str(component["output_name"]), component_key, used_names
                )
                output_path = stage / output_name
                shutil.copyfile(source_payload, output_path)
                if output_path.stat().st_size <= 0:
                    raise PackageBuildError(
                        f"component {component_key} produced an empty payload"
                    )
                version = str(
                    (input_item.get("component_versions") or {}).get(component_key)
                    or component["default_version"]
                )
                built_components.append(
                    {
                        "component_key": component_key,
                        "label": component["label"],
                        "file_attr": int(component["file_attr"]),
                        "version": version,
                        "output_name": output_name,
                        "size": output_path.stat().st_size,
                        "sha256": sha256_file(output_path),
                        "materialization": kind,
                        "source_upload_id": input_item["upload_id"],
                        "source_name": input_item["original_name"],
                        "source_sha256": input_item["sha256"],
                    }
                )
        built_components.sort(key=_component_sort_key)
        si_ini = generate_si_ini(project, plan, built_components)
        si_path = stage / SI_INI_NAME
        si_path.write_text(si_ini, encoding="utf-8", newline="\n")

        manifest = {
            "schema_version": "1.0",
            "project_code": plan["project_code"],
            "package_version": plan["version"],
            "mode": plan["mode"],
            "is_patch": bool(plan["is_patch"]),
            "packet_attr": int(plan["packet_attr"]),
            "publisher": project["publisher"],
            "catalog_digest": plan["catalog_digest"],
            "plan_hash": plan["plan_hash"],
            "confirmation_hash": plan["confirmation_hash"],
            "inputs": [
                {
                    "upload_id": item["upload_id"],
                    "original_name": item["original_name"],
                    "size": item["size"],
                    "sha256": item["sha256"],
                    "included": bool(item["include"]),
                    "components": list(
                        item.get("selected_components")
                        if item.get("selected_components") is not None
                        else item.get("selected_component") or []
                    ),
                }
                for item in plan["inputs"]
            ],
            "components": built_components,
            "si_ini_sha256": sha256_file(si_path),
        }
        manifest_path = stage / MANIFEST_NAME
        # Canonical bytes make repeat builds of one confirmed plan identical.
        manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8", newline="\n")

        filename = package_filename(project, plan)
        fd, partial_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".partial", dir=str(destination))
        os.close(fd)
        partial_artifact = Path(partial_name)
        _create_deterministic_tgz(
            stage,
            partial_artifact,
            [SI_INI_NAME, MANIFEST_NAME]
            + [component["output_name"] for component in built_components],
        )
        validation = validate_full_package_artifact(
            partial_artifact, expected_manifest=manifest
        )
        artifact_hash = sha256_file(partial_artifact)
        artifact_size = partial_artifact.stat().st_size
        final_path = destination / filename
        if final_path.exists():
            if sha256_file(final_path) != artifact_hash:
                raise PackageBuildError(
                    f"collision: existing artifact {filename} has different bytes"
                )
            partial_artifact.unlink()
        else:
            os.replace(partial_artifact, final_path)
        partial_artifact = None

        result = BuildResult(
            {
                "status": "built",
                "artifact_path": str(final_path),
                "artifact_name": filename,
                "size": artifact_size,
                "sha256": artifact_hash,
                "project_code": plan["project_code"],
                "version": plan["version"],
                "mode": plan["mode"],
                "is_patch": bool(plan["is_patch"]),
                "catalog_digest": plan["catalog_digest"],
                "plan_hash": plan["plan_hash"],
                "confirmation_hash": plan["confirmation_hash"],
                "manifest": manifest,
                "manifest_sha256": validation["manifest_sha256"],
                "si_ini": si_ini,
                "si_ini_sha256": validation["si_ini_sha256"],
                "components": built_components,
                "validation": validation.to_dict(),
            }
        )
        _atomic_write_json(destination / f"{filename}.build-result.json", result)
        return result
    except ArchiveInspectionError as exc:
        raise PackageBuildError(str(exc)) from exc
    finally:
        if partial_artifact is not None:
            try:
                partial_artifact.unlink()
            except OSError:
                pass
        shutil.rmtree(scratch, ignore_errors=True)


build_package = build_full_package
validate_package_artifact = validate_full_package_artifact


__all__ = [
    "ArchiveInspectionError",
    "BuildResult",
    "CatalogValidationError",
    "ClassificationDraft",
    "ConfirmedPackagePlan",
    "DEFAULT_CATALOG_PATH",
    "FullPackageError",
    "MANIFEST_NAME",
    "PackageBuildError",
    "PackageCatalog",
    "PlanValidationError",
    "SI_INI_NAME",
    "SerializableDict",
    "build_confirmation_questions",
    "build_full_package",
    "build_package",
    "canonical_hash",
    "canonical_json",
    "classify_inputs",
    "compute_confirmed_plan_hash",
    "compute_draft_plan_hash",
    "confirm_plan",
    "create_confirmed_plan",
    "create_draft_plan",
    "generate_si_ini",
    "infer_version",
    "infer_versions",
    "inspect_archive",
    "list_archive_members",
    "load_catalog",
    "load_package_catalog",
    "normalise_inputs",
    "normalize_inputs",
    "package_filename",
    "sha256_file",
    "validate_catalog",
    "validate_confirmed_plan",
    "validate_full_package_artifact",
    "validate_package_artifact",
    "validate_package_catalog",
]
