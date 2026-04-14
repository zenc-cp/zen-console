# ZenConsole System Summary

**Name:** ZenConsole (Hermes Web UI for ZenOps)
**Repo:** github.com/zenc-cp/zen-console
**Runtime:** GCP VM (nanoclaw-v2-jp), port 8787
**Access:** https://z3nops.com/console/ (Caddy → zen-console :8787)
**Stack:** Python HTTP server + Vanilla JS (no build step, no framework)

---

## Architecture

```
Browser
   │
   │ HTTPS (z3nops.com/console/*)
   ▼
Caddy :443
   │
   │ reverse proxy
   ▼
zen-console/server.py  (port 8787)
   │
   ├──────────────────────────────────────────┐
   │                                          │
   ▼                                          ▼
api/  (REST handlers)                  static/  (HTML + JS)
   │                                          │
   ├─ routes.py                              ├─ index.html
   ├─ models.py                              ├─ sessions.js
   ├─ auth.py                                ├─ messages.js   ← SSE streaming
   ├─ streaming.py                           ├─ tasks.js
   ├─ model_router.py                        ├─ workspace.js
   ├─ task_routes.py                          ├─ ui.js
   ├─ task_store.py                           ├─ panels.js
   ├─ task_worker.py                          ├─ commands.js
   ├─ task_integration.py                     ├─ boot.js
   ├─ task_notify.py                          └─ style.css
   ├─ thinking_router.py
   ├─ vision.py
   ├─ upload.py
   ├─ helpers.py
   ├─ profiles.py
   ├─ workspace.py
   └─ config.py
            │
            ▼
       hermes-agent/  (venv)
       ~/claw/        (workspace root, configurable)
```

---

## Sessions + Messages Flow

```
User action (send message)
    │
    ▼
sessions.js: send()
    ├─ POST /api/session              ← create/update session
    │   returns {session_id, ...}
    │
    ├─ POST /api/chat/{id}/message    ← submit message
    │   returns {stream_id}
    │
    └─ SSE /console/api/chat/stream?stream_id=XXX
            │
            │  Server-side streaming (OpenRouter API)
            │  Yields: token, tool_call, done, error events
            ▼
        messages.js: _wireSSE(source)
            │
            ├─ 'token'   → _flushTokenBuffer()  → RAF → DOM append
            ├─ 'tool_call' → renderToolCallInline() → DOM
            ├─ 'done'    → finalizeMessage() → INFLIGHT cleanup
            └─ 'error'   → show error toast
```

**Bug fixed:** Old EventSource was never closed when switching chats.
Old RAF callbacks wrote to recycled DOM elements → messages disappeared.
Fix: `_activeEs` global, closed on `loadSession()` and new `send()`.

---

## Static Files (static/)

| File | Role |
|------|------|
| `index.html` | Single-page app shell |
| `sessions.js` | Session list, loadSession(), createSession(), deleteSession() |
| `messages.js` | SSE stream handling, message rendering, tool call display |
| `ui.js` | General UI helpers: toasts, modals, spinner, copy, renderMarkdown |
| `tasks.js` | Agent task delegation (task_store integration) |
| `workspace.js` | File browser, workspace path selector |
| `commands.js` | Slash commands (/exit, /agent, /model, /cost, /help) |
| `panels.js` | Approval card, panels UI |
| `boot.js` | App init, router, auth check |
| `style.css` | All styles (CSS custom properties, dark theme) |

---

## API Routes (api/)

### Session Routes (`api/routes.py`)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/sessions` | List all sessions |
| `POST` | `/api/session` | Create new session |
| `GET` | `/api/session?session_id=X` | Get session + messages |
| `DELETE` | `/api/session/{id}` | Delete session |

### Chat Routes (`api/routes.py`)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/chat/{id}/message` | Submit message, returns stream_id |
| `GET` | `/console/api/chat/stream?stream_id=X` | SSE stream of response tokens |

### Task Routes (`api/task_routes.py`)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/tasks` | List all tasks |
| `POST` | `/api/tasks` | Create task |
| `GET` | `/api/tasks/{id}` | Get task |
| `PATCH` | `/api/tasks/{id}` | Update task |
| `DELETE` | `/api/tasks/{id}` | Delete task |

### Auth (`api/auth.py`)
- Bearer token check on all `/api/*` routes
- Token validated against `HERMES_WEBUI_AUTH_PASSWORD` env / `.env` file

### Model Router (`api/model_router.py`)
- Routes requests to configured LLM (OpenRouter default)
- Supports multiple model profiles via `profiles.py`

### Thinking Router (`api/thinking_router.py`)
- Routes to thinking mode models (e.g., o3, deepseek-r1)
- Falls back to non-thinking for standard models

### Vision (`api/vision.py`)
- Image upload + vision model inference
- Stores uploaded images in `~/claw/uploads/`

### Upload (`api/upload.py`)
- File upload handler
- Stores files in `~/claw/uploads/`

### Workspace (`api/workspace.py`)
- File browse, read, write operations
- Sandboxed to `HERMES_WEBUI_DEFAULT_WORKSPACE` (default: `~/claw/`)

---

## Message Streaming (api/streaming.py)

Server pushes SSE events over HTTP:

```python
SSE event types:
  token       → {"token": "fragment", "done": false}
  tool_call   → {"tool_call": {"name": "...", "id": "..."}}
  tool_result → {"tool_result": {"id": "...", "result": "..."}}
  done        → {"done": true, "usage": {...}, "error": null}
  error       → {"done": true, "error": "message"}
```

