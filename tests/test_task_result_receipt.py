"""Receipt projection through the existing result route, with synthetic inputs."""
import json
from pathlib import Path
from unittest import mock

import pytest

from api.task_store import TaskStore
from api.task_routes import handle_task_result
from test_background_tasks_3_4 import MockHandler


@pytest.fixture
def receipt_store(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / 'receipt-test.db')
    monkeypatch.setattr('api.task_routes.get_task_store', lambda: store)
    yield store
    store._conn.close()


def task(store, result='PASS', status='completed'):
    row = store.create_task('synthetic-session', 'PRIVATE_SYNTHETIC_PROMPT', 'synthetic-model', 'PRIVATE_SYNTHETIC_WORKSPACE')
    if status == 'completed':
        assert store.set_result(row['task_id'], result)
    elif status != 'queued':
        assert store.update_status(row['task_id'], status, error='PRIVATE_SYNTHETIC_ERROR')
    return row['task_id']


def get(task_id, **params):
    handler = MockHandler()
    handle_task_result(handler, {'task_id': task_id, **params})
    return handler.status, handler.response_json()


def test_default_payload_remains_unchanged(receipt_store):
    task_id = task(receipt_store)
    status, body = get(task_id)
    assert status == 200
    assert 'receipt' not in body
    assert set(body) == {'task_id', 'status', 'result', 'error', 'model', 'workspace', 'profile', 'duration', 'prompt', 'created_at', 'started_at', 'completed_at', 'tool_log'}
    assert body['result'] == 'PASS'


def test_opt_in_receipt_is_bound_to_actual_route_result(receipt_store):
    task_id = task(receipt_store)
    status, body = get(task_id, receipt='1')
    assert status == 200
    assert 'receipt' in body
    envelope = body['receipt']
    assert envelope['kind'] == 'received'
    assert envelope['reason'] == 'RECEIVED'
    assert envelope['accepted'] is False
    from api.result_receipt import project_result
    expected = project_result(body, expected_task_id=task_id, source_scope='zen-console-task-result')
    assert envelope['record'] == json.loads(expected.receipt)
    assert envelope['record']['task_id'] == task_id
    serialized = json.dumps(envelope)
    assert 'PRIVATE_SYNTHETIC_' not in serialized
    assert 'PASS' not in serialized


@pytest.mark.parametrize('status,kind', [('queued', 'pending'), ('running', 'pending'), ('failed', 'rejected'), ('cancelled', 'rejected')])
def test_non_completed_receipt_never_accepts(receipt_store, status, kind):
    task_id = task(receipt_store, status=status)
    _, body = get(task_id, receipt='1')
    assert body['status'] == status
    assert body['receipt']['kind'] == kind
    assert body['receipt']['accepted'] is False
    assert body['receipt']['record'] is None
    assert 'PRIVATE_SYNTHETIC_' not in json.dumps(body['receipt'])


@pytest.mark.parametrize('result', ['', 'x' * 65537, '\u754c' * 21846], ids=['empty', 'character-overflow', 'byte-overflow'])
def test_opt_in_rejects_unreceiptable_result_without_breaking_payload(receipt_store, result):
    task_id = task(receipt_store, result=result)
    status, body = get(task_id, receipt='1')
    assert status == 200 and body['result'] == result
    assert body['receipt']['kind'] == 'rejected'
    assert body['receipt']['record'] is None
    assert body['receipt']['accepted'] is False


def test_opt_in_is_explicit(receipt_store):
    task_id = task(receipt_store)
    for value in ('0', 'true', '', 'unexpected'):
        assert 'receipt' not in get(task_id, receipt=value)[1]


def test_unknown_task_preserves_error_response(receipt_store):
    status, body = get('missing-synthetic-task', receipt='1')
    assert status == 404 and 'receipt' not in body


