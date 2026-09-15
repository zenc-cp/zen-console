"""No server conftest, real credentials, Hermes tools, network or child processes.

Actual models, streaming, store, worker and cancellation route are exercised.
Only the external producer/dependencies and thread scheduling are controlled.
The synchronous producer completes before the worker drains its queue, so an
empty test queue raises immediately rather than simulating a 60-second wait.
"""
import collections
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import types
from unittest import mock

import pytest

REPO = Path(os.environ.get('PERSISTENCE_PROBE_REPO', Path(__file__).resolve().parents[1])).resolve()
RUN = Path(os.environ['PERSISTENCE_PROBE_RUN']).resolve() if os.environ.get('PERSISTENCE_PROBE_RUN') else None
OVERLAY = Path(os.environ['PERSISTENCE_PROBE_OVERLAY']) if os.environ.get('PERSISTENCE_PROBE_OVERLAY') else None
ACTIVE_GUARD = None


def audit(event, args):
    if ACTIVE_GUARD is None:
        return
    root, violations, writes = ACTIVE_GUARD
    targets = []
    if event == 'open':
        path, mode, flags = args
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if writing and not isinstance(path, int):
            targets = [path]
    elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'sqlite3.connect'):
        targets = [args[0]]
    elif event in ('os.rename', 'os.replace'):
        targets = list(args[:2])
    elif event.startswith(('socket.', 'subprocess.', 'os.system', 'os.exec', 'os.spawn')):
        violations.append(event)
        raise AssertionError('External effect forbidden: ' + event)
    for target in targets:
        path = Path(os.fsdecode(target)).resolve()
        if not path.is_relative_to(root):
            violations.append(event + ':outside-synthetic-root')
            raise AssertionError('Write/database access outside synthetic root forbidden')
        writes.append({'event': event, 'path': str(path.relative_to(root))})


sys.addaudithook(audit)


class Handler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


class FinishedProducerQueue(queue.Queue):
    def get(self, block=True, timeout=None):
        self.probe_reads = getattr(self, 'probe_reads', 0) + 1
        assert self.probe_reads <= 32, 'Finished producer did not supply a terminal event'
        return super().get(block=False)


class InlineThread:
    def __init__(self, *, target, args=(), kwargs=None, **unused):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


