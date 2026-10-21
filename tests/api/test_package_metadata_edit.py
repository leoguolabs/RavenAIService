"""Tests for project-member / admin Raven package metadata editing.

Covers the ``raven-package-metadata-editing`` capability:

* service-level normalization, field preservation, and persistence;
* ``PATCH /packages/{id}/metadata`` authorization (anonymous / member /
  non-member / disabled / unknown / unassociated / admin / missing);
* request validation (empty body, bad tags, trimming + dedup, clearing);
* that edited description/tags participate in list filtering and search.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import packages as packages_api
from app.api.users import get_current_user, get_optional_user
from app.models.database import get_db
from app.services import project_repo_member_service, project_repo_service
from app.services.raven_package_service import (
    MetadataValidationError,
    PACKAGE_DESCRIPTION_MAX_LEN,
    PACKAGE_TAG_MAX_COUNT,
    PACKAGE_TAG_MAX_LEN,
    normalize_description,
    normalize_tags,
    raven_package_service,
)


# ─────────────────────────── fixtures ───────────────────────────


class _FakeUser:
    def __init__(self, user_id: str, role: str = "user") -> None:
        self.id = user_id
        self.role = role


class _FakeRepo:
    def __init__(self, repo_id: int, project_code: str, enabled: bool = True) -> None:
        self.id = repo_id
        self.project_code = project_code
        self.enabled = enabled


# Mutable holder so each test can set the "current user" the endpoint sees.
class _AuthState:
    user: Optional[_FakeUser] = None


@pytest.fixture
def auth() -> _AuthState:
    state = _AuthState()
    state.user = None
    return state


@pytest.fixture
def app(auth: _AuthState) -> FastAPI:
    application = FastAPI()
    application.include_router(packages_api.router)
    application.dependency_overrides[get_db] = lambda: None
    application.dependency_overrides[get_optional_user] = lambda: auth.user
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(raven_package_service, "data_dir", tmp_path / "raven")
    monkeypatch.setattr(raven_package_service, "uploads_dir", tmp_path / "raven" / "uploads")
    monkeypatch.setattr(
        raven_package_service, "metadata_file", tmp_path / "raven" / "package-metadata.json"
    )
    return raven_package_service


@pytest.fixture
def registry(monkeypatch):
    """Configurable project registry + membership.

    ``alpha`` (enabled, repo 1) and ``beta`` (enabled, repo 2) exist;
    ``gamma`` is disabled. Membership starts empty; tests append tuples.
    """
    repos = {
        "alpha": _FakeRepo(1, "alpha", enabled=True),
        "beta": _FakeRepo(2, "beta", enabled=True),
        "gamma": _FakeRepo(3, "gamma", enabled=False),
    }
    members: set[tuple[int, str]] = set()

    async def fake_get_by_project_code(
        db: Any, code: str, *, require_repo: bool = False
    ) -> Optional[_FakeRepo]:
        repo = repos.get(str(code).strip().lower())
        # Mirror production behavior: disabled projects resolve to None.
        if repo is None or not repo.enabled:
            return None
        return repo

    async def fake_is_member(db: Any, project_repo_id: int, user_id: str) -> bool:
        return (project_repo_id, str(user_id)) in members

    monkeypatch.setattr(
        project_repo_service, "get_by_project_code", fake_get_by_project_code
    )
    monkeypatch.setattr(project_repo_member_service, "is_member", fake_is_member)
    return members


def _seed(store, tmp_path, entries):
    files_dir = tmp_path / "raven" / "uploads"
    files_dir.mkdir(parents=True, exist_ok=True)
    packages = []
    for pid, name, project in entries:
        file_path = files_dir / name
        file_path.write_bytes(b"dummy-bytes")
        packages.append(
            {
                "id": pid,
                "name": name,
                "version": "1.0.0",
                "projectCode": project,
                "path": str(file_path),
                "size": 11,
                "createdAt": "2025-01-01T00:00:00Z",
                "metadata": {
                    "isPatch": False,
                    "components": [{"name": "modem", "version": "1.0.0"}],
                    "tags": ["orig"],
                    "description": "original",
                    "sha256": "deadbeef",
                    "customFields": {},
                },
            }
        )
    store.save_packages(packages)


# ─────────────────── 5.1 service normalization ───────────────────


def test_normalize_description_trims_and_clears():
    assert normalize_description("  hello  ") == "hello"
    assert normalize_description(None) == ""
    assert normalize_description("") == ""


def test_normalize_description_rejects_non_string_and_too_long():
    with pytest.raises(MetadataValidationError):
        normalize_description(123)
    with pytest.raises(MetadataValidationError):
        normalize_description("x" * (PACKAGE_DESCRIPTION_MAX_LEN + 1))


def test_normalize_tags_trims_filters_dedups_in_order():
    assert normalize_tags([" stable ", "ka", "stable", ""]) == ["stable", "ka"]


def test_normalize_tags_rejects_non_list_and_non_string_items():
    with pytest.raises(MetadataValidationError):
        normalize_tags("stable,ka")
    with pytest.raises(MetadataValidationError):
        normalize_tags(["ok", 5])


def test_normalize_tags_enforces_limits():
    with pytest.raises(MetadataValidationError):
        normalize_tags(["x" * (PACKAGE_TAG_MAX_LEN + 1)])
    with pytest.raises(MetadataValidationError):
        normalize_tags([f"tag{i}" for i in range(PACKAGE_TAG_MAX_COUNT + 1)])


def test_update_package_metadata_preserves_other_fields(isolated_store, tmp_path):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    before = isolated_store.get_package("pkg-1")

    saved = isolated_store.update_package_metadata(
        "pkg-1", description="Release notes", tags=["ka", "stable"]
    )
    assert saved is not None
    assert saved["metadata"]["description"] == "Release notes"
    assert saved["metadata"]["tags"] == ["ka", "stable"]
    # Non-editable fields preserved.
    assert saved["path"] == before["path"]
    assert saved["size"] == before["size"]
    assert saved["version"] == before["version"]
    assert saved["projectCode"] == before["projectCode"]
    assert saved["metadata"]["sha256"] == before["metadata"]["sha256"]
    assert saved["metadata"]["isPatch"] == before["metadata"]["isPatch"]
    assert saved["metadata"]["components"] == before["metadata"]["components"]
    assert saved["createdAt"] == before["createdAt"]


def test_update_package_metadata_persists_and_partial(isolated_store, tmp_path):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    isolated_store.update_package_metadata("pkg-1", tags=["only-tags"])
    reloaded = isolated_store.get_package("pkg-1")
    # Tags changed, description left untouched.
    assert reloaded["metadata"]["tags"] == ["only-tags"]
    assert reloaded["metadata"]["description"] == "original"


def test_update_package_metadata_missing_returns_none(isolated_store, tmp_path):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    assert isolated_store.update_package_metadata("nope", description="x") is None


# ─────────────────── 5.2 API authorization ───────────────────


def _patch(client, pid, body):
    return client.patch(f"/packages/{pid}/metadata", json=body)


def test_anonymous_caller_gets_401(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = None
    resp = _patch(client, "pkg-1", {"description": "x"})
    assert resp.status_code == 401
    assert isolated_store.get_package("pkg-1")["metadata"]["description"] == "original"


def test_project_member_can_edit(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = _FakeUser("alice")
    registry.add((1, "alice"))  # member of repo 1 (alpha)
    resp = _patch(client, "pkg-1", {"description": "notes", "tags": ["stable"]})
    assert resp.status_code == 200
    pkg = isolated_store.get_package("pkg-1")
    assert pkg["metadata"]["description"] == "notes"
    assert pkg["metadata"]["tags"] == ["stable"]


def test_non_member_gets_403(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-2", "pkg-2.tgz", "beta")])
    auth.user = _FakeUser("alice")
    registry.add((1, "alice"))  # member of alpha only, not beta
    resp = _patch(client, "pkg-2", {"description": "x"})
    assert resp.status_code == 403
    assert isolated_store.get_package("pkg-2")["metadata"]["description"] == "original"


def test_disabled_project_gets_403(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-3", "pkg-3.tgz", "gamma")])
    auth.user = _FakeUser("alice")
    registry.add((3, "alice"))  # member of the disabled project's repo
    resp = _patch(client, "pkg-3", {"description": "x"})
    assert resp.status_code == 403


def test_unknown_project_code_gets_403(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-4", "pkg-4.tgz", "ghost")])
    auth.user = _FakeUser("alice")
    resp = _patch(client, "pkg-4", {"description": "x"})
    assert resp.status_code == 403


def test_unassociated_package_non_admin_gets_403(
    client, isolated_store, tmp_path, registry, auth
):
    _seed(isolated_store, tmp_path, [("pkg-5", "pkg-5.tgz", "")])
    auth.user = _FakeUser("alice")
    resp = _patch(client, "pkg-5", {"description": "x"})
    assert resp.status_code == 403


def test_admin_can_edit_unassociated(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-5", "pkg-5.tgz", "")])
    auth.user = _FakeUser("root", role="admin")
    resp = _patch(client, "pkg-5", {"description": "by admin"})
    assert resp.status_code == 200
    assert isolated_store.get_package("pkg-5")["metadata"]["description"] == "by admin"


def test_missing_package_authenticated_gets_404(
    client, isolated_store, tmp_path, registry, auth
):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = _FakeUser("root", role="admin")
    resp = _patch(client, "missing", {"description": "x"})
    assert resp.status_code == 404


# ─────────────────── 5.2 validation ───────────────────


def test_empty_body_rejected(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = _FakeUser("root", role="admin")
    resp = _patch(client, "pkg-1", {})
    assert resp.status_code == 400
    assert isolated_store.get_package("pkg-1")["metadata"]["description"] == "original"


def test_invalid_tags_payload_rejected(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = _FakeUser("root", role="admin")
    resp = _patch(client, "pkg-1", {"tags": "stable,ka"})
    assert resp.status_code == 400
    assert isolated_store.get_package("pkg-1")["metadata"]["tags"] == ["orig"]


def test_tags_trimmed_and_deduped_via_api(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = _FakeUser("root", role="admin")
    resp = _patch(client, "pkg-1", {"tags": [" stable ", "ka", "stable", ""]})
    assert resp.status_code == 200
    assert isolated_store.get_package("pkg-1")["metadata"]["tags"] == ["stable", "ka"]


def test_clear_editable_metadata(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = _FakeUser("root", role="admin")
    resp = _patch(client, "pkg-1", {"description": None, "tags": []})
    assert resp.status_code == 200
    pkg = isolated_store.get_package("pkg-1")
    assert pkg["metadata"]["description"] == ""
    assert pkg["metadata"]["tags"] == []


def test_detail_exposes_can_edit_flag(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    # member sees true
    auth.user = _FakeUser("alice")
    registry.add((1, "alice"))
    body = client.get("/packages/pkg-1").json()
    assert body["data"]["canEditMetadata"] is True
    # anonymous sees false
    auth.user = None
    body = client.get("/packages/pkg-1").json()
    assert body["data"]["canEditMetadata"] is False


# ─────────────────── 5.3 discovery participation ───────────────────


def test_updated_tags_used_by_list_filter(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = _FakeUser("root", role="admin")
    _patch(client, "pkg-1", {"tags": ["stable"]})
    body = client.get("/packages", params={"tags": "stable"}).json()
    assert [p["id"] for p in body["data"]["packages"]] == ["pkg-1"]


def test_updated_description_used_by_search(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = _FakeUser("root", role="admin")
    _patch(client, "pkg-1", {"description": "baseband hotfix"})
    body = client.get("/packages", params={"search": "baseband hotfix"}).json()
    assert [p["id"] for p in body["data"]["packages"]] == ["pkg-1"]


def test_components_save_reload_search_and_clear(client, isolated_store, tmp_path, registry, auth):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = _FakeUser("alice")
    registry.add((1, "alice"))
    before = isolated_store.get_package("pkg-1")
    archive = Path(before["path"]).read_bytes()
    resp = _patch(client, "pkg-1", {"components": [{"name": " netconf-server ", "version": " 0.9.32 "}, {"name": "helper"}]})
    assert resp.status_code == 200
    expected = [{"name": "netconf-server", "version": "0.9.32"}, {"name": "helper", "version": ""}]
    reloaded = client.get("/packages/pkg-1").json()["data"]
    assert reloaded["metadata"]["components"] == expected
    saved = isolated_store.get_package("pkg-1")
    saved["metadata"]["components"] = before["metadata"]["components"]
    assert saved == before
    assert Path(before["path"]).read_bytes() == archive
    matches, total = isolated_store.text_search("netconf-server", fields=["components"])
    assert total == 1
    assert matches[0]["id"] == "pkg-1"
    assert _patch(client, "pkg-1", {"components": []}).status_code == 200
    assert isolated_store.get_package("pkg-1")["metadata"]["components"] == []


@pytest.mark.parametrize("components", [None, "x", ["legacy"], [None], [{}], [{"name": " "}], [{"name": 1}], [{"name": "x", "version": None}], [{"name": "x" * 257}], [{"name": "x", "version": "v" * 257}], [{"name": "x"}] * 101])
def test_invalid_components_reject_entire_patch(client, isolated_store, tmp_path, registry, auth, components):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = _FakeUser("root", role="admin")
    before = isolated_store.get_package("pkg-1")
    resp = _patch(client, "pkg-1", {"components": components, "description": "must not persist"})
    assert resp.status_code == 400
    assert isolated_store.get_package("pkg-1") == before


@pytest.mark.parametrize("role,expected", [(None, 401), ("user", 403), ("admin", 200)])
def test_component_edit_authorization(client, isolated_store, tmp_path, registry, auth, role, expected):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    auth.user = _FakeUser("outside", role=role) if role else None
    before = isolated_store.get_package("pkg-1")
    assert _patch(client, "pkg-1", {"components": []}).status_code == expected
    if expected != 200:
        assert isolated_store.get_package("pkg-1") == before


@pytest.mark.parametrize("role,expected", [(None, 401), ("user", 403), ("admin", 200)])
def test_delete_package_requires_admin(client, isolated_store, tmp_path, auth, role, expected):
    _seed(isolated_store, tmp_path, [("pkg-1", "pkg-1.tgz", "alpha")])
    if role is None:
        # Anonymous callers hit the real login dependency.
        pass
    else:
        client.app.dependency_overrides[get_current_user] = lambda: _FakeUser("actor", role=role)
    resp = client.delete("/packages/pkg-1")
    assert resp.status_code == expected
    if expected == 200:
        assert resp.json()["success"] is True
        assert isolated_store.get_package("pkg-1") is None
    else:
        assert isolated_store.get_package("pkg-1") is not None
        if expected == 403:
            assert "管理员" in resp.json()["detail"] or "administrator" in resp.json()["detail"].lower()
