"""Tests for app/services/skills_service.py."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from typing import Dict

import pytest


def _build_zip(members: Dict[str, bytes]) -> bytes:
    """Create an in-memory zip containing the given {arcname: bytes} entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


_SKILL_MD = (
    "---\n"
    "name: log-grep-helper\n"
    "description: Helper skill that knows how to grep panic logs\n"
    "---\n"
    "# Body\n"
    "Use this skill to grep for panic patterns.\n"
).encode("utf-8")

_PROJECT_SKILL_MD = (
    "---\n"
    "name: proj-helper\n"
    "description: Project-level helper for satellite telemetry\n"
    "---\n"
    "# Body\n"
    "Use this for satellite telemetry analysis.\n"
).encode("utf-8")

_XLSX_SKILL_MD = (
    "---\n"
    "name: xlsx\n"
    "description: Use this skill any time a spreadsheet file is the primary "
    "input or output, including Excel .xlsx files, CSV files, formulas, "
    "formatting, or spreadsheet analysis.\n"
    "---\n"
    "# XLSX creation, editing, and analysis\n"
    "Use pandas to read Excel data and openpyxl for formulas and formatting.\n"
    "For formula recalculation run the bundled script at scripts/recalc.py.\n"
).encode("utf-8")


@pytest.fixture()
def isolated_skills_dir(tmp_path, monkeypatch):
    """Point skills_service at a fresh tmp dir for each test."""
    from app.config import settings
    from app.agents import required_skill_policy

    # Optional catalog tests; required defaults have their own integration suite.
    monkeypatch.setattr(required_skill_policy, "_REQUIRED_SKILLS", {})

    monkeypatch.setattr(settings, "skills_data_dir", str(tmp_path / "agent_skills"))
    monkeypatch.setattr(settings, "project_skills_data_dir", str(tmp_path / "project_skills"))
    return tmp_path


def test_list_skills_empty(isolated_skills_dir):
    from app.services import skills_service
    assert skills_service.list_skills("log_analysis") == []


def test_install_skill_top_level_skill_md(isolated_skills_dir):
    from app.services import skills_service
    zip_bytes = _build_zip({"SKILL.md": _SKILL_MD, "scripts/run.sh": b"#!/bin/sh\n"})
    entry = skills_service.install_skill(
        "log_analysis", zip_bytes=zip_bytes, source_filename="grep.zip"
    )
    assert entry["name"] == "log-grep-helper"
    assert entry["enabled"] is True
    assert entry["source_filename"] == "grep.zip"
    listed = skills_service.list_skills("log_analysis")
    assert len(listed) == 1 and listed[0]["id"] == "log-grep-helper"


def test_install_skill_wrapped_directory(isolated_skills_dir):
    from app.services import skills_service
    zip_bytes = _build_zip(
        {
            "log-grep-helper/SKILL.md": _SKILL_MD,
            "log-grep-helper/reference.txt": b"hello",
        }
    )
    entry = skills_service.install_skill(
        "log_analysis", zip_bytes=zip_bytes, source_filename="wrapped.zip"
    )
    assert entry["name"] == "log-grep-helper"


def test_install_rejects_missing_skill_md(isolated_skills_dir):
    from app.services import skills_service
    zip_bytes = _build_zip({"reference.txt": b"hi"})
    with pytest.raises(skills_service.SkillValidationError):
        skills_service.install_skill(
            "log_analysis", zip_bytes=zip_bytes, source_filename="bad.zip"
        )


def test_install_rejects_missing_frontmatter(isolated_skills_dir):
    from app.services import skills_service
    zip_bytes = _build_zip({"SKILL.md": b"# Just a markdown body, no frontmatter\n"})
    with pytest.raises(skills_service.SkillValidationError):
        skills_service.install_skill(
            "log_analysis", zip_bytes=zip_bytes, source_filename="bad.zip"
        )


