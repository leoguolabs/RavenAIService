"""Unit tests for bug_fix_service (task 4.4 / 6.3).

Covers the dispatch predicate, task creation from analysis, MR row recording
(no token leakage), and the three terminal-state transitions.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.models.bug_fix import BugFixMergeRequestStatus, BugFixTaskStatus
from app.services import bug_fix_service


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        Base.metadata.drop_all(engine)


def _analysis(**overrides):
    base = {
        "status": "completed",
        "requires_code_fix": True,
        "proposed_fixes": [{"title": "Null deref in parser", "description": "fix it"}],
        "summary": "A null pointer dereference.",
    }
    base.update(overrides)
    return base


# ───────────────────────── should_dispatch ─────────────────────────

def test_should_dispatch_true_when_all_signals_present():
    assert bug_fix_service.should_dispatch(_analysis()) is True


def test_should_dispatch_true_when_status_ok():
    # LogAnalysisAgent emits status="ok" (not "completed") on success; the
    # dispatch gate must treat it as a successful analysis.
    assert bug_fix_service.should_dispatch(_analysis(status="ok")) is True


def test_should_dispatch_false_when_not_completed():
    assert bug_fix_service.should_dispatch(_analysis(status="error")) is False


def test_should_dispatch_false_on_schema_mismatch():
    assert bug_fix_service.should_dispatch(_analysis(status="schema_mismatch")) is False


def test_should_dispatch_false_when_flag_off():
    assert bug_fix_service.should_dispatch(_analysis(requires_code_fix=False)) is False


def test_should_dispatch_false_when_no_fixes():
    assert bug_fix_service.should_dispatch(_analysis(proposed_fixes=[])) is False


def test_related_repository_proposals_are_not_auto_dispatched(session):
    related = {"title": "Fix protocol stack", "suspected_files": ["repo:related_repos/stack_three/app/a.cpp"]}
    primary = {"title": "Fix OAM", "suspected_files": ["repo:app/oam.cpp"]}
    assert not bug_fix_service.should_dispatch(_analysis(proposed_fixes=[related]))

    analysis = _analysis(proposed_fixes=[related, primary])
    assert bug_fix_service.should_dispatch(analysis)
    task = bug_fix_service.create_task_from_analysis(session, project_repo_id=3, analysis_result=analysis)
    assert json.loads(task.proposed_fixes_json) == [primary]
    assert analysis["proposed_fixes"] == [related, primary]


def test_should_dispatch_false_on_legacy_result_without_fields():
    # legacy result missing the new fields → safe default
    assert bug_fix_service.should_dispatch({"status": "completed"}) is False


def test_should_dispatch_handles_non_dict():
    assert bug_fix_service.should_dispatch(None) is False  # type: ignore[arg-type]


# ─────────────────────── create_task_from_analysis ─────────────────

def test_create_task_persists_fields(session):
    task = bug_fix_service.create_task_from_analysis(
        session,
        project_repo_id=3,
        analysis_result=_analysis(),
        source_log_id="log-1",
        source_analysis_task_id="celery-9",
    )
    session.commit()

    assert task.id
    assert task.project_repo_id == 3
    assert task.source_log_id == "log-1"
    assert task.source_analysis_task_id == "celery-9"
    assert task.status == BugFixTaskStatus.PENDING
    # title derived from first fix title
    assert task.title == "Null deref in parser"
    fixes = json.loads(task.proposed_fixes_json)
    assert fixes[0]["title"] == "Null deref in parser"


def test_create_task_title_falls_back_to_summary(session):
    result = _analysis(proposed_fixes=[{"description": "no title"}])
    task = bug_fix_service.create_task_from_analysis(
        session, project_repo_id=1, analysis_result=result
    )
    assert task.title == "A null pointer dereference."


# ─────────────────────── record_merge_request ──────────────────────

def test_record_merge_request_strips_and_persists(session):
    task = bug_fix_service.create_task_from_analysis(
        session, project_repo_id=1, analysis_result=_analysis()
    )
    session.commit()

    mr = bug_fix_service.record_merge_request(
        session,
        task.id,
        {
            "title": "Fix null deref",
            "description": "desc",
            "branch_name": "bugfix/ai-1-0",
            "base_branch": "main",
            "mr_url": "https://gitlab.example.com/foo/bar/-/merge_requests/12",
            "mr_iid": 12,
            "commit_sha": "abc1234",
            "changed_files": [{"path": "a.py", "added": 2, "removed": 1}],
            "diff_stat": {"files": 1, "insertions": 2, "deletions": 1},
        },
    )
    session.commit()

    assert mr.branch_name == "bugfix/ai-1-0"
    assert mr.base_branch == "main"
    assert mr.mr_iid == "12"
    assert mr.status == BugFixMergeRequestStatus.CREATED
    # mr_url contains no credentials
    assert "@" not in (mr.mr_url or "")
    assert json.loads(mr.changed_files_json)[0]["path"] == "a.py"
    assert json.loads(mr.diff_stat_json)["insertions"] == 2


def test_record_merge_request_without_url_marks_push_failed(session):
    task = bug_fix_service.create_task_from_analysis(
        session, project_repo_id=1, analysis_result=_analysis()
    )
    session.commit()
    mr = bug_fix_service.record_merge_request(
        session, task.id, {"branch_name": "b", "base_branch": "main"}
    )
    assert mr.status == BugFixMergeRequestStatus.PUSH_FAILED


# ───────────────────────────── finalize ────────────────────────────

def test_finalize_succeeded(session):
    task = bug_fix_service.create_task_from_analysis(
        session, project_repo_id=1, analysis_result=_analysis()
    )
    session.commit()
    status = bug_fix_service.finalize(session, task.id, merge_request_count=2)
    assert status == BugFixTaskStatus.SUCCEEDED
    assert task.finished_at is not None


def test_finalize_partial_when_mrs_and_error(session):
    task = bug_fix_service.create_task_from_analysis(
        session, project_repo_id=1, analysis_result=_analysis()
    )
    session.commit()
    status = bug_fix_service.finalize(
        session, task.id, merge_request_count=1, error="push_failed"
    )
    assert status == BugFixTaskStatus.PARTIAL
    assert task.error == "push_failed"


def test_finalize_failed_when_no_mrs(session):
    task = bug_fix_service.create_task_from_analysis(
        session, project_repo_id=1, analysis_result=_analysis()
    )
    session.commit()
    status = bug_fix_service.finalize(session, task.id, merge_request_count=0)
    assert status == BugFixTaskStatus.FAILED


def test_mark_running_sets_status_and_celery_id(session):
    task = bug_fix_service.create_task_from_analysis(
        session, project_repo_id=1, analysis_result=_analysis()
    )
    session.commit()
    bug_fix_service.mark_running(session, task.id, celery_task_id="cid-1")
    assert task.status == BugFixTaskStatus.RUNNING
    assert task.celery_task_id == "cid-1"
    assert task.started_at is not None
