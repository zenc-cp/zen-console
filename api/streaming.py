"""
Hermes Web UI -- SSE streaming engine and agent thread runner.
Includes Sprint 10 cancel support via CANCEL_FLAGS.
"""
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

from api.config import (
    STREAMS, STREAMS_LOCK, CANCEL_FLAGS, CLI_TOOLSETS,
    _get_session_agent_lock, _set_thread_env, _clear_thread_env,
    resolve_model_provider,
)

# Thinking/response stream separator (MiniMax M2.7 extended thinking)
_THINKING_OPEN  = "<thinking>"
_THINKING_CLOSE = "</thinking>"

# Lazy import to avoid circular deps -- hermes-agent is on sys.path via api/config.py
try:
    from run_agent import AIAgent
except ImportError:
    AIAgent = None
from api.models import get_session, title_from
from api.workspace import set_last_workspace

# Fields that are safe to send to LLM provider APIs.
# Everything else (attachments, timestamp, _ts, etc.) is display-only
# metadata added by the webui and must be stripped before the API call.
_API_SAFE_MSG_KEYS = {'role', 'content', 'tool_calls', 'tool_call_id', 'name', 'refusal'}

# ── Hallucination guard: detect and strip fabricated tool output ──────────

_FAKE_OUTPUT_PATTERNS = [
    re.compile(r'\{\s*"output"\s*:\s*"[^"]*"\s*,\s*"exit_code"\s*:\s*\d+[^}]*\}'),
    re.compile(r'\{\s*"output"\s*:\s*"[^"]*"\s*,\s*"stderr"\s*:\s*"[^"]*"\s*,\s*"exit_code"\s*:\s*\d+[^}]*\}'),
    re.compile(r'Executing:\s*\n\s*[{`]'),
    re.compile(r'(?:Output|Result):\s*\n\s*\{\s*"(?:output|result|exit_code)'),
    re.compile(r'(?:Running|Executing)\s+`[^`]+`[:\s]*\n```(?:json|bash|sh)?\s*\n\s*\{[^}]*"(?:output|exit_code)"'),
]

_FAKE_TOKEN_PATTERN = re.compile(
    r'\{\s*"(?:output|exit_code|stderr)"\s*:'
)


def _contains_fake_tool_output(text: str) -> bool:
    if not text:
        return False
    for pat in _FAKE_OUTPUT_PATTERNS:
        if pat.search(text):
            return True
    return False


def _strip_fake_tool_output(text: str) -> str:
    if not text:
        return text
    cleaned = re.sub(
        r'\{\s*"output"\s*:\s*"[^"]*"\s*,\s*(?:"stderr"\s*:\s*"[^"]*"\s*,\s*)?'
        r'"exit_code"\s*:\s*\d+[^}]*\}',
        '[Fabricated output removed — use the terminal tool to execute commands]',
        text,
    )
    return cleaned


_ANTI_HALLUCINATION_PROMPT = """
CRITICAL EXECUTION RULES — TOOL USE ONLY:
- You MUST use the terminal tool to run ANY shell command. NEVER write command output yourself.
- NEVER write {"output": ..., "exit_code": ...} in your response text. That JSON format comes ONLY from actual tool execution results.
- NEVER fabricate, guess, or simulate command output. If you need to know the result of a command, call the terminal tool.
- If a command fails, report the actual error from the tool result. Never fabricate success.
- "Executing:" followed by JSON you wrote is NOT execution — it is hallucination. Use the terminal tool instead.
- When asked to run a command, you MUST call the terminal tool. Writing the expected output is not acceptable.
- After calling a tool, wait for the real result before continuing. Do not predict or pre-write tool results.
""".strip()


# Context window guard
_MAX_HISTORY_CHARS = 400_000

def _trim_history(messages):
    if not messages:
        return messages
    total = sum(len(str(m.get("content", ""))) for m in messages)
    if total <= _MAX_HISTORY_CHARS:
        return messages
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]
    while len(other_msgs) > 2:
        total = sum(len(str(m.get("content", ""))) for m in system_msgs + other_msgs)
        if total <= _MAX_HISTORY_CHARS:
            break
        other_msgs = other_msgs[2:]
    trimmed = system_msgs + other_msgs
    print(f"[webui] context trim: {len(messages)} to {len(trimmed)} messages", flush=True)
    return trimmed


