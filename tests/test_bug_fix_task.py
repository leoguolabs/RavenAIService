"""Tests for run_bug_fix_task terminal states (task 6.3).

Exercises the three terminal states (succeeded / partial / failed) by mocking
the workspace clone and the coding agent, and asserts MR rows land in the DB
without any token leakage.
"""

from __future__ import annotations

import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import Base
from app.models.bug_fix import BugFixMergeRequest, BugFixTask, BugFixTaskStatus
from app.models.project_repo import ProjectRepo
from app.models.log import LogRecord
from app.services import bug_fix_service


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    Base.metadata.drop_all(engine)


def _seed(Session, *, fixes=None):
    s = Session()
    repo = ProjectRepo(
        project_code="foo",
        project_name="Foo",
        repo_url="https://gitlab.example.com/foo/bar.git",
        project_card="Foo project source and bug fixes",
        default_branch="main",
        git_token="secret",
        enabled=True,
    )
    s.add(repo)
    s.flush()
    task = bug_fix_service.create_task_from_analysis(
        s,
        project_repo_id=repo.id,
        analysis_result={
            "status": "completed",
            "requires_code_fix": True,
            "proposed_fixes": fixes or [{"title": "fix"}],
            "summary": "s",
        },
        source_log_id="log-1" if s.get(LogRecord, "log-1") else None,
    )
    s.commit()
    task_id = task.id
    s.close()
    return task_id


def _run(monkeypatch, Session, agent_result):
    from app.tasks import bug_fix as bf

    monkeypatch.setattr(bf, "SessionLocal", Session)
    fake_ctx = object()
    monkeypatch.setattr(bf, "prepare", lambda **kw: fake_ctx)
    monkeypatch.setattr(bf, "cleanup", lambda ctx: None)

    class _FakeAgent:
        def run_sync(self, ctx):
            return agent_result

    monkeypatch.setattr(bf, "BugFixCodingAgent", _FakeAgent)
    task_id = _seed(Session)
    # Call the bound task function directly to avoid the eager result backend
    # (redis) which is not available in the test environment.
    result = bf.run_bug_fix_task.run(task_id)
    return task_id, result


def test_run_bug_fix_task_succeeded(monkeypatch, session_factory):
    Session = session_factory
    agent_result = {
        "status": "succeeded",
        "model": "provider/served-model",
        "trace_events": [{"type": "system_notice", "kind": "raven_route",
                          "executor": "antigravity_cli", "provider": "provider", "model": "provider/served-model"}],
        "merge_requests": [
            {
                "title": "Fix",
                "branch_name": "bugfix/ai-1-0",
                "base_branch": "main",
                "mr_url": "https://gitlab.example.com/foo/bar/-/merge_requests/1",
                "mr_iid": "1",
                "commit_sha": "abc1234",
            }
        ],
    }
    task_id, result = _run(monkeypatch, Session, agent_result)
    assert result["merge_request_count"] == 1

    s = Session()
    task = s.get(BugFixTask, task_id)
    assert task.status == BugFixTaskStatus.SUCCEEDED
    assert task.model == "provider/served-model"
    assert json.loads(task.trace_events_json) == agent_result["trace_events"]
    mrs = s.query(BugFixMergeRequest).filter_by(task_id=task_id).all()
    assert len(mrs) == 1
    assert "@" not in (mrs[0].mr_url or "")
    s.close()


def test_run_bug_fix_task_partial(monkeypatch, session_factory):
    Session = session_factory
    agent_result = {
        "status": "partial",
        "error_kind": "push_failed",
        "merge_requests": [
            {"title": "Fix", "branch_name": "b1", "base_branch": "main",
             "mr_url": "https://gitlab.example.com/foo/bar/-/merge_requests/2"},
        ],
    }
    task_id, _ = _run(monkeypatch, Session, agent_result)
    s = Session()
    task = s.get(BugFixTask, task_id)
    assert task.status == BugFixTaskStatus.PARTIAL
    assert task.error == "push_failed"
    s.close()


def test_run_bug_fix_task_failed_no_output(monkeypatch, session_factory):
    Session = session_factory
    agent_result = {"status": "failed", "error_kind": "no_root_cause_found", "merge_requests": []}
    task_id, _ = _run(monkeypatch, Session, agent_result)
    s = Session()
    task = s.get(BugFixTask, task_id)
    assert task.status == BugFixTaskStatus.FAILED
    assert s.query(BugFixMergeRequest).filter_by(task_id=task_id).count() == 0
    s.close()


