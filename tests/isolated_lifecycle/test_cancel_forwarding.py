"""One public task-cancel endpoint contract with a synthetic stream boundary."""
import importlib.util
from pathlib import Path
import sys
import threading
import types
import pytest


@pytest.mark.parametrize('state', ['running', 'completed', 'failed', 'cancelled'])
def test_task_cancel_only_signals_stream_after_accepted_state_change(monkeypatch, state):
    root = Path(__file__).resolve().parents[2]
    api = types.ModuleType('api'); api.__path__ = []
    monkeypatch.setitem(sys.modules, 'api', api)
    spec = importlib.util.spec_from_file_location('api.task_store', root / 'api/task_store.py')
    store_mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'api.task_store', store_mod); spec.loader.exec_module(store_mod)
    store = store_mod.TaskStore(':memory:')
    store_mod._store = store
    config = types.ModuleType('api.config')
    config.CANCEL_FLAGS = {}; config.STREAMS_LOCK = threading.Lock()
    monkeypatch.setitem(sys.modules, 'api.config', config)
    helpers = types.ModuleType('api.helpers')
    helpers.require = lambda body, *names: [body[n] for n in names]
    helpers.bad = lambda *args: (_ for _ in ()).throw(AssertionError('Unexpected invalid request'))
    helpers.j = lambda handler, body: handler.append(body)
    monkeypatch.setitem(sys.modules, 'api.helpers', helpers)
    calls = []
    streaming = types.ModuleType('api.streaming')
    def cancel_stream(stream_id):
        tid = stream_id.removeprefix('bg_')
        # The committed task cancellation must precede interruption/sentinel.
        assert store.get_task(tid)['status'] == 'cancelled'
        assert config.CANCEL_FLAGS[stream_id].is_set()
        calls.append(stream_id)
        return True
    streaming.cancel_stream = cancel_stream
    monkeypatch.setitem(sys.modules, 'api.streaming', streaming)
    spec = importlib.util.spec_from_file_location('api.task_routes', root / 'api/task_routes.py')
    routes = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'api.task_routes', routes); spec.loader.exec_module(routes)
    try:
        task = store.create_task('synthetic', 'SYNTHETIC', 'synthetic/model', 'synthetic')
        tid = task['task_id']; assert store.claim_task(tid)
        if state != 'running':
            assert store.update_status(tid, state)
        flag = threading.Event()
        config.CANCEL_FLAGS['bg_' + tid] = flag
        response = []
        assert routes.handle_task_cancel(response, {'task_id': tid})
        accepted = state == 'running'
        assert response == [{'ok': True, 'cancelled': accepted}]
        assert calls == (['bg_' + tid] if accepted else [])
        assert flag.is_set() is accepted
        assert store.get_task(tid)['status'] == ('cancelled' if accepted else state)
    finally:
        store._conn.close()
