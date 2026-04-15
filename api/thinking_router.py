"""conductor/lib/thinking_router.py — Standalone thinking/token SSE router.

Routes streamed text to token or thinking callbacks.
Supports both <thinking>...</thinking> and <think>...</think> tags
(used by different model families: Gemma 4, Qwen, etc.).

Usage:
    router = ThinkingRouter(on_token_fn, on_thinking_fn)
    router.feed("some <think>deep</think> response text")
    router.flush()
"""
from __future__ import annotations

from typing import Callable

# Tag variants used by different models
_TAG_PAIRS = [
    ("<thinking>", "</thinking>"),  # OpenAI / Anthropic style
    ("<think>", "</think>"),        # Gemma 4, Qwen, DeepSeek-R1
    ("<thought>", "</thought>"),    # Gemma 4 MoE variant
]

# All possible opening tags and closing tags
_ALL_OPENS = [t[0] for t in _TAG_PAIRS]
_ALL_CLOSES = [t[1] for t in _TAG_PAIRS]


class ThinkingRouter:
    """Routes streamed text to token or thinking callbacks.

    Text inside thinking tags is accumulated and delivered to *on_thinking*
    when the closing tag is seen. All other text is forwarded to *on_token*
    immediately.

    Handles tags split across multiple feed() calls.
    """

    def __init__(
        self,
        on_token: Callable[[str], None],
        on_thinking: Callable[[str], None],
    ) -> None:
        self._on_token = on_token
        self._on_thinking = on_thinking
        self._in_thinking: bool = False
        self._think_buf: list[str] = []
        self._partial: str = ""
        # Which close tag to look for (set when we find an open tag)
        self._active_close: str = ""

    def feed(self, text: str) -> None:
        if not text:
            return
        remaining = self._partial + text
        self._partial = ""
        while remaining:
            if self._in_thinking:
                remaining = self._feed_in_thinking(remaining)
            else:
                remaining = self._feed_normal(remaining)

    def flush(self) -> None:
        if self._think_buf:
            self._on_thinking("".join(self._think_buf))
            self._think_buf = []
            self._in_thinking = False
        if self._partial:
            self._on_token(self._partial)
            self._partial = ""

    def _feed_normal(self, text: str) -> str:
        # Find the earliest opening tag
        best_idx = -1
        best_open = ""
        best_close = ""
        for open_tag, close_tag in _TAG_PAIRS:
            idx = text.find(open_tag)
            if idx != -1 and (best_idx == -1 or idx < best_idx):
                best_idx = idx
                best_open = open_tag
                best_close = close_tag

        if best_idx == -1:
            # No tag found — check for partial prefix of ANY opening tag
            safe, self._partial = _split_partial_prefix_multi(text, _ALL_OPENS)
            if safe:
                self._on_token(safe)
            return ""

        # Opening tag found
        if best_idx > 0:
            self._on_token(text[:best_idx])
        self._in_thinking = True
        self._active_close = best_close
        return text[best_idx + len(best_open):]

    def _feed_in_thinking(self, text: str) -> str:
        close_tag = self._active_close or "</thinking>"
        close_idx = text.find(close_tag)

        if close_idx == -1:
            safe, self._partial = _split_partial_prefix(text, close_tag)
            if safe:
                self._think_buf.append(safe)
            return ""

        self._think_buf.append(text[:close_idx])
        self._on_thinking("".join(self._think_buf))
        self._think_buf = []
        self._in_thinking = False
        self._active_close = ""
        return text[close_idx + len(close_tag):]


def _split_partial_prefix(text: str, tag: str) -> tuple[str, str]:
    """Split text into safe part + potential partial tag prefix."""
    for length in range(min(len(tag) - 1, len(text)), 0, -1):
        suffix = text[-length:]
        if tag.startswith(suffix):
            return text[:-length], suffix
    return text, ""


def _split_partial_prefix_multi(text: str, tags: list[str]) -> tuple[str, str]:
    """Like _split_partial_prefix but checks against multiple tags.
    Returns the most conservative split (longest partial match)."""
    longest_partial = ""
    for tag in tags:
        _, partial = _split_partial_prefix(text, tag)
        if len(partial) > len(longest_partial):
            longest_partial = partial
    if longest_partial:
        return text[:-len(longest_partial)], longest_partial
    return text, ""
