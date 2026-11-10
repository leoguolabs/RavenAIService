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
@pytest.mark.parametrize('decision', ['confirm', 'confirm_with_warning', 'cancel', 'custom', 'owner_cancel'])
async def test_agent_artifact_requires_complete_broker_review(package_case, monkeypatch, decision):
    c = package_case
    c.inputs.append({**c.inputs[0], 'upload_id': 'reference', 'original_name': 'reference.bin'})
    c.plan['excluded_inputs'] = [{'upload_id': 'reference', 'reason': 'Reference only'}]
    warnings = ['The optional firmware is absent. Confirm publishing the software-only package.'] if decision == 'confirm_with_warning' else []
    if warnings:
        c.plan['publication_warnings'] = warnings
    c.plan_path.write_text(json.dumps(c.plan))
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
        question, = request['questions']
        assert '1.2.30.12.202609170917' in question['question']
        assert 'repo/updater.c' not in json.dumps(request)
        assert 'FileAttr' not in question['question']
        review = request['publication_review']
        assert review['components'] == [{'name': 'new-type', 'version': '1.2.30.12.202609170917'}]
        assert review['warnings'] == warnings
        assert review['exclusions'] == [{'name': 'reference.bin', 'reason': 'Reference only'}]
        # A second connection obtains the same pending card through trace replay.
        restored = chat_run_service._derive_pending_clarifications(trace)
        assert restored[0]['request_id'] == request['request_id']
        assert restored[0]['publication_review'] == review
        with pytest.raises(PermissionError):
            service.assert_session_access('s', owner_scope='user:other', user=SimpleNamespace(id='other'))
        if decision == 'owner_cancel':
            assert service.cancel('s', user=SimpleNamespace(id='u'))
        else:
            answers = [{'question_index': 0, 'selected_labels': ['取消 / Cancel' if decision == 'cancel' else '确认 / Confirm']}]
            if decision == 'custom':
                answers[0] = {'question_index': 0, 'selected_labels': [], 'custom_text': 'other-project'}
            broker = chat_run_service.get_broker_by_run_id('r')
            assert broker.resolve(request['request_id'], {'answers': answers})
        if decision in {'confirm', 'confirm_with_warning'}:
            result = await asyncio.wait_for(runner, timeout=3)
            artifact = result['artifact']
            assert artifact['download_url'].startswith('/raven/api/download/')
            assert raven_package_service.get_package(artifact['package_id'])
            assert job.confirmed_plan['components'][0]['evidence'] == c.plan['components'][0]['evidence']
        else:
            with pytest.raises(Exception):
                await asyncio.wait_for(runner, timeout=3)
            assert not (c.root / 'index.json').exists()
            assert not job.artifacts
    finally:
        if not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
