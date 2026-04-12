"""api/task_worker.py — Background task worker daemon thread.

Polls TaskStore for queued tasks every 2 seconds, picks one at a time,
and runs it through the same _run_agent_streaming() pipeline.
Events are captured into the DB instead of an SSE queue.

Starts as a daemon thread from server.py main().
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
                # Clean up stale running tasks on each pass
                self._store.cleanup_stale_running(timeout_minutes=30)

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
        """Run the task through the agent pipeline, capturing all events."""
        from api.streaming import _run_agent_streaming, STREAMS, STREAMS_LOCK

        task_id = task['task_id']
        stream_id = f"bg_{task_id}"

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
            })
            # Add assistant message (the result)
            session.messages.append({
                'role': 'assistant',
                'content': result,
                '_ts': ts + 0.001,
                '_bg_task': task.get('task_id', ''),
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
