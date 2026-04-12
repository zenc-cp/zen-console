"""Tests for api/task_store.py (Phase 1) and api/task_worker.py (Phase 2)."""

import json
import queue
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.task_store import TaskStore


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_store(tmp_path):
    """Fresh TaskStore backed by a temp DB file."""
    return TaskStore(tmp_path / "tasks.db")


@pytest.fixture
def populated_store(tmp_store):
    """Store with a couple of tasks pre-created."""
    t1 = tmp_store.create_task("s1", "prompt A", "model1", "/ws")
    time.sleep(0.01)  # ensure ordering by created_at
    t2 = tmp_store.create_task("s1", "prompt B", "model1", "/ws")
    return tmp_store, t1, t2


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: TaskStore
# ─────────────────────────────────────────────────────────────────────────────

class TestCreateTask:
    def test_create_task_returns_dict(self, tmp_store):
        task = tmp_store.create_task("sess1", "Hello world", "gpt-4", "/workspace")
        assert isinstance(task, dict)
        assert task["task_id"]
        assert task["status"] == "queued"
        assert task["created_at"]
        assert task["prompt"] == "Hello world"
        assert task["model"] == "gpt-4"
        assert task["session_id"] == "sess1"

    def test_create_task_default_fields(self, tmp_store):
        task = tmp_store.create_task("s", "p", "m", "w")
        assert task["result"] == ""
        assert task["error"] == ""
        assert task["started_at"] == ""
        assert task["completed_at"] == ""
        assert task["cancelled_at"] == ""
        assert task["attachments"] == []
        assert task["progress"] == {}
        assert task["notify_config"] == {}


class TestGetTask:
    def test_get_task_found(self, tmp_store):
        created = tmp_store.create_task("s1", "prompt", "model", "/ws")
        fetched = tmp_store.get_task(created["task_id"])
        assert fetched is not None
        assert fetched["task_id"] == created["task_id"]

    def test_get_task_not_found(self, tmp_store):
        assert tmp_store.get_task("nonexistent_id") is None


class TestClaimTask:
    def test_claim_task_from_queued(self, tmp_store):
        task = tmp_store.create_task("s1", "p", "m", "w")
        result = tmp_store.claim_task(task["task_id"])
        assert result is True
        updated = tmp_store.get_task(task["task_id"])
        assert updated["status"] == "running"
        assert updated["started_at"] != ""

    def test_claim_task_already_running(self, tmp_store):
        task = tmp_store.create_task("s1", "p", "m", "w")
        tmp_store.claim_task(task["task_id"])  # first claim
        result = tmp_store.claim_task(task["task_id"])  # second claim
        assert result is False


class TestGetNextQueued:
    def test_get_next_queued_fifo(self, populated_store):
        store, t1, t2 = populated_store
        next_task = store.get_next_queued()
        assert next_task is not None
        assert next_task["task_id"] == t1["task_id"]  # oldest first

    def test_get_next_queued_empty(self, tmp_store):
        assert tmp_store.get_next_queued() is None

    def test_get_next_queued_skips_running(self, tmp_store):
        t = tmp_store.create_task("s1", "p", "m", "w")
        tmp_store.claim_task(t["task_id"])
        assert tmp_store.get_next_queued() is None


class TestSetResult:
    def test_set_result(self, tmp_store):
        task = tmp_store.create_task("s1", "p", "m", "w")
        ok = tmp_store.set_result(task["task_id"], "The answer is 42")
        assert ok is True
        updated = tmp_store.get_task(task["task_id"])
        assert updated["result"] == "The answer is 42"
        assert updated["status"] == "completed"
        assert updated["completed_at"] != ""

    def test_set_result_custom_status(self, tmp_store):
        task = tmp_store.create_task("s1", "p", "m", "w")
        tmp_store.set_result(task["task_id"], "oops", status="failed")
        updated = tmp_store.get_task(task["task_id"])
        assert updated["status"] == "failed"


