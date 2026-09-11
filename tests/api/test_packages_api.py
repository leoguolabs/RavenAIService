"""Integration tests for the project-dimension package management API.

Covers the BREAKING surface from the package-project-association spec:
``projectCode`` filtering (incl. the deprecated ``type`` alias and the
``__unassociated__`` special value), upload project validation with no
residual files on failure, scan registering orphans as unassociated,
project-scoped download, the new stats shape, and the Prometheus
``raven_package_activity_total`` label set.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import packages as packages_api
from app.models.database import get_db
from app.services.raven_package_service import raven_package_service


class _FakeRepo:
    def __init__(self, project_code: str, enabled: bool = True) -> None:
        self.id = 1
        self.project_code = project_code
        self.project_name = project_code.upper()
        self.enabled = enabled
        # 包检索要求项目关联了代码仓库（repo_url 非空）。
        self.repo_url = f"https://git.example/{project_code}.git"


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(packages_api.router)
    application.dependency_overrides[get_db] = lambda: None
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the shared service singleton at an isolated tmp data dir."""
    monkeypatch.setattr(raven_package_service, "data_dir", tmp_path / "raven")
    monkeypatch.setattr(raven_package_service, "uploads_dir", tmp_path / "raven" / "uploads")
    monkeypatch.setattr(
        raven_package_service, "metadata_file", tmp_path / "raven" / "package-metadata.json"
    )
    return raven_package_service


@pytest.fixture
def registry(monkeypatch):
    """Fake project_repo registry: only ``demo-proj`` exists and is enabled."""

    async def fake_get_by_project_code(
        db: Any, code: str, *, require_repo: bool = False
    ) -> Optional[_FakeRepo]:
        if str(code).strip().lower() == "demo-proj":
            return _FakeRepo("demo-proj")
        return None

    async def fake_supports_agent(_db: Any, repo: Optional[_FakeRepo], agent_key: str) -> bool:
        return repo is not None and repo.enabled and agent_key == "package_search"

    from app.services import project_repo_service

    monkeypatch.setattr(project_repo_service, "get_by_project_code", fake_get_by_project_code)
    monkeypatch.setattr(project_repo_service, "supports_agent", fake_supports_agent)
    return fake_get_by_project_code


def _seed(store, tmp_path, entries):
    """Seed package metadata with real (dummy) files so pruning keeps them."""
    files_dir = tmp_path / "raven" / "uploads"
    files_dir.mkdir(parents=True, exist_ok=True)
    packages = []
    for pid, name, project in entries:
        file_path = files_dir / name
        file_path.write_bytes(b"dummy-bytes")
        packages.append({
            "id": pid,
            "name": name,
            "version": "1.0.0",
            "projectCode": project,
            "path": str(file_path),
            "size": 11,
            "createdAt": "2025-01-01T00:00:00Z",
            "metadata": {"isPatch": False, "components": [], "tags": [],
                         "description": "", "sha256": "deadbeef", "customFields": {}},
        })
    store.save_packages(packages)


def test_project_validation_allows_repoless_configuration_manager(monkeypatch):
    """Package ownership is catalog-bound and must not require a Git URL."""
    from app.services import project_repo_service
    from app.services.raven_package_service import validate_project_code

    repo = _FakeRepo("config-only")
    repo.repo_url = ""
    seen: dict[str, bool] = {}

    async def fake_get_by_project_code(_db, code, *, require_repo=False):
        seen["require_repo"] = require_repo
        return repo if code == "config-only" else None

    async def fake_supports_agent(_db, candidate, agent_key):
        return candidate is repo and agent_key == "package_search"

    monkeypatch.setattr(
        project_repo_service,
        "get_by_project_code",
        fake_get_by_project_code,
    )
    monkeypatch.setattr(
        project_repo_service,
        "supports_agent",
        fake_supports_agent,
    )

    assert asyncio.run(validate_project_code(None, "config-only")) is repo
    assert seen == {"require_repo": False}


# ────────────────────── GET /packages filtering ──────────────────────


def test_list_packages_filters_by_project_code(client, isolated_store, tmp_path):
    _seed(isolated_store, tmp_path, [
        ("a", "a.tgz", "demo-proj"),
        ("b", "b.tgz", "other-proj"),
        ("c", "c.tgz", ""),
    ])
    body = client.get("/packages", params={"projectCode": "demo-proj"}).json()
    items = body["data"]["packages"]
    assert [p["id"] for p in items] == ["a"]


def test_list_packages_legacy_type_alias(client, isolated_store, tmp_path):
    _seed(isolated_store, tmp_path, [
        ("a", "a.tgz", "lingxi-10"),
        ("b", "b.tgz", "ka-tx"),
    ])
    body = client.get("/packages", params={"type": "lingxi-10"}).json()
    items = body["data"]["packages"]
    assert [p["id"] for p in items] == ["a"]


