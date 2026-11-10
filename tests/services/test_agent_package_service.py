"""Agent-authored artifact validation and lifecycle regression tests."""
import asyncio
import hashlib
import io
import json
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.package_search.workspace import prepare
from app.config import settings
from app.services.agent_package_service import AgentPackageError, validate_agent_package
from app.services.package_search_chat_service import AgentJob, PackageSearchChatService


def write_archive(path, ini, payload=b'component', extra=None):
    with tarfile.open(path, 'w:gz') as tar:
        for name, data in [('si.ini', ini.encode()), ('new-payload', payload)]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if extra:
            info = tarfile.TarInfo(extra)
            info.type = tarfile.SYMTYPE
            info.linkname = '/etc/passwd'
            tar.addfile(info)


@pytest.fixture
def package_case(tmp_path):
    source = tmp_path / 'source.bin'
    source.write_bytes(b'component')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    inputs = [{'upload_id': 'one', 'path': str(source), 'original_name': source.name,
               'size': source.stat().st_size, 'sha256': digest}]
    plan = {
        'project_code': 'project-not-in-catalog', 'version': '1.1.0.0', 'mode': 'full',
        'packet_attr': 4242, 'publisher': 'GalaxySpace', 'evidence': ['repo/updater.c:12 PacketAttr=4242;'],
        'components': [{'upload_id': 'one', 'component_key': 'new-type', 'output_name': 'new-payload',
                        'file_attr': 909, 'version': '1.2.30.12.202609170917', 'sha256': digest,
                        'evidence': ['repo/updater.c:14 ID=909;']}], 'excluded_inputs': [],
    }
    ini = ('Packet_Ver=V1.1.0.0;\nPacketAttr=4242;\nPublisher=GalaxySpace;\nFileNumInPacket=1;\n'
           'FileName_1=new-payload;\nFileAttr_1=909;\nFileVer_1=V1.2.30.12.202609170917;\n')
    plan_path = tmp_path / 'review.json'
    plan_path.write_text(json.dumps(plan))
    archive = tmp_path / 'release.tgz'
    write_archive(archive, ini)
    projects = [{'id': 8, 'project_code': plan['project_code'], 'project_name': 'New project'}]
    return SimpleNamespace(root=tmp_path, plan=plan, plan_path=plan_path, archive=archive,
                           inputs=inputs, projects=projects, ini=ini)


def validate(case):
    return validate_agent_package(case.root, 'review.json', 'release.tgz', inputs=case.inputs, projects=case.projects)


def test_unknown_business_ids_and_timestamp_versions_do_not_require_catalog(package_case):
    result = validate(package_case)
    assert result['components'][0]['version'] == '1.2.30.12.202609170917'
    assert result['packet_attr'] == 4242
    assert result['version'] == '1.1.0.0'
    assert result['execution'] == 'agent'


@pytest.mark.parametrize('status', ['cancelled', 'error'])
def test_empty_terminal_answer_has_visible_recovery_message(status):
    answer = PackageSearchChatService._format_agent_result(
        {'status': status, 'answer': '', 'packaging': True, 'error_kind': 'upstream'},
        question='package', context_meta={},
    )
    assert '## 配置管理员说明' in answer
    assert '当前对话' in answer
    assert '已完成本轮打包' not in answer
    if status == 'cancelled':
        assert '未发布整包' in answer


@pytest.mark.parametrize('fault', ['payload', 'ini', 'symlink', 'missing_input', 'duplicate_id', 'input_tamper', 'path_escape'])
def test_invalid_artifacts_are_rejected(package_case, fault):
    c = package_case
    if fault == 'payload':
        write_archive(c.archive, c.ini, b'changed')
    elif fault == 'ini':
        write_archive(c.archive, c.ini.replace('FileAttr_1=909', 'FileAttr_1=301'))
    elif fault == 'symlink':
        write_archive(c.archive, c.ini, extra='link')
    elif fault == 'missing_input':
        c.inputs.append({**c.inputs[0], 'upload_id': 'two'})
    elif fault == 'duplicate_id':
        c.plan['components'].append(dict(c.plan['components'][0]))
        c.plan_path.write_text(json.dumps(c.plan))
    elif fault == 'input_tamper':
        Path(c.inputs[0]['path']).write_bytes(b'changed')
    elif fault == 'path_escape':
        c.archive.unlink()
        c.archive.symlink_to(c.plan_path)
    with pytest.raises((AgentPackageError, ValueError)):
        validate(c)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['exception', 'error_result', 'cancelled'])