class TestCancelTask:
    def test_cancel_task_queued(self, tmp_store):
        task = tmp_store.create_task("s1", "p", "m", "w")
        ok = tmp_store.cancel_task(task["task_id"])
        assert ok is True
        updated = tmp_store.get_task(task["task_id"])
        assert updated["status"] == "cancelled"
        assert updated["cancelled_at"] != ""

    def test_cancel_task_running(self, tmp_store):
        task = tmp_store.create_task("s1", "p", "m", "w")
        tmp_store.claim_task(task["task_id"])
        ok = tmp_store.cancel_task(task["task_id"])
        assert ok is True  # can cancel running tasks too

    def test_cancel_task_completed(self, tmp_store):
        task = tmp_store.create_task("s1", "p", "m", "w")
        tmp_store.set_result(task["task_id"], "done")
        ok = tmp_store.cancel_task(task["task_id"])
        assert ok is False  # can't cancel completed


class TestUpdateProgress:
    def test_update_progress(self, tmp_store):
        task = tmp_store.create_task("s1", "p", "m", "w")
        ok = tmp_store.update_progress(task["task_id"], {"tokens": 100, "preview": "hello"})
        assert ok is True
        updated = tmp_store.get_task(task["task_id"])
        assert updated["progress"]["tokens"] == 100
        assert updated["progress"]["preview"] == "hello"


class TestListTasks:
    def test_list_tasks_filter_status(self, tmp_store):
        t1 = tmp_store.create_task("s1", "p", "m", "w")
        t2 = tmp_store.create_task("s2", "p", "m", "w")
        tmp_store.claim_task(t2["task_id"])
        queued = tmp_store.list_tasks(status="queued")
        assert len(queued) == 1
        assert queued[0]["task_id"] == t1["task_id"]

    def test_list_tasks_filter_session(self, tmp_store):
        tmp_store.create_task("session-A", "p", "m", "w")
        tmp_store.create_task("session-A", "p", "m", "w")
        tmp_store.create_task("session-B", "p", "m", "w")
        results = tmp_store.list_tasks(session_id="session-A")
        assert len(results) == 2
        for r in results:
            assert r["session_id"] == "session-A"

    def test_list_tasks_ordered_by_created_desc(self, tmp_store):
        t1 = tmp_store.create_task("s", "first", "m", "w")
        time.sleep(0.01)
        t2 = tmp_store.create_task("s", "second", "m", "w")
        tasks = tmp_store.list_tasks()
        assert tasks[0]["task_id"] == t2["task_id"]  # newest first

    def test_list_tasks_limit_offset(self, tmp_store):
        for i in range(5):
            tmp_store.create_task("s", f"p{i}", "m", "w")
        page1 = tmp_store.list_tasks(limit=3, offset=0)
        page2 = tmp_store.list_tasks(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 2


class TestCountByStatus:
    def test_count_by_status(self, tmp_store):
        t1 = tmp_store.create_task("s", "p", "m", "w")
        t2 = tmp_store.create_task("s", "p", "m", "w")
        t3 = tmp_store.create_task("s", "p", "m", "w")
        tmp_store.claim_task(t2["task_id"])
        tmp_store.set_result(t3["task_id"], "done")
        counts = tmp_store.count_by_status()
        assert counts["queued"] == 1
        assert counts["running"] == 1
        assert counts["completed"] == 1
        assert counts["failed"] == 0
        assert counts["cancelled"] == 0

    def test_count_by_status_empty(self, tmp_store):
        counts = tmp_store.count_by_status()
        assert counts == {"queued": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}


class TestCleanupStaleRunning:
    def test_cleanup_stale_running(self, tmp_store):
        task = tmp_store.create_task("s", "p", "m", "w")
        tmp_store.claim_task(task["task_id"])

        # Manually backdate started_at to be 31 minutes ago
        past = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        tmp_store._execute(
            "UPDATE tasks SET started_at = ? WHERE task_id = ?",
            (past, task["task_id"]),
        )

        count = tmp_store.cleanup_stale_running(timeout_minutes=30)
        assert count == 1
        updated = tmp_store.get_task(task["task_id"])
        assert updated["status"] == "failed"
        assert "Stale" in updated["error"]

    def test_cleanup_stale_skips_fresh(self, tmp_store):
        task = tmp_store.create_task("s", "p", "m", "w")
        tmp_store.claim_task(task["task_id"])
        # Task was just started, should NOT be cleaned up
        count = tmp_store.cleanup_stale_running(timeout_minutes=30)
        assert count == 0
        assert tmp_store.get_task(task["task_id"])["status"] == "running"


class TestPurgeOld:
    def test_purge_old(self, tmp_store):
        t1 = tmp_store.create_task("s", "p", "m", "w")
        t2 = tmp_store.create_task("s", "p", "m", "w")
        tmp_store.set_result(t1["task_id"], "done")
        tmp_store.set_result(t2["task_id"], "done")

        # Backdate created_at to 8 days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        tmp_store._execute(
            "UPDATE tasks SET created_at = ? WHERE task_id = ?",
            (old_ts, t1["task_id"]),
        )

        deleted = tmp_store.purge_old(days=7)
        assert deleted == 1
        assert tmp_store.get_task(t1["task_id"]) is None
        assert tmp_store.get_task(t2["task_id"]) is not None

    def test_purge_old_does_not_delete_running(self, tmp_store):
        task = tmp_store.create_task("s", "p", "m", "w")
        tmp_store.claim_task(task["task_id"])
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        tmp_store._execute(
            "UPDATE tasks SET created_at = ? WHERE task_id = ?",
            (old_ts, task["task_id"]),
        )
        deleted = tmp_store.purge_old(days=7)
        assert deleted == 0  # running tasks not purged


