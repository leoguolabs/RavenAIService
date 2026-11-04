"""Exact group-set semantics, with incomplete scans unable to claim absence."""
import hashlib

import pytest
from app.agents import evidence_statistics as stats


def query(**overrides):
    return dict(path='log', mode='compare', pattern=r'g=(?P<group>\w+) v=(?P<value>\w+)',
                left_group='before', right_group='after', **overrides)


def test_exact_set_diff_counts_duplicates_without_treating_them_as_new_values(tmp_path):
    raw=b'g=before v=a\ng=before v=a\ng=before v=b\ng=after v=b\ng=after v=c\ng=other v=z\n'
    (tmp_path/'log').write_bytes(raw)
    r=stats.scan_statistics(str(tmp_path),query());c=r['comparison']
    assert r['complete'] and c['complete'] and r['scanned_sha256']==hashlib.sha256(raw).hexdigest()
    assert c['left']['unique_values']==2 and c['left']['indexed_matches']==3
    assert c['right']['unique_values']==2 and c['common_unique_values']==1
    assert c['only_left']==[dict(value='a',count=2,first_line=1,last_line=2)]
    assert c['only_right']==[dict(value='c',count=1,first_line=5,last_line=5)]
    assert c['left']['first']['line']==1 and c['right']['last']['line']==5
    assert r['outside_selected_group_matches']==1


def test_missing_group_is_explicit_not_an_observed_empty_snapshot(tmp_path):
    (tmp_path/'log').write_text('g=before v=a\n')
    c=stats.scan_statistics(str(tmp_path),query())['comparison']
    assert c['complete'] and not c['right']['observed']
    assert c['right']['indexed_matches']==0 and c['only_left'][0]['value']=='a'


@pytest.mark.parametrize('budget', ['values','bytes','bad_utf8'])
def test_partial_scans_do_not_emit_set_absence_claims(tmp_path,monkeypatch,budget):
    raw=b'g=before v=a\ng=before v=b\ng=after v=c\n'
    if budget=='values':monkeypatch.setattr(stats,'MAX_COMPARE_VALUES',1)
    if budget=='bytes':monkeypatch.setattr(stats,'MAX_FILE_BYTES',15)
    if budget=='bad_utf8':raw+=b'\xff\n'
    (tmp_path/'log').write_bytes(raw)
    r=stats.scan_statistics(str(tmp_path),query());c=r['comparison']
    assert not r['complete'] and not c['complete']
    assert c['only_left'] is None and c['only_right'] is None and c['common_unique_values'] is None


@pytest.mark.parametrize('changes', [dict(right_group='before'),dict(left_group=''),
    dict(pattern=r'(?P<value>\w+)'),dict(pattern=r'(?P<group>\w+)')])
def test_invalid_identifiers_or_captures_are_rejected(tmp_path,changes):
    (tmp_path/'log').write_text('x')
    q=query();q.update(changes)
    with pytest.raises(ValueError):stats.scan_statistics(str(tmp_path),q)


@pytest.mark.asyncio
async def test_actual_isolated_worker_delivers_full_comparison(tmp_path):
    (tmp_path/'log').write_text('g=before v=旧\ng=after v=新\n')
    r=(await stats.run_statistics_batch(str(tmp_path),[query()]))['results'][0]
    assert r['complete'] and r['comparison']['only_left'][0]['value']=='旧'
    assert r['comparison']['only_right'][0]['value']=='新'
