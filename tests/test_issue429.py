"""
Tests for issue #429 — Feishu/WeChat sessions show 'N/A' source_tag
instead of a platform name or nothing.

Root cause: sessions in hermes-agent's state.db may have source field
set to NULL, empty string, or a legacy/unknown value (e.g. 'N/A').
The WebUI was displaying whatever raw value it received.

Fix: in static/sessions.js:
  - _formatSourceTag() returns null for unknown/unrecognised tags
    (previously returned the raw tag string, surfacing 'N/A' etc.)
  - metaBits push is guarded: only push if _formatSourceTag returns
    a non-null value
  - [SYSTEM:] title fallback uses _SOURCE_DISPLAY map only, falls
    back to 'Gateway' -- never surfaces an unknown raw source_tag

Tests verify via JS source inspection (structural) only — no live
server needed.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text()


# ── Source-level structural checks ───────────────────────────────────────────

def test_format_source_tag_returns_null_for_unknown():
    """_formatSourceTag must return null (not the raw tag) for unrecognised values."""
    # The fixed function must have a null/falsy fallback, not return the raw tag
    # Pattern: names[tag] || tag  →  names[tag] || null
    # Find the _formatSourceTag function body
    start = SESSIONS_JS.find('function _formatSourceTag(')
    assert start != -1, "_formatSourceTag not found in sessions.js"
    fn_window = SESSIONS_JS[start:start+300]
    # Must NOT return the raw tag as fallback — old pattern was: return names[tag]||tag
    assert 'return names[tag]||tag' not in fn_window, (
        "_formatSourceTag must not return the raw tag for unknown values — "
        "this causes 'N/A' or other garbage to appear in the session list"
    )


def test_format_source_tag_has_null_fallback():
    """_formatSourceTag must return null (or falsy) for unknown tags."""
    start = SESSIONS_JS.find('function _formatSourceTag(')
    assert start != -1
    fn_window = SESSIONS_JS[start:start+500]  # wider to cover full function body
    # Should have: return names[tag] || null
    assert 'return names[tag]||null' in fn_window or 'return names[tag] || null' in fn_window, (
        "_formatSourceTag should return null for unknown tags to suppress display"
    )


def test_metabits_push_is_guarded():
    """metaBits push of _formatSourceTag result must be guarded against null."""
    # The fix uses a temp variable pattern:
    #   const _stLabel = _formatSourceTag(s.source_tag); if(_stLabel) metaBits.push(_stLabel)
    idx = SESSIONS_JS.find('_stLabel')
    assert idx != -1, (
        "_stLabel guard variable not found — metaBits.push(_formatSourceTag()) "
        "must check the return value before pushing to avoid null/N/A entries"
    )
    context = SESSIONS_JS[idx:idx+120]
    assert 'if(_stLabel)' in context or 'if (_stLabel)' in context, (
        f"_stLabel must be checked before pushing. Context: {context!r}"
    )
    assert 'metaBits.push(_stLabel)' in context, (
        f"Expected metaBits.push(_stLabel). Context: {context!r}"
    )


def test_known_platforms_still_display():
    """Known platform tags (telegram, feishu, weixin, etc.) must still appear."""
    start = SESSIONS_JS.find('function _formatSourceTag(')
    assert start != -1
    fn_window = SESSIONS_JS[start:start+500]  # wider to cover full function body
    for platform in ('telegram', 'feishu', 'weixin', 'discord', 'slack'):
        assert platform in fn_window, (
            f"Platform '{platform}' missing from _formatSourceTag names map"
        )


def test_system_prompt_title_fallback_no_raw_source():
    """[SYSTEM:] title fallback must use display map or 'Gateway', not raw source_tag."""
    # Find the [SYSTEM:] guard block
    idx = SESSIONS_JS.find("cleanTitle.startsWith('[SYSTEM:')")
    assert idx != -1, "[SYSTEM:] guard not found in sessions.js"
    block = SESSIONS_JS[idx:idx+200]
    # The fallback must end with ||'Gateway' and must look up via _SOURCE_DISPLAY
    # It must NOT just use s.source_tag directly as a fallback
    # Old broken pattern: (_SOURCE_DISPLAY[s.source_tag]||s.source_tag||'Gateway')
    # Fixed pattern:      (_SOURCE_DISPLAY[s.source_tag]||'Gateway')
    assert "||s.source_tag||" not in block, (
        "System prompt title fallback must not use s.source_tag directly — "
        "this would surface 'N/A' as a session title for unknown source values. "
        f"Found: {block!r}"
    )
    assert "'Gateway'" in block, (
        "System prompt title fallback must have 'Gateway' as the final fallback"
    )


def test_source_tag_guard_before_dataset_set():
    """el.dataset.source assignment must be guarded (only set for known/non-empty tags)."""
    # This is already guarded in the original: if(s.source_tag) el.dataset.source=...
    # Verify it's still there
    idx = SESSIONS_JS.find('el.dataset.source=s.source_tag')
    assert idx != -1, "dataset.source assignment not found"
    context = SESSIONS_JS[max(0, idx-40):idx+50]
    assert 'if(' in context or '&&' in context, (
        "el.dataset.source assignment must be guarded against null/empty source_tag"
    )


def test_na_string_not_in_known_names():
    """'N/A' must not appear as a value in the _formatSourceTag names map."""
    start = SESSIONS_JS.find('function _formatSourceTag(')
    assert start != -1
    fn_window = SESSIONS_JS[start:start+500]
    # Find where the const names = {...} map ends (closing brace)
    map_start = fn_window.find('const names={')
    map_end = fn_window.find('};', map_start)
    names_map = fn_window[map_start:map_end+2] if map_end != -1 else fn_window[map_start:map_start+200]
    assert "'N/A'" not in names_map and '"N/A"' not in names_map, (
        f"'N/A' must not be a value in the source tag names map. Found: {names_map!r}"
    )
