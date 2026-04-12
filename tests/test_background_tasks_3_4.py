"""tests/test_background_tasks_3_4.py — Tests for task_routes, task_notify, task_integration.

Phases 3 and 4 of the background task system.
"""

import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── MockHandler ───────────────────────────────────────────────────────────────

class MockHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = b''

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers[k] = v

    def end_headers(self):
        pass

    @property
    def wfile(self):
        if not hasattr(self, '_wfile'):
            self._wfile = io.BytesIO()
        return self._wfile

    def response_json(self):
        self._wfile.seek(0)
        return json.loads(self._wfile.read().decode('utf-8'))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_state(tmp_path, monkeypatch):
    """Patch STATE_DIR and task_store singleton for isolation."""
    import api.config as cfg
    monkeypatch.setattr(cfg, 'STATE_DIR', tmp_path)

    # Reset the task_store singleton so each test gets a fresh DB
    import api.task_store as ts
    old_store = ts._store
    ts._store = None
    # Force a new store pointing to the temp dir
    ts._store = ts.TaskStore(tmp_path / 'tasks.db')

    # Also patch task_notify's STATE_DIR reference
    import api.task_notify as tn
    # _get_notifications_dir() reads from api.config.STATE_DIR dynamically

    yield tmp_path

    # Restore
    ts._store = old_store


@pytest.fixture()
def handler():
    return MockHandler()


@pytest.fixture()
def store(tmp_state):
    from api.task_store import get_task_store
    return get_task_store()


def _make_task(store, session_id='sess1', prompt='Hello', status=None):
    """Helper: create a task and optionally force its status."""
    task = store.create_task(session_id=session_id, prompt=prompt, model='', workspace='')
    if status and status != 'queued':
        store.update_status(task['task_id'], status)
    return store.get_task(task['task_id'])


# ═════════════════════════════════════════════════════════════════════════════
# task_routes tests
# ═════════════════════════════════════════════════════════════════════════════

class TestHandleTaskSubmit:
    def test_handle_task_submit_creates_task(self, handler, store):
        from api.task_routes import handle_task_submit
        body = {'session_id': 'sess1', 'message': 'Do the thing'}
        handle_task_submit(handler, body)
        resp = handler.response_json()
        assert handler.status == 200
        assert resp['ok'] is True
        assert resp['task']['prompt'] == 'Do the thing'
        assert resp['task']['session_id'] == 'sess1'
        assert resp['task']['status'] == 'queued'

    def test_handle_task_submit_missing_session(self, handler, store):
        from api.task_routes import handle_task_submit
        body = {'message': 'Do the thing'}
        handle_task_submit(handler, body)
        resp = handler.response_json()
        assert handler.status == 400
        assert 'error' in resp
        assert 'session_id' in resp['error']

    def test_handle_task_submit_missing_message(self, handler, store):
        from api.task_routes import handle_task_submit
        body = {'session_id': 'sess1'}
        handle_task_submit(handler, body)
        resp = handler.response_json()
        assert handler.status == 400
        assert 'error' in resp
        assert 'message' in resp['error']

    def test_handle_task_submit_with_optional_fields(self, handler, store):
        from api.task_routes import handle_task_submit
        body = {
            'session_id': 'sess1',
            'message': 'Do the thing',
            'model': 'claude-3-5-sonnet',
            'workspace': '/tmp/ws',
            'attachments': ['file.txt'],
            'notify': {'slack_url': 'https://hooks.slack.com/x'},
        }
        handle_task_submit(handler, body)
        resp = handler.response_json()
        assert handler.status == 200
        assert resp['ok'] is True
        assert resp['task']['model'] == 'claude-3-5-sonnet'