def test_literal_receipt_vectors_and_prior_identity():
    from api.result_receipt import project_result
    fixtures = json.loads((Path(__file__).parent / 'fixtures/task_result_receipt.json').read_bytes())
    for vector in fixtures['golden']['vectors']:
        source = vector['result_descriptor']
        result = source['text'] if 'text' in source else source['repeat']['unit'] * source['repeat']['count']
        response = {'task_id': vector['expected_task_id'], 'status': 'completed', 'result': result, 'error': ''}
        outcome = project_result(response, expected_task_id=vector['expected_task_id'], source_scope=vector['source_scope'])
        assert outcome.receipt == vector['canonical_receipt_utf8'].encode('utf-8')
        assert outcome.accepted is False
        repeated = project_result(response, expected_task_id=vector['expected_task_id'], source_scope=vector['source_scope'], previous=outcome.receipt)
        assert repeated == outcome
        changed = dict(response, result=result + '\n')
        if len(changed['result'].encode('utf-8')) <= 65536:
            assert project_result(changed, expected_task_id=vector['expected_task_id'], source_scope=vector['source_scope'], previous=outcome.receipt).kind == 'conflict'


DATA = json.loads((Path(__file__).parent / 'fixtures/task_result_receipt.json').read_bytes())
GOLD = {v['id']: v for v in DATA['golden']['vectors']}
STAGES = {'snapshot_utf8_encode': '_encode_snapshot', 'previous_utf8_decode': '_decode_previous_utf8', 'previous_json_parse': '_parse_previous_json'}


def fixture_text(desc):
    return desc['text'] if 'text' in desc else desc['repeat']['unit'] * desc['repeat']['count']


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def fixture_previous(desc):
    if desc is None:
        return None
    kind = desc['kind']
    if kind == 'bytes_repeat':
        return desc['ascii'].encode('ascii') * desc['count']
    if kind == 'nested_arrays':
        return b'[' * desc['depth'] + b'0' + b']' * desc['depth']
    raw = GOLD[desc['vector']]['canonical_receipt_utf8'].encode('ascii')
    if kind == 'vector':
        return raw
    if kind == 'duplicate_key':
        return b'{"accepted":false,' + raw[1:]
    if kind == 'replace_field':
        return canonical(dict(json.loads(raw), **{desc['field']: desc['value']}))
    if kind == 'prefix_ascii':
        return desc['prefix'].encode('ascii') + raw
    if kind == 'prefix_hex':
        return bytes.fromhex(desc['hex']) + raw
    raise AssertionError('Unknown fixture descriptor')


def projection(result='PASS', previous=None, **changes):
    from api.result_receipt import project_result
    response = dict(task_id='task-001', status='completed', result=result, error='')
    response.update(changes)
    return project_result(response, expected_task_id='task-001', source_scope='synthetic-review', previous=previous)


@pytest.mark.parametrize('case', DATA['guards']['cases'], ids=lambda c: c['id'])
def test_normative_guard_order(case):
    import contextlib
    import api.result_receipt as receipt
    vector = GOLD[case['response_from']]
    response = {'task_id': vector['expected_task_id'], 'status': case['status_override'], 'error': '',
                'result': fixture_text(case['result_override'] or vector['result_descriptor'])}
    with contextlib.ExitStack() as stack:
        spies = {stage: stack.enter_context(mock.patch.object(receipt, name, wraps=getattr(receipt, name))) for stage, name in STAGES.items()}
        out = receipt.project_result(response, expected_task_id=vector['expected_task_id'], source_scope=vector['source_scope'], previous=fixture_previous(case['previous_descriptor']))
    assert (out.kind, out.reason, out.accepted) == (case['expected_kind'], case['expected_reason'], False)
    if out.kind != 'received':
        assert out.receipt is None and out.snapshot is None
    for stage in case['must_not_call']:
        assert spies[stage].call_count == 0


@pytest.mark.parametrize('field', ['task_id', 'status', 'result', 'error'])
def test_strict_response_field_types(field):
    for value in (None, True, 1, [], {}):
        assert projection(**{field: value}).reason == 'INVALID_RESPONSE'


