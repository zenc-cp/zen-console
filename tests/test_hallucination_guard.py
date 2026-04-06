"""
Tests for the hallucination guard in api/streaming.py.

These tests verify that the guard correctly detects and strips fabricated
tool output from model responses — the core fix for the "Hermes writes fake
tool results instead of actually calling tools" bug.
"""
import sys
import pathlib

# Add repo root to path so we can import api.streaming
REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.streaming import (
    _contains_fake_tool_output,
    _strip_fake_tool_output,
    _ANTI_HALLUCINATION_PROMPT,
)


class TestContainsFakeToolOutput:
    """Test _contains_fake_tool_output detection."""

    def test_simple_fake_output(self):
        text = '{"output": "total 396\\ndrwxr-xr-x", "exit_code": 0}'
        assert _contains_fake_tool_output(text) is True

    def test_fake_output_with_stderr(self):
        text = '{"output": "hello world", "stderr": "", "exit_code": 0}'
        assert _contains_fake_tool_output(text) is True

    def test_executing_with_json(self):
        text = 'Executing:\n{"output": "some result", "exit_code": 0}'
        assert _contains_fake_tool_output(text) is True

    def test_executing_with_backtick(self):
        text = 'Executing:\n```json\n{"output": "result"}\n```'
        assert _contains_fake_tool_output(text) is True

    def test_output_label_with_json(self):
        text = 'Output:\n{"output": "test", "exit_code": 0}'
        assert _contains_fake_tool_output(text) is True

    def test_result_label_with_json(self):
        text = 'Result:\n{"result": "success", "exit_code": 0}'
        assert _contains_fake_tool_output(text) is True

    def test_running_backtick_command(self):
        text = 'Running `ls -la`:\n```json\n{"output": "total 4", "exit_code": 0}\n```'
        assert _contains_fake_tool_output(text) is True

    def test_normal_text_not_flagged(self):
        text = "I'll run the ls command for you to check the directory contents."
        assert _contains_fake_tool_output(text) is False

    def test_code_discussion_not_flagged(self):
        text = 'The function returns a dict like {"name": "test", "value": 42}'
        assert _contains_fake_tool_output(text) is False

    def test_empty_string(self):
        assert _contains_fake_tool_output("") is False

    def test_none(self):
        assert _contains_fake_tool_output(None) is False

    def test_json_in_code_block_discussion(self):
        """Regular JSON discussion should not be flagged."""
        text = 'The API returns:\n```json\n{"name": "test", "status": "ok"}\n```'
        assert _contains_fake_tool_output(text) is False

    def test_mentioning_exit_code_in_prose(self):
        """Talking about exit codes in prose should not be flagged."""
        text = "The command returned exit_code 1, indicating an error."
        assert _contains_fake_tool_output(text) is False

    def test_real_world_hallucination_pattern(self):
        """Match the exact pattern from the bug report."""
        text = '''Executing:
{"output": "total 396\ndrwxr-xr-x  2 user user  4096 Apr  1 12:00 scripts\n-rw-r--r--  1 user user  1234 Apr  1 12:00 README.md", "exit_code": 0}'''
        assert _contains_fake_tool_output(text) is True


class TestStripFakeToolOutput:
    """Test _strip_fake_tool_output sanitization."""

    def test_strips_fake_json(self):
        text = 'Here is the result: {"output": "hello world", "exit_code": 0}'
        result = _strip_fake_tool_output(text)
        assert '"output"' not in result
        assert '"exit_code"' not in result
        assert 'Fabricated output removed' in result

    def test_strips_fake_json_with_stderr(self):
        text = '{"output": "data", "stderr": "warn", "exit_code": 1}'
        result = _strip_fake_tool_output(text)
        assert '"output"' not in result
        assert 'Fabricated output removed' in result

    def test_preserves_surrounding_text(self):
        text = 'Before text. {"output": "fake", "exit_code": 0} After text.'
        result = _strip_fake_tool_output(text)
        assert 'Before text.' in result
        assert 'After text.' in result

    def test_no_change_for_clean_text(self):
        text = "This is a normal assistant response with no fake output."
        assert _strip_fake_tool_output(text) == text

    def test_empty_string(self):
        assert _strip_fake_tool_output("") == ""

    def test_none(self):
        assert _strip_fake_tool_output(None) is None

    def test_multiple_fake_blocks(self):
        text = (
            'First: {"output": "a", "exit_code": 0}\n'
            'Second: {"output": "b", "exit_code": 1}'
        )
        result = _strip_fake_tool_output(text)
        # Both should be replaced
        assert result.count('Fabricated output removed') == 2


class TestAntiHallucinationPrompt:
    """Test the system prompt content."""

    def test_prompt_exists(self):
        assert _ANTI_HALLUCINATION_PROMPT
        assert len(_ANTI_HALLUCINATION_PROMPT) > 100

    def test_prompt_mentions_terminal_tool(self):
        assert 'terminal tool' in _ANTI_HALLUCINATION_PROMPT

    def test_prompt_mentions_never_fabricate(self):
        assert 'NEVER' in _ANTI_HALLUCINATION_PROMPT
        assert 'fabricate' in _ANTI_HALLUCINATION_PROMPT.lower()

    def test_prompt_mentions_output_format(self):
        assert '"output"' in _ANTI_HALLUCINATION_PROMPT
        assert '"exit_code"' in _ANTI_HALLUCINATION_PROMPT
