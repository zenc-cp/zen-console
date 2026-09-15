"""Execution ownership regressions against actual working-copy modules."""
import ast
import contextlib
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
import queue
import sqlite3
import threading
from unittest import mock

import pytest

import api.config as config
import api.streaming as streaming
import api.task_store as stores
import api.task_worker as workers
import api.task_sweeper as sweeper
from api.task_integration import _requeue_dispatch_timeouts

REAL_QUEUE = queue.Queue


class InlineThread:
    def __init__(self, target, args=(), **kw):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


class BoundedQueue(REAL_QUEUE):
    def get(self, *a, **kw):
        try:
            return super().get(block=False)
        except queue.Empty:
            raise AssertionError('Synthetic queue exhausted; no unbounded heartbeat')


class Driver:
    def __init__(self):
        self.store = stores.TaskStore(':memory:')
        self.task = self.store.create_task('synthetic-session', 'synthetic prompt', 'synthetic-model', 'synthetic-workspace')
        self.worker = workers.BackgroundWorker(self.store)
        self.worker._inject_into_session, self.worker._notify = mock.Mock(), mock.Mock()
        self.events, self.calls = [], 0
        real_broadcast = self.worker._broadcast
        def broadcast(task_id, event, data):
            self.events.append((event, self.store.get_task(task_id)['status']))
            real_broadcast(task_id, event, data)
        self.worker._broadcast = broadcast

    @property
    def task_id(self):
        return self.task['task_id']

    def force(self, status, **fields):
        """Explicit fixture-only SQL, not a lifecycle API for application callers."""
        fields = dict(status=status, **fields)
        self.store._execute('UPDATE tasks SET ' + ', '.join(k + ' = ?' for k in fields) + ' WHERE task_id = ?', [*fields.values(), self.task_id])

    def claim(self):
        if hasattr(self.store, 'claim_execution'):
            claim = self.store.claim_execution(self.task_id)
            assert claim is not None
            return dict(self.store.get_task(self.task_id), _execution_claim=claim)
        assert self.store.claim_task(self.task_id)
        return self.store.get_task(self.task_id)

    def sid(self, task):
        claim = task.get('_execution_claim')
        return claim.stream_id if claim is not None else 'bg_' + self.task_id

    def finish(self, task):
        if hasattr(self.store, 'finish_execution'):
            return self.store.finish_execution(task['_execution_claim'], status='completed', result='synthetic result')
        return self.store.set_result(self.task_id, 'synthetic result')

    def drive(self, task, *, event='done', hook=None, stream_id=None):
        sid = self.sid(task) if stream_id is None else stream_id
        def producer(*args):
            self.calls += 1
            assert args[4] == sid
            q = config.STREAMS[sid]
            q.put(('token', {'text': 'synthetic result'}))
            q.put((event, {'message': 'synthetic error'} if event != 'done' else {}))
            if hook:
                hook()
        with mock.patch.object(streaming, '_run_agent_streaming', side_effect=producer), mock.patch.object(workers.threading, 'Thread', InlineThread), mock.patch.object(workers.queue, 'Queue', BoundedQueue):
            self.worker._run_agent_for_task(task, self.task_id, sid)

    def terminal(self):
        return [e for e in self.events if e[0] in ('done', 'error', 'cancel')]

    def suppressed(self):
        assert self.terminal() == []
        self.worker._inject_into_session.assert_not_called()
        self.worker._notify.assert_not_called()

    def age(self):
        stamp = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.force(self.store.get_task(self.task_id)['status'], created_at=stamp, started_at=stamp, updated_at=stamp)
        return stamp

    def sweep(self):
        return sweeper._sweep_once(self.store, config.STREAMS, config.STREAMS_LOCK)


@pytest.fixture
def d():
    config.STREAMS.clear(); config.CANCEL_FLAGS.clear(); workers.TASK_SUBSCRIBERS.clear()
    driver = Driver()
    yield driver
    driver.store._conn.close()


def test_normal_commit_precedes_publication(d):
    task = d.claim(); d.drive(task)
    assert d.store.get_task(d.task_id)['status'] == 'completed'
    assert d.terminal() == [('done', 'completed')]
    d.worker._inject_into_session.assert_called_once()
    d.worker._notify.assert_called_once()


