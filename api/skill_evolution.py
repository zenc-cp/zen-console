"""api/skill_evolution.py — Skill evolution management API."""

import subprocess
import json
import logging
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Path to the evolution script in the conductor
EVOLVE_SCRIPT = Path.home() / "claw" / "conductor" / "lib" / "zen_evolve.py"

# Directory that contains the skills to list
_SKILLS_DIR = Path.home() / "claw" / "conductor" / "lib"

# Path to the chain-log SQLite (used for health metrics)
_CHAIN_LOG_DB = Path.home() / "claw" / "conductor" / "chain_log.db"

# Timeout for subprocess calls to zen_evolve.py
_EVOLVE_TIMEOUT = 60  # seconds


# ---------------------------------------------------------------------------
# list_skills
# ---------------------------------------------------------------------------

def list_skills() -> list:
    """List all skills in conductor/lib/ with metadata.

    Scans ``~/claw/conductor/lib/*.py``, extracts their module-level docstrings
    and file metadata.

    Returns:
        list of dicts::

            [
                {
                    "name": "brain_scoring",
                    "lines": 245,
                    "description": "...",
                    "last_modified": "2025-07-14T10:23:00+00:00",
                    "path": "/home/user/claw/conductor/lib/brain_scoring.py",
                }
            ]
    """
    skills = []

    if not _SKILLS_DIR.exists():
        logger.warning("list_skills: skills directory not found: %s", _SKILLS_DIR)
        return skills

    for py_file in sorted(_SKILLS_DIR.glob("*.py")):
        # Skip private / dunder files and zen_evolve itself
        if py_file.name.startswith('_') or py_file.name == 'zen_evolve.py':
            continue

        name = py_file.stem
        description = ""
        lines = 0

        try:
            text = py_file.read_text(encoding='utf-8', errors='replace')
            lines = len(text.splitlines())
            # Extract module-level docstring (first triple-quoted string)
            description = _extract_docstring(text)
        except (OSError, PermissionError) as exc:
            logger.debug("list_skills: could not read %s: %s", py_file, exc)

        try:
            mtime = py_file.stat().st_mtime
            last_modified = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except OSError:
            last_modified = ""

        skills.append({
            "name": name,
            "lines": lines,
            "description": description,
            "last_modified": last_modified,
            "path": str(py_file),
        })

    return skills


def _extract_docstring(source: str) -> str:
    """Return the first module-level docstring from Python source text.

    Uses a simple heuristic: find the first occurrence of ``\"\"\"...\"\"\"`` or
    ``'...'``.  Falls back to the first non-blank comment line.
    """
    import ast
    try:
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree)
        if docstring:
            # Return first line only, capped at 200 chars
            first_line = docstring.strip().split('\n')[0]
            return first_line[:200]
    except SyntaxError:
        pass

    # Fallback: find the first # comment
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith('#') and len(stripped) > 1:
            return stripped[1:].strip()[:200]

    return ""


# ---------------------------------------------------------------------------
# evolve_skill
# ---------------------------------------------------------------------------

def evolve_skill(skill_name: str, feedback: str = "") -> dict:
    """Trigger evolution for a specific skill.

    Runs::

        python3 zen_evolve.py --skill <name> [--feedback <feedback>]

    in the skill's directory with a 60-second timeout.

    Args:
        skill_name: Name of the skill module (without ``.py`` extension).
        feedback:   Optional free-text feedback for the evolution prompt.

    Returns:
        dict::

            {"status": "ok", "changes": "...", "diff": "..."}

        On failure::

            {"status": "error", "error": "...", "returncode": N}
    """
    if not EVOLVE_SCRIPT.exists():
        return {
            "status": "error",
            "error": f"Evolution script not found: {EVOLVE_SCRIPT}",
            "returncode": -1,
        }

    # Validate skill_name to prevent shell injection
    if not skill_name or not _is_safe_skill_name(skill_name):
        return {
            "status": "error",
            "error": f"Invalid skill name: {skill_name!r}",
            "returncode": -1,
        }

    cmd = [
        "python3",
        str(EVOLVE_SCRIPT),
        "--skill", skill_name,
    ]
    if feedback and feedback.strip():
        cmd += ["--feedback", feedback.strip()]

    env = os.environ.copy()
    cwd = str(EVOLVE_SCRIPT.parent)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_EVOLVE_TIMEOUT,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("evolve_skill: timed out after %ds for skill %r", _EVOLVE_TIMEOUT, skill_name)
        return {
            "status": "error",
            "error": f"Evolution timed out after {_EVOLVE_TIMEOUT}s",
            "returncode": -1,
        }
    except Exception as exc:
        logger.error("evolve_skill: subprocess error: %s", exc)
        return {
            "status": "error",
            "error": str(exc),
            "returncode": -1,
        }

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if result.returncode != 0:
        return {
            "status": "error",
            "error": stderr.strip() or stdout.strip() or "Evolution failed",
            "returncode": result.returncode,
            "stdout": stdout[:2000],
            "stderr": stderr[:2000],
        }

    # Parse structured output if the script emits JSON, otherwise return raw
    changes = ""
    diff = ""
    try:
        parsed = json.loads(stdout)
        changes = parsed.get("changes", "")
        diff = parsed.get("diff", "")
    except (json.JSONDecodeError, ValueError):
        # Non-JSON output — use stdout as the "changes" text
        changes = stdout.strip()

    return {
        "status": "ok",
        "skill": skill_name,
        "changes": changes,
        "diff": diff,
        "returncode": result.returncode,
        "raw_output": stdout[:4000],
    }


