"""
Approval system shim for zen-console.

Maps zen-console's expected API (has_pending, pop_pending, approve_session, etc.)
to the actual functions available in hermes-agent's tools/approval.py.

This allows zen-console to use hermes-agent's approval system when running
alongside a hermes-agent installation.
"""
from __future__ import annotations

import threading
from typing import Optional

# Import everything from hermes-agent's real approval module
try:
    from hermes_agent.tools.approval import (
        _pending,
        _lock,
        _permanent_approved,
        submit_pending as _real_submit_pending,
        approve_session as _real_approve_session,
        approve_permanent as _real_approve_permanent,
        save_permanent_allowlist as _real_save_permanent_allowlist,
        is_approved as _real_is_approved,
        has_blocking_approval as _has_blocking_approval,
    )
except ImportError:
    # Fallback: define everything as no-ops if hermes-agent not available
    _pending: dict = {}
    _lock = threading.Lock()
    _permanent_approved: set = set()

    def _real_submit_pending(session_key: str, approval: dict) -> None:
        with _lock:
            _pending[session_key] = approval

    def _real_approve_session(*a, **k): pass
    def _real_approve_permanent(*a, **k): pass
    def _real_save_permanent_allowlist(*a, **k): pass
    def _real_is_approved(*a, **k): return True
    def _has_blocking_approval(*a, **k): return False


# ── zen-console expected API ────────────────────────────────────────────────

def has_pending(session_key: str) -> bool:
    """Check if there is a pending approval for the given session."""
    with _lock:
        return session_key in _pending


def pop_pending(session_key: str) -> Optional[dict]:
    """Remove and return the pending approval entry for the session, if any."""
    with _lock:
        return _pending.pop(session_key, None)


def submit_pending(session_key: str, approval: dict) -> None:
    """Submit a new pending approval entry."""
    _real_submit_pending(session_key, approval)


def approve_session(session_key: str, pattern_key: str) -> None:
    _real_approve_session(session_key, pattern_key)


def approve_permanent(pattern_key: str) -> None:
    _real_approve_permanent(pattern_key)


def save_permanent_allowlist(patterns: set) -> None:
    _real_save_permanent_allowlist(patterns)


def is_approved(session_key: str, pattern_key: str) -> bool:
    return _real_is_approved(session_key, pattern_key)


__all__ = [
    "has_pending",
    "pop_pending",
    "submit_pending",
    "approve_session",
    "approve_permanent",
    "save_permanent_allowlist",
    "is_approved",
    "_pending",
    "_lock",
    "_permanent_approved",
]
