"""Tests for the claude-worker token-stats wire-in.

Covers the four integration points:
1. `_detect_context_window_size` reads the system/init model field
2. `_format_token_count_short` produces readable short forms
3. `_format_context_window_label` produces the ls/repl display string
4. `ls` output includes a `context:` line
5. `read --context` prints a one-line summary
6. `tokens` subcommand prints context + session totals
7. REPL banner includes the context label

The end-to-end tests use the stub_claude harness, which now emits
realistic `usage` blocks in assistant messages (see stub_claude.py).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# -- Low-level helper tests --------------------------------------------------


class TestDetectContextWindowSize:
    """Parse the model string in system/init to determine context window."""

    def test_model_with_1m_suffix_returns_1m(self, tmp_path):
        from claude_worker.cli import (
            CONTEXT_WINDOW_1M,
            _detect_context_window_size,
        )

        log = tmp_path / "log"
        log.write_text(
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-opus-4-6[1m]",
                }
            )
            + "\n"
        )
        assert _detect_context_window_size(log) == CONTEXT_WINDOW_1M

    def test_model_without_1m_returns_default(self, tmp_path):
        from claude_worker.cli import (
            CONTEXT_WINDOW_DEFAULT,
            _detect_context_window_size,
        )

        log = tmp_path / "log"
        log.write_text(
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-opus-4-6",
                }
            )
            + "\n"
        )
        assert _detect_context_window_size(log) == CONTEXT_WINDOW_DEFAULT

    def test_nonexistent_file_returns_sensible_default(self, tmp_path):
        from claude_worker.cli import (
            CONTEXT_WINDOW_1M,
            _detect_context_window_size,
        )

        # 1M is the optimistic fallback — it under-reports the percentage
        # rather than over-reporting, which is the safer failure mode.
        assert _detect_context_window_size(tmp_path / "nope") == CONTEXT_WINDOW_1M

    def test_log_without_init_returns_default(self, tmp_path):
        from claude_worker.cli import (
            CONTEXT_WINDOW_1M,
            _detect_context_window_size,
        )

        log = tmp_path / "log"
        log.write_text(
            json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
        )
        assert _detect_context_window_size(log) == CONTEXT_WINDOW_1M


class TestFormatTokenCountShort:
    """Compact token count formatting for display."""

    def test_small_value_unchanged(self):
        from claude_worker.cli import _format_token_count_short

        assert _format_token_count_short(42) == "42"
        assert _format_token_count_short(999) == "999"

    def test_thousands(self):
        from claude_worker.cli import _format_token_count_short

        assert _format_token_count_short(1_000) == "1k"
        assert _format_token_count_short(763_716) == "763k"

    def test_round_million_no_decimal(self):
        """Regression: 1,000,000 should render as '1M' not '1.0M'."""
        from claude_worker.cli import _format_token_count_short

        assert _format_token_count_short(1_000_000) == "1M"
        assert _format_token_count_short(2_000_000) == "2M"

    def test_non_round_million_has_decimal(self):
        from claude_worker.cli import _format_token_count_short

        assert _format_token_count_short(1_234_567) == "1.2M"


class TestFormatContextWindowLabel:
    """The ls/repl display label combining percentage and raw numbers."""

    def test_returns_none_for_missing_log(self, tmp_path):
        from claude_worker.cli import _format_context_window_label

        assert _format_context_window_label(tmp_path / "nope") is None

    def test_returns_none_for_log_without_assistant(self, tmp_path):
        from claude_worker.cli import _format_context_window_label

        log = tmp_path / "log"
        log.write_text(
            json.dumps(
                {"type": "system", "subtype": "init", "model": "claude-opus-4-6"}
            )
            + "\n"
        )
        assert _format_context_window_label(log) is None

    def test_builds_label_from_assistant_usage(self, tmp_path):
        from claude_worker.cli import _format_context_window_label

        log = tmp_path / "log"
        entries = [
            {
                "type": "system",
                "subtype": "init",
                "model": "claude-opus-4-6[1m]",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "id": "msg_abc",
                    "content": [{"type": "text", "text": "hi"}],
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_creation_input_tokens": 200,
                        "cache_read_input_tokens": 700_000,
                    },
                },
                "uuid": "aaaa-1111",
            },
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        label = _format_context_window_label(log)
        # total = 100 + 200 + 700_000 = 700_300; 700300 / 1M = 70%
        assert label is not None
        assert "%" in label
        assert "700k" in label
        assert "1M" in label


# -- End-to-end integration tests using running_worker fixture ---------------


class TestLsShowsContextLine:
    """`ls` output should include a `context:` line for workers with usage."""

    def test_context_line_present_after_first_turn(self, running_worker, capsys):
        from claude_worker.cli import cmd_list
        import argparse

        handle = running_worker(name="ls-ctx", initial_message="hi")
        # Wait for the full init + response + result to land
        assert handle.wait_for_log('"type": "result"', timeout=5.0)

        cmd_list(argparse.Namespace())
        captured = capsys.readouterr()
        assert "ls-ctx" in captured.out
        assert "context:" in captured.out
        handle.stop()


class TestReadContextFlag:
    """`read --context` prints a one-line summary and exits."""

    def test_read_context_prints_label(self, running_worker, capsys):
        import argparse
        from claude_worker.cli import cmd_read

        handle = running_worker(name="read-ctx", initial_message="hi")
        assert handle.wait_for_log('"type": "result"', timeout=5.0)

        args = argparse.Namespace(
            name=handle.name,
            follow=False,
            since=None,
            until=None,
            last_turn=False,
            n=None,
            count=False,
            summary=False,
            verbose=False,
            exclude_user=False,
            color=False,
            no_color=True,
            chat=None,
            all_chats=True,
            context=True,
        )
        result = cmd_read(args)
        assert result == (None, None)  # short-circuit return
        captured = capsys.readouterr()
        # Output should be a single line like "NN% (Nk/1M)"
        assert "%" in captured.out
        assert "/" in captured.out
        handle.stop()


class TestCmdTokens:
    """The `tokens` subcommand prints context + session totals."""

    def test_tokens_output_includes_both_sections(self, running_worker, capsys):
        import argparse
        from claude_worker.cli import cmd_tokens

        handle = running_worker(name="tokens-test", initial_message="hi")
        assert handle.wait_for_log('"type": "result"', timeout=5.0)

        cmd_tokens(argparse.Namespace(name=handle.name))
        captured = capsys.readouterr()
        assert "Worker: tokens-test" in captured.out
        assert "Context window:" in captured.out
        assert "Session totals" in captured.out
        assert "input_tokens:" in captured.out
        assert "output_tokens:" in captured.out
        assert "cache_creation:" in captured.out
        assert "cache_read:" in captured.out
        assert "total_tokens:" in captured.out
        assert "unique_api_calls:" in captured.out
        handle.stop()


class TestReplBannerIncludesContext:
    """REPL entry banner should include the context window label when
    the worker has assistant turns to compute from."""

    def test_banner_shows_context_label(self, running_worker, monkeypatch, capsys):
        from claude_worker import cli as cw_cli

        handle = running_worker(name="repl-ctx", initial_message="hi")
        assert handle.wait_for_log('"type": "result"', timeout=5.0)

        # Age log past the status threshold
        old_time = handle.log_path.stat().st_mtime - 5.0
        os.utime(handle.log_path, (old_time, old_time))

        # EOF immediately so REPL prints banner + exits
        def fake_input(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr(cw_cli, "STATUS_IDLE_THRESHOLD_SECONDS", 0.2)

        cw_cli.cmd_repl(__import__("argparse").Namespace(name=handle.name, chat=None))

        captured = capsys.readouterr()
        assert "=== claude-worker REPL: repl-ctx" in captured.out
        assert "context:" in captured.out
        handle.stop()