@pytest.fixture
def env(tmp_path, monkeypatch, request):
    global ACTIVE_GUARD
    effects, writes, imports, save_events = [], [], {}, []
    state = tmp_path.resolve()
    ACTIVE_GUARD = state, effects, writes
    probes = []
    for obj, name in ((socket, 'socket'), (socket, 'create_connection'), (subprocess, 'Popen')):
        probe = mock.Mock(side_effect=AssertionError('Network/process effect forbidden'))
        monkeypatch.setattr(obj, name, probe)
        probes.append(probe)
    package = types.ModuleType('api')
    package.__path__ = [str(REPO / 'api')]
    monkeypatch.setitem(sys.modules, 'api', package)

    def module(name, **values):
        m = types.ModuleType(name)
        m.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, m)
        if name.startswith('api.'):
            monkeypatch.setattr(package, name[4:], m, raising=False)
        return m

    cfg = module('api.config', STATE_DIR=state, SESSION_DIR=state / 'sessions',
                 SESSION_INDEX_FILE=state / 'sessions' / '_index.json',
                 SESSIONS=collections.OrderedDict(), SESSIONS_MAX=1000,
                 DEFAULT_WORKSPACE=str(state / 'workspace'), DEFAULT_MODEL='synthetic',
                 PROJECTS_FILE=state / 'projects.json', HOME=state / 'home',
                 LOCK=threading.RLock(), STREAMS_LOCK=threading.RLock(),
                 STREAMS={}, CANCEL_FLAGS={}, AGENT_INSTANCES={}, CLI_TOOLSETS=[],
                 IMAGE_EXTS={'.png'}, MD_EXTS={'.md'},
                 _get_session_agent_lock=lambda sid: threading.RLock(),
                 _set_thread_env=lambda **kw: None, _clear_thread_env=lambda: None,
                 resolve_model_provider=lambda name: (name, 'synthetic', None),
                 get_config=lambda: {'platform_toolsets': {'cli': []}},
                 load_settings=lambda: {'sync_to_insights': True})
    cfg.SESSION_DIR.mkdir()
    module('api.workspace', get_last_workspace=lambda: cfg.DEFAULT_WORKSPACE,
           set_last_workspace=lambda value: None)
    module('api.profiles', get_active_profile_name=lambda: 'synthetic',
           get_active_hermes_home=lambda: state / 'home',
           switch_profile=lambda *a, **k: (_ for _ in ()).throw(AssertionError('Profile switching forbidden')))
    module('agent', __path__=[])
    module('agent.redact', redact_sensitive_text=lambda value: value)
    module('tools', __path__=[])
    module('tools.approval', register_gateway_notify=lambda *a: None,
           unregister_gateway_notify=lambda *a: None, has_pending=lambda *a: False,
           _pending={}, _lock=threading.RLock())
    module('hermes_cli', __path__=[])
    module('hermes_cli.runtime_provider', resolve_runtime_provider=lambda **kw: {'provider': 'synthetic', 'api_key': None, 'base_url': None})
    module('hermes_state', SessionDB=lambda: None)

    e = types.SimpleNamespace(cfg=cfg, before_return=lambda: None, mode=None,
                              answer='SYNTHETIC_PRODUCER_ANSWER', trace=[], observation={}, insights=[])
    module('api.task_notify', notify_task_complete=lambda *a, **kw: e.trace.append('synthetic_notification_sink'))

    def record_insights(**values):
        task = e.store.get_task(e.task['task_id']) if hasattr(e, 'task') else None
        e.insights.append(dict(values, task_status=task['status'] if task else 'foreground'))

    module('api.state_sync', sync_session_usage=record_insights)

    class FakeAgent:
        def __init__(self, **kwargs):
            assert kwargs['provider'] == 'synthetic'
            assert kwargs['api_key'] is None and kwargs['enabled_toolsets'] == []
            self.kwargs = kwargs
            self.session_id = kwargs['session_id']
            self.session_prompt_tokens, self.session_completion_tokens = 3, 4
            self.session_estimated_cost_usd = 0.125

        def run_conversation(self, **kwargs):
            e.trace.append('fake_agent_called')
            messages = copy.deepcopy(kwargs['conversation_history']) + [
                {'role': 'user', 'content': kwargs['persist_user_message']},
                {'role': 'assistant', 'content': e.answer},
            ]
            if e.mode.endswith('_compression'):
                self.session_id = 'synthetic_rotated'
            e.before_return()
            if e.mode.endswith('_exception'):
                raise RuntimeError('synthetic producer exception')
            if e.mode.startswith(('foreground_', 'background_')):
                self.kwargs['tool_progress_callback']('tool.started', 'synthetic_tool', 'synthetic preview', {'argument': 'synthetic'})
                self.kwargs['tool_progress_callback']('tool.completed', 'synthetic_tool', 'synthetic result', {}, duration=0.0, is_error=False)
                self.kwargs['stream_delta_callback'](e.answer)
            return {'messages': messages, 'final_response': e.answer}

        def interrupt(self, *args):
            e.trace.append('fake_interrupt')

    module('run_agent', AIAgent=FakeAgent)

    def load(name):
        relative = Path('api') / (name + '.py')
        path = OVERLAY / relative if OVERLAY and (OVERLAY / relative).is_file() else REPO / relative
        spec = importlib.util.spec_from_file_location('api.' + name, path)
        m = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, 'api.' + name, m)
        monkeypatch.setattr(package, name, m, raising=False)
        spec.loader.exec_module(m)
        assert Path(m.__file__).resolve() == path.resolve()
        imports[name] = str(path)
        return m

    e.models, e.store_module = load('models'), load('task_store')
    load('helpers')
    load('result_receipt')
    e.streaming, e.worker_module, e.routes = load('streaming'), load('task_worker'), load('task_routes')
    e.store = e.store_module.TaskStore(state / 'tasks.db')
    monkeypatch.setattr(e.store_module, '_store', e.store)
    original_save = e.models.Session.save

    def real_save_observed(session, *a, **kw):
        task = e.store.get_task(e.task['task_id']) if hasattr(e, 'task') else None
        save_events.append({'task_status': task['status'] if task else 'setup',
                            'old_claim_current': e.store.is_current_execution(e.claim) if hasattr(e, 'claim') else None,
                            'messages': copy.deepcopy(session.messages),
                            'pending_user_message': session.pending_user_message})
        return original_save(session, *a, **kw)

    monkeypatch.setattr(e.models.Session, 'save', real_save_observed)
    e.session = e.models.Session(session_id='synthetic_session', title='Synthetic baseline',
                                 workspace=cfg.DEFAULT_WORKSPACE, model='synthetic',
                                 messages=[{'role': 'user', 'content': 'BASELINE_HISTORY', '_ts': 1}],
                                 created_at=1, updated_at=1, active_stream_id='other_stream',
                                 pending_user_message='PENDING_MUST_SURVIVE', pending_started_at=1)
    cfg.SESSIONS[e.session.session_id] = e.session
    e.session.save()
    try:
        yield e
    finally:
        e.observation.setdefault('mode', e.mode)
        e.observation.setdefault('thread_schedule', 'inline producer completes before bounded queue drain')
        if hasattr(e, 'task'):
            e.observation.setdefault('task_after', e.store.get_task(e.task['task_id']))
        e.observation.update({'imports': imports, 'save_events': save_events,
                              'source_sha256': {name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in imports.items()},
                              'insights': e.insights,
                              'trace': e.trace, 'effect_violations': effects,
                              'network_process_probe_calls': [p.call_count for p in probes],
                              'synthetic_writes': writes,
                              'real_session_save_called': True})
        e.store._conn.close()
        ACTIVE_GUARD = None
        callspec = getattr(request.node, 'callspec', None)
        suffix = callspec.id if callspec is not None else request.node.name
        if RUN is not None:
            (RUN / ('case-' + suffix + '.json')).write_text(json.dumps(e.observation, indent=2) + '\n', encoding='utf-8')
        assert not effects and all(p.call_count == 0 for p in probes)


