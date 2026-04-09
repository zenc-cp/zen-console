#!/usr/bin/env python3
"""flush.py — Hermes session memory flusher (Tier 1).

Called at SessionEnd (detached subprocess from streaming.py).
Also called at PreCompact (before context is lost).

Reads the session transcript from claw.db, sends it to OpenRouter
minimax/minimax-m2.7 with max 2 turns, extracts decisions/lessons,
and appends a dated entry to ~/claw/memory/daily/YYYY-MM-DD.md.

Usage:
    python3 flush.py <session_id>

Environment:
    OPENROUTER_API_KEY  — required
    CLAWDB              — optional, defaults to ~/claw/claw.db
    HERMES_HOME         — optional, for memory path resolution
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

def _claw_root() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "")
    if hermes_home:
        p = Path(hermes_home)
        if (p / "claw.db").exists() or (p / "memory").exists():
            return p
    return Path.home() / "claw"

def _db_path() -> Path:
    env = os.environ.get("CLAWDB", "")
    if env:
        return Path(env)
    return _claw_root() / "claw.db"

def _daily_dir() -> Path:
    return _claw_root() / "memory" / "daily"

def _daily_file() -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _daily_dir() / f"{today}.md"

# ── DB: load session messages ────────────────────────────────────────────────

def _load_session_messages(session_id: str) -> list[dict]:
    """Return the message list for a session from claw.db."""
    db = _db_path()
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(str(db), timeout=5)
        conn.row_factory = sqlite3.Row
        # zen-console stores sessions in the 'sessions' table, messages in JSON field
        row = conn.execute(
            "SELECT messages FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        conn.close()
        if row and row["messages"]:
            data = json.loads(row["messages"])
            if isinstance(data, list):
                return data
    except Exception as e:
        print(f"[flush] DB read error: {e}", flush=True)
    return []

def _extract_transcript(messages: list[dict]) -> str:
    """Convert message list to a readable transcript string (max ~8000 chars)."""
    lines = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if not isinstance(content, str):
            try:
                content = json.dumps(content)
            except Exception:
                content = str(content)
        if role in ("user", "assistant"):
            prefix = "USER" if role == "user" else "HERMES"
            lines.append(f"[{prefix}] {content[:600]}")
    full = "\n".join(lines)
    # Truncate to ~8000 chars to keep prompt short
    if len(full) > 8000:
        full = full[-8000:]
        full = "[...truncated...]\n" + full
    return full

# ── OpenRouter call ──────────────────────────────────────────────────────────

def _call_openrouter(transcript: str, session_id: str) -> str | None:
    """Send transcript to minimax-m2.7 via OpenRouter. Returns extracted text or None."""
    import urllib.request

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("[flush] OPENROUTER_API_KEY not set — skipping LLM extraction", flush=True)
        return None

    prompt = f"""You are a memory archivist for an AI system called Hermes/ZenOps.

Analyze this session transcript and extract a compact memory entry.
Return ONLY the formatted memory entry — no explanation.

Format:
## Session Memory — {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")} UTC

**Session:** {session_id[:12]}
**Decisions Made:**
- [bullet list of key decisions, max 5]

**Lessons / Patterns:**
- [bullet list of lessons learned, max 5]

**Errors / Warnings:**
- [bullet list of errors or warnings encountered, max 3, or "None" if none]

**Context for Next Session:**
[1-2 sentences summarizing what was left incomplete or what should be remembered next session]

---

SESSION TRANSCRIPT:
{transcript}
"""

    payload = json.dumps({
        "model": "minimax/minimax-m2.7",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 600,
        "temperature": 0.3,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://z3nops.com",
            "X-Title": "ZenOps-Hermes",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[flush] OpenRouter call failed: {e}", flush=True)
        return None

# ── Write to daily log ───────────────────────────────────────────────────────

def _append_to_daily(entry: str) -> None:
    """Append memory entry to today's daily log file."""
    daily_dir = _daily_dir()
    daily_dir.mkdir(parents=True, exist_ok=True)
    daily_file = _daily_file()

    # Create file with header if new
    if not daily_file.exists():
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        header = f"# Daily Memory Log — {today_str}\n\n"
        daily_file.write_text(header, encoding="utf-8")

    with daily_file.open("a", encoding="utf-8") as f:
        f.write("\n" + entry + "\n")

    print(f"[flush] Appended memory entry to {daily_file}", flush=True)

# ── Fallback: minimal summary without LLM ────────────────────────────────────

def _minimal_entry(session_id: str, messages: list[dict]) -> str:
    """Create a minimal entry when LLM is unavailable."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    user_msgs = [m for m in messages if m.get("role") == "user"]
    asst_msgs = [m for m in messages if m.get("role") == "assistant"]
    first_user = ""
    if user_msgs:
        c = user_msgs[0].get("content", "")
        if isinstance(c, str):
            first_user = c[:200]

    return f"""## Session Memory — {now} UTC

**Session:** {session_id[:12]}
**Messages:** {len(user_msgs)} user / {len(asst_msgs)} assistant
**First user message:** {first_user}

**Decisions Made:**
- [LLM unavailable — raw session recorded for manual review]

**Lessons / Patterns:**
- None extracted

**Errors / Warnings:**
- None

**Context for Next Session:**
Session recorded without LLM extraction. Run compile-memory.py to process.

---
"""

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("[flush] Usage: flush.py <session_id>", flush=True)
        sys.exit(1)

    session_id = sys.argv[1]
    print(f"[flush] Processing session {session_id[:12]}...", flush=True)

    # Small delay to ensure DB write is committed before we read
    time.sleep(1)

    messages = _load_session_messages(session_id)
    if not messages:
        print(f"[flush] No messages found for session {session_id[:12]} — skipping", flush=True)
        sys.exit(0)

    print(f"[flush] Loaded {len(messages)} messages", flush=True)

    transcript = _extract_transcript(messages)
    entry = _call_openrouter(transcript, session_id)

    if not entry:
        print("[flush] LLM extraction failed — writing minimal entry", flush=True)
        entry = _minimal_entry(session_id, messages)

    _append_to_daily(entry)
    print("[flush] Done.", flush=True)


if __name__ == "__main__":
    main()