@pytest.mark.parametrize('field', ['task_id', 'source_scope'])
def test_identity_does_not_allow_final_newline(field):
    from api.result_receipt import project_result
    response = dict(task_id='task-001', status='completed', result='PASS', error='')
    context = dict(expected_task_id='task-001', source_scope='synthetic-review')
    context['expected_task_id' if field == 'task_id' else field] += '\n'
    assert project_result(response, **context).reason == 'INVALID_CONTEXT'
    previous = json.loads(GOLD['G01']['canonical_receipt_utf8'])
    previous[field] += '\n'
    assert projection(previous=canonical(previous)).reason == 'INVALID_PREVIOUS'


def test_exact_byte_limits_and_unicode_failure():
    from api.result_receipt import project_result
    assert projection('x' * 65536).kind == 'received'
    assert projection('x' * 65537).reason == 'RESULT_TOO_LARGE'
    assert projection('\U0001f642' * 16384).kind == 'received'
    assert projection('\U0001f642' * 16385).reason == 'RESULT_TOO_LARGE'
    assert projection('\ud800').reason == 'INVALID_UNICODE'
    response = dict(task_id='t' * 64, status='completed', result='PASS', error='')
    out = project_result(response, expected_task_id='t' * 64, source_scope='s' * 64)
    assert out.kind == 'received' and len(out.receipt) <= 436


def test_prior_strict_types_fields_bytes_and_identity():
    valid = json.loads(GOLD['G01']['canonical_receipt_utf8'])
    mutations = [('accepted', True), ('accepted', 0), ('schema_version', True), ('schema_version', 1.0), ('schema_version', 2), ('snapshot_bytes', True), ('snapshot_bytes', 4.0), ('snapshot_bytes', 0), ('snapshot_bytes', 65537), ('snapshot_sha256', 'A' * 64), ('receipt_id', '0' * 64), ('encoding', 'UTF-8'), ('kind', 'other'), ('extra', 'field')]
    for field, value in mutations:
        assert projection(previous=canonical(dict(valid, **{field: value}))).reason == 'INVALID_PREVIOUS'
    for field in valid:
        data = dict(valid); del data[field]
        assert projection(previous=canonical(data)).reason == 'INVALID_PREVIOUS'
    for raw in (b'', b'null', b'[]', b'true', b'\xff', b'NaN', b'1e100', b'{', b' ' + canonical(valid), canonical(valid) + b'\n', json.dumps(valid, sort_keys=True).encode(), canonical(valid).replace(b'"schema_version":1', b'"schema_version":1e0'), 'text', bytearray(canonical(valid)), memoryview(canonical(valid)), {}):
        assert projection(previous=raw).reason == 'INVALID_PREVIOUS'
    assert projection(previous=GOLD['G04']['canonical_receipt_utf8'].encode()).reason == 'PREVIOUS_IDENTITY_MISMATCH'


def test_input_and_output_immutability_and_private_extras():
    import contextlib
    import dataclasses
    import io
    from api.result_receipt import project_result
    class Poison:
        def __repr__(self):
            raise AssertionError('Unknown response value traversed')
    response = dict(task_id='task-001', status='completed', result='PRIVATE_SYNTHETIC_CANARY', error='', prompt=Poison(), workspace=Poison(), tool_log=Poison())
    original = dict(response)
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        out = project_result(response, expected_task_id='task-001', source_scope='synthetic-review')
    assert response == original
    assert out.snapshot == b'PRIVATE_SYNTHETIC_CANARY'
    response['result'] = 'changed later'
    assert out.snapshot == b'PRIVATE_SYNTHETIC_CANARY'
    assert 'PRIVATE_SYNTHETIC_CANARY' not in repr(out)
    assert stdout.getvalue() == stderr.getvalue() == ''
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.accepted = True
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.snapshot = b'changed'


def test_default_route_does_not_call_projector(receipt_store):
    task_id = task(receipt_store)
    with mock.patch('api.result_receipt.project_result', side_effect=AssertionError('Default path called projector')) as call:
        assert get(task_id)[0] == 200
        assert get(task_id, receipt='0')[0] == 200
    call.assert_not_called()
