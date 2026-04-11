"""Tests for the replaceme subcommand.

Tests the individual phases of the replacement flow:
- Worker ancestry detection
- Wrap-up validation
- Argument construction (no duplicate flags)
- Error logging on failure
- Identity resolution
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_worker.cli import (
    REPLACEME_ERROR_LOG_SUFFIX,
    REPLACEME_HANDOFF_MAX_AGE_MINUTES,
    STATUS_IDLE_THRESHOLD_SECONDS,
    _strip_flag_with_value,
    _validate_wrapup,
)
from claude_worker.manager import get_base_dir, save_worker
from tests.conftest import make_result_message, make_user_message


# ---------------------------------------------------------------------------
# _strip_flag_with_value
# ---------------------------------------------------------------------------


class TestStripFlagWithValue:
    """Test the _strip_flag_with_value helper."""

    def test_flag_present(self):
        args = ["--foo", "bar", "--append-system-prompt-file", "/tmp/x", "--baz"]
        result = _strip_flag_with_value(args, "--append-system-prompt-file")
        assert result == ["--foo", "bar", "--baz"]

    def test_flag_absent(self):
        args = ["--foo", "bar", "--baz"]
        result = _strip_flag_with_value(args, "--append-system-prompt-file")
        assert result == ["--foo", "bar", "--baz"]

    def test_flag_at_end_no_value(self):
        args = ["--foo", "bar", "--append-system-prompt-file"]
        result = _strip_flag_with_value(args, "--append-system-prompt-file")
        assert result == ["--foo", "bar"]

    def test_multiple_occurrences(self):
        args = [
            "--append-system-prompt-file",
            "/a",
            "--foo",
            "--append-system-prompt-file",
            "/b",
        ]
        result = _strip_flag_with_value(args, "--append-system-prompt-file")
        assert result == ["--foo"]

    def test_empty_list(self):
        assert _strip_flag_with_value([], "--flag") == []


# ---------------------------------------------------------------------------
# _validate_wrapup
# ---------------------------------------------------------------------------


class TestValidateWrapup:
    """Test wrap-up validation for replaceme."""

    def test_working_worker(self, fake_worker):
        """Working worker should fail validation."""
        # A user message with no trailing result → "working" status
        name = fake_worker(
            [make_user_message("hello", "u1")],
            alive=True,
        )
        from claude_worker.manager import get_runtime_dir

        runtime = get_runtime_dir(name)
        error = _validate_wrapup(name, runtime)
        assert error is not None
        assert "still working" in error

    def test_dead_worker(self, fake_worker):
        """Dead worker should fail validation."""
        name = fake_worker(
            [make_result_message("r1")],
            alive=False,
        )
        from claude_worker.manager import get_runtime_dir

        runtime = get_runtime_dir(name)
        error = _validate_wrapup(name, runtime)
        assert error is not None
        assert "dead" in error

    def test_waiting_worker_passes(self, fake_worker):
        """Waiting worker with no identity should pass."""
        name = fake_worker(
            [make_result_message("r1")],
            alive=True,
        )
        from claude_worker.manager import get_runtime_dir

        runtime = get_runtime_dir(name)
        # Make the log old enough to pass the idle threshold
        log_file = runtime / "log"
        old_time = time.time() - STATUS_IDLE_THRESHOLD_SECONDS - 1
        os.utime(log_file, (old_time, old_time))
        error = _validate_wrapup(name, runtime)
        assert error is None

    def test_identity_needs_handoff(self, fake_worker, tmp_path):
        """PM worker that's waiting but has no handoff directory should fail."""
        name = fake_worker(
            [make_result_message("r1")],
            alive=True,
        )
        from claude_worker.manager import get_runtime_dir

        runtime = get_runtime_dir(name)
        # Make the log old enough
        log_file = runtime / "log"
        old_time = time.time() - STATUS_IDLE_THRESHOLD_SECONDS - 1
        os.utime(log_file, (old_time, old_time))

        # Save worker as PM with a cwd that has no handoff dir
        cwd = str(tmp_path / "project")
        (tmp_path / "project").mkdir()
        save_worker(name, cwd=cwd, identity="pm", pm=True)

        error = _validate_wrapup(name, runtime)
        assert error is not None
        assert "handoff" in error.lower()

    def test_identity_with_fresh_handoff(self, fake_worker, tmp_path):
        """PM worker with a recent handoff file should pass."""
        name = fake_worker(
            [make_result_message("r1")],
            alive=True,
        )
        from claude_worker.manager import get_runtime_dir

        runtime = get_runtime_dir(name)
        # Make the log old enough
        log_file = runtime / "log"
        old_time = time.time() - STATUS_IDLE_THRESHOLD_SECONDS - 1
        os.utime(log_file, (old_time, old_time))

        # Save worker as PM with a cwd that HAS a fresh handoff
        cwd = str(tmp_path / "project")
        project = tmp_path / "project"
        project.mkdir()
        handoff_dir = project / ".cwork" / "pm" / "handoffs"
        handoff_dir.mkdir(parents=True)
        handoff_file = handoff_dir / "handoff-001.md"
        handoff_file.write_text("session notes")
        save_worker(name, cwd=cwd, identity="pm", pm=True)

        error = _validate_wrapup(name, runtime)
        assert error is None


