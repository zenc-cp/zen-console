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
from dataclasses import dataclass, field


def _execution_stream_id(task_id: str, token: str) -> str:
    return f"bg_{task_id}_{token}" if token else f"bg_{task_id}"


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    task_id: str
    token: str = field(repr=False)
    version: int = field(default=1, init=False)

    @property
    def stream_id(self) -> str:
        return _execution_stream_id(self.task_id, self.token)


def _valid_claim(claim) -> bool:
    return (type(claim) is ExecutionClaim and type(claim.version) is int
            and claim.version == 1 and type(claim.task_id) is str
            and 1 <= len(claim.task_id) <= 64 and type(claim.token) is str
            and len(claim.token) == 32
            and all(c in '0123456789abcdef' for c in claim.token))


def _snapshot_values(snapshot):
    if type(snapshot) is not dict:
        return None
    fields = ('task_id', 'status', 'execution_token', 'started_at', 'created_at', 'updated_at')
    values = tuple(snapshot.get(name) for name in fields)
    if any(type(value) is not str for value in values):
        return None
    token = values[2]
    if not 1 <= len(values[0]) <= 64 or (token and (len(token) != 32 or any(c not in '0123456789abcdef' for c in token))):
        return None
    return values


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
    updated_at    TEXT NOT NULL DEFAULT '',
    execution_token TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""

# Migration: add updated_at column to existing DBs
_MIGRATION = """
ALTER TABLE tasks ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
"""

_MIGRATION2 = """
ALTER TABLE tasks ADD COLUMN tool_log TEXT NOT NULL DEFAULT '[]';
"""

