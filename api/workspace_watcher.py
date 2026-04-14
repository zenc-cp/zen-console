"""api/workspace_watcher.py — Watch workspace for file changes, emit SSE events."""

import os
import time
import json
import threading
from pathlib import Path

# Directories and file extensions to ignore
_IGNORE_DIRS = {'.git', '__pycache__', 'node_modules', '.mypy_cache', '.pytest_cache', '.tox'}
_IGNORE_EXTS = {'.pyc', '.pyo', '.pyd'}


def _should_ignore(path: str) -> bool:
    """Return True if the path should be excluded from change tracking."""
    p = Path(path)
    # Check each part for ignored directory names
    for part in p.parts:
        if part in _IGNORE_DIRS:
            return True
    # Check file extension
    if p.suffix in _IGNORE_EXTS:
        return True
    return False


def _scan(workspace_path: str) -> dict:
    """Return a dict of {str_path: mtime} for all files under workspace_path.

    Only regular files are tracked; symlinks, sockets, etc. are ignored.
    Files/dirs matching the ignore list are skipped.
    """
    snapshot = {}
    root = Path(workspace_path)
    if not root.exists():
        return snapshot
    try:
        for entry in root.rglob('*'):
            if not entry.is_file():
                continue
            str_path = str(entry)
            if _should_ignore(str_path):
                continue
            try:
                snapshot[str_path] = entry.stat().st_mtime
            except (OSError, PermissionError):
                pass
    except (OSError, PermissionError):
        pass
    return snapshot


def watch_workspace(workspace_path: str, event_queue, stop_event: threading.Event):
    """Watch workspace for file changes using polling (portable, no inotify dependency).

    Polls every 2 seconds. Detects:
    - New files (created)
    - Modified files (mtime changed)
    - Deleted files

    Emits events to event_queue as:
        ('file_change', {'type': 'created|modified|deleted', 'path': '...', 'size': N})

    Ignores: .git/, __pycache__/, node_modules/, .pyc files

    Args:
        workspace_path: Absolute path to the directory to watch.
        event_queue:    A queue.Queue (or compatible) to push events onto.
        stop_event:     A threading.Event; when set, the watcher exits cleanly.
    """
    POLL_INTERVAL = 2.0  # seconds

    # Build initial snapshot
    previous = _scan(workspace_path)

    while not stop_event.is_set():
        stop_event.wait(POLL_INTERVAL)
        if stop_event.is_set():
            break

        current = _scan(workspace_path)

        # --- Detect created and modified files ---
        for path, mtime in current.items():
            if path not in previous:
                # New file
                size = 0
                try:
                    size = os.path.getsize(path)
                except OSError:
                    pass
                event_queue.put(('file_change', {
                    'type': 'created',
                    'path': path,
                    'size': size,
                }))
            elif mtime != previous[path]:
                # Modified file
                size = 0
                try:
                    size = os.path.getsize(path)
                except OSError:
                    pass
                event_queue.put(('file_change', {
                    'type': 'modified',
                    'path': path,
                    'size': size,
                }))

        # --- Detect deleted files ---
        for path in previous:
            if path not in current:
                event_queue.put(('file_change', {
                    'type': 'deleted',
                    'path': path,
                    'size': 0,
                }))

        previous = current


# ---------------------------------------------------------------------------
# SSE route handler (to be registered in routes.py — see CHANGES.md)
# ---------------------------------------------------------------------------
#
# Example integration snippet for routes.py handle_get():
#
#   elif path == '/api/workspace/watch':
#       from api.workspace_watcher import handle_workspace_watch
#       handle_workspace_watch(handler, parsed)
#       return True
#


def handle_workspace_watch(handler, parsed):
    """SSE endpoint: GET /api/workspace/watch?path=/home/slimslimchan/claw

    Streams file-change events as SSE until the client disconnects.
    Each event is of the form:

        event: file_change
        data: {"type": "created|modified|deleted", "path": "...", "size": N}

    Query parameters:
        path (required): Absolute path to the directory to watch.
    """
    import queue
    from urllib.parse import parse_qs

    qs = parse_qs(parsed.query)
    watch_path = qs.get('path', [''])[0].strip()
    if not watch_path:
        # Default to DEFAULT_WORKSPACE if no path given
        try:
            from api.config import DEFAULT_WORKSPACE
            watch_path = str(DEFAULT_WORKSPACE)
        except ImportError:
            watch_path = os.path.expanduser('~')

    watch_path = os.path.expanduser(watch_path)
    watch_path = os.path.realpath(watch_path)

    # Security: reject paths that do not exist
    if not os.path.isdir(watch_path):
        handler.send_response(400)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps({'error': f'Not a directory: {watch_path}'}).encode())
        return

    # Set up SSE response headers
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('X-Accel-Buffering', 'no')
    handler.send_header('Connection', 'keep-alive')
    handler.end_headers()

    # Send an initial "connected" event so the client knows the stream is live
    try:
        _sse_write(handler, 'connected', {
            'path': watch_path,
            'message': f'Watching {watch_path}',
        })
    except (BrokenPipeError, ConnectionResetError):
        return

    # Launch the watcher in a background thread
    eq = queue.Queue()
    stop_event = threading.Event()

    watcher_thread = threading.Thread(
        target=watch_workspace,
        args=(watch_path, eq, stop_event),
        daemon=True,
        name=f'workspace-watcher-{watch_path[:20]}',
    )
    watcher_thread.start()

    try:
        while True:
            try:
                event_type, data = eq.get(timeout=30)
            except queue.Empty:
                # Heartbeat to keep the connection alive
                try:
                    handler.wfile.write(b': heartbeat\n\n')
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                continue

            try:
                _sse_write(handler, event_type, data)
            except (BrokenPipeError, ConnectionResetError):
                break
    finally:
        stop_event.set()


def _sse_write(handler, event: str, data: dict):
    """Write a single SSE frame to the handler."""
    payload = json.dumps(data, ensure_ascii=False)
    frame = f'event: {event}\ndata: {payload}\n\n'
    handler.wfile.write(frame.encode('utf-8'))
    handler.wfile.flush()
