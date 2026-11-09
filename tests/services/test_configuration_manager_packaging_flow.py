"""Real review broker: Agent-authored artifact -> confirmation -> publication."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.package_search_chat_service import AgentJob, PackageSearchChatService
from app.services.chat_run_service import chat_run_service
from .test_agent_package_service import package_case  # shared realistic wire artifact fixture


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['confirm', 'cancel', 'custom', 'owner_cancel'])
async def test_agent_artifact_requires_complete_broker_review(package_case, monkeypatch, decision):
    c = package_case
    monkeypatch.setattr(settings, 'disk_reserve_bytes', 0)
    from app.services.raven_package_service import raven_package_service
    monkeypatch.setattr(raven_package_service, 'uploads_dir', c.root / 'published')
    monkeypatch.setattr(raven_package_service, 'metadata_file', c.root / 'index.json')
    monkeypatch.setattr(raven_package_service, 'data_dir', c.root)
    ctx = SimpleNamespace(temp_dir=str(c.root), task_json_path=str(c.root / 'task.json'),
                          metadata={}, task_id='task', project_code='')
    Path(ctx.task_json_path).write_text('{}')
    service = PackageSearchChatService()
    service.registry_dir = c.root / 'registry'
    job = AgentJob(session_id='s', task_id='task', context_meta={}, question='build', user_id='u',
                   remember=False, started_at=time.monotonic(), run_id='r', owner_scope='user:u',
                   input_manifest={'inputs': c.inputs}, project_catalog=c.projects)
    service._jobs['s'] = job
    trace = []
    runner = asyncio.create_task(service._publish_agent_package(job, ctx, 'review.json', 'release.tgz', trace.append))
    try:
        for _ in range(200):
            request = next((e for e in trace if e.get('type') == 'clarification_request'), None)
            if request:
                break
            await asyncio.sleep(.005)
        assert request
        assert not (c.root / 'index.json').exists()
        assert len(request['questions']) == 2
        assert '1.2.30.12.202609170917' in request['questions'][1]['question']
        # A second connection obtains the same pending card through trace replay.
        restored = chat_run_service._derive_pending_clarifications(trace)
        assert restored[0]['request_id'] == request['request_id']
        with pytest.raises(PermissionError):
            service.assert_session_access('s', owner_scope='user:other', user=SimpleNamespace(id='other'))
        if decision == 'owner_cancel':
            assert service.cancel('s', user=SimpleNamespace(id='u'))
        else:
            answers = [{'question_index': i, 'selected_labels': ['取消 / Cancel' if decision == 'cancel' else '确认 / Confirm']}
                       for i in range(2)]
            if decision == 'custom':
                answers[0] = {'question_index': 0, 'selected_labels': [], 'custom_text': 'other-project'}
            broker = chat_run_service.get_broker_by_run_id('r')
            assert broker.resolve(request['request_id'], {'answers': answers})
        if decision == 'confirm':
            result = await asyncio.wait_for(runner, timeout=3)
            artifact = result['artifact']
            assert artifact['download_url'].startswith('/raven/api/download/')
            assert raven_package_service.get_package(artifact['package_id'])
        else:
            with pytest.raises(Exception):
                await asyncio.wait_for(runner, timeout=3)
            assert not (c.root / 'index.json').exists()
            assert not job.artifacts
    finally:
        if not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
