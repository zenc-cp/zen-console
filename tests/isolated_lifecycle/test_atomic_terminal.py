"""Focused private contract tests; no global server conftest or SDK import."""
import importlib.util
from pathlib import Path
import threading
import pytest

SOURCE = Path(__file__).resolve().parents[2] / 'api/task_store.py'
spec = importlib.util.spec_from_file_location('private_task_store', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

@pytest.fixture
def store(tmp_path):
    value = module.TaskStore(tmp_path / 'synthetic.db')
    yield value
    value._conn.close()

def new_task(store, state='running'):
    task = store.create_task('synthetic', 'SYNTHETIC', 'synthetic/model', 'synthetic')
    if state == 'running':
        assert store.claim_task(task['task_id'])
    elif state != 'queued':
        assert store.update_status(task['task_id'], state, error='original-error')
    return task['task_id']

@pytest.mark.parametrize('state', ['queued', 'cancelled', 'completed', 'failed'])
def test_terminal_writer_cannot_replace_nonrunning_record(store, state):
    tid = new_task(store, state)
    before = store.get_task(tid)
    assert store.set_result_if_running(tid, 'late', status='completed') is False
    assert store.get_task(tid) == before

@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_terminal_writer_records_running_outcome_atomically(store, status):
    tid = new_task(store)
    assert store.set_result_if_running(tid, 'synthetic-result', status=status, error='synthetic-error')
    row = store.get_task(tid)
    assert row['status'] == status and row['result'] == 'synthetic-result'
    assert row['error'] == 'synthetic-error' and row['completed_at'] and row['updated_at']
    assert bool(row['cancelled_at']) == (status == 'cancelled')

@pytest.mark.parametrize('status', ['queued', 'running', 'bogus'])
def test_reject_nonterminal_argument_without_write(store, status):
    tid = new_task(store)
    before = store.get_task(tid)
    with pytest.raises(ValueError):
        store.set_result_if_running(tid, 'late', status=status)
    assert store.get_task(tid) == before

def test_missing_record_cannot_be_written(store):
    assert store.set_result_if_running('synthetic-absent', 'late') is False

def test_successful_cancel_wins_before_late_completion(store):
    tid = new_task(store)
    assert store.cancel_task(tid)
    assert not store.set_result_if_running(tid, 'late')
    assert store.get_task(tid)['status'] == 'cancelled'

def test_cancel_completion_race_never_acknowledges_both(store):
    assert callable(getattr(store, 'set_result_if_running', None))
    for _ in range(30):
        tid = new_task(store)
        barrier = threading.Barrier(3)
        outcomes = {}
        def cancel():
            barrier.wait(); outcomes['cancel'] = store.cancel_task(tid)
        def done():
            barrier.wait(); outcomes['done'] = store.set_result_if_running(tid, 'synthetic')
        threads = [threading.Thread(target=cancel), threading.Thread(target=done)]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(1); assert not thread.is_alive()
        assert set(outcomes) == {'cancel', 'done'}
        assert sum(outcomes.values()) == 1
        expected = 'cancelled' if outcomes['cancel'] else 'completed'
        assert store.get_task(tid)['status'] == expected
