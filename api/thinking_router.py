"""conductor/lib/thinking_router.py — Standalone thinking/token SSE router.

Extracted from streaming.py so it can be unit-tested independently.
streaming.py imports and uses this router.

Usage:
    router = ThinkingRouter(on_token_fn, on_thinking_fn)
    router.feed("some <thinking>deep</thinking> response text")
    router.flush()
"""
from __future__ import annotations

from typing import Callable

_THINKING_OPEN = "<thinking>"
_THINKING_CLOSE = "</thinking>"


class ThinkingRouter:
    """Routes streamed text to token or thinking callbacks.

    Text inside ``<thinking>...</thinking>`` tags is accumulated and
    delivered to *on_thinking* when the closing tag is seen.  All other
    text is forwarded to *on_token* immediately.

    The router is designed to handle tags split across multiple ``feed()``
    calls: it keeps an internal buffer for partial tag matches so no bytes
    are lost or mis-routed.

    Args:
        on_token: Callable invoked with plain (non-thinking) text chunks.
        on_thinking: Callable invoked with the full content of each
            ``<thinking>…</thinking>`` block once the closing tag arrives.
    """

    def __init__(
        self,
        on_token: Callable[[str], None],
        on_thinking: Callable[[str], None],
    ) -> None:
        self._on_token = on_token
        self._on_thinking = on_thinking

        # Whether we are currently inside a <thinking> block
        self._in_thinking: bool = False

        # Accumulated text inside the current <thinking> block
        self._think_buf: list[str] = []

        # Partial tag buffer: holds trailing bytes that might be the start
        # of a <thinking> or </thinking> tag but whose full tag hasn't
        # arrived yet.  This ensures split tags work across feed() calls.
        self._partial: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, text: str) -> None:
        """Process a new chunk of text, routing to token or thinking."""
        if not text:
            return

        # Prepend any partial tag bytes carried over from the last call
        remaining = self._partial + text
        self._partial = ""

        while remaining:
            if self._in_thinking:
                remaining = self._feed_in_thinking(remaining)
            else:
                remaining = self._feed_normal(remaining)

    def flush(self) -> None:
        """Flush any pending partial buffers.

        If we are in the middle of a thinking block the accumulated text is
        emitted as a thinking event even though no closing tag was seen
        (defensive: the stream may have ended early).  Any partial tag bytes
        are flushed as a token event.
        """
        if self._think_buf:
            self._on_thinking("".join(self._think_buf))
            self._think_buf = []
            self._in_thinking = False

        if self._partial:
            self._on_token(self._partial)
            self._partial = ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _feed_normal(self, text: str) -> str:
        """Process *text* while NOT inside a thinking block.

        Returns the unconsumed remainder (always empty — whole string is
        consumed; caller should not loop on the return value unless routing
        switches to thinking mode).
        """
        open_idx = text.find(_THINKING_OPEN)

        if open_idx == -1:
            # No opening tag found — but the text might end with a *partial*
            # prefix of <thinking> (e.g. text ends with "<thi").  Hold those
            # trailing bytes in self._partial so the next feed() can complete
            # the tag check.
            safe, self._partial = _split_partial_prefix(text, _THINKING_OPEN)
            if safe:
                self._on_token(safe)
            return ""

        # Opening tag found
        if open_idx > 0:
            # Emit everything before the tag as a token
            self._on_token(text[:open_idx])

        self._in_thinking = True
        return text[open_idx + len(_THINKING_OPEN):]

    def _feed_in_thinking(self, text: str) -> str:
        """Process *text* while INSIDE a thinking block.

        Returns the unconsumed remainder after the closing tag (or empty
        string if the closing tag hasn't arrived yet).
        """
        close_idx = text.find(_THINKING_CLOSE)

        if close_idx == -1:
            # Closing tag not present — but text might end with a partial
            # prefix of </thinking>.
            safe, self._partial = _split_partial_prefix(text, _THINKING_CLOSE)
            if safe:
                self._think_buf.append(safe)
            return ""

        # Closing tag found
        self._think_buf.append(text[:close_idx])
        self._on_thinking("".join(self._think_buf))
        self._think_buf = []
        self._in_thinking = False
        return text[close_idx + len(_THINKING_CLOSE):]


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _split_partial_prefix(text: str, tag: str) -> tuple[str, str]:
    """Split *text* into a safe part and a potential partial *tag* prefix.

    Checks whether any suffix of *text* is a prefix of *tag*.  If so,
    returns ``(text_without_suffix, suffix)``; otherwise returns
    ``(text, "")``.

    This prevents bytes that might be part of an opening or closing tag
    from being emitted prematurely when the tag is split across feed()
    calls.

    Examples::

        _split_partial_prefix("hello <thi", "<thinking>")
        # → ("hello ", "<thi")

        _split_partial_prefix("hello world", "<thinking>")
        # → ("hello world", "")
    """
    # Walk backwards through possible prefix lengths
    for length in range(min(len(tag) - 1, len(text)), 0, -1):
        suffix = text[-length:]
        if tag.startswith(suffix):
            return text[:-length], suffix
    return text, ""