def test_install_rejects_zip_slip(isolated_skills_dir):
    from app.services import skills_service
    zip_bytes = _build_zip(
        {"../escape.txt": b"oops", "SKILL.md": _SKILL_MD}
    )
    with pytest.raises(skills_service.SkillValidationError):
        skills_service.install_skill(
            "log_analysis", zip_bytes=zip_bytes, source_filename="evil.zip"
        )


def test_install_conflict_then_overwrite(isolated_skills_dir):
    from app.services import skills_service
    zip_bytes = _build_zip({"SKILL.md": _SKILL_MD})
    skills_service.install_skill(
        "log_analysis", zip_bytes=zip_bytes, source_filename="a.zip"
    )
    with pytest.raises(skills_service.SkillConflictError):
        skills_service.install_skill(
            "log_analysis", zip_bytes=zip_bytes, source_filename="b.zip"
        )
    entry = skills_service.install_skill(
        "log_analysis", zip_bytes=zip_bytes, source_filename="b.zip", overwrite=True
    )
    assert entry["source_filename"] == "b.zip"


def test_set_enabled_and_delete(isolated_skills_dir):
    from app.services import skills_service
    zip_bytes = _build_zip({"SKILL.md": _SKILL_MD})
    skills_service.install_skill(
        "log_analysis", zip_bytes=zip_bytes, source_filename="a.zip"
    )
    updated = skills_service.set_skill_enabled("log_analysis", "log-grep-helper", False)
    assert updated["enabled"] is False

    skills_service.delete_skill("log_analysis", "log-grep-helper")
    assert skills_service.list_skills("log_analysis") == []
    with pytest.raises(skills_service.SkillNotFoundError):
        skills_service.delete_skill("log_analysis", "log-grep-helper")


def test_materialize_enabled_only(isolated_skills_dir, tmp_path):
    from app.services import skills_service

    # Install two skills
    enabled_md = _SKILL_MD
    disabled_md = (
        "---\n"
        "name: archived-skill\n"
        "description: a disabled one\n"
        "---\n"
        "body\n"
    ).encode("utf-8")
    skills_service.install_skill(
        "log_analysis",
        zip_bytes=_build_zip({"SKILL.md": enabled_md}),
        source_filename="enabled.zip",
    )
    skills_service.install_skill(
        "log_analysis",
        zip_bytes=_build_zip({"SKILL.md": disabled_md}),
        source_filename="disabled.zip",
    )
    skills_service.set_skill_enabled("log_analysis", "archived-skill", False)

    cwd = tmp_path / "agent_cwd"
    cwd.mkdir()
    materialized = skills_service.materialize_enabled_skills("log_analysis", str(cwd))
    assert materialized == ["log-grep-helper"]

    target = cwd / ".claude" / "skills" / "log-grep-helper" / "SKILL.md"
    assert target.is_file()
    assert b"name: log-grep-helper" in target.read_bytes()

    # Disabled skill should NOT appear
    assert not (cwd / ".claude" / "skills" / "archived-skill").exists()


def test_materialize_all_enabled_skills_regardless_of_question(isolated_skills_dir, tmp_path):
    """Dynamic loading model: every enabled skill is materialized; the model
    decides at inference time which ones to load via the Skill tool."""
    from app.services import skills_service

    smu_md = (
        "---\n"
        "name: smu-baseband-interfaces\n"
        "description: SMU RS422 baseband file transfer protocol analysis\n"
        "---\n"
        "Use for SMU, RS422, baseband, file transfer failures.\n"
    ).encode("utf-8")
    antenna_md = (
        "---\n"
        "name: ka-phased-array-antenna\n"
        "description: KA phased array antenna calibration and beam logs\n"
        "---\n"
        "Use for antenna calibration, beam position, and phased-array issues.\n"
    ).encode("utf-8")

    skills_service.install_skill(
        "log_analysis",
        zip_bytes=_build_zip({"SKILL.md": smu_md}),
        source_filename="smu.zip",
    )
    skills_service.install_skill(
        "log_analysis",
        zip_bytes=_build_zip({"SKILL.md": antenna_md}),
        source_filename="antenna.zip",
    )

    cwd = tmp_path / "agent_cwd_all"
    cwd.mkdir()
    materialized = skills_service.materialize_enabled_skills("log_analysis", cwd)
    assert sorted(materialized) == [
        "ka-phased-array-antenna",
        "smu-baseband-interfaces",
    ]
    assert (cwd / ".claude" / "skills" / "smu-baseband-interfaces" / "SKILL.md").is_file()
    assert (cwd / ".claude" / "skills" / "ka-phased-array-antenna" / "SKILL.md").is_file()

    overviews = skills_service.enabled_skill_overviews("log_analysis")
    assert {
        "name": "smu-baseband-interfaces",
        "description": "SMU RS422 baseband file transfer protocol analysis",
    } in overviews
    assert {
        "name": "ka-phased-array-antenna",
        "description": "KA phased array antenna calibration and beam logs",
    } in overviews