@pytest.mark.parametrize('terminal', ['done', 'error', 'cancel'])
def test_successful_cancellation_blocks_late_terminal(d, terminal):
    task = d.claim()
    def cancel():
        assert d.store.cancel_task(d.task_id)
    d.drive(task, event=terminal, hook=cancel)
    assert d.store.get_task(d.task_id)['status'] == 'cancelled'
    d.suppressed()


def test_timeout_blocks_late_completion(d):
    task = d.claim()
    d.drive(task, hook=lambda: d.force('failed', error='synthetic timeout'))
    assert d.store.get_task(d.task_id)['status'] == 'failed'
    d.suppressed()


def test_new_claim_blocks_old_done_and_old_cleanup(d):
    old = d.claim(); newer = []; marker = object()
    def reclaim():
        d.force('queued'); new = d.claim(); newer.append(new)
        config.STREAMS[d.sid(new)] = marker
    d.drive(old, hook=reclaim)
    assert d.store.get_task(d.task_id)['status'] == 'running'
    assert d.sid(old) != d.sid(newer[0])
    assert config.STREAMS[d.sid(newer[0])] is marker
    d.suppressed()


def test_duplicate_completion_cannot_republish(d):
    task = d.claim(); assert d.finish(task)
    assert not d.finish(task)
    d.drive(task)
    assert d.calls == 0
    d.suppressed()


def test_missing_forged_and_wrong_stream_context(d):
    task = d.claim()
    missing = dict(task); missing.pop('_execution_claim', None)
    d.drive(missing)
    assert d.calls == 0
    claim = task['_execution_claim']
    forged = stores.ExecutionClaim(claim.task_id, '0' * 32)
    d.drive(dict(task, _execution_claim=forged))
    d.drive(task, stream_id='bg_wrong_synthetic')
    assert d.calls == 0
    d.suppressed()


@pytest.mark.parametrize('terminal', ['error', 'cancel'])
def test_owned_failure_terminal_commits_before_broadcast(d, terminal):
    task = d.claim(); d.drive(task, event=terminal)
    assert d.terminal() == [(terminal, 'failed')]
    d.worker._notify.assert_not_called()


def test_lost_write_suppresses_effects(d):
    task = d.claim()
    with mock.patch.object(d.store, 'finish_execution', return_value=False):
        d.drive(task)
    assert d.store.get_task(d.task_id)['status'] == 'running'
    d.suppressed()


def test_claim_is_frozen_single_winner_and_not_public(d):
    task = d.claim(); claim = task['_execution_claim']
    assert claim.version == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.token = 'changed'
    assert claim.token not in repr(claim)
    assert 'execution_token' not in d.store.get_task(d.task_id)
    assert 'execution_token' not in d.store.list_tasks()[0]
    assert d.store.claim_execution(d.task_id) is None
    assert not d.store.finish_execution(claim, status='queued')


@pytest.mark.parametrize('race', ['complete', 'reclaim', 'heartbeat'])
def test_sweeper_rechecks_captured_generation_and_liveness(d, race):
    task = d.claim(); old_stamp = d.age(); real_list = d.store.list_tasks
    def intervene(*a, **kw):
        rows = real_list(*a, **kw)
        if kw.get('status') == 'running':
            if race == 'complete':
                assert d.finish(task)
            elif race == 'reclaim':
                d.force('queued'); d.claim()
                d.force('running', created_at=old_stamp, started_at=old_stamp, updated_at=old_stamp)
            else:
                d.store.update_progress(d.task_id, {'synthetic': True})
        return rows
    with mock.patch.object(d.store, 'list_tasks', side_effect=intervene):
        summary = d.sweep()
    assert summary['running_timed_out'] == 0
    assert d.store.get_task(d.task_id)['status'] == ('completed' if race == 'complete' else 'running')


def test_queued_timeout_does_not_overwrite_new_claim(d):
    d.age(); real_list = d.store.list_tasks
    def intervene(*a, **kw):
        rows = real_list(*a, **kw)
        if kw.get('status') == 'queued':
            d.claim()
        return rows
    with mock.patch.object(d.store, 'list_tasks', side_effect=intervene):
        assert d.sweep()['queued_timed_out'] == 0
    assert d.store.get_task(d.task_id)['status'] == 'running'


