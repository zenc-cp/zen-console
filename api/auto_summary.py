"""api/auto_summary.py — Generate session titles from conversation content."""

import json
import threading
import urllib.request
import urllib.error
import base64
import logging

logger = logging.getLogger(__name__)

# Basic auth for the local Hermes proxy (if used)
_AUTH = base64.b64encode(b"zen:z3nch4n@ZenOps").decode()

# OpenRouter endpoint for direct summarization (avoids circular dependency on
# the Hermes agent runner — we just call the API ourselves)
_OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

_SUMMARIZE_SYSTEM = (
    "You are a helpful assistant that generates concise session titles. "
    "Return ONLY the title — no quotes, no punctuation at the end, no extra text."
)
_SUMMARIZE_PROMPT = (
    "Summarize this conversation in 5-8 words as a title. "
    "Return ONLY the title, nothing else."
)


def _get_openrouter_key() -> str | None:
    """Read the OpenRouter API key from the active profile's .env or environment."""
    import os
    key = os.environ.get('OPENROUTER_API_KEY')
    if key:
        return key
    # Try the Hermes profile .env
    try:
        from api.profiles import get_active_hermes_home
        env_path = get_active_hermes_home() / '.env'
    except ImportError:
        from pathlib import Path
        env_path = Path.home() / '.hermes' / '.env'
    try:
        if env_path.exists():
            for line in env_path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line.startswith('OPENROUTER_API_KEY='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def summarize_session(messages: list, model: str = "openrouter/xiaomi/mimo-v2-flash") -> str:
    """Generate a 5-8 word title for a session based on its messages.

    Uses the cheapest model (mimo-v2-flash) to summarize.
    Falls back to first user message truncated to 60 chars if LLM fails.

    Args:
        messages: List of message dicts with 'role' and 'content'.
        model:    LLM model to use for summarization (OpenRouter format).

    Returns:
        Short title string (5-8 words), or first-user-message fallback.
    """
    # --- Build fallback from first user message ---
    fallback = "Untitled"
    for m in messages:
        if m.get('role') == 'user':
            c = m.get('content', '')
            if isinstance(c, list):
                # Multi-modal content — extract text parts
                c = ' '.join(
                    p.get('text', '')
                    for p in c
                    if isinstance(p, dict) and p.get('type') == 'text'
                )
            text = str(c).strip()
            if text:
                fallback = text[:60]
                break

    # --- Collect a representative subset of messages ---
    user_count = 0
    assistant_count = 0
    context_msgs = []
    for m in messages:
        role = m.get('role', '')
        if role not in ('user', 'assistant'):
            continue
        if role == 'user' and user_count >= 3:
            continue
        if role == 'assistant' and assistant_count >= 2:
            continue
        content = m.get('content', '')
        if isinstance(content, list):
            content = ' '.join(
                p.get('text', '')
                for p in content
                if isinstance(p, dict) and p.get('type') == 'text'
            )
        content = str(content).strip()[:200]
        context_msgs.append({'role': role, 'content': content})
        if role == 'user':
            user_count += 1
        else:
            assistant_count += 1
        if user_count >= 3 and assistant_count >= 2:
            break

    if not context_msgs:
        return fallback

    # --- Build the summarization payload ---
    # Strip the "openrouter/" prefix if present for the API call
    api_model = model
    if api_model.startswith('openrouter/'):
        api_model = api_model[len('openrouter/'):]

    payload = {
        "model": api_model,
        "messages": [
            {"role": "system", "content": _SUMMARIZE_SYSTEM},
            *context_msgs,
            {"role": "user", "content": _SUMMARIZE_PROMPT},
        ],
        "max_tokens": 32,
        "temperature": 0.3,
    }

    api_key = _get_openrouter_key()
    if not api_key:
        logger.warning("auto_summary: no OPENROUTER_API_KEY — using fallback title")
        return fallback

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://zen-console.local",
        "X-Title": "Zen Console Auto-Summary",
    }

    try:
        req = urllib.request.Request(
            _OPENROUTER_API_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode('utf-8')
        data = json.loads(raw)
        title = (
            data.get('choices', [{}])[0]
            .get('message', {})
            .get('content', '')
            .strip()
        )
        # Strip surrounding quotes the model might add
        title = title.strip('"').strip("'").strip()
        if title:
            logger.info("auto_summary: generated title %r", title)
            return title[:80]  # safety cap
    except urllib.error.HTTPError as exc:
        logger.warning("auto_summary: HTTP %s from OpenRouter — using fallback", exc.code)
    except Exception as exc:
        logger.warning("auto_summary: summarization failed (%s) — using fallback", exc)

    return fallback


# ---------------------------------------------------------------------------
# Background auto-summarization helper
# ---------------------------------------------------------------------------

def auto_summarize_session(session_id: str, messages: list, model: str = "openrouter/xiaomi/mimo-v2-flash") -> None:
    """Generate a title for *session_id* and update it in-place (background-safe).

    Calls summarize_session() and saves the result to the session object.
    Designed to be called from a daemon thread — all errors are swallowed.

    Only updates when the session title is still "Untitled" (or empty), to
    avoid clobbering titles the user has already set manually.
    """
    try:
        from api.models import get_session
        session = get_session(session_id)
        if session is None:
            return
        # Guard: only update if title is still the default
        current_title = getattr(session, 'title', 'Untitled') or 'Untitled'
        if current_title.strip() not in ('Untitled', ''):
            return
        title = summarize_session(messages, model=model)
        if title and title != 'Untitled':
            session.title = title
            session.save()
            logger.info("auto_summary: session %s titled %r", session_id, title)
    except Exception as exc:
        logger.debug("auto_summary: background update failed (%s)", exc)


def trigger_auto_summary(session_id: str, messages: list, model: str = "openrouter/xiaomi/mimo-v2-flash") -> None:
    """Fire auto-summarization in a daemon thread (non-blocking).

    Call this after the first assistant response in a new session:

        if session.title in ('Untitled', '') and len(assistant_messages) == 1:
            from api.auto_summary import trigger_auto_summary
            trigger_auto_summary(session.session_id, session.messages, session.model)
    """
    t = threading.Thread(
        target=auto_summarize_session,
        args=(session_id, messages, model),
        daemon=True,
        name=f'auto-summary-{session_id[:8]}',
    )
    t.start()


# ---------------------------------------------------------------------------
# POST /api/session/summarize route handler
# (Register in routes.py handle_post — see CHANGES.md)
# ---------------------------------------------------------------------------

def handle_session_summarize(handler, body: dict) -> None:
    """POST /api/session/summarize

    Request body:
        {"session_id": "abc123"}

    Response:
        {"title": "Generated title here"}

    Fetches the session's messages, calls summarize_session(), updates the
    session's title on disk, and returns the new title.
    """
    from api.helpers import bad, j

    session_id = (body.get('session_id') or '').strip()
    if not session_id:
        bad(handler, 'Missing required field: session_id')
        return

    try:
        from api.models import get_session
        session = get_session(session_id)
    except KeyError:
        bad(handler, f'Session not found: {session_id}', 404)
        return

    model = body.get('model', 'openrouter/xiaomi/mimo-v2-flash')
    title = summarize_session(session.messages, model=model)

    if title and title != 'Untitled':
        session.title = title
        session.save()

    j(handler, {'title': title, 'session_id': session_id})