Client (`messages.js`) reconstructs the full response from token fragments.
Tool calls rendered inline as collapsible cards during streaming.

---

## Task System (task_store + task_worker)

```
task_store (JSON file: ~/claw/.hermes/tasks.jsonl)
    ↑ task_routes.py (REST API)
    ↑ task_worker.py (background polling)
    ↑ task_integration.py (integrates with hermes-agent venv)
    ↑ task_notify.py (pushes results via SSE or polling)
```

Agents (Hunter, Sentinel, etc.) run as background tasks via `task_worker`.
Results stored in `task_store`, retrieved via `GET /api/tasks/{id}`.

---

## Approval Flow

```
Agent submits pending action
    → task_store (status: pending)
    → SSE event: {type: "approval_required", task_id: "..."}
    → panels.js: renderApprovalCard()
    → User clicks Approve / Reject
    → POST /api/tasks/{id}/approve|reject
    → task_worker picks up → executes
```

---

## Configuration

Environment variables (set in systemd service or `.env`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `HERMES_WEBUI_PORT` | 8787 | Listen port |
| `HERMES_WEBUI_HOST` | 0.0.0.0 | Bind address |
| `HERMES_WEBUI_ROUTE_PREFIX` | /console | URL prefix |
| `HERMES_WEBUI_AUTH_PASSWORD` | (required) | Bearer token |
| `HERMES_WEBUI_DEFAULT_WORKSPACE` | ~/claw | File browser root |
| `HERMES_WEBUI_DEFAULT_MODEL` | openrouter/minimax/minimax-m2.7 | Default LLM |
| `HERMES_WEBUI_AGENT_DIR` | ~/hermes-agent | Agent venv |
| `PYTHONPATH` | ~/hermes-agent | Python path |

---

## Service Configuration

```ini
# /etc/systemd/system/zen-console.service
[Service]
WorkingDirectory=/home/slimslimchan/zen-console
ExecStart=/home/slimslimchan/zen-console/venv-console/bin/python3 server.py
Environment=HERMES_WEBUI_PORT=8787
Environment=HERMES_WEBUI_ROUTE_PREFIX=/console
Environment=HERMES_WEBUI_AUTH_PASSWORD=***
Environment=HERMES_WEBUI_DEFAULT_WORKSPACE=/home/slimslimchan/claw
Environment=HERMES_WEBUI_DEFAULT_MODEL=openrouter/minimax/minimax-m2.7
EnvironmentFile=-/home/slimslimchan/claw/.env
Restart=always
```

---

## Deploy

zen-console has its **own git repo** (`github.com/zenc-cp/zen-console`), separate
from claw-stack-jp. The claw-stack-jp `deploy.yml` SSHes to the VM and does:

```bash
cd ~/zen-console && git pull origin master && sudo systemctl restart zen-console
```

This means zen-console can be updated independently of the main ZenOps stack.

---

## Docker Support (optional)

```yaml
# docker-compose.yml
services:
  zen-console:
    build: .
    ports: ["8787:8787"]
    environment:
      HERMES_WEBUI_AUTH_PASSWORD: ...
      HERMES_WEBUI_DEFAULT_WORKSPACE: /workspace
    volumes:
      - /path/to/claw:/workspace
```

---

## Tests

```bash
cd ~/zen-console
venv-console/bin/python -m pytest tests/ -v
```

---

## Key Features

- [x] Multi-session chat (switchable sidebar)
- [x] SSE streaming responses (real-time token-by-token)
- [x] Tool call rendering (inline collapsible cards)
- [x] Markdown rendering (code blocks, syntax highlighting)
- [x] Vision (image upload + vision model)
- [x] Workspace file browser + editor
- [x] Slash commands (/exit, /agent, /model, /cost, /help)
- [x] Approval cards for pending agent actions
- [x] WebSocket/SSE approval polling
- [x] Multiple model profiles (via profiles.py)
- [x] Thinking mode (deepseek-r1, o3)
- [x] Bearer token auth on all routes
- [x] Dark theme (CSS custom properties)
- [x] Mobile-responsive layout
- [x] **Bug fix:** EventSource closure on chat switch (messages disappearing)
- [x] **Bug fix:** Busy state reset on SSE error (stuck spinner)

---

## Bug Fixes Applied

### 1. Messages Disappearing on Chat Switch (FIXED)
**Root cause:** Old SSE EventSource kept running after chat switch. Old RAF
callbacks wrote to DOM elements that had been recycled by `renderMessages()`.

**Fix:** `_activeEs` global EventSource reference, closed on:
- New `send()` call (before creating new stream)
- `loadSession()` (before fetching new session data)

### 2. Spinner Stuck on SSE Error (FIXED)
**Root cause:** `_err` handler called `setBusy(false)` but didn't clean up
`INFLIGHT[S.session.session_id]`, leaving busy flag permanently set.

**Fix:** `_err` → `setBusy(false)` + `delete INFLIGHT[activeSid]`.

---

## Architecture Decisions

| Decision | Rationale |
|----------|-----------|
| Vanilla JS, no framework | Zero build step, easy to deploy, fast load |
| SSE over WebSocket | HTTP/1.1 compatible, simpler proxy config, text-based |
| JSON file task store | Avoids DB dependency, git-friendly, easy to inspect |
| Workspace sandboxing | Agent can only access configured workspace root |
| Bearer token auth | Simple, HTTPS-only transport, no session cookies |
| Python stdlib HTTP server | No uvicorn/FastAPI dependency, simpler ops |