def _sanitize_messages_for_api(messages):
    """Return a deep copy of messages with only API-safe fields.

    The webui stores extra metadata on messages (attachments, timestamp, _ts)
    for display purposes. Some providers (e.g. Z.AI/GLM) reject unknown fields
    instead of ignoring them, causing HTTP 400 errors on subsequent messages.
    """
    clean = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        sanitized = {k: v for k, v in msg.items() if k in _API_SAFE_MSG_KEYS}
        if sanitized.get('role'):
            clean.append(sanitized)
    return clean


def _sse(handler, event, data):
    """Write one SSE event to the response stream."""
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    handler.wfile.write(payload.encode('utf-8'))
    handler.wfile.flush()


def _run_agent_streaming(session_id, msg_text, model, workspace, stream_id, attachments=None):
    """Run agent in background thread, writing SSE events to STREAMS[stream_id]."""
    q = STREAMS.get(stream_id)
    if q is None:
        return

    # Sprint 10: create a cancel event for this stream
    cancel_event = threading.Event()
    with STREAMS_LOCK:
        CANCEL_FLAGS[stream_id] = cancel_event

    def put(event, data):
        # If cancelled, drop all further events except the cancel event itself
        if cancel_event.is_set() and event not in ('cancel', 'error'):
            return
        try:
            q.put_nowait((event, data))
        except Exception:
            pass

    try:
        s = get_session(session_id)
        s.workspace = str(Path(workspace).expanduser().resolve())
        s.model = model

        _agent_lock = _get_session_agent_lock(session_id)
        # TD1: set thread-local env context so concurrent sessions don't clobber globals
        # Check for pre-flight cancel (user cancelled before agent even started)
        if cancel_event.is_set():
            put('cancel', {'message': 'Cancelled before start'})
            return

        # Resolve profile home for this agent run (snapshot at start)
        try:
            from api.profiles import get_active_hermes_home
            _profile_home = str(get_active_hermes_home())
        except ImportError:
            _profile_home = os.environ.get('HERMES_HOME', '')

        _set_thread_env(
            TERMINAL_CWD=str(s.workspace),
            HERMES_EXEC_ASK='1',
            HERMES_SESSION_KEY=session_id,
            HERMES_HOME=_profile_home,
        )
        # Still set process-level env as fallback for tools that bypass thread-local
        with _agent_lock:
          old_cwd = os.environ.get('TERMINAL_CWD')
          old_exec_ask = os.environ.get('HERMES_EXEC_ASK')
          old_session_key = os.environ.get('HERMES_SESSION_KEY')
          old_hermes_home = os.environ.get('HERMES_HOME')
          os.environ['TERMINAL_CWD'] = str(s.workspace)
          os.environ['HERMES_EXEC_ASK'] = '1'
          os.environ['HERMES_SESSION_KEY'] = session_id
          if _profile_home:
              os.environ['HERMES_HOME'] = _profile_home

          try:
            # ── Hallucination-guarded token callback ──────────────
            _token_buf = []
            _token_buf_len = 0
            _BUF_THRESHOLD = 200

            def _flush_token_buf():
                nonlocal _token_buf, _token_buf_len
                if not _token_buf:
                    return
                combined = ''.join(_token_buf)
                _token_buf = []
                _token_buf_len = 0
                if _contains_fake_tool_output(combined):
                    cleaned = _strip_fake_tool_output(combined)
                    if cleaned.strip():
                        put('token', {'text': cleaned})
                    print('[webui] hallucination guard: stripped fake tool output from stream', flush=True)
                else:
                    put('token', {'text': combined})

            # _route_token: hallucination-guard logic extracted for use by ThinkingRouter
            def _route_token(text):
                nonlocal _token_buf, _token_buf_len
                if _FAKE_TOKEN_PATTERN.search(text) or (_token_buf and _FAKE_TOKEN_PATTERN.search(''.join(_token_buf) + text)):
                    _token_buf.append(text)
                    _token_buf_len += len(text)
                    if _token_buf_len >= _BUF_THRESHOLD:
                        _flush_token_buf()
                    return
                if _token_buf:
                    _token_buf.append(text)
                    _flush_token_buf()
                    return
                put('token', {'text': text})

            # Wire ThinkingRouter: thinking tokens → SSE 'thinking' event;
            # normal tokens → existing hallucination-guard path.
            from api.thinking_router import ThinkingRouter as _TR
            _thinking_router = _TR(
                on_token=_route_token,
                on_thinking=lambda t: put('thinking', {'text': t}),
            )

            def on_token(text):
                if text is None:
                    _thinking_router.flush()
                    _flush_token_buf()
                    return
                _thinking_router.feed(text)

            def on_tool(name, preview, args):
                args_snap = {}
                if isinstance(args, dict):
                    for k, v in list(args.items())[:4]:
                        s2 = str(v); args_snap[k] = s2[:120]+('...' if len(s2)>120 else '')
                put('tool', {'name': name, 'preview': preview, 'args': args_snap})
                # also check for pending approval and surface it immediately
                from webui_tools.approval import has_pending as _has_pending, _pending, _lock
                if _has_pending(session_id):
                    with _lock:
                        p = dict(_pending.get(session_id, {}))
                    if p:
                        put('approval', p)

            if AIAgent is None:
                raise ImportError("AIAgent not available -- check that hermes-agent is on sys.path")
            resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(model)

            # Read per-profile config at call time (not module-level snapshot)
            from api.config import get_config as _get_config
            _cfg = _get_config()

            # Per-profile toolsets (fall back to module-level CLI_TOOLSETS)
            _pt = _cfg.get('platform_toolsets', {})
            _toolsets = _pt.get('cli', CLI_TOOLSETS) if isinstance(_pt, dict) else CLI_TOOLSETS

            # Fallback model from profile config (e.g. for rate-limit recovery)
            _fallback = _cfg.get('fallback_model') or None
            if _fallback:
                # Resolve the fallback through our provider logic too
                fb_model = _fallback.get('model', '')
                fb_provider = _fallback.get('provider', '')
                fb_base_url = _fallback.get('base_url')
                _fallback_resolved = {
                    'model': fb_model,
                    'provider': fb_provider,
                    'base_url': fb_base_url,
                }
            else:
                _fallback_resolved = None

            agent = AIAgent(
                model=resolved_model,
                provider=resolved_provider,
                base_url=resolved_base_url,
                platform='cli',
                quiet_mode=True,
                enabled_toolsets=_toolsets,
                fallback_model=_fallback_resolved,
                session_id=session_id,
                stream_delta_callback=on_token,
                tool_progress_callback=on_tool,
            )
            # Prepend workspace context so the agent always knows which directory
            # to use for file operations, regardless of session age or AGENTS.md defaults.
            workspace_ctx = f"[Workspace: {s.workspace}]\n"
            workspace_system_msg = (
                f"Active workspace at session start: {s.workspace}\n"
                "Every user message is prefixed with [Workspace: /absolute/path] indicating the "
                "workspace the user has selected in the web UI at the time they sent that message. "
                "This tag is the single authoritative source of the active workspace and updates "
                "with every message. It overrides any prior workspace mentioned in this system "
                "prompt, memory, or conversation history. Always use the value from the most recent "
                "[Workspace: ...] tag as your default working directory for ALL file operations: "
                "write_file, read_file, search_files, terminal workdir, and patch. "
                "Never fall back to a hardcoded path when this tag is present."
            )

            # ── Tier 1 SessionStart: inject knowledge index ───────────────
            # Read ~/claw/memory/knowledge/index.md (if it exists) and prepend
            # its contents to workspace_system_msg so Hermes starts each session
            # with accumulated institutional knowledge.
            try:
                _mem_index = Path(_profile_home or str(Path.home() / "claw")) / "memory" / "knowledge" / "index.md"
                if _mem_index.exists():
                    _index_text = _mem_index.read_text(encoding="utf-8", errors="replace")
                    # Cap at 20K chars to avoid prompt bloat
                    if len(_index_text) > 20000:
                        _index_text = _index_text[:20000] + "\n[...index truncated at 20K chars...]\n"
                    workspace_system_msg = (
                        "## ZenOps Knowledge Base (SessionStart)\n"
                        + _index_text
                        + "\n---\n"
                        + workspace_system_msg
                    )
                    print(f"[webui] memory: injected index.md ({len(_index_text)} chars)", flush=True)
            except Exception as _mem_err:
                print(f"[webui] memory: index injection skipped ({_mem_err})", flush=True)
            result = agent.run_conversation(
                user_message=workspace_ctx + msg_text,
                system_message=workspace_system_msg,
                conversation_history=_trim_history(_sanitize_messages_for_api(s.messages)),
                task_id=session_id,
                persist_user_message=msg_text,
            )
            # ── Post-run hallucination scrub ───────────────────────
            _result_msgs = result.get('messages') or s.messages
            _hallucination_count = 0
            for _m in _result_msgs:
                if _m.get('role') == 'assistant' and isinstance(_m.get('content'), str):
                    if _contains_fake_tool_output(_m['content']):
                        _m['content'] = _strip_fake_tool_output(_m['content'])
                        _hallucination_count += 1
            if _hallucination_count:
                print(f'[webui] hallucination guard: scrubbed {_hallucination_count} assistant message(s) with fake tool output', flush=True)
            s.messages = _result_msgs
            # Stamp 'timestamp' on any messages that don't have one yet
            _now = time.time()
            for _m in s.messages:
                if isinstance(_m, dict) and not _m.get('timestamp') and not _m.get('_ts'):
                    _m['timestamp'] = int(_now)
            s.title = title_from(s.messages, s.title)
            # Read token/cost usage from the agent object (if available)
            input_tokens = getattr(agent, 'session_prompt_tokens', 0) or 0
            output_tokens = getattr(agent, 'session_completion_tokens', 0) or 0
            estimated_cost = getattr(agent, 'session_estimated_cost_usd', None)
            s.input_tokens = (s.input_tokens or 0) + input_tokens
            s.output_tokens = (s.output_tokens or 0) + output_tokens
            if estimated_cost:
                s.estimated_cost = (s.estimated_cost or 0) + estimated_cost
            # Extract tool call metadata grouped by assistant message index
            # Each tool call gets assistant_msg_idx so the client can render
            # cards inline with the assistant bubble that triggered them.
            tool_calls = []
            pending_names = {}   # tool_call_id -> name
            pending_args = {}    # tool_call_id -> args dict
            pending_asst_idx = {} # tool_call_id -> index in s.messages
            for msg_idx, m in enumerate(s.messages):
                if m.get('role') == 'assistant':
                    c = m.get('content', '')
                    if isinstance(c, list):
                        for p in c:
                            if isinstance(p, dict) and p.get('type') == 'tool_use':
                                tid = p.get('id', '')
                                pending_names[tid] = p.get('name', '')
                                pending_args[tid] = p.get('input', {})
                                pending_asst_idx[tid] = msg_idx
                elif m.get('role') == 'tool':
                    tid = m.get('tool_call_id') or m.get('tool_use_id', '')
                    name = pending_names.get(tid, '')
                    if not name or name == 'tool':
                        continue  # skip unresolvable tool entries
                    asst_idx = pending_asst_idx.get(tid, -1)
                    args = pending_args.get(tid, {})
                    raw = str(m.get('content', ''))
                    try:
                        rd = json.loads(raw)
                        snippet = str(rd.get('output') or rd.get('result') or rd.get('error') or raw)[:200]
                    except Exception:
                        snippet = raw[:200]
                    # Truncate args values for storage
                    args_snap = {}
                    if isinstance(args, dict):
                        for k, v in list(args.items())[:6]:
                            s2 = str(v)
                            args_snap[k] = s2[:120] + ('...' if len(s2) > 120 else '')
                    tool_calls.append({
                        'name': name, 'snippet': snippet, 'tid': tid,
                        'assistant_msg_idx': asst_idx, 'args': args_snap,
                    })
            s.tool_calls = tool_calls
            # Tag the matching user message with attachment filenames for display on reload
            # Only tag a user message whose content relates to this turn's text
            # (msg_text is the full message including the [Attached files: ...] suffix)
            if attachments:
                for m in reversed(s.messages):
                    if m.get('role') == 'user':
                        content = str(m.get('content', ''))
                        # Match if content is part of the sent message or vice-versa
                        base_text = msg_text.split('\n\n[Attached files:')[0].strip()
                        if base_text[:60] in content or content[:60] in msg_text:
                            m['attachments'] = attachments
                            break
            s.save()
            usage = {'input_tokens': input_tokens, 'output_tokens': output_tokens, 'estimated_cost': estimated_cost}
            put('done', {'session': s.compact() | {'messages': s.messages, 'tool_calls': tool_calls}, 'usage': usage})

            # ── Tier 1 SessionEnd: spawn flush.py detached ────────────────
            # Persist session memory to ~/claw/memory/daily/YYYY-MM-DD.md
            # without blocking the SSE response.
            try:
                import subprocess as _sp
                _flush_script = Path(__file__).parent.parent / "scripts" / "flush.py"
                # Fallback: check common install locations
                if not _flush_script.exists():
                    _flush_script = Path(_profile_home or str(Path.home() / "claw")) / "scripts" / "flush.py"
                if _flush_script.exists():
                    _flush_env = os.environ.copy()
                    _flush_env["HERMES_HOME"] = _profile_home or str(Path.home() / "claw")
                    _sp.Popen(
                        [sys.executable, str(_flush_script), session_id],
                        env=_flush_env,
                        stdout=open(os.devnull, "w"),
                        stderr=open(os.devnull, "w"),
                        start_new_session=True,
                    )
                    print(f"[webui] memory: flush.py spawned for session {session_id[:12]}", flush=True)
                else:
                    print(f"[webui] memory: flush.py not found at {_flush_script}", flush=True)
            except Exception as _flush_err:
                print(f"[webui] memory: SessionEnd flush error ({_flush_err})", flush=True)
          finally:
            if old_cwd is None: os.environ.pop('TERMINAL_CWD', None)
            else: os.environ['TERMINAL_CWD'] = old_cwd
            if old_exec_ask is None: os.environ.pop('HERMES_EXEC_ASK', None)
            else: os.environ['HERMES_EXEC_ASK'] = old_exec_ask
            if old_session_key is None: os.environ.pop('HERMES_SESSION_KEY', None)
            else: os.environ['HERMES_SESSION_KEY'] = old_session_key
            if old_hermes_home is None: os.environ.pop('HERMES_HOME', None)
            else: os.environ['HERMES_HOME'] = old_hermes_home

    except Exception as e:
        print('[webui] stream error:\n' + traceback.format_exc(), flush=True)
        err_str = str(e)
        # Detect rate limit errors specifically so the client can show a helpful card
        # rather than the generic "Connection lost" message
        is_rate_limit = 'rate limit' in err_str.lower() or '429' in err_str or 'RateLimitError' in type(e).__name__
        if is_rate_limit:
            put('apperror', {
                'message': err_str,
                'type': 'rate_limit',
                'hint': 'Rate limit reached. The fallback model (if configured) was also exhausted. Try again in a moment.',
            })
        else:
            put('apperror', {'message': err_str, 'type': 'error'})
    finally:
        _clear_thread_env()  # TD1: always clear thread-local context
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
            CANCEL_FLAGS.pop(stream_id, None)

# ============================================================
# SECTION: HTTP Request Handler
# do_GET: read-only API endpoints + SSE stream + static HTML
# do_POST: mutating endpoints (session CRUD, chat, upload, approval)
# Routing is a flat if/elif chain. See ARCHITECTURE.md section 4.1.
# ============================================================


def cancel_stream(stream_id: str) -> bool:
    """Signal an in-flight stream to cancel. Returns True if the stream existed."""
    with STREAMS_LOCK:
        if stream_id not in STREAMS:
            return False
        flag = CANCEL_FLAGS.get(stream_id)
        if flag:
            flag.set()
        # Put a cancel sentinel into the queue so the SSE handler wakes up
        q = STREAMS.get(stream_id)
        if q:
            try:
                q.put_nowait(('cancel', {'message': 'Cancelled by user'}))
            except Exception:
                pass
    return True