def test_sweeper_targets_won_stream_without_orphan_flags(d):
    task = d.claim(); d.age(); sid = d.sid(task)
    config.STREAMS[sid] = REAL_QUEUE(); config.CANCEL_FLAGS[sid] = threading.Event()
    summary = d.sweep()
    assert summary['running_timed_out'] == 1 and summary['streams_cleaned'] == 1
    assert sid not in config.STREAMS and config.CANCEL_FLAGS[sid].is_set()
    d.force('queued'); config.CANCEL_FLAGS.clear()
    assert d.sweep()['queued_timed_out'] == 1
    assert config.CANCEL_FLAGS == {}


def test_cleanup_cannot_touch_new_attempt_after_expiry(d):
    task = d.claim(); d.age(); old_sid = d.sid(task)
    config.STREAMS[old_sid] = REAL_QUEUE(); config.CANCEL_FLAGS[old_sid] = threading.Event()
    real_expire = d.store.expire_task_if_current; newer = []; marker = object()
    def reclaim(*a, **kw):
        sid = real_expire(*a, **kw)
        if sid is not None:
            d.force('queued'); new = d.claim(); newer.append(new)
            config.STREAMS[d.sid(new)] = marker
            config.CANCEL_FLAGS[d.sid(new)] = threading.Event()
        return sid
    with mock.patch.object(d.store, 'expire_task_if_current', side_effect=reclaim):
        assert d.sweep()['running_timed_out'] == 1
    new_sid = d.sid(newer[0])
    assert config.STREAMS[new_sid] is marker
    assert not config.CANCEL_FLAGS[new_sid].is_set()


def test_post_commit_failure_is_not_exactly_once(d):
    task = d.claim()
    d.worker._inject_into_session.side_effect = RuntimeError('synthetic post-commit failure')
    with pytest.raises(RuntimeError, match='post-commit'):
        d.drive(task)
    assert d.store.get_task(d.task_id)['status'] == 'completed'
    d.worker._notify.assert_not_called()
    d.drive(task)
    assert d.calls == 1 and d.terminal() == [('done', 'completed')]


def test_legacy_writers_cannot_override_claimed_execution(d):
    task = d.claim()
    assert not d.store.set_result(d.task_id, 'unowned result')
    assert not d.store.update_status(d.task_id, 'completed')
    assert not d.store.update_status(d.task_id, 'queued')
    assert d.store.is_current_execution(task['_execution_claim'])
    assert d.store.cancel_task(d.task_id)
    assert not d.store.set_result(d.task_id, 'late result')
    assert not d.store.update_status(d.task_id, 'completed')


def test_startup_requeue_is_snapshot_conditional_and_bounded(d):
    task = d.claim(); d.force('failed', error='dispatch timeout', progress='{}')
    real_list = d.store.list_tasks
    def intervene(*a, **kw):
        rows = real_list(*a, **kw)
        d.force('queued'); d.claim()
        return rows
    with mock.patch.object(d.store, 'list_tasks', side_effect=intervene):
        assert _requeue_dispatch_timeouts(d.store) == 0
    assert d.store.get_task(d.task_id)['status'] == 'running'
    d.force('failed', error='dispatch timeout', progress='{"requeue_count":3}')
    assert _requeue_dispatch_timeouts(d.store) == 0


def test_startup_requeues_current_failure_once(d):
    d.claim(); d.force('failed', error='dispatch timeout', progress='{}')
    assert _requeue_dispatch_timeouts(d.store) == 1
    assert _requeue_dispatch_timeouts(d.store) == 0
    assert d.store.get_task(d.task_id)['progress']['requeue_count'] == 1


def test_startup_cleanup_preserves_fresh_heartbeat(d):
    d.claim()
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    d.force('running', started_at=old)
    assert d.store.cleanup_stale_running() == 0
    assert d.store.get_task(d.task_id)['status'] == 'running'


def test_prestart_cancel_flag_is_preserved_by_real_streaming_source():
    """Execute only the bounded initialization fragment, not the real producer."""
    source = ast.parse((Path(__file__).parents[1] / 'api/streaming.py').read_bytes())
    function = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == '_run_agent_streaming')
    start = next(i for i, n in enumerate(function.body) if isinstance(n, (ast.Assign, ast.With)) and 'cancel_event' in ast.unparse(n))
    statements = function.body[start:start + 2] if isinstance(function.body[start], ast.Assign) else [function.body[start]]
    flag = threading.Event(); flag.set()
    namespace = {'threading': threading, 'STREAMS_LOCK': threading.RLock(), 'CANCEL_FLAGS': {'synthetic-stream': flag}, 'stream_id': 'synthetic-stream'}
    exec(compile(ast.Module(body=statements, type_ignores=[]), '<stream-cancel-initialization>', 'exec'), namespace)
    assert namespace['cancel_event'] is flag and namespace['cancel_event'].is_set()