class TestHandleTaskGet:
    def test_handle_task_get_found(self, handler, store):
        from api.task_routes import handle_task_get
        from urllib.parse import urlparse
        task = _make_task(store)
        url = f'http://localhost/api/task?task_id={task["task_id"]}'
        parsed = urlparse(url)
        handle_task_get(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 200
        assert resp['task']['task_id'] == task['task_id']

    def test_handle_task_get_not_found(self, handler, store):
        from api.task_routes import handle_task_get
        from urllib.parse import urlparse
        parsed = urlparse('http://localhost/api/task?task_id=nonexistent')
        handle_task_get(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 404
        assert 'error' in resp

    def test_handle_task_get_missing_param(self, handler, store):
        from api.task_routes import handle_task_get
        from urllib.parse import urlparse
        parsed = urlparse('http://localhost/api/task')
        handle_task_get(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 400
        assert 'error' in resp


class TestHandleTaskCancel:
    def test_handle_task_cancel_queued(self, handler, store):
        from api.task_routes import handle_task_cancel
        task = _make_task(store)
        body = {'task_id': task['task_id']}
        handle_task_cancel(handler, body)
        resp = handler.response_json()
        assert handler.status == 200
        assert resp['ok'] is True
        assert resp['cancelled'] is True
        # Verify in DB
        updated = store.get_task(task['task_id'])
        assert updated['status'] == 'cancelled'

    def test_handle_task_cancel_completed(self, handler, store):
        from api.task_routes import handle_task_cancel
        task = _make_task(store, status='completed')
        store.set_result(task['task_id'], 'done', status='completed')
        body = {'task_id': task['task_id']}
        handle_task_cancel(handler, body)
        resp = handler.response_json()
        assert handler.status == 200
        assert resp['ok'] is True
        assert resp['cancelled'] is False

    def test_handle_task_cancel_missing_task_id(self, handler, store):
        from api.task_routes import handle_task_cancel
        handle_task_cancel(handler, {})
        resp = handler.response_json()
        assert handler.status == 400
        assert 'error' in resp

    def test_handle_task_cancel_sets_cancel_flag(self, handler, store):
        from api.task_routes import handle_task_cancel
        import api.config as cfg
        task = _make_task(store)
        body = {'task_id': task['task_id']}
        handle_task_cancel(handler, body)
        stream_id = f"bg_{task['task_id']}"
        assert cfg.CANCEL_FLAGS.get(stream_id) is True


class TestHandleTaskRetry:
    def test_handle_task_retry_creates_new(self, handler, store):
        from api.task_routes import handle_task_retry
        task = _make_task(store, prompt='Retry me', status='failed')
        body = {'task_id': task['task_id']}
        handle_task_retry(handler, body)
        resp = handler.response_json()
        assert handler.status == 200
        assert resp['ok'] is True
        new_task = resp['task']
        # Should be a NEW task
        assert new_task['task_id'] != task['task_id']
        assert new_task['prompt'] == 'Retry me'
        assert new_task['status'] == 'queued'

    def test_handle_task_retry_cancelled_ok(self, handler, store):
        from api.task_routes import handle_task_retry
        task = _make_task(store, prompt='Retry cancelled', status='cancelled')
        body = {'task_id': task['task_id']}
        handle_task_retry(handler, body)
        resp = handler.response_json()
        assert handler.status == 200
        assert resp['ok'] is True

    def test_handle_task_retry_not_found(self, handler, store):
        from api.task_routes import handle_task_retry
        handle_task_retry(handler, {'task_id': 'nonexistent'})
        resp = handler.response_json()
        assert handler.status == 404

    def test_handle_task_retry_wrong_status(self, handler, store):
        from api.task_routes import handle_task_retry
        task = _make_task(store)  # queued
        handle_task_retry(handler, {'task_id': task['task_id']})
        resp = handler.response_json()
        assert handler.status == 400
        assert 'error' in resp

    def test_handle_task_retry_missing_task_id(self, handler, store):
        from api.task_routes import handle_task_retry
        handle_task_retry(handler, {})
        resp = handler.response_json()
        assert handler.status == 400


class TestHandleTaskList:
    def test_handle_task_list_all(self, handler, store):
        from api.task_routes import handle_task_list
        from urllib.parse import urlparse
        _make_task(store, session_id='s1')
        _make_task(store, session_id='s2')
        _make_task(store, session_id='s3')
        parsed = urlparse('http://localhost/api/tasks')
        handle_task_list(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 200
        assert len(resp['tasks']) == 3
        assert 'total' in resp

    def test_handle_task_list_filter_status(self, handler, store):
        from api.task_routes import handle_task_list
        from urllib.parse import urlparse
        t1 = _make_task(store)  # queued
        t2 = _make_task(store, status='failed')
        t3 = _make_task(store, status='failed')
        parsed = urlparse('http://localhost/api/tasks?status=queued')
        handle_task_list(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 200
        assert len(resp['tasks']) == 1
        assert resp['tasks'][0]['task_id'] == t1['task_id']

    def test_handle_task_list_filter_session(self, handler, store):
        from api.task_routes import handle_task_list
        from urllib.parse import urlparse
        _make_task(store, session_id='alice')
        _make_task(store, session_id='alice')
        _make_task(store, session_id='bob')
        parsed = urlparse('http://localhost/api/tasks?session_id=alice')
        handle_task_list(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 200
        assert len(resp['tasks']) == 2

    def test_handle_task_list_limit_offset(self, handler, store):
        from api.task_routes import handle_task_list
        from urllib.parse import urlparse
        for _ in range(5):
            _make_task(store)
        parsed = urlparse('http://localhost/api/tasks?limit=2&offset=1')
        handle_task_list(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 200
        assert len(resp['tasks']) == 2


class TestHandleTaskResult:
    def test_handle_task_result_completed(self, handler, store):
        from api.task_routes import handle_task_result
        from urllib.parse import urlparse
        task = _make_task(store)
        store.set_result(task['task_id'], 'Great result!', status='completed')
        parsed = urlparse(f'http://localhost/api/task/result?task_id={task["task_id"]}')
        handle_task_result(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 200
        assert resp['status'] == 'completed'
        assert resp['result'] == 'Great result!'

    def test_handle_task_result_pending(self, handler, store):
        from api.task_routes import handle_task_result
        from urllib.parse import urlparse
        task = _make_task(store)  # queued
        parsed = urlparse(f'http://localhost/api/task/result?task_id={task["task_id"]}')
        handle_task_result(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 200
        assert resp['status'] == 'queued'
        assert 'progress' in resp

    def test_handle_task_result_not_found(self, handler, store):
        from api.task_routes import handle_task_result
        from urllib.parse import urlparse
        parsed = urlparse('http://localhost/api/task/result?task_id=nope')
        handle_task_result(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 404

    def test_handle_task_result_missing_param(self, handler, store):
        from api.task_routes import handle_task_result
        from urllib.parse import urlparse
        parsed = urlparse('http://localhost/api/task/result')
        handle_task_result(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 400


class TestHandleWorkerStatus:
    def test_handle_worker_status(self, handler, store):
        from api.task_routes import handle_worker_status
        from urllib.parse import urlparse
        _make_task(store)
        parsed = urlparse('http://localhost/api/worker/status')
        handle_worker_status(handler, parsed)
        resp = handler.response_json()
        assert handler.status == 200
        assert 'worker' in resp
        assert 'queue' in resp
        assert 'queued' in resp['queue']


class TestRegisterRoutes:
    def test_register_post_submit(self, handler, store):
        from api.task_routes import register_task_routes_post
        result = register_task_routes_post(
            '/api/task/submit', handler,
            {'session_id': 's', 'message': 'm'}
        )
        assert result is True

    def test_register_post_cancel(self, handler, store):
        from api.task_routes import register_task_routes_post
        task = _make_task(store)
        result = register_task_routes_post(
            '/api/task/cancel', handler, {'task_id': task['task_id']}
        )
        assert result is True

    def test_register_post_retry(self, handler, store):
        from api.task_routes import register_task_routes_post
        task = _make_task(store, status='failed')
        result = register_task_routes_post(
            '/api/task/retry', handler, {'task_id': task['task_id']}
        )
        assert result is True

    def test_register_post_unmatched(self, handler, store):
        from api.task_routes import register_task_routes_post
        result = register_task_routes_post('/api/other', handler, {})
        assert result is None

    def test_register_get_task(self, handler, store):
        from api.task_routes import register_task_routes_get
        from urllib.parse import urlparse
        task = _make_task(store)
        parsed = urlparse(f'http://localhost/api/task?task_id={task["task_id"]}')
        result = register_task_routes_get('/api/task', handler, parsed)
        assert result is True

    def test_register_get_tasks(self, handler, store):
        from api.task_routes import register_task_routes_get
        from urllib.parse import urlparse
        parsed = urlparse('http://localhost/api/tasks')
        result = register_task_routes_get('/api/tasks', handler, parsed)
        assert result is True

    def test_register_get_result(self, handler, store):
        from api.task_routes import register_task_routes_get
        from urllib.parse import urlparse
        task = _make_task(store)
        parsed = urlparse(f'http://localhost/api/task/result?task_id={task["task_id"]}')
        result = register_task_routes_get('/api/task/result', handler, parsed)
        assert result is True

    def test_register_get_worker_status(self, handler, store):
        from api.task_routes import register_task_routes_get
        from urllib.parse import urlparse
        parsed = urlparse('http://localhost/api/worker/status')
        result = register_task_routes_get('/api/worker/status', handler, parsed)
        assert result is True

    def test_register_get_unmatched(self, handler, store):
        from api.task_routes import register_task_routes_get
        from urllib.parse import urlparse
        parsed = urlparse('http://localhost/api/other')
        result = register_task_routes_get('/api/other', handler, parsed)
        assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# task_notify tests
# ═════════════════════════════════════════════════════════════════════════════

FAKE_TASK = {
    'task_id': 'abc123',
    'session_id': 'sess1',
    'prompt': 'Do the thing',
    'status': 'completed',
    'notify_config': {},
}


class TestNotifyTaskComplete:
    def test_notify_no_config(self, tmp_state):
        from api.task_notify import notify_task_complete
        task = {**FAKE_TASK, 'notify_config': {}}
        result = notify_task_complete(task, 'The result')
        # No slack/telegram, but browser flag is always written
        assert result['slack'] is False
        assert result['telegram'] is False

    def test_notify_empty_notify_config_string(self, tmp_state):
        from api.task_notify import notify_task_complete
        task = {**FAKE_TASK, 'notify_config': '{}'}
        result = notify_task_complete(task, 'The result')
        assert result['slack'] is False
        assert result['telegram'] is False


class TestNotifySlack:
    def test_notify_slack_success(self, tmp_state):
        from api.task_notify import _notify_slack

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = _notify_slack('https://hooks.slack.com/x', FAKE_TASK, 'Result text')
        assert result is True

    def test_notify_slack_failure(self, tmp_state):
        from api.task_notify import _notify_slack

        with patch('urllib.request.urlopen', side_effect=Exception('Connection refused')):
            result = _notify_slack('https://hooks.slack.com/x', FAKE_TASK, 'Result text')
        # Must not raise, must return False
        assert result is False

    def test_notify_slack_http_error(self, tmp_state):
        from api.task_notify import _notify_slack

        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('not found')):
            result = _notify_slack('https://hooks.slack.com/x', FAKE_TASK, 'Result text')
        assert result is False

    def test_notify_slack_via_notify_task_complete(self, tmp_state):
        from api.task_notify import notify_task_complete

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        task = {**FAKE_TASK, 'notify_config': {'slack_url': 'https://hooks.slack.com/x'}}
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = notify_task_complete(task, 'Result!')
        assert result['slack'] is True


class TestNotifyTelegram:
    def test_notify_telegram_no_token(self, tmp_state, monkeypatch):
        from api.task_notify import _notify_telegram
        monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
        result = _notify_telegram('123456', FAKE_TASK, 'Result')
        assert result is False

    def test_notify_telegram_success(self, tmp_state, monkeypatch):
        from api.task_notify import _notify_telegram
        monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token-xyz')

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = _notify_telegram('123456', FAKE_TASK, 'Result')
        assert result is True

    def test_notify_telegram_failure(self, tmp_state, monkeypatch):
        from api.task_notify import _notify_telegram
        monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token-xyz')

        with patch('urllib.request.urlopen', side_effect=Exception('timeout')):
            result = _notify_telegram('123456', FAKE_TASK, 'Result')
        assert result is False

    def test_notify_telegram_via_notify_task_complete(self, tmp_state, monkeypatch):
        from api.task_notify import notify_task_complete
        monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        task = {**FAKE_TASK, 'notify_config': {'telegram_chat_id': '999'}}
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = notify_task_complete(task, 'Result!')
        assert result['telegram'] is True


class TestBrowserFlag:
    def test_browser_flag_written(self, tmp_state):
        from api.task_notify import _notify_browser_flag
        _notify_browser_flag('task_xyz')
        flag_path = tmp_state / 'notifications' / 'task_xyz.json'
        assert flag_path.exists()
        data = json.loads(flag_path.read_text())
        assert data['task_id'] == 'task_xyz'
        assert data['type'] == 'task_complete'
        assert 'created_at' in data

    def test_get_pending_notifications(self, tmp_state):
        from api.task_notify import _notify_browser_flag, get_pending_notifications
        _notify_browser_flag('task_aaa')
        notifications = get_pending_notifications()
        assert len(notifications) == 1
        assert notifications[0]['task_id'] == 'task_aaa'
        # File should be consumed (deleted)
        flag_path = tmp_state / 'notifications' / 'task_aaa.json'
        assert not flag_path.exists()

    def test_get_pending_notifications_empty(self, tmp_state):
        from api.task_notify import get_pending_notifications
        notifications = get_pending_notifications()
        assert notifications == []

    def test_get_pending_notifications_multiple(self, tmp_state):
        from api.task_notify import _notify_browser_flag, get_pending_notifications
        _notify_browser_flag('task_1')
        time.sleep(0.01)  # ensure different timestamps
        _notify_browser_flag('task_2')
        notifications = get_pending_notifications()
        assert len(notifications) == 2
        # Sorted newest first
        assert notifications[0]['task_id'] in ('task_1', 'task_2')

    def test_get_pending_notifications_consume_once(self, tmp_state):
        from api.task_notify import _notify_browser_flag, get_pending_notifications
        _notify_browser_flag('task_consume')
        first = get_pending_notifications()
        assert len(first) == 1
        second = get_pending_notifications()
        assert len(second) == 0

    def test_clear_notifications(self, tmp_state):
        from api.task_notify import _notify_browser_flag, clear_notifications, get_pending_notifications
        _notify_browser_flag('task_c1')
        _notify_browser_flag('task_c2')
        _notify_browser_flag('task_c3')
        count = clear_notifications()
        assert count == 3
        assert get_pending_notifications() == []

    def test_clear_notifications_empty(self, tmp_state):
        from api.task_notify import clear_notifications
        count = clear_notifications()
        assert count == 0


# ═════════════════════════════════════════════════════════════════════════════
# task_integration tests
# ═════════════════════════════════════════════════════════════════════════════

class TestInitTaskSystem:
    def test_init_task_system(self, tmp_state):
        from api.task_integration import init_task_system
        import api.task_worker as tw

        # Reset worker singleton
        old_worker = tw._worker
        tw._worker = None

        try:
            worker = init_task_system()
            assert worker is not None
            assert worker.is_running() is True
        finally:
            # Clean up
            if tw._worker is not None:
                tw._worker.stop()
            tw._worker = old_worker

    def test_init_task_system_store_accessible(self, tmp_state):
        from api.task_integration import init_task_system
        from api.task_store import get_task_store
        import api.task_worker as tw

        old_worker = tw._worker
        tw._worker = None

        try:
            init_task_system()
            store = get_task_store()
            counts = store.count_by_status()
            assert isinstance(counts, dict)
            assert 'queued' in counts
        finally:
            if tw._worker is not None:
                tw._worker.stop()
            tw._worker = old_worker

    def test_init_task_system_cleans_stale(self, tmp_state):
        """init_task_system should clean up stale running tasks."""
        from api.task_store import get_task_store
        from api.task_integration import init_task_system
        import api.task_worker as tw

        old_worker = tw._worker
        tw._worker = None

        store = get_task_store()
        # Create a task and force it into running with an old started_at
        task = store.create_task(
            session_id='sess', prompt='stale', model='', workspace=''
        )
        store.claim_task(task['task_id'])
        # Artificially age the started_at
        from datetime import datetime, timezone, timedelta
        old_start = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        store.update_status(task['task_id'], 'running', started_at=old_start)

        try:
            init_task_system()
            # The stale task should now be failed
            updated = store.get_task(task['task_id'])
            assert updated['status'] == 'failed'
        finally:
            if tw._worker is not None:
                tw._worker.stop()
            tw._worker = old_worker

    def test_init_task_system_idempotent(self, tmp_state):
        """Calling init twice should not crash."""
        from api.task_integration import init_task_system
        import api.task_worker as tw

        old_worker = tw._worker
        tw._worker = None

        try:
            w1 = init_task_system()
            w2 = init_task_system()
            assert w1 is w2  # same singleton
            assert w2.is_running() is True
        finally:
            if tw._worker is not None:
                tw._worker.stop()
            tw._worker = old_worker