@pytest.mark.parametrize('mode', [
    'foreground_success', 'foreground_compression', 'background_success',
    'background_compression', 'background_newer_history', 'background_exception',
    'cancelled_result', 'cancelled_compression', 'expired_result',
    'reclaimed_result', 'cancelled_exception', 'prestart_cancel',
])
def test_persistence_ownership(env, monkeypatch, mode):
    e = env
    e.mode = mode
    before_bytes = e.session.path.read_bytes()
    before_cache = copy.deepcopy(e.session.__dict__)
    if mode.startswith('foreground_'):
        q = queue.Queue()
        e.cfg.STREAMS['foreground_probe'] = q
        e.streaming._run_agent_streaming(e.session.session_id, 'SYNTHETIC_PROMPT',
                                        'synthetic', e.cfg.DEFAULT_WORKSPACE, 'foreground_probe')
        assert any(item[0] == 'done' for item in list(q.queue))
    else:
        e.task = e.store.create_task(session_id=e.session.session_id, prompt='SYNTHETIC_PROMPT',
                                     model='other_synthetic' if mode == 'prestart_cancel' else 'synthetic',
                                     workspace=e.cfg.DEFAULT_WORKSPACE)
        e.claim = e.store.claim_execution(e.task['task_id'])
        assert e.claim is not None
        e.worker = e.worker_module.BackgroundWorker(e.store)
        # Do not alter the production queue timeout or sleep. With the producer
        # synchronously finished, its empty capture queue can be checked directly.
        monkeypatch.setattr(e.worker_module, 'threading', types.SimpleNamespace(Thread=InlineThread))
        monkeypatch.setattr(e.worker_module, 'queue', types.SimpleNamespace(Queue=FinishedProducerQueue, Empty=queue.Empty))

        def before_return():
            if mode.startswith('cancelled') or mode == 'prestart_cancel':
                h = Handler()
                assert e.routes.handle_task_cancel(h, {'task_id': e.task['task_id']})
                assert h.status == 200 and json.loads(h.wfile.getvalue())['cancelled']
                assert e.cfg.CANCEL_FLAGS[e.claim.stream_id].is_set()
            elif mode == 'expired_result':
                snapshot = e.store.list_tasks(status='running', include_execution_token=True)[0]
                assert e.store.expire_task_if_current(snapshot, error='synthetic expiry', completed_at=e.worker_module._utcnow()) == e.claim.stream_id
            elif mode == 'reclaimed_result':
                # A test-owned dispatch-failure/requeue sequence, using real store
                # methods. This is not a claim about live startup recovery timing.
                assert e.store.finish_execution(e.claim, status='failed', error='dispatch timeout: synthetic recovery fixture')
                snapshot = e.store.list_tasks(status='failed', include_execution_token=True)[0]
                assert e.store.requeue_task_if_current(snapshot, progress={'requeue_count': 1})
                e.new_claim = e.store.claim_execution(e.task['task_id'])
                assert e.new_claim is not None and e.new_claim != e.claim
            elif mode == 'background_newer_history':
                e.session.messages.append({'role': 'assistant', 'content': 'NEWER_CONCURRENT_HISTORY', '_ts': 2})
                e.session.save()
            e.trace.append('ownership_transition:' + mode)

        if mode == 'prestart_cancel':
            class PrecancelThread(InlineThread):
                def start(self):
                    before_return()
                    super().start()
            monkeypatch.setattr(e.worker_module, 'threading', types.SimpleNamespace(Thread=PrecancelThread))
        e.before_return = before_return
        task = e.store.get_execution_task(e.claim)
        e.worker._execute_task(dict(task, _execution_claim=e.claim))
        e.observation['task_after'] = e.store.get_task(e.task['task_id'])
        e.observation['worker_counts'] = {'processed': e.worker._processed, 'errors': e.worker._errors}
    disk = json.loads(e.session.path.read_text(encoding='utf-8'))
    e.observation.update({'mode': mode, 'disk_after': disk,
                          'cache_after': copy.deepcopy(e.session.__dict__),
                          'before_cache': before_cache,
                          'file_unchanged': e.session.path.read_bytes() == before_bytes})
    completes = mode.startswith('foreground_') or mode in ('background_success', 'background_compression', 'background_newer_history')
    if completes:
        assert any(m.get('content') == e.answer for m in disk['messages'])
        assert disk['input_tokens'] == 3 and disk['output_tokens'] == 4
        assert disk['estimated_cost'] == 0.125
        assert len(e.insights) == 1
        if mode.startswith('background_'):
            assert e.insights[0]['task_status'] == 'completed'
            assert [row['type'] for row in e.observation['task_after']['tool_log']] == ['call', 'result']
            assert e.observation['task_after']['status'] == 'completed'
            assert e.observation['task_after']['result'] == e.answer
            answers = [m for m in disk['messages'] if m.get('content') == e.answer]
            assert len(answers) == 1, 'Background result must be appended once, after its winning commit'
            assert answers[0]['_bg_task'] == e.task['task_id']
            assert answers[0]['_bg_status'] == 'completed'
            assert disk['session_id'] == 'synthetic_session'
            assert disk['messages'][0] == before_cache['messages'][0]
            assert disk['pending_user_message'] == before_cache['pending_user_message']
            assert disk['active_stream_id'] == before_cache['active_stream_id']
            if mode == 'background_newer_history':
                assert any(m.get('content') == 'NEWER_CONCURRENT_HISTORY' for m in disk['messages'])
        elif mode == 'foreground_compression':
            assert disk['session_id'] == 'synthetic_rotated'
            assert 'synthetic_session' not in e.cfg.SESSIONS
            assert e.cfg.SESSIONS['synthetic_rotated'] is e.session
    else:
        cancelled = mode.startswith('cancelled') or mode == 'prestart_cancel'
        expected_status = 'cancelled' if cancelled else 'running' if mode == 'reclaimed_result' else 'failed'
        assert e.observation['task_after']['status'] == expected_status
        assert e.observation['task_after']['result'] == ''
        assert e.worker._processed == 0
        assert e.insights == []
        assert e.session.__dict__ == before_cache, 'Non-owner or failed producer changed cached session state'
        assert e.session.path.read_bytes() == before_bytes, 'Non-owner or failed producer changed persisted session state'
        if mode == 'prestart_cancel':
            assert 'fake_agent_called' not in e.trace


