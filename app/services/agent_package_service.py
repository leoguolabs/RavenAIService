"""Validate Agent-authored packages without interpreting project business rules.

The Agent owns discovery, component mapping and byte construction. This boundary
only checks the review plan against uploaded bytes and the actual wire manifest.
There is deliberately no project catalog, version inference or ID lookup here.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path
from typing import Any, BinaryIO

from app.config import settings
from app.services.full_package_service import canonical_json, sha256_file


class AgentPackageError(ValueError):
    pass


def workspace_file(workspace: Path, value: Any) -> Path:
    raw = Path(str(value or ""))
    candidate = raw if raw.is_absolute() else workspace / raw
    # Reject links before resolving as well as links in ancestor directories.
    if any(p.is_symlink() for p in (candidate, *candidate.parents)):
        raise AgentPackageError("Package paths must not contain symlinks")
    path = candidate.resolve()
    if workspace.resolve() not in path.parents or not path.is_file():
        raise AgentPackageError("Package file must exist inside this workspace")
    return path


def _text(value: Any, field: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AgentPackageError(f"Missing or invalid {field}")
    value = value.strip()
    if any(ord(c) < 32 for c in value) or (field not in {"evidence", "exclusion reason"} and any(c in value for c in "=;\x7f")):
        raise AgentPackageError(f"Unsafe characters in {field}")
    return value


def _name(value: Any) -> str:
    name = _text(value, "output_name")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise AgentPackageError("Package members must have flat, safe names")
    return name


def _number(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 0xFFFFFFFF:
        raise AgentPackageError(f"{field} must be a positive 32-bit integer")
    return value


def _evidence(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 30:
        raise AgentPackageError("Cite source/manifest/code evidence for every decision")
    return [_text(item, "evidence", limit=4000) for item in value]


def _hash_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def validate_agent_package(
    workspace: str | Path,
    plan_path: str,
    artifact_path: str,
    *,
    inputs: list[dict[str, Any]],
    projects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a normalized immutable review snapshot, hashing without extraction."""
    root = Path(workspace).resolve()
    plan_file = workspace_file(root, plan_path)
    if plan_file.stat().st_size > 1024 * 1024:
        raise AgentPackageError("Review plan exceeds 1 MiB")
    raw = json.loads(plan_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AgentPackageError("Review plan must be an object")
    code = _text(raw.get("project_code"), "project_code")
    project = next((p for p in projects if p.get("project_code") == code), None)
    if project is None:
        raise AgentPackageError("Select an enabled Configuration Manager project from task.json")
    artifact = workspace_file(root, artifact_path)
    if not artifact.name.endswith((".tgz", ".tar.gz")):
        raise AgentPackageError("Artifact must be a TGZ archive")
    if not 0 < artifact.stat().st_size <= int(settings.upload_max_size_mb) * 1024 * 1024:
        raise AgentPackageError("Artifact is empty or exceeds the upload limit")
    plan = {
        "schema_version": 1,
        "execution": "agent",
        "project_code": code,
        "project_repo_id": project.get("id"),
        "project_name": project.get("project_name") or code,
        "version": _text(raw.get("version"), "version"),
        "mode": _text(raw.get("mode"), "mode"),
        "packet_attr": _number(raw.get("packet_attr"), "PacketAttr"),
        "publisher": _text(raw.get("publisher"), "Publisher"),
        "evidence": _evidence(raw.get("evidence")),
        "artifact_path": str(artifact),
        "artifact_sha256": sha256_file(artifact),
        "artifact_size": artifact.stat().st_size,
    }
    if plan["mode"] not in {"full", "patch"}:
        raise AgentPackageError("mode must be full or patch")
    manifest = {str(item["upload_id"]): item for item in inputs}
    if not manifest or len(manifest) != len(inputs):
        raise AgentPackageError("Invalid staged input manifest")
    components = raw.get("components")
    if not isinstance(components, list) or not 1 <= len(components) <= 256:
        raise AgentPackageError("Provide 1..256 components")
    normalized = []
    used: set[str] = set()
    names: set[str] = {"si.ini"}
    attrs: set[int] = set()
    for item in components:
        if not isinstance(item, dict):
            raise AgentPackageError("Invalid component")
        upload_id = str(item.get("upload_id") or "")
        if upload_id not in manifest:
            raise AgentPackageError("Component references an unknown upload_id")
        name = _name(item.get("output_name"))
        attr = _number(item.get("file_attr"), "FileAttr")
        if name in names or attr in attrs:
            raise AgentPackageError("Duplicate output name or FileAttr")
        names.add(name)
        attrs.add(attr)
        used.add(upload_id)
        digest = str(item.get("sha256") or "").lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise AgentPackageError("Each payload needs its SHA-256")
        normalized.append({
            "upload_id": upload_id,
            "component_key": _text(item.get("component_key"), "component_key"),
            "output_name": name,
            "file_attr": attr,
            "version": _text(item.get("version"), "component version"),
            "sha256": digest,
            "evidence": _evidence(item.get("evidence")),
        })
    excluded = raw.get("excluded_inputs", [])
    if not isinstance(excluded, list):
        raise AgentPackageError("excluded_inputs must be a list")
    exclusions = {}
    for item in excluded:
        if not isinstance(item, dict):
            raise AgentPackageError("Invalid exclusion")
        uid = str(item.get("upload_id") or "")
        if uid not in manifest or uid in used or uid in exclusions:
            raise AgentPackageError("Excluded input is unknown, included or duplicated")
        exclusions[uid] = _text(item.get("reason"), "exclusion reason", limit=2000)
    if used | exclusions.keys() != manifest.keys():
        raise AgentPackageError("Account for every uploaded file, including exclusions")
    plan["inputs"] = []
    for uid, item in manifest.items():
        source = workspace_file(root, item["path"])
        if source.stat().st_size != item["size"] or sha256_file(source) != item["sha256"]:
            raise AgentPackageError("Uploaded bytes changed since staging")
        plan["inputs"].append({
            "upload_id": uid, "original_name": item["original_name"],
            "path": str(source), "size": item["size"], "sha256": item["sha256"],
            "include": uid in used, "reason": exclusions.get(uid),
            "components": [c["component_key"] for c in normalized if c["upload_id"] == uid],
        })
    plan["components"] = normalized
    _validate_archive(artifact, plan)
    if sha256_file(artifact) != plan["artifact_sha256"]:
        raise AgentPackageError("Artifact changed during validation")
    plan["plan_hash"] = hashlib.sha256(canonical_json(plan).encode()).hexdigest()
    return plan


def _validate_archive(path: Path, plan: dict[str, Any]) -> None:
    components = {c["output_name"]: c for c in plan["components"]}
    expected = {"si.ini", *components}
    found: set[str] = set()
    total = 0
    ini = None
    # Streaming traversal avoids unbounded getmembers() allocation.
    with tarfile.open(path, "r|gz") as archive:
        for member in archive:
            if len(found) >= len(expected) or not member.isfile() or member.issparse():
                raise AgentPackageError("Unexpected, linked or non-regular archive member")
            name = _name(member.name)
            if name not in expected or name in found or member.size <= 0:
                raise AgentPackageError(f"Unexpected/duplicate/empty payload: {name}")
            found.add(name)
            total += member.size
            if total > 2 * 1024**3:
                raise AgentPackageError("Expanded package exceeds 2 GiB")
            stream = archive.extractfile(member)
            if stream is None:
                raise AgentPackageError("Unreadable archive member")
            if name == "si.ini":
                if member.size > 1024 * 1024:
                    raise AgentPackageError("si.ini exceeds 1 MiB")
                ini = {}
                for line in stream.read().decode("utf-8-sig").splitlines():
                    line = line.strip()
                    if not line or line.startswith(("#", ";")):
                        continue
                    if "=" not in line:
                        raise AgentPackageError("Invalid si.ini line")
                    key, value = line.split("=", 1)
                    key, value = key.strip(), value.strip().removesuffix(";")
                    if key in ini:
                        raise AgentPackageError(f"Duplicate si.ini key: {key}")
                    ini[key] = value
            elif _hash_stream(stream) != components[name]["sha256"]:
                raise AgentPackageError(f"Payload SHA-256 mismatch: {name}")
    if found != expected or ini is None:
        raise AgentPackageError("Archive is missing declared payloads or si.ini")
    values = {
        "Packet_Ver": "V" + plan["version"].lstrip("Vv"),
        "PacketAttr": str(plan["packet_attr"]), "Publisher": plan["publisher"],
        "FileNumInPacket": str(len(components)),
    }
    declared_names = set()
    for index in range(1, len(components) + 1):
        name = ini.get(f"FileName_{index}")
        if name not in components or name in declared_names:
            raise AgentPackageError("si.ini payload mapping differs from review plan")
        declared_names.add(name)
        component = components[name]
        values[f"FileAttr_{index}"] = str(component["file_attr"])
        values[f"FileVer_{index}"] = "V" + component["version"].lstrip("Vv")
    for key, value in values.items():
        if ini.get(key) != value:
            raise AgentPackageError(f"si.ini {key} differs from review plan")


def review_questions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Present the whole validated plan for one publication decision."""
    overview = (f"{plan['project_name']} ({plan['project_code']}) | {plan['version']} | "
                f"{plan['mode']} | PacketAttr={plan['packet_attr']} | Publisher={plan['publisher']}")
    sections = ["\n".join([overview, *plan["evidence"]])]
    for item in plan["inputs"]:
        mapped = [c for c in plan["components"] if c["upload_id"] == item["upload_id"]]
        detail = "\n".join(
            f"{c['component_key']} → {c['output_name']}, FileAttr={c['file_attr']}, "
            f"version={c['version']}\n" + "\n".join(c["evidence"]) for c in mapped
        ) if mapped else "排除 / Exclude: " + item["reason"]
        sections.append(f"{item['original_name']}\n{detail}")
    return [{
        "question": "\n\n".join(sections),
        "header": "发布确认", "multiSelect": False, "allow_custom": False,
        "options": [
            {"label": "确认 / Confirm", "description": "按上述 Agent 方案发布 / Publish this Agent plan"},
            {"label": "取消 / Cancel", "description": "不发布 / Do not publish"},
        ],
    }]
