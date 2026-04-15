"""
Structured SSE event helpers for Multica-style agent observability.

These helpers wrap the low-level `put(event, data)` queue function used inside
`_run_agent_streaming` (streaming.py).  Import and call them at the appropriate
lifecycle points described in the integration guide (CHANGES.md).

Usage
-----
In streaming.py, inside `_run_agent_streaming`, after `def put(event, data):`
import and use these helpers as follows:

    from streaming_events import (
        emit_agent_status, emit_progress, emit_context_info, emit_cost
    )

    # At generation start:
    emit_context_info(put, input_tokens=..., model=resolved_model)
    emit_agent_status(put, 'thinking')

    # When the agent starts using a tool:
    emit_agent_status(put, 'tool_use', detail=tool_name)

    # When writing tokens:
    emit_agent_status(put, 'writing')

    # After done event is queued:
    emit_agent_status(put, 'idle')
    emit_cost(put, input_tokens, output_tokens, model, cost_usd=estimated_cost)
"""

from __future__ import annotations


# ── Status constants ──────────────────────────────────────────────────────────
# Valid values for emit_agent_status `status` parameter.
AGENT_STATUS_IDLE      = 'idle'
AGENT_STATUS_THINKING  = 'thinking'
AGENT_STATUS_TOOL_USE  = 'tool_use'
AGENT_STATUS_WRITING   = 'writing'
AGENT_STATUS_ERROR     = 'error'

# Dot colours that correspond to each status (mirrors the frontend CSS).
STATUS_DOT_COLORS: dict[str, str] = {
    AGENT_STATUS_IDLE:     '#666666',
    AGENT_STATUS_THINKING: '#C9A84C',   # gold
    AGENT_STATUS_TOOL_USE: '#4a9eff',   # blue
    AGENT_STATUS_WRITING:  '#ffffff',   # white
    AGENT_STATUS_ERROR:    '#ff4444',   # red
}


def emit_agent_status(put, status: str, detail: str = '') -> None:
    """Emit an agent_status SSE event.

    The frontend listens for this event and updates the coloured status dot
    displayed next to the "Hermes" label in the assistant message header.

    Parameters
    ----------
    put:
        The `put(event, data)` callable from `_run_agent_streaming`.
    status:
        One of ``'idle'``, ``'thinking'``, ``'tool_use'``, ``'writing'``,
        ``'error'``.  Use the ``AGENT_STATUS_*`` constants defined above.
    detail:
        Optional human-readable detail string, e.g. the tool name currently
        being called.  Shown in the dot tooltip.
    """
    if status not in STATUS_DOT_COLORS:
        raise ValueError(
            f"Unknown agent status {status!r}.  "
            f"Must be one of: {list(STATUS_DOT_COLORS)}"
        )
    put('agent_status', {
        'status': status,
        'detail': detail,
        'color': STATUS_DOT_COLORS[status],
    })


def emit_progress(put, step: int, total: int, label: str = '') -> None:
    """Emit a progress SSE event for multi-step operations.

    Renders a thin animated progress bar below the currently streaming
    assistant message.  Call repeatedly as steps complete; once step == total
    the bar disappears on the next ``done`` event.

    Parameters
    ----------
    put:
        The `put(event, data)` callable from `_run_agent_streaming`.
    step:
        Current completed step (0-based or 1-based, as long as consistent).
    total:
        Total number of steps expected.
    label:
        Short description of the current step, e.g. ``"Searching files…"``.
    """
    if total <= 0:
        raise ValueError("`total` must be greater than 0")
    pct = round(min(step / total, 1.0) * 100)
    put('progress', {
        'step': step,
        'total': total,
        'label': label,
        'pct': pct,
    })


def emit_context_info(
    put,
    input_tokens: int,
    model: str,
    cached: bool = False,
) -> None:
    """Emit a context_info SSE event at the start of generation.

    Renders a small "Context: 50K tokens · MiniMax M2.7" badge at the top of
    the assistant response bubble.

    Parameters
    ----------
    put:
        The `put(event, data)` callable from `_run_agent_streaming`.
    input_tokens:
        Number of prompt/input tokens sent to the model.
    model:
        The resolved model identifier string (e.g. ``"minimax/minimax-m2.7"``).
    cached:
        ``True`` if the context was served from the provider's prompt cache,
        allowing the frontend to show a cache-hit indicator.
    """
    # Derive a human-friendly short model label (last path segment, capitalised)
    model_label = model.split('/')[-1] if '/' in model else model

    put('context_info', {
        'input_tokens': input_tokens,
        'model': model,
        'model_label': model_label,
        'cached': cached,
    })


def emit_cost(
    put,
    input_tokens: int,
    output_tokens: int,
    model: str,
    cost_usd: float = 0.0,
) -> None:
    """Emit a cost SSE event at the end of generation.

    Renders a ``"1.2K in · 500 out · $0.001"`` badge at the bottom of the
    assistant response bubble.  Extends the existing P5 usage badge with
    structured data so the frontend can display richer formatting.

    Parameters
    ----------
    put:
        The `put(event, data)` callable from `_run_agent_streaming`.
    input_tokens:
        Total prompt tokens consumed.
    output_tokens:
        Total completion tokens generated.
    model:
        The resolved model identifier string.
    cost_usd:
        Estimated cost in US dollars, or 0 if the provider does not report it.
    """
    cost_usd = cost_usd or 0.0

    def _fmt_k(n: int) -> str:
        """Format a token count as e.g. '1.2K' or '500'."""
        if n >= 1000:
            return f"{n / 1000:.1f}K"
        return str(n)

    cost_str = ''
    if cost_usd > 0:
        if cost_usd < 0.01:
            cost_str = f'~${cost_usd:.4f}'
        else:
            cost_str = f'~${cost_usd:.3f}'

    put('cost', {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'model': model,
        'cost_usd': cost_usd,
        # Pre-formatted strings for simple frontend templates
        'input_fmt':  _fmt_k(input_tokens),
        'output_fmt': _fmt_k(output_tokens),
        'cost_str':   cost_str,
    })


# ── Integration example (not executed) ───────────────────────────────────────

def _integration_example():
    """Illustrative call-site inside _run_agent_streaming (never executed)."""
    raise RuntimeError("This is documentation only — do not call directly.")

    # ── Paste these into streaming.py at the appropriate call sites ────────

    # 1. At the top of the try block, after `resolved_model` is set:
    emit_context_info(put, input_tokens=0, model=resolved_model)  # noqa: F821
    emit_agent_status(put, AGENT_STATUS_THINKING)

    # 2. Inside on_tool() callback, after the existing put('tool', …) call:
    emit_agent_status(put, AGENT_STATUS_TOOL_USE, detail=name)  # noqa: F821

    # 3. Inside on_token() callback, on first non-None token:
    emit_agent_status(put, AGENT_STATUS_WRITING)

    # 4. After put('done', …):
    emit_agent_status(put, AGENT_STATUS_IDLE)
    emit_cost(
        put,                            # noqa: F821
        input_tokens=input_tokens,      # noqa: F821
        output_tokens=output_tokens,    # noqa: F821
        model=resolved_model,           # noqa: F821
        cost_usd=estimated_cost or 0.0, # noqa: F821
    )
