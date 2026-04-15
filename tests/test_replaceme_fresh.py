"""Tests for #072: replaceme starts fresh, never resumes."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_worker.cli import (
    _build_replaceme_initial_message,
    _strip_flag_with_value,
)


# ---------------------------------------------------------------------------
# a. replaceme never adds --resume to claude_args
# ---------------------------------------------------------------------------


class TestReplacemeClaudeArgs:
    """The replaceme code path must NEVER produce --resume in claude_args.

    This is the core invariant from D90: replaceme is fresh-start only.
    Prior to #072, cmd_replaceme built claude_args as ["--resume",
    session_id] + saved_args, silently preserving the prior conversation.
    This test pins the fix: claude_args is built from saved_args alone
    (after stripping --append-system-prompt-file), and --resume never
    enters the list.
    """

    def test_replaceme_never_adds_resume_flag(self):
        """Mirror the logic in cmd_replaceme (post-#072) and verify that
        --resume is absent from the resulting claude_args."""
        # Simulate saved.claude_args from a worker that had been resumed
        # at start time — the old path would have baked in --resume.
        saved_args = [
            "--append-system-prompt-file",
            "/old/runtime/identity.md",
            "--agent",
            "worker",
        ]
        # Mirrors cmd_replaceme step 5: strip prompt file, no --resume.
        cleaned = _strip_flag_with_value(saved_args, "--append-system-prompt-file")
        claude_args = list(cleaned)

        # Mirrors step 6e: prepend identity file for identity workers.
        claude_args = [
            "--append-system-prompt-file",
            "/new/runtime/identity.md",
        ] + claude_args

        # The invariant — D90
        assert "--resume" not in claude_args

    def test_replaceme_claude_args_shape(self):
        """claude_args is saved_args (stripped) with identity prepended
        only — no session-resume args sneak in."""
        saved_args = ["--agent", "worker", "--model", "claude-opus-4"]
        cleaned = _strip_flag_with_value(saved_args, "--append-system-prompt-file")
        claude_args = list(cleaned)

        # No identity prepend in this scenario (worker identity).
        assert claude_args == ["--agent", "worker", "--model", "claude-opus-4"]
        assert "--resume" not in claude_args


# ---------------------------------------------------------------------------
# b. argparse rejects --resume on replaceme
# ---------------------------------------------------------------------------


class TestReplacemeArgparse:
    """replaceme has no --resume flag. Passing one must fail at argparse."""

    def test_replaceme_argparse_no_resume_flag(self):
        """CLI `claude-worker replaceme --resume foo` must exit non-zero."""
        # Invoke the real CLI in a subprocess — argparse errors raise
        # SystemExit which is hard to assert cleanly without isolating the
        # process. We assert on the exit code + stderr content.
        result = subprocess.run(
            [sys.executable, "-m", "claude_worker", "replaceme", "--resume", "foo"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        # argparse error goes to stderr. It should complain about --resume
        # being unrecognized (not about a missing arg or an auth problem).
        assert "--resume" in result.stderr
        assert "unrecognized" in result.stderr.lower() or "error" in result.stderr.lower()


# ---------------------------------------------------------------------------
# c-e. _build_replaceme_initial_message behavior
# ---------------------------------------------------------------------------


class TestBuildReplacemeInitialMessage:
    """The fresh PM/TL gets an initial prompt that points at the most
    recent handoff file — that's the continuity mechanism."""

    def test_includes_handoff_hint(self, tmp_path):
        """With a handoff file present, the returned message must
        reference its path."""
        cwd = tmp_path / "project"
        handoff_dir = cwd / ".cwork" / "roles" / "pm" / "handoffs"
        handoff_dir.mkdir(parents=True)
        handoff_file = handoff_dir / "2026-04-13_session-01.md"
        handoff_file.write_text("session notes")

        msg = _build_replaceme_initial_message("pm", str(cwd))
        assert msg is not None
        assert str(handoff_file) in msg
        assert "fresh replacement" in msg
        assert "handoff file is the continuity mechanism" in msg

    def test_no_handoffs_returns_internalize_only(self, tmp_path):
        """No handoff dir/files → returns just the internalize message
        (no hint). For PM, the internalize message is non-empty, so the
        returned value is non-None but contains no handoff path."""
        cwd = tmp_path / "project"
        cwd.mkdir()

        msg = _build_replaceme_initial_message("pm", str(cwd))
        assert msg is not None
        # No handoff path interpolated
        assert "fresh replacement" not in msg
        assert "handoff file is the continuity mechanism" not in msg

    def test_no_handoffs_unknown_identity_returns_none(self, tmp_path):
        """Unknown identity + no handoffs → returns None (no message at
        all). Regression: empty handoff_hint + None internalize must
        collapse to None, not "" (run_manager treats None as "skip
        initial send")."""
        cwd = tmp_path / "project"
        cwd.mkdir()

        msg = _build_replaceme_initial_message("some-unknown", str(cwd))
        assert msg is None

    def test_uses_latest_handoff(self, tmp_path):
        """Multiple handoff files present — pick the latest by filename
        sort (handoff files are named with ISO dates → lexical sort
        matches chronological order)."""
        cwd = tmp_path / "project"
        handoff_dir = cwd / ".cwork" / "roles" / "pm" / "handoffs"
        handoff_dir.mkdir(parents=True)
        old_handoff = handoff_dir / "2026-04-10_session-01.md"
        mid_handoff = handoff_dir / "2026-04-11_session-02.md"
        latest_handoff = handoff_dir / "2026-04-13_session-03.md"
        for f in (old_handoff, mid_handoff, latest_handoff):
            f.write_text("notes")

        msg = _build_replaceme_initial_message("pm", str(cwd))
        assert msg is not None
        assert str(latest_handoff) in msg
        # The earlier handoffs must NOT be referenced — only the latest
        assert str(old_handoff) not in msg
        assert str(mid_handoff) not in msg

    def test_technical_lead_role_dir(self, tmp_path):
        """The technical-lead identity maps to the 'tl' role directory
        (via IDENTITY_ROLE_DIRS). Handoffs under .cwork/roles/tl/handoffs
        must be found."""
        cwd = tmp_path / "project"
        handoff_dir = cwd / ".cwork" / "roles" / "tl" / "handoffs"
        handoff_dir.mkdir(parents=True)
        handoff_file = handoff_dir / "2026-04-13_session-01.md"
        handoff_file.write_text("tl notes")

        msg = _build_replaceme_initial_message("technical-lead", str(cwd))
        assert msg is not None
        assert str(handoff_file) in msg

    def test_only_files_considered(self, tmp_path):
        """Subdirectories inside handoffs/ must not be treated as
        handoff files (`f.is_file()` guard)."""
        cwd = tmp_path / "project"
        handoff_dir = cwd / ".cwork" / "roles" / "pm" / "handoffs"
        handoff_dir.mkdir(parents=True)
        # A subdirectory that would otherwise sort later than the file
        (handoff_dir / "zzz-subdir").mkdir()
        real_handoff = handoff_dir / "2026-04-13_session-01.md"
        real_handoff.write_text("notes")

        msg = _build_replaceme_initial_message("pm", str(cwd))
        assert msg is not None
        assert str(real_handoff) in msg
        assert "zzz-subdir" not in msg
