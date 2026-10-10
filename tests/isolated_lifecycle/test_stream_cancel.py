"""Execute captured streaming public functions, with standard-library ports only.

AST isolation drops module-level production/SDK imports, but the selected function
bodies run unmodified. It does not execute a model, SDK transport or real profile.
"""
import ast
import logging
from pathlib import Path
import queue
import threading
import pytest

SOURCE = Path(__file__).resolve().parents[2] / 'api/streaming.py'

def public_stream_functions():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('_run_agent_streaming', 'cancel_stream')]
    assert len(functions) == 2
    namespace = {'threading': threading, 'logger': logging.getLogger('synthetic-stream'),
                 'STREAMS': {}, 'STREAMS_LOCK': threading.Lock(), 'CANCEL_FLAGS': {},
                 'AGENT_INSTANCES': {}, '_clear_thread_env': lambda: None,
                 'session_boundary_calls': []}
    class ForbiddenSessionBoundary(BaseException):
        pass
    def unexpected_get_session(*args):
        namespace['session_boundary_calls'].append(args)
        # The real function catches Exception. A synthetic BaseException makes
        # this forbidden boundary fail directly, not inside unrelated SDK ports.
        raise ForbiddenSessionBoundary('Pre-cancelled producer reached the session/model boundary')
    namespace['get_session'] = unexpected_get_session
    module = ast.Module(body=functions, type_ignores=[])
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return namespace

def test_preflight_cancel_is_not_overwritten_by_producer_startup():
    namespace = public_stream_functions()
    stream = 'bg_synthetic-preflight'; capture = queue.Queue()
    flag = threading.Event(); flag.set()
    namespace['STREAMS'][stream] = capture
    namespace['CANCEL_FLAGS'][stream] = flag
    namespace['_run_agent_streaming']('synthetic', 'SYNTHETIC', 'synthetic/model', 'synthetic', stream, [])
    event, data = capture.get_nowait()
    assert event == 'cancel'
    assert data == {'message': 'Cancelled before start'}
    assert capture.empty()
    assert stream not in namespace['STREAMS'] and stream not in namespace['CANCEL_FLAGS']

@pytest.mark.parametrize('interrupt_raises', [False, True])
def test_cancel_stream_attempts_interrupt_and_wakes_queue(interrupt_raises):
    namespace = public_stream_functions()
    stream = 'bg_synthetic-active'; capture = queue.Queue()
    flag = threading.Event(); calls = []
    class SyntheticAgent:
        def interrupt(self, reason):
            calls.append(reason)
            if interrupt_raises: raise RuntimeError('SYNTHETIC INTERRUPT FAILURE')
    namespace['STREAMS'][stream] = capture
    namespace['CANCEL_FLAGS'][stream] = flag
    namespace['AGENT_INSTANCES'][stream] = SyntheticAgent()
    assert namespace['cancel_stream'](stream)
    assert calls == ['Cancelled by user'] and flag.is_set()
    assert capture.get_nowait() == ('cancel', {'message': 'Cancelled by user'})
    # Signal attempt + queue wakeup are not agent termination evidence.

def test_absent_stream_returns_false_without_unrelated_signal():
    namespace = public_stream_functions()
    assert namespace['cancel_stream']('bg_synthetic-absent') is False
    assert not namespace['CANCEL_FLAGS']