_JSON_FIELDS = ('attachments', 'progress', 'notify_config', 'tool_log')


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
        # Inspect columns instead of masking unexpected migration failures.
        columns = {row[1] for row in self._conn.execute('PRAGMA table_info(tasks)')}
        for column, statement in (
            ('updated_at', _MIGRATION),
            ('tool_log', _MIGRATION2),
            ('execution_token', "ALTER TABLE tasks ADD COLUMN execution_token TEXT NOT NULL DEFAULT '';"),
        ):
            if column not in columns:
                self._conn.executescript(statement)
        self._conn.commit()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _row_to_dict(self, row, *, include_execution_token: bool = False) -> dict:
        if row is None:
            return None
        d = dict(row)
        if not include_execution_token:
            d.pop('execution_token', None)
        for field in _JSON_FIELDS:
            raw = d.get(field, '')
            if isinstance(raw, str) and raw:
                try:
                    d[field] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    d[field] = {} if field not in ('attachments', 'tool_log') else []
            elif not raw:
                d[field] = {} if field not in ('attachments', 'tool_log') else []
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
        # Dedup guard: if a task with the same profile + same cycle prefix
        # is already queued or running, refuse the new enqueue and return
        # the existing task. Prevents cron over-fire from swarming the queue
        # when an agent cycle takes longer than its cron interval.
        # See: zenops dispatch-timeout incident (Apr 29-30, 2026).
        cycle_key = (prompt or "").strip()[:80]
        if profile and cycle_key:
            row = self._fetchone(
                "SELECT task_id, status FROM tasks "
                "WHERE profile = ? AND substr(prompt, 1, 80) = ? "
                "AND status IN ('queued', 'running') LIMIT 1",
                (profile, cycle_key),
            )
            if row:
                existing = self.get_task(row[0])
                if existing is not None:
                    existing["deduped"] = True
                    existing["dedup_reason"] = (
                        f"prior task {row[0]} ({row[1]}) for profile='{profile}' "
                        f"cycle still active"
                    )
                return existing
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
        """Legacy setup only: cannot alter a claimed or terminal execution.

        Workers use finish_execution; maintenance uses captured-state methods.
        """
        allowed = {'started_at', 'completed_at', 'cancelled_at', 'error', 'result', 'progress'}
        if status not in ('queued', 'running', 'completed', 'failed', 'cancelled') or not set(kwargs) <= allowed:
            return False
        fields = {'status': status, 'updated_at': _utcnow()}
        fields.update(kwargs)
        set_clause = ', '.join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]
        cur = self._execute(
            f"UPDATE tasks SET {set_clause} WHERE task_id = ? AND execution_token = '' AND status = 'queued'",
            values,
        )
        return cur.rowcount > 0

    def update_progress(self, task_id: str, progress: dict) -> bool:
        cur = self._execute(
            "UPDATE tasks SET progress = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(progress), _utcnow(), task_id),
        )
        return cur.rowcount > 0

    def append_tool_log(self, task_id: str, entry: dict) -> bool:
        """Append a single tool log entry to the task's tool_log JSON array."""
        row = self._fetchone("SELECT tool_log FROM tasks WHERE task_id = ?", (task_id,))
        if row is None:
            return False
        raw = row[0] if isinstance(row[0], str) else '[]'
        try:
            log_entries = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            log_entries = []
        log_entries.append(entry)
        # Cap at 200 entries to prevent unbounded growth
        if len(log_entries) > 200:
            log_entries = log_entries[-200:]
        cur = self._execute(
            "UPDATE tasks SET tool_log = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(log_entries), _utcnow(), task_id),
        )
        return cur.rowcount > 0

    def set_result(self, task_id: str, result: str, status: str = 'completed') -> bool:
        """Legacy setup for never-claimed queued rows; not a worker completion API."""
        if status not in ('completed', 'failed'):
            return False
        completed_at = _utcnow()
        cur = self._execute(
            "UPDATE tasks SET result = ?, status = ?, completed_at = ?, updated_at = ? "
            "WHERE task_id = ? AND execution_token = '' AND status = 'queued'",
            (result, status, completed_at, completed_at, task_id),
        )
        return cur.rowcount > 0

    def cancel_task(self, task_id: str) -> bool:
        """Compatibility wrapper; the route also signals the captured stream."""
        return self.cancel_task_with_stream(task_id)[0]

    def cancel_task_with_stream(self, task_id: str) -> tuple[bool, str | None]:
        """Cancel the captured status/attempt, never a newer claim or requeue."""
        with self._lock:
            row = self._conn.execute(
                "SELECT execution_token, status FROM tasks WHERE task_id = ? AND status IN ('queued', 'running')",
                (task_id,),
            ).fetchone()
            if row is None:
                return False, None
            token, status = row['execution_token'], row['status']
            stamp = _utcnow()
            cur = self._conn.execute(
                "UPDATE tasks SET status = 'cancelled', cancelled_at = ?, updated_at = ? "
                "WHERE task_id = ? AND execution_token = ? AND status = ?",
                (stamp, stamp, task_id, token, status),
            )
            self._conn.commit()
            if cur.rowcount != 1:
                return False, None
            # No producer exists for a queued attempt; do not create orphan flags.
            return True, _execution_stream_id(task_id, token) if status == 'running' else None

    def list_tasks(
        self,
        status: str = None,
        session_id: str = None,
        limit: int = 50,
        offset: int = 0,
        *,
        include_execution_token: bool = False,
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
        return [self._row_to_dict(r, include_execution_token=include_execution_token) for r in rows]

    def get_next_queued(self) -> dict | None:
        """Return the oldest queued task (FIFO), or None."""
        row = self._fetchone(
            "SELECT * FROM tasks WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        )
        return self._row_to_dict(row)

    def claim_task(self, task_id: str) -> bool:
        """Compatibility only; workers must retain claim_execution's value."""
        return self.claim_execution(task_id) is not None

    def claim_execution(self, task_id: str) -> ExecutionClaim | None:
        if type(task_id) is not str or not 1 <= len(task_id) <= 64:
            return None
        claim = ExecutionClaim(task_id, uuid.uuid4().hex)
        stamp = _utcnow()
        cur = self._execute(
            "UPDATE tasks SET status = 'running', started_at = ?, updated_at = ?, execution_token = ?, "
            "result = '', error = '', completed_at = '', cancelled_at = '' "
            "WHERE task_id = ? AND status = 'queued'",
            (stamp, stamp, claim.token, task_id),
        )
        return claim if cur.rowcount == 1 else None

    def is_current_execution(self, claim) -> bool:
        if not _valid_claim(claim):
            return False
        return self._fetchone(
            "SELECT 1 FROM tasks WHERE task_id = ? AND status = 'running' AND execution_token = ?",
            (claim.task_id, claim.token),
        ) is not None

    def get_execution_task(self, claim, *, status='running') -> dict | None:
        """Read metadata for this attempt only, without exposing its token."""
        if not _valid_claim(claim) or status not in ('running', 'completed', 'failed'):
            return None
        return self._row_to_dict(self._fetchone(
            "SELECT * FROM tasks WHERE task_id = ? AND execution_token = ? AND status = ?",
            (claim.task_id, claim.token, status),
        ))

    def finish_execution(self, claim, *, status, result='', error='') -> bool:
        """Only a winning running-to-terminal commit permits publication."""
        if not _valid_claim(claim) or status not in ('completed', 'failed'):
            return False
        stamp = _utcnow()
        cur = self._execute(
            "UPDATE tasks SET status = ?, result = ?, error = ?, completed_at = ?, updated_at = ? "
            "WHERE task_id = ? AND status = 'running' AND execution_token = ?",
            (status, result, error, stamp, stamp, claim.task_id, claim.token),
        )
        return cur.rowcount == 1

    def expire_task_if_current(self, snapshot, *, error, completed_at) -> str | None:
        """Compare captured identity, status and liveness before timing out."""
        values = _snapshot_values(snapshot)
        if values is None or snapshot['status'] not in ('queued', 'running'):
            return None
        cur = self._execute(
            "UPDATE tasks SET status = 'failed', error = ?, completed_at = ?, updated_at = ? "
            "WHERE task_id = ? AND status = ? AND execution_token = ? "
            "AND started_at = ? AND created_at = ? AND updated_at = ?",
            (error, completed_at, completed_at, *values),
        )
        return _execution_stream_id(snapshot['task_id'], snapshot['execution_token']) if cur.rowcount == 1 else None

    def requeue_task_if_current(self, snapshot, *, progress) -> bool:
        """Requeue a captured dispatch failure without overwriting newer state."""
        values = _snapshot_values(snapshot)
        if (values is None or snapshot['status'] != 'failed' or type(progress) is not dict
                or type(snapshot.get('error')) is not str or 'dispatch timeout' not in snapshot['error']):
            return False
        cur = self._execute(
            "UPDATE tasks SET status = 'queued', error = '', started_at = '', completed_at = '', "
            "progress = ?, updated_at = ? WHERE task_id = ? AND status = ? AND execution_token = ? "
            "AND started_at = ? AND created_at = ? AND updated_at = ? AND error = ?",
            (json.dumps(progress), _utcnow(), *values, snapshot['error']),
        )
        return cur.rowcount == 1

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
        """Atomically expire current old running rows without a recent heartbeat."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
        cutoff_str = cutoff.isoformat()
        stamp = _utcnow()
        cur = self._execute(
            """
            UPDATE tasks SET status = 'failed', error = 'Stale: worker timeout',
                             completed_at = ?, updated_at = ?
            WHERE status = 'running'
              AND started_at != ''
              AND started_at < ?
              AND (updated_at = '' OR updated_at < ?)
            """,
            (stamp, stamp, cutoff_str, cutoff_str),
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