def test_folded_yaml_description_surfaces_in_overviews(isolated_skills_dir):
    from app.services import skills_service

    verifier_md = (
        "---\n"
        "name: skill-verifier\n"
        "description: >\n"
        "  Use this skill when the user asks to verify skill loading,\n"
        "  asks \"橙子是什么颜色\", or says \"skill password\".\n"
        "---\n"
        "# Body\n"
        "No trigger words are repeated here.\n"
    ).encode("utf-8")

    entry = skills_service.install_project_skill(
        "sat1",
        zip_bytes=_build_zip({"SKILL.md": verifier_md}),
        source_filename="verifier.zip",
    )
    assert "橙子是什么颜色" in entry["description"]

    overviews = skills_service.enabled_skill_overviews(
        "project_expert", project_code="sat1"
    )
    assert any(
        o["name"] == "skill-verifier" and "橙子是什么颜色" in o["description"]
        for o in overviews
    )


def test_xlsx_skill_with_runtime_scripts_is_materialized_with_scripts(
    isolated_skills_dir, tmp_path
):
    from app.services import skills_service

    xlsx_zip = _build_zip(
        {
            "SKILL.md": _XLSX_SKILL_MD,
            "scripts/recalc.py": b"from office.soffice import get_soffice_env\n",
            "scripts/office/soffice.py": b"def get_soffice_env():\n    return {}\n",
            "scripts/office/__init__.py": b"",
        }
    )
    entry = skills_service.install_skill(
        "project_expert",
        zip_bytes=xlsx_zip,
        source_filename="xlsx.zip",
    )
    assert entry["name"] == "xlsx"
    assert entry["enabled"] is True

    cwd = tmp_path / "agent_cwd_xlsx"
    cwd.mkdir()
    materialized = skills_service.materialize_enabled_skills("project_expert", cwd)
    assert materialized == ["xlsx"]
    skill_dir = cwd / ".claude" / "skills" / "xlsx"
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "scripts" / "recalc.py").is_file()
    assert (skill_dir / "scripts" / "office" / "soffice.py").is_file()


def _named_skill_md(name: str, description: str, body: str = "") -> bytes:
    return (
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n"
    ).encode("utf-8")


