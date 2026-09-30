import json

import pytest

from labeler.scenario_reaudit import (
    Reaudit,
    assemble,
    atomic_json,
    compare,
    pair_kind,
    parse_ai,
    parse_review,
    title_from_query,
)


def test_assembly_respects_manual_blanks_and_exclusions():
    assert assemble({'context': '背景', 'query': '问题'}, '旧问题')['utterance'] == '背景\n\n问题'
    assert assemble({'context': '背景', 'query': ''}, '旧问题')['utterance'] == '背景'
    assert assemble({'context': '背景', 'context_not_needed': True}, '问题')['utterance'] == '问题'
    assert assemble({'context': '背景', 'query_not_needed': True}, '问题')['utterance'] == '背景'
    assert not assemble({'not_needed': True, 'query': '问题'}, '')['eligible']
    assert not assemble({'query': '', 'context': ''}, '旧问题')['eligible']
    assert not assemble(None, '旧问题')['eligible']


def test_scene_comparison():
    a = {'source': 'A', 'status': 'keep', 'scenarios': ['S1', 'S2']}
    b = {'source': 'B', 'status': 'keep', 'scenarios': ['S2', 'S1']}
    assert pair_kind(a, b) == 'agree'
    assert pair_kind(a, None) == 'single'
    assert pair_kind(None, None) == 'none'
    ai = {'status': 'keep', 'scenarios': ['S2', 'S3']}
    assert compare(ai, a) == {'conflict': True, 'added': ['S3'], 'removed': ['S1'], 'status_changed': False}
    assert pair_kind({'status': 'drop', 'scenarios': ['S1']}, {'status': 'drop', 'scenarios': []}) == 'both_drop'


@pytest.mark.parametrize('value', [
    {'status': 'keep', 'scenarios': [], 'reason': '空'},
    {'status': 'keep', 'scenarios': ['unknown'], 'reason': '无效'},
    {'status': 'keep', 'scenarios': ['S1'], 'reason': ''},
    {'status': 'keep', 'scenarios': list('abcdef'), 'reason': '太多'},
])
def test_invalid_model_output(value):
    with pytest.raises(ValueError):
        parse_ai(json.dumps(value), {'S1', *'abcdef'})


def test_isolated_generation_and_versioned_final(tmp_path, monkeypatch):
    directory = tmp_path / 'new'
    original = tmp_path / 'old.json'
    original.write_text('untouched')
    row = {'note_id': 'n1', 'title': '测试', 'utterance': '新背景\n\n新问题', 'eligible': True,
               'input_sig': 'hash', 'source_flags': {'context_not_needed': True}, 'references': {'A': {'source': 'A', 'status': 'keep', 'scenarios': ['OLD']}}}
    excluded = dict(row, note_id='excluded', eligible=False)
    snapshot = {'rows': [row, excluded], 'taxonomy': [{'id': 'S1', 'path': '一级 > 二级 > 三级', 'definition': '定义'}],
                    'default_pair': ['A', 'B'], 'sources': ['A', 'B'], 'model': 'test', 'created_at': 'time'}
    atomic_json(directory / 'snapshot.json', snapshot)
    class Client:
        def chat(self, system, payload, **kwargs):
            data = json.loads(payload)
            assert data['用户发言'] == row['utterance']
            assert 'OLD' not in payload and 'references' not in payload
            assert data['输入说明']['context已移除'] is True
            return json.dumps({'title': '根据新问题生成的具体摘要', 'status': 'keep', 'scenarios': ['S1'], 'reason': '新文本依据'})
    monkeypatch.setattr('labeler.scenario_reaudit.make_client', lambda *a, **k: Client())
    audit = Reaudit(directory, 1)
    try:
        audit.run('n1')
        assert audit.result('n1')['ok']
        assert audit.queue(['n1']) == 0
        client = audit.app.test_client()
        state = client.get('/api/state').get_json()
        assert state['conflicts'] == 1 and state['counts']['done'] == 1
        assert state['total'] == 1
        assert state['rows'][0]['title'] == '根据新问题生成的具体摘要'
        assert '已移除context' in state['rows'][0]['change_hint']
        assert len(client.get('/api/export').get_json()['rows']) == 1
        body = {'note_id': 'n1', 'operator': '测试人', 'status': 'keep', 'scenarios': ['S1'], 'revision': 0}
        assert client.post('/api/final', json=body).status_code == 200
        assert audit.decision('n1')['utterance'] == row['utterance']
        assert audit.decision('n1')['utterance_modified'] is False
        assert client.post('/api/final', json=body).status_code == 409
        body['revision'] = 1
        assert client.post('/api/final', json=body).status_code == 200
        assert audit.decision('n1')['revision'] == 2
        assert (directory / 'history/n1.jsonl').exists()
        assert json.loads((directory / 'snapshot.json').read_text(encoding='utf-8')) == snapshot
        assert original.read_text() == 'untouched'
        body.update(revision=2, status='drop')
        assert client.post('/api/final', json=body).status_code == 200
        assert client.get('/api/state').get_json()['rows'][0]['final']['status'] == 'drop'
        assert client.get('/api/export').get_json()['rows'][0]['final']['status'] == 'drop'
        body.update(revision=3, status='scenario_insufficient', scenarios=[], reason='现有三级场景无法覆盖',
                    utterance='修改后的拼接内容')
        assert client.post('/api/final', json=body).status_code == 200
        assert audit.decision('n1')['status'] == 'scenario_insufficient'
        assert audit.decision('n1')['scenarios'] == []
        assert audit.decision('n1')['utterance'] == '修改后的拼接内容'
        assert audit.decision('n1')['utterance_modified'] is True
    finally:
        audit.pool.shutdown(wait=True)


