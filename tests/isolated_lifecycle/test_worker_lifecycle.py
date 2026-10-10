"""Public worker/cancel/subscriber controls using synthetic producer boundaries.

The actual private worker, store and task endpoint run. cancel_stream is executed
from its source AST with synthetic agents. No server, SDK, account, real profile,
model or network is used. Timeout acceleration applies only to test scheduler ports.
"""
import ast
import importlib.util
import logging
from pathlib import Path
import queue
import sys
import threading
import types
import pytest

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    package = types.ModuleType('api'); package.__path__ = []
    monkeypatch.setitem(sys.modules, 'api', package)
    def load(name, filename):
        spec = importlib.util.spec_from_file_location(name, ROOT / 'api' / filename)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module
    store_module = load('api.task_store', 'task_store.py')
    store = store_module.TaskStore(tmp_path / 'synthetic.db'); store_module._store = store
    config = types.ModuleType('api.config')
    config.STREAMS = {}; config.CANCEL_FLAGS = {}; config.AGENT_INSTANCES = {}
    config.STREAMS_LOCK = threading.Lock()
    monkeypatch.setitem(sys.modules, 'api.config', config)
    sessions, controls, notifications, errors = {}, {}, [], []
    models = types.ModuleType('api.models')
    models.get_session = lambda sid: sessions[sid]
    models.new_session = lambda **kwargs: (_ for _ in ()).throw(AssertionError('Unexpected new-session effect'))
    monkeypatch.setitem(sys.modules, 'api.models', models)
    helpers = types.ModuleType('api.helpers')
    helpers.require = lambda body, *names: [body[name] for name in names]
    helpers.bad = lambda *args: (_ for _ in ()).throw(AssertionError('Unexpected invalid request'))
    helpers.j = lambda handler, body: handler.append(body)
    monkeypatch.setitem(sys.modules, 'api.helpers', helpers)
    notify = types.ModuleType('api.task_notify')
    def notify_task_complete(task, result):
        notifications.append((task['task_id'], result))
        controls[task['session_id']].notified.set()
    notify.notify_task_complete = notify_task_complete
    monkeypatch.setitem(sys.modules, 'api.task_notify', notify)
    streaming = types.ModuleType('api.streaming')
    streaming.STREAMS = config.STREAMS; streaming.STREAMS_LOCK = config.STREAMS_LOCK
    source = ROOT / 'api/streaming.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'cancel_stream')
    namespace = {'STREAMS': config.STREAMS, 'STREAMS_LOCK': config.STREAMS_LOCK,
                 'CANCEL_FLAGS': config.CANCEL_FLAGS, 'AGENT_INSTANCES': config.AGENT_INSTANCES,
                 'logger': logging.getLogger('synthetic-cancel')}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    streaming.cancel_stream = namespace['cancel_stream']
    def producer(sid, prompt, model, workspace, stream_id, attachments):
        control = controls[sid]
        capture = config.STREAMS[stream_id]
        class SyntheticAgent:
            def interrupt(self, reason):
                control.interrupted.set()
                # Deliberately not termination: test must release producer itself.
        config.AGENT_INSTANCES[stream_id] = SyntheticAgent()
        control.started.set()
        try:
            if control.mode == 'blocked':
                if not control.release.wait(2):
                    raise AssertionError('Synthetic producer release missing')
            if control.mode == 'apperror':
                capture.put(('apperror', {'message': 'SYNTHETIC FAILURE', 'type': 'error'}))
            else:
                capture.put(('token', {'text': 'SYNTHETIC RESULT'}))
                capture.put(('done', {}))
                if control.mode == 'terminal_cleanup':
                    if not control.release.wait(2):
                        raise AssertionError('Synthetic terminal-cleanup release missing')
        except BaseException as error:
            errors.append(error)
        finally:
            config.AGENT_INSTANCES.pop(stream_id, None)
            control.exited.set()
    streaming._run_agent_streaming = producer
    monkeypatch.setitem(sys.modules, 'api.streaming', streaming)
    worker_module = load('api.task_worker', 'task_worker.py')
    # Keep worker stop bounded in a deterministic fixture. Actual producer joins
    # without a timeout are NOT accelerated, so detachment cannot hide here.
    created_threads = []
    class SchedulerThread(threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_threads.append(self)
        def join(self, timeout=None):
            return super().join(0.02 if timeout == 10 else timeout)
    worker_module.threading = types.SimpleNamespace(Thread=SchedulerThread, Lock=threading.Lock)
    class SchedulerQueue(queue.Queue):
        def get(self, block=True, timeout=None):
            return super().get(block=block, timeout=0.01 if timeout == 60 else timeout)
    worker_module.queue = types.SimpleNamespace(Queue=SchedulerQueue, Empty=queue.Empty)
    routes = load('api.task_routes', 'task_routes.py')
    worker = worker_module.BackgroundWorker(store, poll_interval=0.002)
    def submit(sid, mode='done'):
        control = types.SimpleNamespace(mode=mode, started=threading.Event(), release=threading.Event(),
                                        exited=threading.Event(), interrupted=threading.Event(),
                                        notified=threading.Event(), saved=threading.Event())
        controls[sid] = control
        session = types.SimpleNamespace(session_id=sid, messages=[], save=control.saved.set)
        sessions[sid] = session
        task = store.create_task(sid, 'SYNTHETIC', 'synthetic/model', 'synthetic',
                                 notify_config={'synthetic': True})
        subscriber = worker_module.subscribe_task(task['task_id'])
        return task['task_id'], control, subscriber
    value = types.SimpleNamespace(store=store, worker=worker, submit=submit, routes=routes,
                                  notifications=notifications, sessions=sessions, config=config)
    try:
        yield value
    finally:
        for control in controls.values(): control.release.set()
        worker.stop()
        # Original stop can discard a still-live thread handle. Track all
        # fixture-created threads instead of trusting that implementation claim.
        for thread in created_threads:
            if thread.ident is not None:
                thread.join(1)
                assert not thread.is_alive(), 'Fixture left a tracked thread alive'
        for control in controls.values():
            if control.started.is_set(): assert control.exited.wait(1), 'Fixture left a producer alive'
        store._conn.close()
        assert not errors, errors


@pytest.mark.parametrize('mode', ['done', 'apperror'])
def test_cancel_winning_during_terminal_commit_closes_subscriber(pipeline, monkeypatch, mode):
    tid, control, subscriber = pipeline.submit('synthetic-cancel-commit-race', mode)
    entered, release_commit = threading.Event(), threading.Event()
    original = pipeline.store.set_result_if_running
    def delayed_commit(task_id, *args, **kwargs):
        if task_id == tid:
            entered.set()
            assert release_commit.wait(1), 'Synthetic commit barrier not released'
        return original(task_id, *args, **kwargs)
    monkeypatch.setattr(pipeline.store, 'set_result_if_running', delayed_commit)
    try:
        pipeline.worker.start()
        assert entered.wait(1)
        if mode == 'done':
            assert subscriber.get(timeout=1)[0] == 'token'
        response = []
        pipeline.routes.handle_task_cancel(response, {'task_id': tid})
        assert response == [{'ok': True, 'cancelled': True}]
        release_commit.set()
        event, _ = subscriber.get(timeout=0.5)
        assert event == 'cancel'
        assert pipeline.store.get_task(tid)['status'] == 'cancelled'
        assert pipeline.store.get_task(tid)['result'] == ''
        assert not pipeline.notifications and not pipeline.sessions['synthetic-cancel-commit-race'].messages
    finally:
        release_commit.set()


def test_terminal_event_does_not_release_producer_ownership_before_cleanup(pipeline):
    tid, first, subscriber = pipeline.submit('synthetic-terminal-cleanup', 'terminal_cleanup')
    pipeline.worker.start(); assert first.notified.wait(1)
    assert subscriber.get(timeout=1)[0] == 'token'
    assert subscriber.get(timeout=1)[0] == 'done'
    assert pipeline.store.get_task(tid)['status'] == 'completed'
    assert not first.exited.is_set()
    next_tid, following, _ = pipeline.submit('synthetic-after-cleanup')
    assert not following.started.wait(0.05), 'Terminal event is not producer cleanup completion'
    assert pipeline.store.get_task(next_tid)['status'] == 'queued'
    first.release.set()
    assert first.exited.wait(1) and following.started.wait(1) and following.notified.wait(1)


def test_completion_history_uses_committed_terminal_metadata(pipeline):
    tid, control, subscriber = pipeline.submit('synthetic-completion-context')
    pipeline.worker.start()
    assert control.notified.wait(1)
    assert subscriber.get(timeout=1)[0] == 'token'
    assert subscriber.get(timeout=1)[0] == 'done'
    row = pipeline.store.get_task(tid)
    assert row['status'] == 'completed' and row['completed_at'] and row['started_at']
    messages = pipeline.sessions['synthetic-completion-context'].messages
    assert len(messages) == 2
    assert messages[-1]['content'] == 'SYNTHETIC RESULT'
    assert messages[-1]['_bg_status'] == 'completed'
    assert messages[-1]['_bg_duration'], 'Terminal history must not use the pre-claim task snapshot'


def test_application_error_closes_public_subscriber_as_error(pipeline):
    tid, control, subscriber = pipeline.submit('synthetic-app-error', 'apperror')
    pipeline.worker.start()
    event, data = subscriber.get(timeout=1)
    assert event == 'error', 'Task SSE terminal vocabulary is done/error/cancel, not apperror'
    assert data['message'] == 'SYNTHETIC FAILURE'
    assert control.exited.wait(1)
    row = pipeline.store.get_task(tid)
    assert row['status'] == 'failed' and row['error'] == 'SYNTHETIC FAILURE'
    assert not pipeline.notifications and not pipeline.sessions['synthetic-app-error'].messages


def test_cancel_closes_subscriber_without_dispatching_next_while_producer_alive(pipeline):
    tid, first, subscriber = pipeline.submit('synthetic-blocked', 'blocked')
    pipeline.worker.start(); assert first.started.wait(1)
    next_tid, following, _ = pipeline.submit('synthetic-next')
    response = []
    pipeline.routes.handle_task_cancel(response, {'task_id': tid})
    assert response == [{'ok': True, 'cancelled': True}]
    assert first.interrupted.wait(1)
    event, _ = subscriber.get(timeout=0.5)
    assert event == 'cancel'
    assert not first.exited.is_set(), 'Synthetic interruption is not producer termination'
    assert not following.started.wait(0.05), 'Worker detached a live producer and started another task'
    assert pipeline.store.get_task(next_tid)['status'] == 'queued'
    first.release.set()
    assert first.exited.wait(1) and following.started.wait(1) and following.notified.wait(1)
    assert pipeline.store.get_task(tid)['status'] == 'cancelled'
    assert pipeline.store.get_task(tid)['result'] == ''
    assert pipeline.store.get_task(next_tid)['status'] == 'completed'
    assert pipeline.notifications == [(next_tid, 'SYNTHETIC RESULT')]
    assert not pipeline.sessions['synthetic-blocked'].messages


def test_stop_keeps_live_worker_visible_and_start_does_not_duplicate_it(pipeline):
    _, first, _ = pipeline.submit('synthetic-stop-blocked', 'blocked')
    pipeline.worker.start(); assert first.started.wait(1)
    pipeline.worker.stop()
    assert not first.exited.is_set()
    assert pipeline.worker.is_running(), 'Stop timeout must not disguise an active worker as gone'
    _, following, _ = pipeline.submit('synthetic-stop-next')
    pipeline.worker.start()
    assert not following.started.wait(0.05), 'Start duplicated a stopping worker'
    first.release.set()
    assert first.exited.wait(1)