def test_relevant_selection_excludes_unrelated_production_like_skills(
    isolated_skills_dir, tmp_path
):
    from app.services import skills_service

    for name, description in (
        ("xlsx", "Excel xlsx spreadsheet workbook analysis"),
        ("docx", "Word docx document editing"),
    ):
        skills_service.install_skill(
            "log_analysis",
            zip_bytes=_build_zip(
                {"SKILL.md": _named_skill_md(name, description)}
            ),
            source_filename=f"{name}.zip",
        )

    project_skills = (
        (
            "ka-phased-array-antenna",
            "Ka相控阵接收天线、波束和变频状态日志分析",
        ),
        ("smu-baseband-interfaces", "SMU与基带接口及RS422文件传输"),
        ("lx10-telemetry", "灵犀10平台通用遥测字典"),
        ("payload-management-unit", "载荷管理单元PMU软件维护"),
        ("tcpt027-db-modify", "TCPT027数据库字段修改流程"),
    )
    for name, description in project_skills:
        skills_service.install_project_skill(
            "oam_lx10",
            zip_bytes=_build_zip(
                {"SKILL.md": _named_skill_md(name, description)}
            ),
            source_filename=f"{name}.zip",
        )

    cwd = tmp_path / "production_like"
    cwd.mkdir()
    selected = skills_service.materialize_relevant_enabled_skills(
        "log_analysis",
        cwd,
        query_text=(
            "分析当前日志，确认Ka接收天线波束3的变频状态控制字是否配置成功\n"
            "0902-ka-ra-01.tar.gz"
        ),
        project_code="oam_lx10",
    )
    available = skills_service.enabled_skill_overviews(
        "log_analysis",
        project_code="oam_lx10",
    )

    assert selected == ["ka-phased-array-antenna"]
    assert [item["name"] for item in available] == [
        "xlsx",
        "docx",
        "ka-phased-array-antenna",
        "smu-baseband-interfaces",
        "lx10-telemetry",
        "payload-management-unit",
        "tcpt027-db-modify",
    ]
    assert not (cwd / ".claude" / "skills" / "payload-management-unit").exists()
    assert not (cwd / ".claude" / "skills" / "tcpt027-db-modify").exists()
    assert not (cwd / ".claude" / "skills" / "xlsx").exists()
    assert not (cwd / ".claude" / "skills" / "docx").exists()


def test_relevant_selection_uses_attachment_suffix_and_no_match_is_empty(
    isolated_skills_dir, tmp_path
):
    from app.services import skills_service

    for name, description in (
        ("xlsx", "Excel xlsx spreadsheet workbook analysis"),
        ("docx", "Word docx document editing"),
    ):
        skills_service.install_skill(
            "project_expert",
            zip_bytes=_build_zip(
                {"SKILL.md": _named_skill_md(name, description)}
            ),
            source_filename=f"{name}.zip",
        )

    assert skills_service.select_relevant_skill_names(
        "project_expert", query_text="请汇总附件中的数据\nstatus-report.xlsx"
    ) == ["xlsx"]
    assert skills_service.select_relevant_skill_names(
        "project_expert", query_text="解释今天的天气"
    ) == []

    cwd = tmp_path / "no_match"
    cwd.mkdir()
    assert skills_service.materialize_relevant_enabled_skills(
        "project_expert", cwd, query_text="解释今天的天气"
    ) == []
    assert not (cwd / ".claude" / "skills").exists()


