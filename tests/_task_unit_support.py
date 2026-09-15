"""Explicit pytest unit isolation; never load the server conftest or real agent."""
import contextlib
from pathlib import Path
import socket
import subprocess
import sys
import threading
import types
import uuid
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[1]
_state = None
_probe_calls = {'socket.socket': 0, 'socket.create_connection': 0, 'subprocess.Popen': 0}
_results = []
_selected = []
_deselected = []


class SyntheticSession:
    def __init__(self, workspace='', model=''):
        self.session_id = uuid.uuid4().hex
        self.workspace, self.model = workspace, model
        self.messages = []
        self.saved = 0

    def save(self):
        self.saved += 1


def install(state_dir):
    global _state
    _state = Path(state_dir).resolve()
    _state.mkdir(parents=True, exist_ok=False)
    assert 'api.config' not in sys.modules, 'Run unit isolation in a fresh process'
    sys.path.insert(0, str(REPO))
    import api
    assert Path(api.__file__).resolve().parent == REPO / 'api'
    config = types.ModuleType('api.config')
    config.STATE_DIR = _state
    config.IMAGE_EXTS, config.MD_EXTS = {'.png', '.jpg'}, {'.md'}
    config.STREAMS, config.CANCEL_FLAGS, config.SESSIONS = {}, {}, {}
    config.STREAMS_LOCK, config.LOCK = threading.RLock(), threading.RLock()
    models = types.ModuleType('api.models')
    def get_session(session_id):
        if session_id not in config.SESSIONS:
            raise KeyError(session_id)
        return config.SESSIONS[session_id]
    def new_session(workspace='', model=''):
        session = SyntheticSession(workspace, model)
        config.SESSIONS[session.session_id] = session
        return session
    models.get_session, models.new_session = get_session, new_session
    streaming = types.ModuleType('api.streaming')
    streaming.STREAMS, streaming.STREAMS_LOCK = config.STREAMS, config.STREAMS_LOCK
    def forbidden_producer(*a, **kw):
        raise AssertionError('Real producer was not replaced by a synthetic test producer')
    streaming._run_agent_streaming = forbidden_producer
    profiles = types.ModuleType('api.profiles')
    profiles.get_active_profile_name = lambda: 'synthetic'
    profiles.switch_profile = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('Real profile switching forbidden'))
    for name, module in (('config', config), ('models', models), ('streaming', streaming), ('profiles', profiles)):
        sys.modules['api.' + name] = module
        setattr(api, name, module)


@pytest.fixture(autouse=True)
def prohibit_test_external_effects():
    # pytest/Windows terminal discovery initializes before application tests.
    # Guard the tests and their fixtures, not framework platform discovery.
    with contextlib.ExitStack() as stack:
        probes = {}
        for obj, name in ((socket, 'socket'), (socket, 'create_connection'), (subprocess, 'Popen')):
            key = obj.__name__ + '.' + name
            probes[key] = stack.enter_context(mock.patch.object(obj, name, side_effect=AssertionError('Network/process effect forbidden in unit test')))
        try:
            yield
        finally:
            for name, probe in probes.items():
                _probe_calls[name] += probe.call_count
            assert all(probe.call_count == 0 for probe in probes.values())


def pytest_collection_finish(session):
    _selected.extend(item.nodeid for item in session.items)
    for item in session.items:
        assert not any(m.name in ('skip', 'skipif', 'xfail') for m in item.iter_markers()), item.nodeid


def pytest_deselected(items):
    _deselected.extend(item.nodeid for item in items)


def pytest_runtest_logreport(report):
    _results.append({'nodeid': report.nodeid, 'when': report.when, 'outcome': report.outcome})


def report():
    imports = {}
    for name in ('task_store', 'task_worker', 'task_routes', 'task_sweeper', 'task_integration', 'task_notify', 'helpers', 'result_receipt'):
        module = sys.modules.get('api.' + name)
        if module is not None:
            path = Path(module.__file__).resolve()
            assert path == REPO / 'api' / (name + '.py'), str(path)
            imports[name] = str(path)
    assert not any(name == 'conftest' or name.endswith('.conftest') for name in sys.modules), 'Server conftest must remain unloaded'
    return {'selected': _selected, 'deselected': _deselected, 'test_reports': _results,
            'effect_probe_calls': dict(_probe_calls),
            'actual_working_copy_imports': imports, 'synthetic_dependencies': ['api.config', 'api.models', 'api.streaming', 'api.profiles'],
            'state_dir': str(_state), 'server_conftest_loaded': False}


def close():
    """Fixtures release mocks; retained synthetic artifacts are not cleaned here."""
