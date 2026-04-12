"""api/task_routes.py — REST endpoints for background task execution.

Provides task submission, status polling, result retrieval, and management.
Integrated into routes.py handle_get and handle_post.
"""

import logging
from urllib.parse import parse_qs, urlparse

from api.helpers import require, bad, j
from api.task_store import get_task_store

log = logging.getLogger(__name__)


# ── POST handlers ─────────────────────────────────────────────────────────────

def handle_task_submit(handler, body) -> True:
    """Submit a new background task.

    Required body fields: session_id, message
    Optional: model, workspace, attachments, notify (dict)
    """
    try:
        require(body, 'session_id', 'message')
    except ValueError as e:
        bad(handler, str(e))
        return True

    session_id = body['session_id']
    message = body['message']
    model = body.get('model', '')
    workspace = body.get('workspace', '')
    attachments = body.get('attachments', [])
    notify = body.get('notify', {})

    task = get_task_store().create_task(
        session_id=session_id,
        prompt=message,
        model=model,
        workspace=workspace,
        attachments=attachments,
        notify_config=notify,
    )
    j(handler, {'ok': True, 'task': task})
    return True


def handle_task_cancel(handler, body) -> True:
    """Cancel a queued or running task.

    Required body fields: task_id
    """
    try:
        require(body, 'task_id')
    except ValueError as e:
        bad(handler, str(e))
        return True

    task_id = body['task_id']

    # Also set the cancel flag if the task has an active stream
    stream_id = f"bg_{task_id}"
    try:
        from api.config import CANCEL_FLAGS
        CANCEL_FLAGS[stream_id] = True
    except Exception:
        pass

    cancelled = get_task_store().cancel_task(task_id)
    j(handler, {'ok': True, 'cancelled': cancelled})
    return True


def handle_task_retry(handler, body) -> True:
    """Retry a failed or cancelled task by creating a new task with the same params.

    Required body fields: task_id
    """
    try:
        require(body, 'task_id')
    except ValueError as e:
        bad(handler, str(e))
        return True

    task_id = body['task_id']
    store = get_task_store()
    original = store.get_task(task_id)

    if original is None:
        bad(handler, f'Task not found: {task_id}', 404)
        return True

    if original['status'] not in ('failed', 'cancelled'):
        bad(handler, f"Cannot retry task with status '{original['status']}'. Must be 'failed' or 'cancelled'.", 400)
        return True

    new_task = store.create_task(
        session_id=original['session_id'],
        prompt=original['prompt'],
        model=original.get('model', ''),
        workspace=original.get('workspace', ''),
        attachments=original.get('attachments', []),
        notify_config=original.get('notify_config', {}),
    )
    j(handler, {'ok': True, 'task': new_task})
    return True


# ── GET handlers ──────────────────────────────────────────────────────────────

def _qs(parsed) -> dict:
    """Extract query string params from a parsed URL or a plain dict."""
    if isinstance(parsed, dict):
        return parsed
    qs = parse_qs(parsed.query)
    return {k: v[0] for k, v in qs.items()}


def handle_task_get(handler, parsed) -> True:
    """Get a single task by task_id query param."""
    params = _qs(parsed)
    task_id = params.get('task_id', '').strip()
    if not task_id:
        bad(handler, 'Missing required query param: task_id')
        return True

    task = get_task_store().get_task(task_id)
    if task is None:
        bad(handler, f'Task not found: {task_id}', 404)
        return True

    j(handler, {'task': task})
    return True


def handle_task_list(handler, parsed) -> True:
    """List tasks with optional filtering.

    Query params: status, session_id, limit (default 50), offset (default 0)
    """
    params = _qs(parsed)
    status = params.get('status') or None
    session_id = params.get('session_id') or None
    try:
        limit = int(params.get('limit', 50))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(params.get('offset', 0))
    except (ValueError, TypeError):
        offset = 0

    store = get_task_store()
    tasks = store.list_tasks(status=status, session_id=session_id, limit=limit, offset=offset)
    total = store.count_by_status()
    j(handler, {'tasks': tasks, 'total': total})
    return True


def handle_task_result(handler, parsed) -> True:
    """Get the result (or partial progress) for a task.

    Query params: task_id
    """
    params = _qs(parsed)
    task_id = params.get('task_id', '').strip()
    if not task_id:
        bad(handler, 'Missing required query param: task_id')
        return True

    task = get_task_store().get_task(task_id)
    if task is None:
        bad(handler, f'Task not found: {task_id}', 404)
        return True

    payload = {
        'task_id': task['task_id'],
        'status': task['status'],
        'result': task.get('result', ''),
        'error': task.get('error', ''),
    }

    if task['status'] != 'completed':
        payload['progress'] = task.get('progress', {})

    j(handler, payload)
    return True


