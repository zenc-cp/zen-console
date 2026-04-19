"""api/task_store.py — Persistent background task store (SQLite).

Tasks survive server restarts and browser disconnects.
DB path: {STATE_DIR}/tasks.db

Lifecycle: queued → running → completed | failed | cancelled
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    prompt        TEXT NOT NULL,
    model         TEXT NOT NULL DEFAULT '',
    workspace     TEXT NOT NULL DEFAULT '',
    attachments   TEXT NOT NULL DEFAULT '[]',
    result        TEXT NOT NULL DEFAULT '',
    progress      TEXT NOT NULL DEFAULT '{}',
    error         TEXT NOT NULL DEFAULT '',
    notify_config TEXT NOT NULL DEFAULT '{}',
    profile       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    started_at    TEXT NOT NULL DEFAULT '',
    completed_at  TEXT NOT NULL DEFAULT '',
    cancelled_at  TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""

# Migration: add updated_at column to existing DBs
_MIGRATION = """
ALTER TABLE tasks ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
"""

_JSON_FIELDS = ('attachments', 'progress', 'notify_config')


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, db_path: Path = None):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        # Run migration for existing DBs
        try:
            self._conn.executescript(_MIGRATION)
        except Exception:
            pass  # column already exists
        self._conn.commit()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _row_to_dict(self, row) -> dict:
        if row is None:
            return None
        d = dict(row)
        for field in _JSON_FIELDS:
            raw = d.get(field, '')
            if isinstance(raw, str) and raw:
                try:
                    d[field] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    d[field] = {} if field != 'attachments' else []
            elif not raw:
                d[field] = {} if field != 'attachments' else []
        return d

    def _execute(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _fetchone(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchone()

    def _fetchall(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    # ── public API ────────────────────────────────────────────────────────────

    def create_task(
        self,
        session_id: str,
        prompt: str,
        model: str,
        workspace: str,
        attachments=None,
        notify_config=None,
        profile=None,
    ) -> dict:
        task_id = uuid.uuid4().hex[:12]
        created_at = _utcnow()
        attachments_json = json.dumps(attachments or [])
        notify_json = json.dumps(notify_config or {})
        self._execute(
            """
            INSERT INTO tasks
                (task_id, session_id, status, prompt, model, workspace,
                 attachments, notify_config, profile, created_at)
            VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, session_id, prompt, model, workspace,
             attachments_json, notify_json, profile or '', created_at),
        )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict | None:
        row = self._fetchone("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        return self._row_to_dict(row)

    def update_status(self, task_id: str, status: str, **kwargs) -> bool:
        """Update status plus any extra fields (started_at, completed_at, error, etc.)."""
        fields = {'status': status, 'updated_at': _utcnow()}
        fields.update(kwargs)
        set_clause = ', '.join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]
        cur = self._execute(
            f"UPDATE tasks SET {set_clause} WHERE task_id = ?",
            values,
        )
        return cur.rowcount > 0

    def update_progress(self, task_id: str, progress: dict) -> bool:
        cur = self._execute(
            "UPDATE tasks SET progress = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(progress), _utcnow(), task_id),
        )
        return cur.rowcount > 0

    def set_result(self, task_id: str, result: str, status: str = 'completed') -> bool:
        completed_at = _utcnow()
        cur = self._execute(
            "UPDATE tasks SET result = ?, status = ?, completed_at = ?, updated_at = ? WHERE task_id = ?",
            (result, status, completed_at, completed_at, task_id),
        )
        return cur.rowcount > 0

    def cancel_task(self, task_id: str) -> bool:
        """Cancel only if queued or running. Returns True if cancelled."""
        cancelled_at = _utcnow()
        cur = self._execute(
            """
            UPDATE tasks SET status = 'cancelled', cancelled_at = ?, updated_at = ?
            WHERE task_id = ? AND status IN ('queued', 'running')
            """,
            (cancelled_at, cancelled_at, task_id),
        )
        return cur.rowcount > 0

    def list_tasks(
        self,
        status: str = None,
        session_id: str = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        conditions = []
        params = []
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params += [limit, offset]
        rows = self._fetchall(
            f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [self._row_to_dict(r) for r in rows]

    def get_next_queued(self) -> dict | None:
        """Return the oldest queued task (FIFO), or None."""
        row = self._fetchone(
            "SELECT * FROM tasks WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        )
        return self._row_to_dict(row)

    def claim_task(self, task_id: str) -> bool:
        """Atomically set status='running' only if currently 'queued'."""
        started_at = _utcnow()
        cur = self._execute(
            """
            UPDATE tasks SET status = 'running', started_at = ?
            WHERE task_id = ? AND status = 'queued'
            """,
            (started_at, task_id),
        )
        return cur.rowcount > 0

    def count_by_status(self) -> dict:
        rows = self._fetchall(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        )
        counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
        for row in rows:
            key = row["status"]
            if key in counts:
                counts[key] = row["cnt"]
        return counts

    def cleanup_stale_running(self, timeout_minutes: int = 30) -> int:
        """Mark running tasks that started more than timeout_minutes ago as failed."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
        cutoff_str = cutoff.isoformat()
        cur = self._execute(
            """
            UPDATE tasks SET status = 'failed', error = 'Stale: worker timeout'
            WHERE status = 'running'
              AND started_at != ''
              AND started_at < ?
            """,
            (cutoff_str,),
        )
        return cur.rowcount

    def purge_old(self, days: int = 7) -> int:
        """Delete completed/failed/cancelled tasks older than N days."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_str = cutoff.isoformat()
        cur = self._execute(
            """
            DELETE FROM tasks
            WHERE status IN ('completed', 'failed', 'cancelled')
              AND created_at < ?
            """,
            (cutoff_str,),
        )
        return cur.rowcount


# ── Module-level singleton ────────────────────────────────────────────────────

_store = None


def get_task_store() -> TaskStore:
    global _store
    if _store is None:
        from api.config import STATE_DIR
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _store = TaskStore(STATE_DIR / 'tasks.db')
    return _store