def _is_safe_skill_name(name: str) -> bool:
    """Return True if *name* is a safe Python module identifier."""
    import re
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


# ---------------------------------------------------------------------------
# skill_health
# ---------------------------------------------------------------------------

def skill_health() -> dict:
    """Return skill health metrics.

    For each skill: usage_count, last_used, error_rate (from chain logs).

    Reads from ``~/claw/conductor/chain_log.db`` if available. The DB is
    expected to have a table ``chain_events`` (or ``events``) with at least:
    ``skill_name``, ``timestamp``, ``status`` columns.

    Returns:
        dict mapping skill name to::

            {
                "usage_count": int,
                "last_used": "ISO8601 string or ''",
                "error_rate": float,   # 0.0–1.0
                "error_count": int,
            }

    Returns empty dict if the SQLite DB is missing or unreadable.
    """
    health: dict = {}

    if not _CHAIN_LOG_DB.exists():
        logger.debug("skill_health: chain log DB not found at %s", _CHAIN_LOG_DB)
        # Return empty metrics for each known skill
        for skill in list_skills():
            health[skill["name"]] = {
                "usage_count": 0,
                "last_used": "",
                "error_rate": 0.0,
                "error_count": 0,
            }
        return health

    try:
        con = sqlite3.connect(str(_CHAIN_LOG_DB), timeout=5)
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        # Discover the table name (support both 'chain_events' and 'events')
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row['name'] for row in cur.fetchall()}
        table = None
        for candidate in ('chain_events', 'events', 'skill_events', 'logs'):
            if candidate in tables:
                table = candidate
                break

        if table is None:
            logger.debug("skill_health: no recognised event table in %s", _CHAIN_LOG_DB)
            con.close()
            return health

        # Discover column names
        cur.execute(f"PRAGMA table_info({table})")
        col_names = {row['name'] for row in cur.fetchall()}

        skill_col = _pick_col(col_names, ('skill_name', 'skill', 'module'))
        ts_col    = _pick_col(col_names, ('timestamp', 'created_at', 'ts', 'time'))
        status_col = _pick_col(col_names, ('status', 'result', 'outcome'))

        if skill_col is None:
            logger.debug("skill_health: cannot find skill column in table %s", table)
            con.close()
            return health

        # Aggregate per skill
        if status_col and ts_col:
            cur.execute(f"""
                SELECT
                    {skill_col}                   AS skill,
                    COUNT(*)                       AS usage_count,
                    MAX({ts_col})                  AS last_used,
                    SUM(CASE WHEN {status_col} IN ('error','failed','failure') THEN 1 ELSE 0 END) AS error_count
                FROM {table}
                WHERE {skill_col} IS NOT NULL AND {skill_col} != ''
                GROUP BY {skill_col}
            """)
        elif ts_col:
            cur.execute(f"""
                SELECT
                    {skill_col} AS skill,
                    COUNT(*)    AS usage_count,
                    MAX({ts_col}) AS last_used,
                    0           AS error_count
                FROM {table}
                WHERE {skill_col} IS NOT NULL AND {skill_col} != ''
                GROUP BY {skill_col}
            """)
        else:
            cur.execute(f"""
                SELECT
                    {skill_col} AS skill,
                    COUNT(*)    AS usage_count,
                    ''          AS last_used,
                    0           AS error_count
                FROM {table}
                WHERE {skill_col} IS NOT NULL AND {skill_col} != ''
                GROUP BY {skill_col}
            """)

        for row in cur.fetchall():
            usage = row['usage_count'] or 0
            errors = row['error_count'] or 0
            health[row['skill']] = {
                "usage_count": usage,
                "last_used": str(row['last_used'] or ''),
                "error_rate": round(errors / usage, 4) if usage > 0 else 0.0,
                "error_count": errors,
            }

        con.close()

    except Exception as exc:
        logger.warning("skill_health: DB read error: %s", exc)

    return health


def _pick_col(available: set, candidates: tuple) -> str | None:
    """Return the first candidate column name that exists in *available*."""
    for c in candidates:
        if c in available:
            return c
    return None


# ---------------------------------------------------------------------------
# Route handlers
# (Register in routes.py — see CHANGES.md)
# ---------------------------------------------------------------------------

def handle_skills_list(handler, parsed) -> None:
    """GET /api/skills — return list of all skills with metadata."""
    from api.helpers import j
    skills = list_skills()
    j(handler, {'skills': skills, 'count': len(skills)})


def handle_skills_evolve(handler, body: dict) -> None:
    """POST /api/skills/evolve

    Request body::

        {"skill": "brain_scoring", "feedback": "too slow"}

    Response::

        {"status": "ok", "changes": "...", "diff": "..."}
    """
    from api.helpers import bad, j

    skill_name = (body.get('skill') or '').strip()
    if not skill_name:
        bad(handler, 'Missing required field: skill')
        return

    feedback = body.get('feedback', '')
    result = evolve_skill(skill_name, feedback=feedback)

    status_code = 200 if result.get('status') == 'ok' else 500
    j(handler, result, status=status_code)


def handle_skills_health(handler, parsed) -> None:
    """GET /api/skills/health — return per-skill health metrics."""
    from api.helpers import j
    metrics = skill_health()
    j(handler, {'health': metrics})