def test_relevant_selection_is_bounded_and_project_override_is_scored(
    isolated_skills_dir, tmp_path
):
    from app.services import skills_service

    for index in range(5):
        name = f"beam-helper-{index}"
        skills_service.install_skill(
            "log_analysis",
            zip_bytes=_build_zip(
                {"SKILL.md": _named_skill_md(name, "beam antenna helper")}
            ),
            source_filename=f"{name}.zip",
        )
    assert len(
        skills_service.select_relevant_skill_names(
            "log_analysis", query_text="beam antenna failure"
        )
    ) == 3

    agent_md = _named_skill_md(
        "shared-skill", "generic maintenance", "unrelated agent body"
    )
    project_md = _named_skill_md(
        "shared-skill", "Ka波束变频专项排查", "project override body"
    )
    skills_service.install_skill(
        "project_expert",
        zip_bytes=_build_zip({"SKILL.md": agent_md}),
        source_filename="agent.zip",
    )
    skills_service.install_project_skill(
        "sat1",
        zip_bytes=_build_zip({"SKILL.md": project_md}),
        source_filename="project.zip",
    )
    cwd = tmp_path / "project_override_relevant"
    cwd.mkdir()
    assert skills_service.materialize_relevant_enabled_skills(
        "project_expert",
        cwd,
        query_text="排查Ka波束变频状态",
        project_code="sat1",
    ) == ["shared-skill"]
    assert "project override body" in (
        cwd / ".claude" / "skills" / "shared-skill" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_unknown_agent_rejected(isolated_skills_dir):
    from app.services import skills_service
    with pytest.raises(skills_service.UnknownAgentError):
        skills_service.list_skills("__nope__")


def test_package_search_is_registered_as_skills_host(isolated_skills_dir):
    from app.services import skills_service

    assert skills_service.SUPPORTED_AGENTS["package_search"]["name"] == (
        "PackageSearchAgent"
    )
    assert skills_service.list_skills("package_search") == []


def test_install_zip_too_large(isolated_skills_dir, monkeypatch):
    from app.services import skills_service
    monkeypatch.setattr(skills_service, "MAX_SKILL_ZIP_BYTES", 64)
    big = _build_zip({"SKILL.md": _SKILL_MD, "blob.bin": b"x" * 1024})
    with pytest.raises(skills_service.SkillValidationError):
        skills_service.install_skill(
            "log_analysis", zip_bytes=big, source_filename="big.zip"
        )


# ═══════════════════════════════════════════════════════════════════════
# 8.1 — Project skill storage: install, list, enable/disable, delete
# ═══════════════════════════════════════════════════════════════════════


class TestProjectSkillStorage:

    def test_list_empty(self, isolated_skills_dir):
        from app.services import skills_service
        assert skills_service.list_project_skills("MYPROJ") == []

    def test_install_and_list(self, isolated_skills_dir):
        from app.services import skills_service
        zip_bytes = _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        entry = skills_service.install_project_skill(
            "MyProj", zip_bytes=zip_bytes, source_filename="proj.zip"
        )
        assert entry["name"] == "proj-helper"
        assert entry["enabled"] is True
        assert entry["source_filename"] == "proj.zip"

        listed = skills_service.list_project_skills("myproj")
        assert len(listed) == 1
        assert listed[0]["id"] == "proj-helper"

    def test_project_code_normalized_to_lowercase(self, isolated_skills_dir):
        from app.services import skills_service
        zip_bytes = _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        skills_service.install_project_skill(
            "MixedCase", zip_bytes=zip_bytes, source_filename="a.zip"
        )
        listed = skills_service.list_project_skills("MIXEDCASE")
        assert len(listed) == 1

    def test_empty_project_code_rejected(self, isolated_skills_dir):
        from app.services import skills_service
        with pytest.raises(skills_service.SkillValidationError):
            skills_service.list_project_skills("")
        with pytest.raises(skills_service.SkillValidationError):
            skills_service.list_project_skills("   ")

    def test_install_conflict_and_overwrite(self, isolated_skills_dir):
        from app.services import skills_service
        zip_bytes = _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        skills_service.install_project_skill(
            "proj1", zip_bytes=zip_bytes, source_filename="a.zip"
        )
        with pytest.raises(skills_service.SkillConflictError):
            skills_service.install_project_skill(
                "proj1", zip_bytes=zip_bytes, source_filename="b.zip"
            )
        entry = skills_service.install_project_skill(
            "proj1", zip_bytes=zip_bytes, source_filename="b.zip", overwrite=True
        )
        assert entry["source_filename"] == "b.zip"

    def test_enable_disable(self, isolated_skills_dir):
        from app.services import skills_service
        zip_bytes = _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        skills_service.install_project_skill(
            "proj1", zip_bytes=zip_bytes, source_filename="a.zip"
        )
        updated = skills_service.set_project_skill_enabled("proj1", "proj-helper", False)
        assert updated["enabled"] is False

        updated = skills_service.set_project_skill_enabled("proj1", "proj-helper", True)
        assert updated["enabled"] is True

    def test_enable_not_found(self, isolated_skills_dir):
        from app.services import skills_service
        _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        with pytest.raises(skills_service.SkillNotFoundError):
            skills_service.set_project_skill_enabled("proj1", "nonexistent", True)

    def test_delete(self, isolated_skills_dir):
        from app.services import skills_service
        zip_bytes = _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        skills_service.install_project_skill(
            "proj1", zip_bytes=zip_bytes, source_filename="a.zip"
        )
        skills_service.delete_project_skill("proj1", "proj-helper")
        assert skills_service.list_project_skills("proj1") == []

    def test_delete_not_found(self, isolated_skills_dir):
        from app.services import skills_service
        with pytest.raises(skills_service.SkillNotFoundError):
            skills_service.delete_project_skill("proj1", "nonexistent")

    def test_disk_validation_on_list(self, isolated_skills_dir):
        """If the skill directory is removed from disk, list drops it."""
        from app.services import skills_service
        zip_bytes = _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        skills_service.install_project_skill(
            "proj1", zip_bytes=zip_bytes, source_filename="a.zip"
        )
        import shutil
        store = skills_service._project_store_root("proj1")
        shutil.rmtree(store / "proj-helper")

        listed = skills_service.list_project_skills("proj1")
        assert listed == []

    def test_file_browsing(self, isolated_skills_dir):
        from app.services import skills_service
        zip_bytes = _build_zip({
            "SKILL.md": _PROJECT_SKILL_MD,
            "scripts/run.sh": b"#!/bin/sh\necho hello\n",
        })
        skills_service.install_project_skill(
            "proj1", zip_bytes=zip_bytes, source_filename="a.zip"
        )
        files = skills_service.list_project_skill_files("proj1", "proj-helper")
        assert files["name"] == "proj-helper"
        assert files["tree"]["type"] == "dir"

        content = skills_service.read_project_skill_file(
            "proj1", "proj-helper", "scripts/run.sh"
        )
        assert content["encoding"] == "utf-8"
        assert "echo hello" in content["content"]

    def test_file_browsing_not_found(self, isolated_skills_dir):
        from app.services import skills_service
        with pytest.raises(skills_service.SkillNotFoundError):
            skills_service.list_project_skill_files("proj1", "nonexistent")

    def test_project_isolation(self, isolated_skills_dir):
        """Skills installed under different project_codes are isolated."""
        from app.services import skills_service
        zip_bytes = _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        skills_service.install_project_skill(
            "projA", zip_bytes=zip_bytes, source_filename="a.zip"
        )
        assert len(skills_service.list_project_skills("projA")) == 1
        assert len(skills_service.list_project_skills("projB")) == 0


# ═══════════════════════════════════════════════════════════════════════
# 8.2 — Unified materialization: merged pool, name conflict, backward compat
# ═══════════════════════════════════════════════════════════════════════


class TestUnifiedMaterialization:

    def test_packaging_skill_does_not_ship_the_legacy_catalog(self, isolated_skills_dir):
        from app.services.full_package_service import DEFAULT_CATALOG_PATH, load_catalog
        from app.services.skills_service import materialize_enabled_skills

        workspace = isolated_skills_dir / "packaging-workspace"
        assert materialize_enabled_skills("package_search", workspace) == ["full-package-build"]
        skill_dir = workspace / ".claude" / "skills" / "full-package-build"
        assert (skill_dir / "SKILL.md").is_file()
        assert not list(skill_dir.rglob(DEFAULT_CATALOG_PATH.name))
        assert load_catalog()["projects"]

    def test_builtin_agent_project_precedence_is_shared_with_overview(
        self,
        isolated_skills_dir,
        tmp_path,
        monkeypatch,
    ):
        """built-in → Agent → project overrides content and description alike."""
        from app.services import skills_service

        builtin_root = tmp_path / "builtin_skills"
        builtin_dir = builtin_root / "shared-skill"
        builtin_dir.mkdir(parents=True)
        (builtin_dir / "SKILL.md").write_text(
            "---\n"
            "name: shared-skill\n"
            "description: built-in version\n"
            "---\n"
            "Built-in body.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            skills_service,
            "_builtin_skills_root",
            lambda agent_key: builtin_root
            if agent_key == "package_search"
            else tmp_path / "no-builtins",
        )

        builtin_cwd = tmp_path / "cwd_builtin"
        builtin_cwd.mkdir()
        assert skills_service.materialize_enabled_skills(
            "package_search",
            builtin_cwd,
        ) == ["shared-skill"]
        assert "Built-in body." in (
            builtin_cwd / ".claude" / "skills" / "shared-skill" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert skills_service.enabled_skill_overviews("package_search") == [
            {"name": "shared-skill", "description": "built-in version"}
        ]

        agent_md = (
            "---\n"
            "name: shared-skill\n"
            "description: Agent version\n"
            "---\n"
            "Agent body.\n"
        ).encode("utf-8")
        project_md = (
            "---\n"
            "name: shared-skill\n"
            "description: project version\n"
            "---\n"
            "Project body.\n"
        ).encode("utf-8")
        skills_service.install_skill(
            "package_search",
            zip_bytes=_build_zip({"SKILL.md": agent_md}),
            source_filename="agent.zip",
        )

        agent_cwd = tmp_path / "cwd_agent"
        agent_cwd.mkdir()
        assert skills_service.materialize_enabled_skills(
            "package_search",
            agent_cwd,
        ) == ["shared-skill"]
        assert "Agent body." in (
            agent_cwd / ".claude" / "skills" / "shared-skill" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert skills_service.enabled_skill_overviews("package_search") == [
            {"name": "shared-skill", "description": "Agent version"}
        ]

        skills_service.install_project_skill(
            "sat1",
            zip_bytes=_build_zip({"SKILL.md": project_md}),
            source_filename="project.zip",
        )
        project_cwd = tmp_path / "cwd_project"
        project_cwd.mkdir()
        assert skills_service.materialize_enabled_skills(
            "package_search",
            project_cwd,
            project_code="sat1",
        ) == ["shared-skill"]
        assert "Project body." in (
            project_cwd / ".claude" / "skills" / "shared-skill" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert skills_service.enabled_skill_overviews(
            "package_search",
            project_code="sat1",
        ) == [{"name": "shared-skill", "description": "project version"}]

    def test_backward_compat_project_code_none(self, isolated_skills_dir, tmp_path):
        """With project_code=None, behavior is identical to pre-change."""
        from app.services import skills_service
        zip_bytes = _build_zip({"SKILL.md": _SKILL_MD})
        skills_service.install_skill(
            "log_analysis", zip_bytes=zip_bytes, source_filename="a.zip"
        )
        cwd = tmp_path / "cwd_compat"
        cwd.mkdir()
        materialized = skills_service.materialize_enabled_skills(
            "log_analysis", str(cwd), project_code=None
        )
        assert materialized == ["log-grep-helper"]
        assert (cwd / ".claude" / "skills" / "log-grep-helper" / "SKILL.md").is_file()

    def test_overviews_backward_compat_project_code_none(self, isolated_skills_dir):
        from app.services import skills_service
        zip_bytes = _build_zip({"SKILL.md": _SKILL_MD})
        skills_service.install_skill(
            "log_analysis", zip_bytes=zip_bytes, source_filename="a.zip"
        )
        overviews = skills_service.enabled_skill_overviews(
            "log_analysis", project_code=None
        )
        assert [o["name"] for o in overviews] == ["log-grep-helper"]

    def test_overviews_merged_pool_includes_project_skills(self, isolated_skills_dir):
        from app.services import skills_service
        agent_zip = _build_zip({"SKILL.md": _SKILL_MD})
        skills_service.install_skill(
            "log_analysis", zip_bytes=agent_zip, source_filename="agent.zip"
        )
        proj_zip = _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        skills_service.install_project_skill(
            "sat1", zip_bytes=proj_zip, source_filename="proj.zip"
        )

        overviews = skills_service.enabled_skill_overviews(
            "log_analysis", project_code="sat1"
        )
        names = [o["name"] for o in overviews]
        assert "log-grep-helper" in names
        assert "proj-helper" in names

    def test_materialize_agent_and_project(self, isolated_skills_dir, tmp_path):
        from app.services import skills_service
        agent_zip = _build_zip({"SKILL.md": _SKILL_MD})
        skills_service.install_skill(
            "log_analysis", zip_bytes=agent_zip, source_filename="agent.zip"
        )
        proj_zip = _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        skills_service.install_project_skill(
            "sat1", zip_bytes=proj_zip, source_filename="proj.zip"
        )

        cwd = tmp_path / "cwd_merged"
        cwd.mkdir()
        materialized = skills_service.materialize_enabled_skills(
            "log_analysis", str(cwd), project_code="sat1"
        )
        assert "log-grep-helper" in materialized
        assert "proj-helper" in materialized
        assert (cwd / ".claude" / "skills" / "log-grep-helper" / "SKILL.md").is_file()
        assert (cwd / ".claude" / "skills" / "proj-helper" / "SKILL.md").is_file()

    def test_name_conflict_project_wins(self, isolated_skills_dir, tmp_path):
        """When agent and project have a skill with the same name, project overwrites."""
        from app.services import skills_service
        agent_md = (
            "---\n"
            "name: shared-skill\n"
            "description: agent version\n"
            "---\n"
            "Agent body.\n"
        ).encode("utf-8")
        proj_md = (
            "---\n"
            "name: shared-skill\n"
            "description: project version\n"
            "---\n"
            "Project body.\n"
        ).encode("utf-8")

        skills_service.install_skill(
            "log_analysis",
            zip_bytes=_build_zip({"SKILL.md": agent_md}),
            source_filename="agent.zip",
        )
        skills_service.install_project_skill(
            "sat1",
            zip_bytes=_build_zip({"SKILL.md": proj_md}),
            source_filename="proj.zip",
        )

        cwd = tmp_path / "cwd_conflict"
        cwd.mkdir()
        materialized = skills_service.materialize_enabled_skills(
            "log_analysis", str(cwd), project_code="sat1"
        )
        assert "shared-skill" in materialized

        skill_md = (cwd / ".claude" / "skills" / "shared-skill" / "SKILL.md").read_text()
        assert "Project body." in skill_md

    def test_overviews_name_conflict_project_preferred(self, isolated_skills_dir):
        """In overviews, the project skill's description wins on name conflict."""
        from app.services import skills_service
        agent_md = (
            "---\n"
            "name: shared-skill\n"
            "description: agent version\n"
            "---\n"
            "body\n"
        ).encode("utf-8")
        proj_md = (
            "---\n"
            "name: shared-skill\n"
            "description: project version\n"
            "---\n"
            "body\n"
        ).encode("utf-8")

        skills_service.install_skill(
            "log_analysis",
            zip_bytes=_build_zip({"SKILL.md": agent_md}),
            source_filename="agent.zip",
        )
        skills_service.install_project_skill(
            "sat1",
            zip_bytes=_build_zip({"SKILL.md": proj_md}),
            source_filename="proj.zip",
        )

        overviews = skills_service.enabled_skill_overviews(
            "log_analysis", project_code="sat1"
        )
        matches = [o for o in overviews if o["name"] == "shared-skill"]
        assert matches == [{"name": "shared-skill", "description": "project version"}]

    def test_disabled_project_skill_excluded(self, isolated_skills_dir, tmp_path):
        from app.services import skills_service
        proj_zip = _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        skills_service.install_project_skill(
            "sat1", zip_bytes=proj_zip, source_filename="a.zip"
        )
        skills_service.set_project_skill_enabled("sat1", "proj-helper", False)

        cwd = tmp_path / "cwd_disabled"
        cwd.mkdir()
        materialized = skills_service.materialize_enabled_skills(
            "log_analysis", str(cwd), project_code="sat1"
        )
        assert "proj-helper" not in materialized

    def test_overviews_names_filter(self, isolated_skills_dir):
        from app.services import skills_service
        agent_zip = _build_zip({"SKILL.md": _SKILL_MD})
        skills_service.install_skill(
            "log_analysis", zip_bytes=agent_zip, source_filename="agent.zip"
        )
        proj_zip = _build_zip({"SKILL.md": _PROJECT_SKILL_MD})
        skills_service.install_project_skill(
            "sat1", zip_bytes=proj_zip, source_filename="proj.zip"
        )

        overviews = skills_service.enabled_skill_overviews(
            "log_analysis", project_code="sat1", names=["proj-helper"]
        )
        assert overviews == [
            {
                "name": "proj-helper",
                "description": "Project-level helper for satellite telemetry",
            }
        ]