async def test_terminal_packaging_failure_is_persisted_and_reopenable(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(settings, 'code_repo_clone_base_dir', str(tmp_path / 'work'))
    ctx = prepare(question='package', session_id='failure-session')
    service = PackageSearchChatService()
    service._persist_job_result = AsyncMock()
    service._finalize_chat_run = AsyncMock()
    service._touch_context = lambda *args, **kwargs: None
    def run(*args):
        if outcome == 'exception':
            raise ValueError('bad release version')
        return {'status': 'cancelled' if outcome == 'cancelled' else 'error',
                'answer': 'cancelled' if outcome == 'cancelled' else 'provider failed', 'error_kind': 'upstream'}
    monkeypatch.setattr('app.agents.package_search.agent.PackageSearchAgent.run_sync', run)
    job = AgentJob(session_id='failure-session', task_id=ctx.task_id, context_meta={},
                   question='package', user_id='u', remember=True, started_at=time.monotonic(),
                   run_id='r', input_manifest={'inputs': [{'upload_id': 'one'}]})
    await service._run_job_async(job, ctx)
    service._persist_job_result.assert_awaited_once()
    assert job.answer
    assert job.done
    if outcome != 'cancelled':
        assert job.error
    service._finalize_chat_run.assert_awaited_once_with(job)


@pytest.mark.asyncio
async def test_workspace_failure_still_terminates_run(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'code_repo_clone_base_dir', str(tmp_path / 'work'))
    ctx = prepare(question='package', session_id='disk-failure')
    service = PackageSearchChatService()
    service._persist_job_result = AsyncMock(side_effect=OSError('disk full'))
    service._finalize_chat_run = AsyncMock()
    monkeypatch.setattr(service, '_revoke_packaging_authority', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk full')))
    monkeypatch.setattr('app.agents.package_search.agent.PackageSearchAgent.run_sync',
                        lambda *a: {'status': 'error', 'answer': 'failed'})
    job = AgentJob(session_id='disk-failure', task_id=ctx.task_id, context_meta={},
                   question='package', user_id='u', remember=True, started_at=time.monotonic(),
                   input_manifest={'inputs': [{'upload_id': 'one'}]})
    await service._run_job_async(job, ctx)
    assert job.done and job.finished_at
    assert 'packaging_publisher' not in ctx.metadata
    service._finalize_chat_run.assert_awaited_once_with(job)


@pytest.mark.asyncio
async def test_agent_invokes_publication_tool_and_records_real_model(package_case, monkeypatch):
    c = package_case
    monkeypatch.setattr(settings, 'code_repo_clone_base_dir', str(c.root / 'work'))
    monkeypatch.setattr(settings, 'disk_reserve_bytes', 0)
    ctx = SimpleNamespace(temp_dir=str(c.root), task_json_path=str(c.root / 'task.json'),
                          metadata={}, task_id='task', project_code='')
    Path(ctx.task_json_path).write_text('{}')
    service = PackageSearchChatService()
    service._persist_job_result = AsyncMock()
    service._finalize_chat_run = AsyncMock()
    service._touch_context = lambda *args, **kwargs: None
    service._save_context = lambda *args, **kwargs: None
    async def confirm(job, ctx, **kwargs):
        assert kwargs['review']['project_name'] == 'New project'
        assert kwargs['review']['components'][0]['name'] == 'new-type'
        assert kwargs['review']['warnings'] == []
        return [{'selected_labels': ['确认 / Confirm'], 'question_index': 0}]
    service._request_packaging_confirmation = confirm
    from app.services.raven_package_service import raven_package_service
    monkeypatch.setattr(raven_package_service, 'uploads_dir', c.root / 'published')
    monkeypatch.setattr(raven_package_service, 'metadata_file', c.root / 'index.json')
    monkeypatch.setattr(raven_package_service, 'data_dir', c.root)
    def run(self, ctx, *args):
        async def invoke():
            first = await ctx.metadata['packaging_publisher']('review.json', 'release.tgz')
            second = await ctx.metadata['packaging_publisher']('review.json', 'release.tgz')
            assert first['artifact']['package_id'] == second['artifact']['package_id']
        asyncio.run(invoke())
        return {'status': 'ok', 'answer': 'Agent built package', 'model': 'real-agent', 'engine': 'claude-agent-sdk'}
    monkeypatch.setattr('app.agents.package_search.agent.PackageSearchAgent.run_sync', run)
    job = AgentJob(session_id='s', task_id='task', context_meta={}, question='build', user_id='u',
                   remember=True, started_at=time.monotonic(), run_id='r',
                   input_manifest={'inputs': c.inputs}, project_catalog=c.projects)
    await service._run_job_async(job, ctx)
    assert job.error is None
    assert job.result['model'] == 'real-agent'
    assert len(job.artifacts) == 1
    assert list((c.root / 'published').glob('*.tgz'))


@pytest.mark.asyncio
async def test_changed_artifact_during_confirmation_never_publishes(package_case, monkeypatch):
    c = package_case
    service = PackageSearchChatService()
    ctx = SimpleNamespace(temp_dir=str(c.root))
    async def confirm(*args, **kwargs):
        write_archive(c.archive, c.ini, b'changed')
        return [{'selected_labels': ['确认 / Confirm']}]
    service._request_packaging_confirmation = confirm
    job = AgentJob(session_id='s', task_id='t', context_meta={}, question='build', user_id='u',
                   remember=True, started_at=time.monotonic(), run_id='r',
                   input_manifest={'inputs': c.inputs}, project_catalog=c.projects)
    with pytest.raises(AgentPackageError):
        await service._publish_agent_package(job, ctx, 'review.json', 'release.tgz', lambda e: None)
    assert not job.artifacts