def test_cancellation_between_last_preview_and_terminal_commit(d):
    task = d.claim(); real_finish = d.store.finish_execution
    def cancel_before_commit(*a, **kw):
        assert d.store.cancel_task(d.task_id)
        return real_finish(*a, **kw)
    with mock.patch.object(d.store, 'finish_execution', side_effect=cancel_before_commit) as finish:
        d.drive(task)
    finish.assert_called_once()
    assert d.store.get_task(d.task_id)['status'] == 'cancelled'
    d.suppressed()


def test_worker_result_reaches_real_receipt_route_and_session(d):
    import hashlib
    from api.task_routes import handle_task_result
    from tests.test_background_tasks_3_4 import MockHandler
    task = d.claim()
    subscriber = REAL_QUEUE(); workers.TASK_SUBSCRIBERS[d.task_id] = [subscriber]
    d.worker._inject_into_session = workers.BackgroundWorker._inject_into_session
    d.drive(task)
    assert [subscriber.get_nowait()[0], subscriber.get_nowait()[0]] == ['token', 'done']
    message = config.SESSIONS['synthetic-session'].messages[-1]
    assert message['_bg_status'] == 'completed'
    assert message['content'] == 'synthetic result'
    d.worker._notify.assert_called_once()
    assert d.worker._notify.call_args.args[0]['status'] == 'completed'
    handler = MockHandler()
    with mock.patch('api.task_routes.get_task_store', return_value=d.store):
        handle_task_result(handler, {'task_id': d.task_id, 'receipt': '1'})
    body = handler.response_json()
    assert handler.status == 200 and body['result'] == message['content']
    assert body['receipt']['kind'] == 'received'
    assert body['receipt']['accepted'] is False
    assert body['receipt']['record']['snapshot_sha256'] == hashlib.sha256(body['result'].encode()).hexdigest()
    assert body['receipt']['record']['task_id'] == d.task_id


def test_route_only_signals_a_winning_running_cancellation(d):
    from api.task_routes import handle_task_cancel
    from tests.test_background_tasks_3_4 import MockHandler
    with mock.patch('api.task_routes.get_task_store', return_value=d.store):
        handler = MockHandler(); handle_task_cancel(handler, {'task_id': d.task_id})
        assert handler.response_json()['cancelled'] is True and config.CANCEL_FLAGS == {}
        d.force('queued'); task = d.claim(); assert d.finish(task)
        flag = threading.Event(); config.CANCEL_FLAGS[d.sid(task)] = flag
        handler = MockHandler(); handle_task_cancel(handler, {'task_id': d.task_id})
        assert handler.response_json()['cancelled'] is False and not flag.is_set()


@pytest.mark.parametrize('race', ['requeue', 'new-claim'])
def test_cancel_cas_rechecks_status_and_token_across_connections(tmp_path, race):
    path = tmp_path / 'cancel-cas.db'
    a, b = stores.TaskStore(path), stores.TaskStore(path)
    task = a.create_task('s', 'synthetic', 'm', 'w')
    claim = a.claim_execution(task['task_id']); connection = a._conn; observed = []
    class CapturedCursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def fetchone(self):
            row = self.cursor.fetchone()
            b._execute("UPDATE tasks SET status = 'failed', error = 'dispatch timeout' WHERE task_id = ?", (task['task_id'],))
            snapshot = b.list_tasks(status='failed', include_execution_token=True)[0]
            assert b.requeue_task_if_current(snapshot, progress={'requeue_count': 1})
            if race == 'new-claim':
                new = b.claim_execution(task['task_id']); assert new.token != claim.token
            observed.append(True)
            return row
    class ConnectionProxy:
        def execute(self, sql, params=()):
            cursor = connection.execute(sql, params)
            return CapturedCursor(cursor) if sql.startswith('SELECT execution_token, status') else cursor
        def commit(self):
            connection.commit()
    a._conn = ConnectionProxy()
    try:
        assert a.cancel_task_with_stream(task['task_id']) == (False, None)
        assert observed == [True]
        assert b.get_task(task['task_id'])['status'] == ('queued' if race == 'requeue' else 'running')
    finally:
        a._conn = connection; connection.close(); b._conn.close()