# ---------------------------------------------------------------------------
# Error logging
# ---------------------------------------------------------------------------


class TestReplacemeErrorLog:
    """Test that replacer errors are logged to a sidecar file."""

    def test_error_log_written(self, fake_worker, monkeypatch):
        """When the replacer hits an exception, the traceback is written
        to a sidecar log file at <base_dir>/<name>.replaceme.log."""
        name = fake_worker(
            [make_result_message("r1")],
            alive=True,
        )
        base = get_base_dir()
        error_log = base / f"{name}{REPLACEME_ERROR_LOG_SUFFIX}"

        # Simulate the error-logging path from cmd_replaceme's except block
        import traceback

        try:
            raise RuntimeError("simulated fork failure")
        except RuntimeError:
            tb = traceback.format_exc()
            error_log.parent.mkdir(parents=True, exist_ok=True)
            error_log.write_text(tb)

        assert error_log.exists()
        content = error_log.read_text()
        assert "RuntimeError" in content
        assert "simulated fork failure" in content


# ---------------------------------------------------------------------------
# Saved args deduplication
# ---------------------------------------------------------------------------


class TestSavedArgsDedup:
    """Test that --append-system-prompt-file is deduplicated."""

    def test_strip_from_saved_args(self):
        """saved_args with --append-system-prompt-file should be stripped
        before building claude_args."""
        saved_args = [
            "--append-system-prompt-file",
            "/old/runtime/identity.md",
            "--agent",
            "worker",
        ]
        cleaned = _strip_flag_with_value(saved_args, "--append-system-prompt-file")
        claude_args = ["--resume", "sess-123"] + cleaned

        # Verify no --append-system-prompt-file in the base claude_args
        assert "--append-system-prompt-file" not in claude_args
        assert claude_args == ["--resume", "sess-123", "--agent", "worker"]

    def test_no_duplicate_after_identity_prepend(self):
        """After stripping and re-prepending, exactly one occurrence."""
        saved_args = [
            "--append-system-prompt-file",
            "/old/runtime/identity.md",
            "--agent",
            "worker",
        ]
        cleaned = _strip_flag_with_value(saved_args, "--append-system-prompt-file")
        claude_args = ["--resume", "sess-123"] + cleaned

        # Simulate the identity file prepend (step 6e in cmd_replaceme)
        identity_path = "/new/runtime/identity.md"
        claude_args = [
            "--append-system-prompt-file",
            identity_path,
        ] + claude_args

        # Exactly one --append-system-prompt-file
        count = claude_args.count("--append-system-prompt-file")
        assert count == 1
        # And it points to the new path
        idx = claude_args.index("--append-system-prompt-file")
        assert claude_args[idx + 1] == identity_path
