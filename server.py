"""
Hermes Web UI -- Main server entry point.
Thin routing shell: imports Handler, delegates to api/routes.py, runs server.
All business logic lives in api/*.
"""
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from api.auth import check_auth
from api.config import HOST, PORT, STATE_DIR, SESSION_DIR, DEFAULT_WORKSPACE
from api.helpers import j
from api.routes import handle_get, handle_post


class Handler(BaseHTTPRequestHandler):
    server_version = 'HermesWebUI/0.2'
    def log_message(self, fmt, *args): pass  # suppress default Apache-style log

    def log_request(self, code='-', size='-'):
        """Structured JSON logs for each request."""
        import json as _json
        duration_ms = round((time.time() - getattr(self, '_req_t0', time.time())) * 1000, 1)
        record = _json.dumps({
            'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'method': self.command or '-',
            'path': self.path or '-',
            'status': int(code) if str(code).isdigit() else code,
            'ms': duration_ms,
        })
        print(f'[webui] {record}', flush=True)

    def do_GET(self):
        self._req_t0 = time.time()
        try:
            parsed = urlparse(self.path)
            if not check_auth(self, parsed): return
            result = handle_get(self, parsed)
            if result is False:
                return j(self, {'error': 'not found'}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            return  # client disconnected — silently ignore
        except Exception as e:
            print(f'[webui] ERROR {self.command} {self.path}\n' + traceback.format_exc(), flush=True)
            try:
                return j(self, {'error': 'Internal server error'}, status=500)
            except (BrokenPipeError, ConnectionResetError):
                return  # client already gone

    def do_POST(self):
        self._req_t0 = time.time()
        try:
            parsed = urlparse(self.path)
            if not check_auth(self, parsed): return
            result = handle_post(self, parsed)
            if result is False:
                return j(self, {'error': 'not found'}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            return  # client disconnected — silently ignore
        except Exception as e:
            print(f'[webui] ERROR {self.command} {self.path}\n' + traceback.format_exc(), flush=True)
            try:
                return j(self, {'error': 'Internal server error'}, status=500)
            except (BrokenPipeError, ConnectionResetError):
                return  # client already gone


def main():
    from api.config import print_startup_config, verify_hermes_imports, _HERMES_FOUND

    print_startup_config()

    ok, missing = verify_hermes_imports()
    if not ok and _HERMES_FOUND:
        print(f'[!!] Warning: Hermes agent found but missing modules: {missing}', flush=True)
        print('     Agent features may not work correctly.', flush=True)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)

    # Start background task worker
    try:
        from api.task_integration import init_task_system
        init_task_system()
    except Exception as e:
        print(f'  [tasks] Warning: background task system failed to start: {e}', flush=True)

    # Start stream reaper (cleans up zombie SSE streams every 60s)
    from api.config import STREAMS, STREAMS_LOCK, CANCEL_FLAGS
    import threading as _thr
    def _stream_reaper():
        import time as _t
        while True:
            _t.sleep(60)
            now = _t.time()
            stale = []
            with STREAMS_LOCK:
                for sid, q in list(STREAMS.items()):
                    # Stream is stale if queue is empty and was created >5 min ago
                    created = getattr(q, '_created', 0)
                    if not created:
                        q._created = now
                        continue
                    if now - created > 300 and q.empty():
                        stale.append(sid)
                for sid in stale:
                    STREAMS.pop(sid, None)
                    CANCEL_FLAGS.pop(sid, None)
            if stale:
                print(f'[webui] stream reaper: cleaned {len(stale)} zombie stream(s)', flush=True)
    _reaper = _thr.Thread(target=_stream_reaper, daemon=True, name='stream-reaper')
    _reaper.start()

    # Start task sweeper (cleans up stuck queued/running tasks)
    try:
        from api.task_sweeper import start_task_sweeper
        from api.task_store import get_task_store
        start_task_sweeper(get_task_store(), STREAMS, STREAMS_LOCK)
        print('  [tasks] Task sweeper started', flush=True)
    except Exception as e:
        print(f'  [tasks] Task sweeper failed: {e}', flush=True)


    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'  Hermes Web UI listening on http://{HOST}:{PORT}', flush=True)
    if HOST == '127.0.0.1':
        print(f'  Remote access: ssh -N -L {PORT}:127.0.0.1:{PORT} <user>@<your-server>', flush=True)
    print(f'  Then open:     http://localhost:{PORT}', flush=True)
    print('', flush=True)
    httpd.serve_forever()

if __name__ == '__main__':
    main()