def test_concurrent_completion_and_cancel_have_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'concurrent-terminal.db'
    a, b = stores.TaskStore(path), stores.TaskStore(path)
    task = a.create_task('s', 'synthetic', 'm', 'w'); claim = a.claim_execution(task['task_id'])
    barrier = threading.Barrier(2)
    def finish():
        barrier.wait(timeout=5)
        return a.finish_execution(claim, status='completed', result='synthetic')
    def cancel():
        barrier.wait(timeout=5)
        return b.cancel_task(task['task_id'])
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            done, cancelled = pool.submit(finish), pool.submit(cancel)
            results = done.result(timeout=5), cancelled.result(timeout=5)
        assert sum(results) == 1
        assert a.get_task(task['task_id'])['status'] == ('completed' if results[0] else 'cancelled')
    finally:
        a._conn.close(); b._conn.close()


# Frozen pre-migration shape: no updated_at, tool_log or execution_token.
LEGACY_SCHEMA = """
CREATE TABLE tasks (
 task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', prompt TEXT NOT NULL,
 model TEXT NOT NULL DEFAULT '', workspace TEXT NOT NULL DEFAULT '',
 attachments TEXT NOT NULL DEFAULT '[]', result TEXT NOT NULL DEFAULT '',
 progress TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
 notify_config TEXT NOT NULL DEFAULT '{}', profile TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT '',
 completed_at TEXT NOT NULL DEFAULT '', cancelled_at TEXT NOT NULL DEFAULT ''
);
"""


def test_legacy_database_migrates_without_changing_existing_task(tmp_path):
    path = tmp_path / 'legacy.db'
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.execute("INSERT INTO tasks(task_id,session_id,prompt,created_at) VALUES ('legacy-task','s','synthetic','2020-01-01T00:00:00+00:00')")
    connection.commit(); connection.close()
    for _ in range(2):
        store = stores.TaskStore(path)
        try:
            row = store.get_task('legacy-task')
            assert row['status'] == 'queued' and row['prompt'] == 'synthetic'
            assert 'execution_token' not in row
            assert {'updated_at', 'tool_log', 'execution_token'} <= {r[1] for r in store._conn.execute('PRAGMA table_info(tasks)')}
        finally:
            store._conn.close()


def test_unexpected_migration_error_is_not_masked(tmp_path):
    path = tmp_path / 'denied-migration.db'
    connection = sqlite3.connect(path); connection.executescript(LEGACY_SCHEMA)
    def deny_alter(action, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK
    connection.set_authorizer(deny_alter)
    try:
        with mock.patch.object(stores.sqlite3, 'connect', return_value=connection):
            with pytest.raises(sqlite3.DatabaseError):
                stores.TaskStore(path)
    finally:
        connection.close()


def test_reclaim_resets_prior_terminal_values_and_uses_fresh_token(d):
    old = d.claim(); assert d.finish(old)
    d.force('queued', error='old failure', cancelled_at='old cancellation')
    current = d.claim()
    assert d.sid(old) != d.sid(current)
    row = d.store.get_task(d.task_id)
    assert all(row[k] == '' for k in ('result', 'error', 'completed_at', 'cancelled_at'))
    assert not d.store.finish_execution(old['_execution_claim'], status='completed', result='late')


def test_invalid_claims_and_snapshots_do_not_mutate_state(d):
    task = d.claim(); original = d.store.get_task(d.task_id)
    for claim in (None, {}, 'claim', stores.ExecutionClaim(d.task_id, ''), stores.ExecutionClaim(d.task_id, 'A' * 32)):
        assert not d.store.is_current_execution(claim)
        assert not d.store.finish_execution(claim, status='completed', result='unowned')
    for snapshot in (None, {}, {'task_id': d.task_id, 'status': 'running'}):
        assert d.store.expire_task_if_current(snapshot, error='bad', completed_at='bad') is None
        assert not d.store.requeue_task_if_current(snapshot, progress={})
    assert d.store.get_task(d.task_id) == original