def test_real_thread_cancellation(env, monkeypatch):
    """Cancel from the driver while a real producer thread waits at a barrier."""
    e = env
    e.mode = 'cancelled_result'
    before_cache, before_bytes = copy.deepcopy(e.session.__dict__), e.session.path.read_bytes()
    ready, release = threading.Event(), threading.Event()
    producer_threads, driver_errors = [], []

    def wait_at_return():
        ready.set()
        assert release.wait(5), 'Driver did not release the synthetic producer'

    e.before_return = wait_at_return
    e.task = e.store.create_task(session_id=e.session.session_id, prompt='SYNTHETIC_PROMPT',
                                 model='synthetic', workspace=e.cfg.DEFAULT_WORKSPACE)
    e.claim = e.store.claim_execution(e.task['task_id'])
    e.worker = e.worker_module.BackgroundWorker(e.store)

    def real_producer_thread(**kwargs):
        thread = threading.Thread(**kwargs)
        producer_threads.append(thread)
        return thread

    class ShortWaitQueue(queue.Queue):
        def get(self, block=True, timeout=None):
            # Test-owned polling interval, not a change to the production timeout.
            return super().get(block=block, timeout=0.05 if timeout is not None else None)

    monkeypatch.setattr(e.worker_module, 'threading', types.SimpleNamespace(Thread=real_producer_thread))
    monkeypatch.setattr(e.worker_module, 'queue', types.SimpleNamespace(Queue=ShortWaitQueue, Empty=queue.Empty))

    def drive():
        try:
            task = e.store.get_execution_task(e.claim)
            e.worker._execute_task(dict(task, _execution_claim=e.claim))
        except BaseException as exc:
            driver_errors.append(repr(exc))

    driver = threading.Thread(target=drive, daemon=True)
    driver.start()
    try:
        assert ready.wait(5), 'Synthetic producer did not reach its return barrier'
        handler = Handler()
        e.routes.handle_task_cancel(handler, {'task_id': e.task['task_id']})
        assert json.loads(handler.wfile.getvalue())['cancelled']
        assert e.cfg.CANCEL_FLAGS[e.claim.stream_id].is_set()
    finally:
        release.set()
        for thread in [driver, *producer_threads]:
            thread.join(5)
        if any(thread.is_alive() for thread in [driver, *producer_threads]):
            # Never unload synthetic boundaries around an orphaned test thread.
            print('Contained persistence probe could not join its test threads', flush=True)
            os._exit(3)
    e.observation.update({'mode': 'real_thread_cancellation',
                          'thread_schedule': 'real producer thread paused; driver cancels; release and join',
                          'producer_threads': len(producer_threads), 'driver_errors': driver_errors,
                          'file_unchanged': e.session.path.read_bytes() == before_bytes,
                          'cache_after': copy.deepcopy(e.session.__dict__)})
    assert not driver_errors and len(producer_threads) == 1
    assert e.store.get_task(e.task['task_id'])['status'] == 'cancelled'
    assert e.worker._processed == 0 and e.insights == []
    assert e.session.__dict__ == before_cache
    assert e.session.path.read_bytes() == before_bytes
