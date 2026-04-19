"""api/task_worker.py — Background task worker daemon thread.

Polls TaskStore for queued tasks every 2 seconds, picks one at a time,
and runs it through the same _run_agent_streaming() pipeline.
Events are captured into the DB instead of an SSE queue.

Starts as a daemon thread from server.py main().

Task 4: Profile support.
  If a task has a 'profile' field set, the worker switches to that Hermes
  profile before invoking the agent, then restores the previous profile
  after the task completes. This allows background tasks to run under a
  role-specific config (model, toolsets, SOUL.md) without affecting the
  main UI's active profile.
"""

import json
import queue
import threading
import time

from api.task_store import TaskStore

# Live stream subscribers: task_id -> list[queue.Queue]
# Browser SSE consumers register here to watch a running task in real-time.
TASK_SUBSCRIBERS: dict[str, list[queue.Queue]] = {}
TASK_SUBSCRIBERS_LOCK = threading.Lock()


class BackgroundWorker:
    def __init__(self, store: TaskStore, poll_interval: float = 2.0, max_retries: int = 1):
        self._store = store
        self._poll_interval = poll_interval
        self._max_retries = max_retries
        self._running: bool = False
        self._thread: threading.Thread | None = None
        self._processed: int = 0
        self._errors: int = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn daemon thread running _loop(). Idempotent."""
        if self._running and self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="bg-worker")
        self._thread.start()

    def stop(self) -> None:
        """Signal the worker to stop and wait for it."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    # ── main loop ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                # NOTE: cleanup_stale_running is handled by task_sweeper (30min timeout).
                # Don't duplicate here — the sweeper runs every 60s and is the authority.
                task = self._store.get_next_queued()
                if task is None:
                    time.sleep(self._poll_interval)
                    continue

                if not self._store.claim_task(task['task_id']):
                    continue  # someone else claimed it

                self._execute_task(task)
            except Exception:
                # Never let the loop die due to an unexpected exception
                self._errors += 1
                time.sleep(self._poll_interval)

    # ── task execution ────────────────────────────────────────────────────────

    def _execute_task(self, task: dict) -> None:
        """Run the task through the agent pipeline, capturing all events.

        Task 4: If the task specifies a 'profile', switch to it before running
        the agent and restore the previous profile when done. Profile switching
        is wrapped in a try/finally so a failed switch never leaves the server
        in the wrong profile state.
        """
        from api.streaming import _run_agent_streaming, STREAMS, STREAMS_LOCK

        task_id = task['task_id']
        stream_id = f"bg_{task_id}"

        # Task 4: Apply profile before agent run
        _prev_profile = None
        _task_profile = task.get('profile')
        if _task_profile:
            try:
                from api.profiles import switch_profile, get_active_profile_name
                _prev_profile = get_active_profile_name()
                if _prev_profile != _task_profile:
                    switch_profile(_task_profile)
                    import logging as _log
                    _log.getLogger(__name__).info(
                        'Task %s: switched to profile %r (was %r)',
                        task_id[:8], _task_profile, _prev_profile,
                    )
                else:
                    _prev_profile = None  # no switch needed, nothing to restore
            except Exception as _pe:
                import logging as _log
                _log.getLogger(__name__).warning(
                    'Task %s: profile switch to %r failed: %s',
                    task_id[:8], _task_profile, _pe,
                )
                _prev_profile = None  # switch failed, don't attempt restore

        try:
            self._run_agent_for_task(task, task_id, stream_id)
        finally:
            # Task 4: Restore previous profile after task completes
            if _prev_profile is not None:
                try:
                    from api.profiles import switch_profile
                    switch_profile(_prev_profile)
                except Exception as _re:
                    import logging as _log
                    _log.getLogger(__name__).warning(
                        'Task %s: profile restore to %r failed: %s',
                        task_id[:8], _prev_profile, _re,
                    )

    def _run_agent_for_task(self, task: dict, task_id: str, stream_id: str) -> None:
        """Internal: run agent pipeline for a task and capture all events."""
        from api.streaming import _run_agent_streaming, STREAMS, STREAMS_LOCK

        # Create a capture queue (same as SSE stream, but we read from it ourselves)
        q = queue.Queue()
        with STREAMS_LOCK:
            STREAMS[stream_id] = q

        # Run agent in a thread (same as chat/start does)
        agent_thread = threading.Thread(
            target=_run_agent_streaming,
            args=(
                task['session_id'],
                task['prompt'],
                task['model'],
                task['workspace'],
                stream_id,
                json.loads(task.get('attachments', '[]'))
                if isinstance(task.get('attachments'), str)
                else (task.get('attachments') or []),
            ),
            daemon=True,
        )
        agent_thread.start()

        # Capture events from the queue
        full_text = []
        token_count = 0
        last_progress_update = 0

        try:
            while True:
                try:
                    event, data = q.get(timeout=60)
                except queue.Empty:
                    # Heartbeat — update progress to show we're still alive
                    self._store.update_progress(task_id, {
                        "tokens": token_count,
                        "status": "waiting",
                    })
                    continue

                # Broadcast event to any live SSE subscribers
                self._broadcast(task_id, event, data)

                if event == 'token':
                    text = data.get('text', '') if isinstance(data, dict) else str(data)
                    full_text.append(text)
                    token_count += 1
                    # Update progress every 50 tokens
                    if token_count - last_progress_update >= 50:
                        self._store.update_progress(task_id, {
                            "tokens": token_count,
                            "preview": ''.join(full_text)[-200:],
                        })
                        last_progress_update = token_count

                elif event == 'thinking':
                    # Capture thinking tokens too — could store separately if needed
                    pass

                elif event == 'tool_call':
                    self._store.update_progress(task_id, {
                        "tokens": token_count,
                        "current_tool": data.get('name', '') if isinstance(data, dict) else '',
                    })

                elif event == 'done':
                    result_text = ''.join(full_text)
                    self._store.set_result(task_id, result_text, status='completed')
                    self._processed += 1
                    # Inject into session message history
                    self._inject_into_session(task, result_text)
                    # Fire notification
                    self._notify(task, result_text)
                    break

                elif event in ('error', 'cancel'):
                    error_msg = (
                        data.get('message', str(data))
                        if isinstance(data, dict)
                        else str(data)
                    )
                    self._store.update_status(task_id, 'failed', error=error_msg)
                    self._errors += 1
                    break

        finally:
            # Always clean up the stream slot
            with STREAMS_LOCK:
                STREAMS.pop(stream_id, None)

    # ── notification ──────────────────────────────────────────────────────────

    @staticmethod
    def _broadcast(task_id: str, event: str, data) -> None:
        """Send event to all live SSE subscribers for this task."""
        with TASK_SUBSCRIBERS_LOCK:
            subs = TASK_SUBSCRIBERS.get(task_id)
            if not subs:
                return
            dead = []
            for i, sq in enumerate(subs):
                try:
                    sq.put_nowait((event, data))
                except Exception:
                    dead.append(i)
            for i in reversed(dead):
                subs.pop(i)

    @staticmethod
    def _inject_into_session(task: dict, result: str) -> None:
        """Append user prompt + assistant result into the session message history.

        This makes background task results appear in the main chat stream
        when the user next loads the session.
        """
        try:
            from api.models import get_session
            session = get_session(task.get('session_id', ''))
            if session is None:
                return
            import time as _time
            ts = _time.time()
            # Add user message (the task prompt)
            session.messages.append({
                'role': 'user',
                'content': task.get('prompt', ''),
                '_ts': ts,
                '_bg_task': task.get('task_id', ''),
                '_bg_model': task.get('model', ''),
                '_bg_workspace': task.get('workspace', ''),
                '_bg_profile': task.get('profile', ''),
            })
            # Add assistant message (the result)
            # Calculate duration from started_at -> completed_at
            _dur = ''
            try:
                from datetime import datetime as _dt, timezone as _tz
                sa = task.get('started_at', '')
                ca = task.get('completed_at', '')
                if sa and ca:
                    s = _dt.fromisoformat(sa)
                    c = _dt.fromisoformat(ca)
                    secs = int((c - s).total_seconds())
                    if secs >= 60:
                        _dur = f'{secs // 60}m {secs % 60}s'
                    else:
                        _dur = f'{secs}s'
            except Exception:
                pass
            session.messages.append({
                'role': 'assistant',
                'content': result,
                '_ts': ts + 0.001,
                '_bg_task': task.get('task_id', ''),
                '_bg_model': task.get('model', ''),
                '_bg_workspace': task.get('workspace', ''),
                '_bg_profile': task.get('profile', ''),
                '_bg_duration': _dur,
                '_bg_status': task.get('status', ''),
            })
            session.save()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('Failed to inject task result into session: %s', exc)

    def _notify(self, task: dict, result: str) -> None:
        """Fire completion notification; failure must not crash the worker."""
        notify_config = task.get('notify_config')
        if not notify_config:
            return
        # Empty dict or falsy value — nothing to do
        if isinstance(notify_config, dict) and not notify_config:
            return
        try:
            from api.task_notify import notify_task_complete
            notify_task_complete(task, result)
        except Exception:
            pass  # notification failure is non-fatal

    # ── status ────────────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "processed": self._processed,
            "errors": self._errors,
        }


# ── Module-level singleton ────────────────────────────────────────────────────

_worker = None


def start_background_worker() -> BackgroundWorker:
    global _worker
    if _worker is None:
        from api.task_store import get_task_store
        _worker = BackgroundWorker(get_task_store())
    _worker.start()
    return _worker


def get_worker() -> BackgroundWorker | None:
    return _worker


# ── Live stream helpers ───────────────────────────────────────────────────────

def subscribe_task(task_id: str) -> queue.Queue:
    """Register a new SSE subscriber for a running task. Returns the queue to read from."""
    q = queue.Queue()
    with TASK_SUBSCRIBERS_LOCK:
        if task_id not in TASK_SUBSCRIBERS:
            TASK_SUBSCRIBERS[task_id] = []
        TASK_SUBSCRIBERS[task_id].append(q)
    return q


def unsubscribe_task(task_id: str, q: queue.Queue) -> None:
    """Remove a subscriber queue."""
    with TASK_SUBSCRIBERS_LOCK:
        subs = TASK_SUBSCRIBERS.get(task_id)
        if subs:
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs:
                del TASK_SUBSCRIBERS[task_id]
