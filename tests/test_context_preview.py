import json
from types import SimpleNamespace

from labeler.context_preview import Preview, parse_proposal, strip_context_hashtags
from labeler.notes import Note


def test_preview_save_isolated_and_conflict_protected(tmp_path):
    note = Note('n1', '求助', '大家帮帮我', (), {})
    p = Preview([(note, {})], tmp_path / 'preview', workers=1)
    p.write('n1', {'context': '请你帮帮我', 'approved': False, 'revision': 1})
    client = p.app.test_client()
    assert client.post('/api/save', json={'note_id': 'n1', 'context': '新内容', 'revision': 0}).status_code == 409
    assert client.post('/api/save', json={'note_id': 'n1', 'context': '新内容', 'revision': 1, 'approved': True}).status_code == 200
    assert p.read('n1')['context'] == '新内容'
    assert client.get('/api/export').json['records'][0]['approved'] is True
    assert client.post('/api/save', json={'note_id': '../bad', 'context': 'x'}).status_code == 400
    assert list(tmp_path.iterdir()) == [tmp_path / 'preview']


def test_not_needed_can_be_set_before_generation_and_exported(tmp_path):
    note = Note('n1', '纯资讯', '没有提问', (), {})
    p = Preview([(note, {})], tmp_path / 'preview', workers=1)
    client = p.app.test_client()
    assert client.post('/api/not-needed', json={
        'note_id': 'n1', 'revision': None, 'not_needed': True}).status_code == 200
    record = p.read('n1')
    assert record['not_needed'] is True and record['context'] == ''
    assert client.get('/api/export').json['records'][0]['not_needed'] is True
    assert p.queue(['n1']) == 0
    assert client.post('/api/not-needed', json={
        'note_id': 'n1', 'revision': record['revision'], 'not_needed': False}).status_code == 200
    assert p.queue(['n1']) == 1


def test_parser_preserves_long_original_style():
    context = '我真的不知道怎么办，想了很久还是很纠结。' * 200
    result = parse_proposal(json.dumps({'context': context, 'changes': ['调整称呼'], 'needs_review': False}))
    assert result['context'] == context


def test_context_hashtags_are_removed_everywhere():
    text = '研究生写论文 #研究生 #sci #sci期刊 #sci发表，但 C# 课程保留。'
    assert strip_context_hashtags(text) == '研究生写论文，但 C# 课程保留。'
    parsed = parse_proposal(json.dumps({
        'context': '准备投稿 #sci期刊 #论文', 'needs_review': False}))
    assert parsed['context'] == '准备投稿'


def test_context_can_be_empty_and_marked_not_needed(tmp_path):
    note = Note('n1', '直接问题', '怎么做？', (), {})
    p = Preview([(note, {})], tmp_path / 'preview', workers=1)
    p.write('n1', {'context': '背景', 'approved': False, 'revision': 1})
    client = p.app.test_client()
    assert client.post('/api/context-not-needed', json={
        'note_id': 'n1', 'revision': 1, 'context_not_needed': True}).status_code == 200
    marked = p.read('n1')
    assert marked['context'] == '' and marked['context_not_needed'] is True
    assert marked['context_backup'] == '背景'
    assert client.post('/api/save', json={
        'note_id': 'n1', 'revision': marked['revision'], 'context': '',
        'approved': True}).status_code == 200
    saved = p.read('n1')
    assert saved['approved'] is True and saved['context'] == ''
    assert client.post('/api/context-not-needed', json={
        'note_id': 'n1', 'revision': saved['revision'],
        'context_not_needed': False}).status_code == 200
    assert p.read('n1')['context'] == '背景'


def test_query_edit_confirm_and_not_needed_roundtrip(tmp_path):
    note = Note('n1', '标题', '正文', (), {})
    p = Preview([(note, {})], tmp_path / 'preview', workers=1)
    client = p.app.test_client()
    assert client.post('/api/query/save', json={
        'note_id': 'n1', 'revision': None, 'query': '我该怎么办？',
        'approved': True}).status_code == 200
    saved = p.read('n1')
    assert saved['query'] == '我该怎么办？' and saved['query_approved'] is True
    exported = client.get('/api/export').json['records']
    assert exported[0]['query'] == '我该怎么办？'
    assert client.post('/api/query/not-needed', json={
        'note_id': 'n1', 'revision': saved['revision'],
        'query_not_needed': True, 'original_query': '旧问题'}).status_code == 200
    marked = p.read('n1')
    assert marked['query'] == '' and marked['query_backup'] == '我该怎么办？'
    assert client.post('/api/query/not-needed', json={
        'note_id': 'n1', 'revision': marked['revision'],
        'query_not_needed': False}).status_code == 200
    restored = p.read('n1')
    assert restored['query'] == '我该怎么办？' and restored['query_not_needed'] is False


def test_knowledge_candidate_path_save_and_mark(tmp_path, monkeypatch):
    note = Note('n1', '考研规划', '备考经验', (), {})
    tax = SimpleNamespace(scenarios=[
        SimpleNamespace(id='S1', path='升学 > 考研 > 备考规划'),
        SimpleNamespace(id='S2', path='就业 > 求职流程 > 简历'),
    ])
    task = [(note, {'annotation': {'scenarios': [{'id': 'S1'}]}})]
    p = Preview(task, tmp_path / 'preview', taxonomy=tax, workers=1)
    client = p.app.test_client()
    path = '升学/考研/备考规划.md'
    assert path in client.get('/api/state').json['knowledge_paths']
    class FakeClient:
        def chat(self, *args, **kwargs):
            return json.dumps({'paths': [path, '就业/求职流程/简历.md'],
                               'reason': '内容同时包含考研规划和简历经验',
                               'needs_review': False}, ensure_ascii=False)
    monkeypatch.setattr('labeler.context_preview.make_client', lambda *a, **k: FakeClient())
    judged = client.post('/api/knowledge/judge', json={
        'note_id': 'n1', 'revision': None, 'content': '每天复习并复盘'}).json
    assert judged['paths'] == [path, '就业/求职流程/简历.md']
    revision = p.read('n1')['revision']
    assert client.post('/api/knowledge/save', json={
        'note_id': 'n1', 'revision': revision, 'content': '每天复习并复盘',
        'paths': judged['paths'], 'approved': True}).status_code == 200
    saved = p.read('n1')
    assert saved['knowledge_approved'] is True
    assert saved['knowledge_paths'] == [path, '就业/求职流程/简历.md']
    assert client.post('/api/knowledge/not-needed', json={
        'note_id': 'n1', 'revision': saved['revision'],
        'knowledge_not_needed': True}).status_code == 200
    assert p.read('n1')['knowledge_not_needed'] is True