def handle_worker_status(handler, parsed) -> True:
    """Return the background worker status and task queue counts."""
    store = get_task_store()
    queue_counts = store.count_by_status()

    # Try to get live worker info if the worker module is loaded
    worker_info = {}
    try:
        from api.task_worker import get_worker
        worker = get_worker()
        worker_info = worker.status() if worker is not None else {'running': False, 'note': 'worker not started'}
    except Exception:
        worker_info = {'running': False, 'note': 'worker not started'}

    j(handler, {'worker': worker_info, 'queue': queue_counts})
    return True


# ── Route registration ────────────────────────────────────────────────────────

def register_task_routes_post(path, handler, body):
    """Called from routes.py handle_post. Returns True if handled, None if not."""
    if path == '/api/task/submit':
        return handle_task_submit(handler, body)
    if path == '/api/task/cancel':
        return handle_task_cancel(handler, body)
    if path == '/api/task/retry':
        return handle_task_retry(handler, body)
    return None


def register_task_routes_get(path, handler, parsed):
    """Called from routes.py handle_get. Returns True if handled, None if not."""
    if path == '/api/task':
        return handle_task_get(handler, parsed)
    if path == '/api/task/result':
        return handle_task_result(handler, parsed)
    if path == '/api/task/stream':
        return handle_task_stream(handler, parsed)
    if path == '/api/tasks':
        return handle_task_list(handler, parsed)
    if path == '/api/worker/status':
        return handle_worker_status(handler, parsed)
    if path == '/api/notifications/pending':
        return handle_notifications_pending(handler, parsed)
    return None


def handle_task_stream(handler, parsed):
    """SSE stream for a running background task. Live token/thinking/tool events.
    
    GET /api/task/stream?task_id=X
    
    Subscribe to the task's event broadcast. If the task is already completed,
    sends the result immediately and closes. If queued, waits until it starts.
    Browser can disconnect anytime — worker keeps running.
    """
    import json as _json
    import queue as _queue
    from urllib.parse import parse_qs
    from api.task_store import get_task_store
    from api.task_worker import subscribe_task, unsubscribe_task

    qs = parse_qs(parsed.query)
    task_id = qs.get('task_id', [''])[0]
    if not task_id:
        return j(handler, {'error': 'task_id required'}, status=400)

    store = get_task_store()
    task = store.get_task(task_id)
    if not task:
        return j(handler, {'error': 'task not found'}, status=404)

    # If already completed/failed, send result immediately as SSE then close
    if task['status'] in ('completed', 'failed', 'cancelled'):
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('X-Accel-Buffering', 'no')
        handler.end_headers()
        if task['status'] == 'completed':
            _sse_write(handler, 'done', {'result': task.get('result', '')})
        elif task['status'] == 'failed':
            _sse_write(handler, 'error', {'message': task.get('error', 'unknown error')})
        else:
            _sse_write(handler, 'cancel', {'message': 'Task was cancelled'})
        return True

    # Subscribe to live events
    sub_q = subscribe_task(task_id)
    
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('X-Accel-Buffering', 'no')
    handler.send_header('Connection', 'keep-alive')
    handler.end_headers()

    try:
        while True:
            try:
                event, data = sub_q.get(timeout=30)
            except _queue.Empty:
                # Heartbeat
                try:
                    handler.wfile.write(b': heartbeat\n\n')
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                continue

            _sse_write(handler, event, data)

            if event in ('done', 'error', 'cancel'):
                break
    except (BrokenPipeError, ConnectionResetError):
        pass  # browser disconnected — worker keeps running
    finally:
        unsubscribe_task(task_id, sub_q)

    return True


def handle_notifications_pending(handler, parsed):
    """GET /api/notifications/pending — returns and consumes pending browser notifications."""
    from api.task_notify import get_pending_notifications
    notifications = get_pending_notifications()
    return j(handler, notifications)


def _sse_write(handler, event, data):
    """Write a single SSE event to the handler."""
    import json as _json
    payload = _json.dumps(data) if not isinstance(data, str) else data
    try:
        handler.wfile.write(f'event: {event}\ndata: {payload}\n\n'.encode())
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