class TestThreadSafety:
    def test_thread_safety(self, tmp_store):
        """Concurrent create_task from 5 threads all succeed."""
        results = []
        errors = []

        def create():
            try:
                t = tmp_store.create_task("s", "prompt", "model", "/ws")
                results.append(t["task_id"])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create) for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert len(errors) == 0
        assert len(results) == 5
        # All task_ids must be unique
        assert len(set(results)) == 5


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: BackgroundWorker
# ─────────────────────────────────────────────────────────────────────────────

def _make_mock_streaming(events):
    """
    Returns a mock _run_agent_streaming that pushes `events` into STREAMS[stream_id].
    events: list of (event_name, data_dict)
    """
    def mock_run(session_id, prompt, model, workspace, stream_id, attachments=None):
        from api.streaming import STREAMS
        q = STREAMS.get(stream_id)
        if q is None:
            return
        for ev, data in events:
            q.put((ev, data))
    return mock_run


class TestWorkerLifecycle:
    def test_worker_starts_and_stops(self, tmp_store):
        from api.task_worker import BackgroundWorker
        worker = BackgroundWorker(tmp_store, poll_interval=0.05)
        worker.start()
        time.sleep(0.1)
        assert worker.is_running() is True
        worker.stop()
        assert worker.is_running() is False

    def test_worker_start_idempotent(self, tmp_store):
        from api.task_worker import BackgroundWorker
        worker = BackgroundWorker(tmp_store, poll_interval=0.05)
        worker.start()
        worker.start()  # second call should be a no-op
        time.sleep(0.05)
        assert worker.is_running() is True
        worker.stop()