def test_list_packages_unassociated_filter(client, isolated_store, tmp_path):
    _seed(isolated_store, tmp_path, [
        ("a", "a.tgz", "demo-proj"),
        ("b", "b.tgz", ""),
    ])
    body = client.get("/packages", params={"projectCode": "__unassociated__"}).json()
    items = body["data"]["packages"]
    assert [p["id"] for p in items] == ["b"]


# ────────────────────── stats overview ──────────────────────


def test_stats_overview_returns_packages_by_project(client, isolated_store, tmp_path):
    _seed(isolated_store, tmp_path, [
        ("a", "a.tgz", "demo-proj"),
        ("b", "b.tgz", "demo-proj"),
        ("c", "c.tgz", ""),
    ])
    data = client.get("/packages/stats/overview").json()["data"]
    assert data["packagesByProject"] == {"demo-proj": 2, "unassociated": 1}
    assert "packagesByType" not in data


# ────────────────────── upload validation ──────────────────────


def _upload(client, project_code: Optional[str]):
    data = {} if project_code is None else {"projectCode": project_code}
    return client.post(
        "/upload",
        files={"file": ("demo-1.0.0.tgz", b"dummy-tgz-bytes", "application/gzip")},
        data=data,
    )


def test_upload_requires_project_code(client, isolated_store, registry):
    resp = _upload(client, None)
    assert resp.status_code == 400
    assert not list(isolated_store.uploads_dir.glob("*")) if isolated_store.uploads_dir.exists() else True


def test_upload_rejects_unknown_project_and_leaves_no_file(client, isolated_store, registry):
    resp = _upload(client, "ghost-proj")
    assert resp.status_code == 400
    uploads = isolated_store.uploads_dir
    assert not uploads.exists() or not list(uploads.glob("*"))


def test_upload_accepts_registered_project(client, isolated_store, registry):
    resp = _upload(client, "demo-proj")
    assert resp.status_code == 200
    saved = resp.json()["package"]
    assert saved["projectCode"] == "demo-proj"
    assert "packageType" not in saved
    stored = isolated_store.load_packages()
    assert stored and stored[0]["projectCode"] == "demo-proj"