def test_review_falls_back_to_query_title():
    result = parse_review(json.dumps({'status': 'keep', 'scenarios': ['S1'], 'reason': '依据'}), {'S1'}, '这是query开头的十八个字摘要')
    assert result['title'] == '这是query开头的十八个字摘要'
    assert result['title_fallback'] is True
    assert title_from_query('  第一行\n第二行，这是后续很长的内容文字用于截断') == '第一行 第二行，这是后续很长的内容文'


def test_auto_finalize_requires_retained_context_and_three_way_agreement(tmp_path):
    directory = tmp_path / 'auto'
    ref = {'source': 'human', 'status': 'keep', 'scenarios': ['S1']}
    rows = [
        {'note_id': 'yes', 'eligible': True, 'source_flags': {}, 'references': {'A': ref, 'B': ref}, 'input_sig': '1'},
        {'note_id': 'removed', 'eligible': True, 'source_flags': {'context_not_needed': True},
             'references': {'A': ref, 'B': ref}, 'input_sig': '2'},
        {'note_id': 'different', 'eligible': True, 'source_flags': {}, 'references': {'A': ref, 'B': ref}, 'input_sig': '3'},
    ]
    snapshot = {'rows': rows, 'taxonomy': [{'id': 'S1', 'path': '一 > 二 > 三', 'definition': ''}],
                    'default_pair': ['A', 'B'], 'sources': ['A', 'B'], 'model': 'test', 'created_at': 'time'}
    atomic_json(directory / 'snapshot.json', snapshot)
    for nid, scenes in [('yes', ['S1']), ('removed', ['S1']), ('different', [])]:
        atomic_json(directory / 'results_v2' / f'{nid}.json',
                    {'ok': True, 'title': '有效的测试问题摘要', 'status': 'keep' if scenes else 'insufficient',
                         'scenarios': scenes, 'reason': '依据'})
    audit = Reaudit(directory, 1)
    try:
        response = audit.app.test_client().post('/api/auto-finalize', json={'enabled': True}).get_json()
        assert response['finalized'] == 1
        assert audit.decision('yes')['automatic'] is True
        assert audit.decision('yes')['operator'] == '系统自动确认'
        assert audit.decision('removed') is None
        assert audit.decision('different') is None
    finally:
        audit.pool.shutdown(wait=True)
