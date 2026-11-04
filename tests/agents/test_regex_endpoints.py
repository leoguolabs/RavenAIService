"""Verify exact pages survive optional regex replay and its failure boundaries."""
import asyncio
import copy
import json

import pytest
from app.agents import evidence_tools as tools
from app.agents import regex_endpoints as endpoint


@pytest.fixture
def workspace(tmp_path):
    (tmp_path/'logs').mkdir()
    (tmp_path/'logs/state.log').write_text('x=ready\n'*40+'x=終止\n')
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize('query', [
    {'path':'logs','pattern':r'(?<=x=)(ready|終止)','limit':2},
    {'path':'logs','pattern':'X=.*','ignore_case':True,'glob':'*.log','offset':10,'limit':2},
    {'path':'logs/state.log','pattern':'x=','literal':True,'limit':2},
])
async def test_original_page_identical_and_exact_last_row_added(workspace, query):
    original=await tools.run_batch(str(workspace),[query],search=True)
    enabled=await tools.run_batch(str(workspace),[query],search=True,regex_endpoints=True)
    saved=copy.deepcopy(enabled)
    extra=enabled['results'][0].pop('regex_endpoints')
    assert enabled==original
    assert any(r['line']==41 and r['text']=='x=終止' for r in extra['rows'])
    assert extra['matching_lines_total']==41
    assert extra['event_history_complete'] is False
    assert all(len(r['source_sha256'])==64 for r in extra['rows'])
    assert json.loads(tools.encode_evidence(saved))['results'][0]['regex_endpoints']==extra


@pytest.mark.asyncio
async def test_scope_budget_falls_back_and_source_search_is_unmodified(workspace,monkeypatch):
    query={'path':'logs','pattern':'x=.*','limit':2}
    original=await tools.run_batch(str(workspace),[query],search=True)
    monkeypatch.setattr(endpoint,'MAX_SOURCE_BYTES',1)
    assert await tools.run_batch(str(workspace),[query],search=True,regex_endpoints=True)==original


@pytest.mark.asyncio
async def test_changed_source_discards_supplement_preserves_page(workspace,monkeypatch):
    original_replay=endpoint.RegexEndpointBatch.replay
    async def mutate(self,*args):
        result=await original_replay(self,*args)
        (workspace/'logs/state.log').write_text('changed\n')
        return result
    monkeypatch.setattr(endpoint.RegexEndpointBatch,'replay',mutate)
    query={'path':'logs','pattern':'x=.*','limit':2}
    original=await tools.run_batch(str(workspace),[query],search=True)
    assert await tools.run_batch(str(workspace),[query],search=True,regex_endpoints=True)==original


@pytest.mark.asyncio
async def test_timeout_preserves_original_and_siblings(workspace,monkeypatch):
    async def timeout(*args):raise asyncio.TimeoutError()
    monkeypatch.setattr(endpoint.RegexEndpointBatch,'replay',timeout)
    requests=[{'path':'logs','pattern':'x=.*','limit':2},{'path':'logs','pattern':'no match'}]
    original=await tools.run_batch(str(workspace),requests,search=True)
    assert await tools.run_batch(str(workspace),requests,search=True,regex_endpoints=True)==original


@pytest.mark.asyncio
async def test_cancelled_replay_reaps_child(workspace,monkeypatch):
    import sys
    real=asyncio.create_subprocess_exec
    children=[]
    async def spawn(*args,**kwargs):
        p=await real(*args,**kwargs);children.append(p);return p
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(tools,'search_arguments',lambda *args:[sys.executable,'-c','import time;time.sleep(30)'])
    batch=endpoint.RegexEndpointBatch(workspace)
    batch.deadline=__import__('time').monotonic()+10
    task=asyncio.create_task(batch.replay({'pattern':'x'}, {'matches':[]}, {}))
    for _ in range(100):
        if children:break
        await asyncio.sleep(.01)
    assert children
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert all(p.returncode is not None for p in children)


@pytest.mark.asyncio
async def test_output_budget_is_shared_and_original_page_is_not_trimmed(workspace,monkeypatch):
    monkeypatch.setattr(endpoint,'MAX_OUTPUT_BYTES',210)
    queries=[{'path':'logs','pattern':'x=.*','limit':2}]*3
    original=await tools.run_batch(str(workspace),queries,search=True)
    assert await tools.run_batch(str(workspace),queries,search=True,regex_endpoints=True)==original


@pytest.mark.asyncio
async def test_symlink_scope_reverts_to_original_search(workspace):
    (workspace/'logs/link.log').symlink_to(workspace/'logs/state.log')
    queries=[{'path':'logs','pattern':'x=.*','limit':2}]
    original=await tools.run_batch(str(workspace),queries,search=True)
    assert await tools.run_batch(str(workspace),queries,search=True,regex_endpoints=True)==original