def _upkg_bytes():
    """A real tar-based upgrade container; upload must preserve every byte."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in (("manifest.json", b'{"name":"netconf-server","mode":"full"}'),
                              ("payload.tar.gz", b"opaque signed payload bytes")):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


@pytest.mark.parametrize("filename", ["netconf-server-v1.0.28-linux-arm64.upkg", "routing-mgmtd.UPKG"])
def test_upkg_upload_metadata_and_download_roundtrip(client, isolated_store, registry, filename):
    payload = _upkg_bytes()
    note = "# netconf-server\n\n### 功能更新\n- 支持完整升级包上传\n"
    response = client.post("/upload", files={"file": (filename, payload, "application/octet-stream")},
                           data={"projectCode": "demo-proj", "version": "v1.0.28",
                                 "packageInfo": json.dumps({"metadata": {"description": note}})})
    assert response.status_code == 200, response.text
    package = response.json()["package"]
    assert package["name"] == filename
    assert package["version"] == "v1.0.28"
    assert package["metadata"]["description"] == note
    assert package["size"] == len(payload)
    assert package["metadata"]["sha256"] == hashlib.sha256(payload).hexdigest()
    download = client.get(f'/download/{package["id"]}')
    assert download.status_code == 200, download.text
    assert download.content == payload


def test_upkg_batch_and_scan_are_supported(client, isolated_store, registry):
    payload = _upkg_bytes()
    response = client.post("/upload/batch", data={"projectCode": "demo-proj"},
                           files=[("file", ("radas.upkg", payload, "application/octet-stream"))])
    assert response.status_code == 200, response.text
    assert not response.json().get("errors")
    assert response.json()["packages"][0]["metadata"]["sha256"] == hashlib.sha256(payload).hexdigest()
    (isolated_store.uploads_dir / "orphan.upkg").write_bytes(payload)
    assert isolated_store.scan_uploads_directory() == 1
    assert isolated_store.scan_uploads_directory() == 0


def test_upload_still_rejects_unsupported_extension(client, isolated_store, registry):
    response = client.post("/upload", data={"projectCode": "demo-proj"},
                           files={"file": ("upgrade.exe", b"unsupported", "application/octet-stream")})
    assert response.status_code == 400
    assert not isolated_store.uploads_dir.exists() or not list(isolated_store.uploads_dir.iterdir())


def test_upload_batch_requires_valid_project(client, isolated_store, registry):
    resp = client.post(
        "/upload/batch",
        files=[("file", ("x-1.0.0.tgz", b"dummy", "application/gzip"))],
        data={"projectCode": "ghost-proj"},
    )
    assert resp.status_code == 400
    uploads = isolated_store.uploads_dir
    assert not uploads.exists() or not list(uploads.glob("*"))


# ────────────────────── scan ──────────────────────


def test_scan_registers_orphans_as_unassociated(client, isolated_store):
    isolated_store.uploads_dir.mkdir(parents=True, exist_ok=True)
    (isolated_store.uploads_dir / "orphan-2.0.0.tgz").write_bytes(b"dummy")
    resp = client.post("/packages/scan")
    assert resp.status_code == 200
    assert resp.json()["data"]["added"] == 1
    pkg = isolated_store.load_packages()[0]
    assert pkg["projectCode"] == ""


# ────────────────────── download by project ──────────────────────


def test_download_by_project_single_file(client, isolated_store, tmp_path):
    _seed(isolated_store, tmp_path, [
        ("a", "a.tgz", "demo-proj"),
        ("b", "b.tgz", "other-proj"),
    ])
    resp = client.get("/download/project/demo-proj")
    assert resp.status_code == 200
    assert resp.content == b"dummy-bytes"


def test_download_by_project_multiple_returns_zip(client, isolated_store, tmp_path):
    _seed(isolated_store, tmp_path, [
        ("a", "a.tgz", "demo-proj"),
        ("b", "b.tgz", "demo-proj"),
    ])
    resp = client.get("/download/project/demo-proj")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"


def test_download_by_project_unknown_is_404(client, isolated_store, tmp_path):
    _seed(isolated_store, tmp_path, [("a", "a.tgz", "demo-proj")])
    assert client.get("/download/project/ghost").status_code == 404


def test_download_by_type_route_removed(client, isolated_store, tmp_path):
    _seed(isolated_store, tmp_path, [("a", "a.tgz", "demo-proj")])
    assert client.get("/download/type/demo-proj").status_code == 404


# ────────────────────── Prometheus labels ──────────────────────


def test_package_activity_metric_has_no_project_label():
    from app.utils import metrics as prom

    labelnames = getattr(prom.raven_package_activity_total, "_labelnames", None)
    if labelnames is not None:  # real prometheus_client Counter
        assert tuple(labelnames) == ("action", "status")
    # the helper accepts exactly action+status and never raises
    prom.record_package_activity(action="upload", status="success")


@pytest.mark.asyncio
async def test_package_activity_event_metadata_uses_project_code(monkeypatch):
    from app.services import metrics_service
    from app.utils import metrics as prom

    recorded_events: list[dict[str, Any]] = []
    prometheus_calls: list[dict[str, Any]] = []

    async def fake_record_business_event(**kwargs: Any) -> dict[str, Any]:
        recorded_events.append(kwargs)
        return {"duplicate": False}

    def fake_record_package_activity(**kwargs: Any) -> None:
        prometheus_calls.append(kwargs)

    monkeypatch.setattr(metrics_service, "record_business_event", fake_record_business_event)
    monkeypatch.setattr(prom, "record_package_activity", fake_record_package_activity)

    await packages_api._record_package_activity(
        action="upload", project_code="demo-proj", count=2
    )

    assert recorded_events == [
        {
            "event_type": "package_activity",
            "source": "package_upload",
            "idempotency_key": recorded_events[0]["idempotency_key"],
            "status": "success",
            "metadata": {"project_code": "demo-proj", "result_count": 2},
        }
    ]
    assert "package_type" not in recorded_events[0]["metadata"]
    assert recorded_events[0]["idempotency_key"].startswith("package_activity:upload:")
    assert prometheus_calls == [
        {"action": "upload", "status": "success"},
        {"action": "upload", "status": "success"},
    ]


@pytest.mark.asyncio
async def test_package_activity_event_metadata_uses_unassociated_for_empty_project(monkeypatch):
    from app.services import metrics_service
    from app.utils import metrics as prom

    recorded_events: list[dict[str, Any]] = []

    async def fake_record_business_event(**kwargs: Any) -> dict[str, Any]:
        recorded_events.append(kwargs)
        return {"duplicate": False}

    monkeypatch.setattr(metrics_service, "record_business_event", fake_record_business_event)
    monkeypatch.setattr(prom, "record_package_activity", lambda **_kwargs: None)

    await packages_api._record_package_activity(action="download_batch", project_code="")

    assert recorded_events[0]["metadata"] == {
        "project_code": "unassociated",
        "result_count": 1,
    }
    assert "package_type" not in recorded_events[0]["metadata"]