def test_run_bug_fix_task_all_already_implemented_succeeds(monkeypatch, session_factory):
    """全部拟修复项已在基线实现（0 MR）→ succeeded，并持久化逐项 fix_outcomes。"""
    import json as _json

    Session = session_factory
    agent_result = {
        "status": "succeeded",
        "merge_requests": [],
        "fix_outcomes": [
            {"fix_index": 1, "title": "guard", "outcome": "already_implemented",
             "reason": "in 9b750d5"},
        ],
    }
    task_id, result = _run(monkeypatch, Session, agent_result)
    assert result["merge_request_count"] == 0

    s = Session()
    task = s.get(BugFixTask, task_id)
    # 无 MR 但「已确认无需修复」不应判失败
    assert task.status == BugFixTaskStatus.SUCCEEDED
    assert task.error is None
    outcomes = _json.loads(task.fix_outcomes_json)
    assert outcomes[0]["outcome"] == "already_implemented"
    assert s.query(BugFixMergeRequest).filter_by(task_id=task_id).count() == 0
    s.close()


def test_run_bug_fix_task_persists_fix_outcomes_alongside_mr(monkeypatch, session_factory):
    """部分项产出 MR、部分项已实现：MR 落库且 fix_outcomes 完整持久化。"""
    import json as _json

    Session = session_factory
    agent_result = {
        "status": "succeeded",
        "merge_requests": [
            {"title": "Fix timer", "branch_name": "bugfix/ai-1-2", "base_branch": "main",
             "mr_url": "https://gitlab.example.com/foo/bar/-/merge_requests/9"},
        ],
        "fix_outcomes": [
            {"fix_index": 1, "title": "guard", "outcome": "already_implemented",
             "reason": "in 9b750d5"},
            {"fix_index": 2, "title": "timer", "outcome": "created_mr",
             "branch_name": "bugfix/ai-1-2",
             "mr_url": "https://gitlab.example.com/foo/bar/-/merge_requests/9"},
        ],
    }
    task_id, _ = _run(monkeypatch, Session, agent_result)

    s = Session()
    task = s.get(BugFixTask, task_id)
    assert task.status == BugFixTaskStatus.SUCCEEDED
    assert s.query(BugFixMergeRequest).filter_by(task_id=task_id).count() == 1
    outcomes = _json.loads(task.fix_outcomes_json)
    assert [o["outcome"] for o in outcomes] == ["already_implemented", "created_mr"]
    s.close()