class TestWorkerProcessesTask:
    def test_worker_processes_queued_task(self, tmp_store):
        """Worker picks up queued task → status becomes running (and eventually completed)."""
        from api.task_worker import BackgroundWorker

        task = tmp_store.create_task("sess1", "What is 2+2?", "model", "/ws")

        token_events = [('token', {'text': 'Four'}), ('done', {})]
        mock_fn = _make_mock_streaming(token_events)

        worker = BackgroundWorker(tmp_store, poll_interval=0.05)
        with patch('api.task_worker.BackgroundWorker._execute_task',
                   wraps=lambda self_ref, t: _patched_execute(self_ref, t, mock_fn)):
            # Use a simpler approach: patch _run_agent_streaming directly
            pass

        with patch('api.streaming._run_agent_streaming', side_effect=mock_fn):
            worker.start()
            # Give the worker time to process
            deadline = time.time() + 5
            while time.time() < deadline:
                t = tmp_store.get_task(task["task_id"])
                if t["status"] in ("completed", "failed"):
                    break
                time.sleep(0.1)
            worker.stop()

        final = tmp_store.get_task(task["task_id"])
        assert final["status"] == "completed"
        assert "Four" in final["result"]

    def test_worker_handles_error(self, tmp_store):
        """Worker handles agent 'error' event → task status='failed'."""
        from api.task_worker import BackgroundWorker

        task = tmp_store.create_task("sess1", "broken prompt", "model", "/ws")

        error_events = [('error', {'message': 'Agent exploded'})]
        mock_fn = _make_mock_streaming(error_events)

        with patch('api.streaming._run_agent_streaming', side_effect=mock_fn):
            worker = BackgroundWorker(tmp_store, poll_interval=0.05)
            worker.start()
            deadline = time.time() + 5
            while time.time() < deadline:
                t = tmp_store.get_task(task["task_id"])
                if t["status"] in ("completed", "failed"):
                    break
                time.sleep(0.1)
            worker.stop()

        final = tmp_store.get_task(task["task_id"])
        assert final["status"] == "failed"
        assert "Agent exploded" in final["error"]

    def test_worker_skips_when_empty(self, tmp_store):
        """No tasks → worker sleeps without crashing, status() reports running."""
        from api.task_worker import BackgroundWorker

        worker = BackgroundWorker(tmp_store, poll_interval=0.05)
        worker.start()
        time.sleep(0.2)
        assert worker.is_running() is True
        st = worker.status()
        assert st["running"] is True
        worker.stop()

    def test_claim_prevents_double_processing(self, tmp_store):
        """Two workers can't claim the same task."""
        from api.task_worker import BackgroundWorker

        task = tmp_store.create_task("s", "p", "m", "w")
        claim1 = tmp_store.claim_task(task["task_id"])
        claim2 = tmp_store.claim_task(task["task_id"])
        assert claim1 is True
        assert claim2 is False

    def test_worker_multiple_token_events(self, tmp_store):
        """Worker correctly concatenates multiple token events into result."""
        from api.task_worker import BackgroundWorker

        task = tmp_store.create_task("s", "Count to three", "model", "/ws")

        events = [
            ('token', {'text': 'one'}),
            ('token', {'text': ' two'}),
            ('token', {'text': ' three'}),
            ('done', {}),
        ]
        mock_fn = _make_mock_streaming(events)

        with patch('api.streaming._run_agent_streaming', side_effect=mock_fn):
            worker = BackgroundWorker(tmp_store, poll_interval=0.05)
            worker.start()
            deadline = time.time() + 5
            while time.time() < deadline:
                t = tmp_store.get_task(task["task_id"])
                if t["status"] == "completed":
                    break
                time.sleep(0.1)
            worker.stop()

        final = tmp_store.get_task(task["task_id"])
        assert final["status"] == "completed"
        assert final["result"] == "one two three"

    def test_worker_status_dict(self, tmp_store):
        from api.task_worker import BackgroundWorker
        worker = BackgroundWorker(tmp_store, poll_interval=0.1)
        st = worker.status()
        assert "running" in st
        assert "processed" in st
        assert "errors" in st
        assert st["running"] is False
        worker.start()
        time.sleep(0.05)
        st2 = worker.status()
        assert st2["running"] is True
        worker.stop()