def test_run_bug_fix_task_forwards_source_log_archive(monkeypatch, session_factory):
    """来源日志的归档路径必须传给工作区 prepare，供重建 logs/ 目录。"""
    from app.models.log import LogRecord
    from app.tasks import bug_fix as bf

    Session = session_factory
    monkeypatch.setattr(bf, "SessionLocal", Session)

    s = Session()
    s.add(
        LogRecord(
            id="log-1",
            filename="upload.zip",
            original_filename="device-logs.zip",
            file_size=1,
            file_path="/data/uploads/upload.zip",
            archive_path="/data/archives/upload.zip",
        )
    )
    s.commit()
    s.close()

    from app.services import bug_fix_context
    monkeypatch.setattr(bug_fix_context, "recover_legacy", lambda *args: {"snapshot_id": "test-snapshot"})
    captured = {}

    def _prepare(**kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(bf, "prepare", _prepare)
    monkeypatch.setattr(bf, "cleanup", lambda ctx: None)

    class _FakeAgent:
        def run_sync(self, ctx):
            return {"status": "failed", "error_kind": "x", "merge_requests": []}

    monkeypatch.setattr(bf, "BugFixCodingAgent", _FakeAgent)

    task_id = _seed(Session)
    bf.run_bug_fix_task.run(task_id)

    assert captured["source_log_archive_path"] == "/data/archives/upload.zip"
    assert captured["source_log_filename"] == "device-logs.zip"


def test_run_bug_fix_task_skips_finished_task(monkeypatch, session_factory):
    """acks_late 重投场景：已终态任务不得重复执行 Agent / 重复提 MR。"""
    from app.tasks import bug_fix as bf

    Session = session_factory
    monkeypatch.setattr(bf, "SessionLocal", Session)

    def _boom(**kw):
        raise AssertionError("prepare must not be called for a finished task")

    monkeypatch.setattr(bf, "prepare", _boom)

    task_id = _seed(Session)
    s = Session()
    task = s.get(BugFixTask, task_id)
    task.status = BugFixTaskStatus.SUCCEEDED
    s.commit()
    s.close()

    result = bf.run_bug_fix_task.run(task_id)
    assert result["status"] == "skipped"

    s = Session()
    task = s.get(BugFixTask, task_id)
    assert task.status == BugFixTaskStatus.SUCCEEDED
    assert s.query(BugFixMergeRequest).filter_by(task_id=task_id).count() == 0
    s.close()


def test_snapshot_to_worker_preserves_context_and_rejected_review(tmp_path, monkeypatch, session_factory):
    import json
    import shutil
    import subprocess
    from types import SimpleNamespace
    import claude_agent_sdk
    from app.config import settings
    from app.agents import anthropic_client
    from app.services.bug_fix_context import capture_source
    from app.services.chat_image_store import StoredImage
    from app.tasks import bug_fix as bf
    monkeypatch.setattr(settings, 'runtime_settings_path', str(tmp_path/'runtime.json'))
    monkeypatch.setattr(settings, 'code_repo_clone_base_dir', str(tmp_path/'workspaces'))
    monkeypatch.setattr(settings, 'bug_fix_context_dir', str(tmp_path/'evidence'))
    monkeypatch.setattr(settings, 'bug_fix_agent_provider', 'anthropic')
    monkeypatch.setattr(settings, 'bug_fix_agent_api_key', 'dedicated-test-key')
    monkeypatch.setattr(settings, 'bug_fix_agent_model', 'advanced-review-model')
    repo_path=tmp_path/'source-repo'
    subprocess.run(['git', 'init', '-b', 'main', str(repo_path)], check=True, capture_output=True)
    (repo_path/'enum.py').write_text('FEED_USER = 1\n')
    subprocess.run(['git','-C',str(repo_path),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo_path),'-c','user.name=Test','-c','user.email=test@example.com','commit','-m','baseline'],check=True,capture_output=True)
    source=tmp_path/'analysis'
    (source/'logs/a').mkdir(parents=True)
    (source/'logs/b').mkdir()
    (source/'logs/a/test.log').write_text('a evidence')
    (source/'logs/b/test.log').write_text('b evidence')
    (source/'task.json').write_text(json.dumps({'question':'original question + OCR','hints':'history'}))
    image=tmp_path/'screen.png';image.write_bytes(b'original-image')
    context=capture_source(SimpleNamespace(temp_dir=source,task_json_path=source/'task.json'),images=[StoredImage('i','image/png','screen',14,str(image))])
    session=session_factory()
    repo=ProjectRepo(project_code='snapshot',project_name='Snapshot',project_card='Snapshot fixture',repo_url=str(repo_path),default_branch='main',enabled=True)
    session.add(repo);session.flush()
    task=bug_fix_service.create_task_from_analysis(session,project_repo_id=repo.id,
        analysis_result={'status':'ok','proposed_fixes':[{'title':'unnecessary change'}]}, source_context=context)
    session.commit();task_id=task.id;session.close()
    shutil.rmtree(source);image.unlink()
    seen={}
    def options(**kwargs):
        seen.update(kwargs)
        return object()
    async def query(**kwargs):
        workspace=__import__('pathlib').Path(seen['cwd'])
        assert (workspace/'logs/a/test.log').read_text()=='a evidence'
        assert (workspace/'logs/b/test.log').read_text()=='b evidence'
        assert (workspace/'images/screen.png').read_bytes()==b'original-image'
        assert json.loads((workspace/'source_analysis.json').read_text())['task']['hints']=='history'
        yield SimpleNamespace(content='{"status":"succeeded","merge_requests":[],"fix_outcomes":[{"fix_index":1,"outcome":"rejected","reason":"enum.py and original logs show correct behavior"}]}')
    monkeypatch.setattr(bf,'SessionLocal',session_factory)
    monkeypatch.setattr(anthropic_client,'build_options',options)
    monkeypatch.setattr(claude_agent_sdk,'query',query)
    bf.run_bug_fix_task.run(task_id)
    session=session_factory();task=session.get(BugFixTask,task_id)
    assert task.status=='succeeded'
    assert task.model=='advanced-review-model'
    assert json.loads(task.fix_outcomes_json)[0]['outcome']=='rejected'
    assert session.query(BugFixMergeRequest).count()==0
    assert not __import__('pathlib').Path(seen['cwd']).exists()
    session.close()
