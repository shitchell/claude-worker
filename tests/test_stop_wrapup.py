"""Tests for two-phase stop wrap-up (Feature 6).

Verifies that cmd_stop sends a wrap-up message before SIGTERM,
that --no-wrap-up skips it, and that dead workers skip wrap-up.

Note: running_worker runs _run_manager_forkless in a thread with
install_signals=False, so the manager's PID file contains the test
process's own PID. We must mock os.kill so cmd_stop doesn't SIGTERM
the test runner. Instead we signal the stub-claude subprocess directly
via handle.stop().

pid_alive() also calls os.kill(pid, 0), so our mock must pass signal-0
calls through to the real os.kill to avoid breaking liveness checks.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_worker.cli import (
    STOP_WRAPUP_MESSAGE,
    cmd_stop,
)
from claude_worker.manager import get_runtime_dir

_real_os_kill = os.kill


def _make_fake_kill(handle, kill_calls):
    """Build a fake os.kill that intercepts lethal signals but passes
    through signal-0 (pid_alive liveness checks) to the real os.kill.

    When a lethal signal (SIGTERM/SIGKILL) is received, records the call
    and stops the handle so the manager thread exits.
    """

    def fake_kill(pid, sig):
        if sig == 0:
            # Liveness check — delegate to the real os.kill
            return _real_os_kill(pid, sig)
        kill_calls.append((pid, sig))
        # Actually stop the stub-claude so the manager thread exits
        handle.stop()

    return fake_kill


class TestStopWrapup:
    """Two-phase shutdown: wrap-up message → wait → SIGTERM."""

    def test_live_worker_gets_wrapup_before_sigterm(self, running_worker):
        """A live worker should receive the wrap-up message in the log
        before SIGTERM brings it down."""
        handle = running_worker(name="wrapup-live", initial_message="hello")
        assert handle.wait_for_log('"type": "result"', timeout=5.0)

        kill_calls = []
        # Snapshot log inside fake_kill — by this point _wait_for_turn
        # has already confirmed the wrap-up response landed in the log.
        log_snapshot = []

        def fake_kill(pid, sig):
            if sig == 0:
                return _real_os_kill(pid, sig)
            # Snapshot the log BEFORE stopping (cleanup deletes the dir)
            try:
                log_snapshot.append(handle.log_path.read_text())
            except OSError:
                pass
            kill_calls.append((pid, sig))
            handle.stop()

        args = argparse.Namespace(
            name="wrapup-live",
            force=False,
            no_wrap_up=False,
        )
        with patch("claude_worker.cli.os.kill", side_effect=fake_kill):
            cmd_stop(args)

        # The log should contain the stub's echo of the wrap-up message
        log_text = log_snapshot[0] if log_snapshot else ""
        assert "[system:stop-requested]" in log_text

        # SIGTERM should have been sent after the wrap-up
        assert len(kill_calls) >= 1
        assert kill_calls[0][1] == signal.SIGTERM

    def test_no_wrap_up_skips_message(self, running_worker):
        """--no-wrap-up should skip the wrap-up message and go straight
        to SIGTERM."""
        handle = running_worker(name="wrapup-skip", initial_message="hello")
        assert handle.wait_for_log('"type": "result"', timeout=5.0)

        # Snapshot the log before stop
        log_before = handle.log_path.read_text() if handle.log_path.exists() else ""

        kill_calls = []
        log_snapshot = []

        def fake_kill(pid, sig):
            if sig == 0:
                return _real_os_kill(pid, sig)
            if handle.log_path.exists():
                log_snapshot.append(handle.log_path.read_text())
            kill_calls.append((pid, sig))
            handle.stop()

        args = argparse.Namespace(
            name="wrapup-skip",
            force=False,
            no_wrap_up=True,
        )
        with patch("claude_worker.cli.os.kill", side_effect=fake_kill):
            cmd_stop(args)

        # The log should NOT contain the wrap-up message
        log_after = log_snapshot[0] if log_snapshot else log_before
        new_log = log_after[len(log_before) :]
        assert "[system:stop-requested]" not in new_log

        # SIGTERM still sent
        assert len(kill_calls) >= 1
        assert kill_calls[0][1] == signal.SIGTERM

    def test_dead_worker_skips_wrapup(self, fake_worker):
        """If the worker PID is not alive, skip wrap-up and go straight
        to cleanup."""
        from tests.conftest import make_system_init, make_result_message

        name = fake_worker(
            [make_system_init("u1"), make_result_message("u2")],
            name="wrapup-dead",
        )
        runtime = get_runtime_dir(name)
        # Write a PID that doesn't exist
        (runtime / "pid").write_text("999999999")

        args = argparse.Namespace(
            name="wrapup-dead",
            force=False,
            no_wrap_up=False,
        )
        # pid_alive will return False for the bogus PID, so wrap-up is
        # skipped. os.kill(999999999, SIGTERM) raises ProcessLookupError.
        cmd_stop(args)

        # The log should not contain the wrap-up message
        log_text = (runtime / "log").read_text() if (runtime / "log").exists() else ""
        assert "[system:stop-requested]" not in log_text

    def test_force_skips_wrapup(self, running_worker):
        """--force should skip wrap-up and send SIGKILL directly."""
        handle = running_worker(name="wrapup-force", initial_message="hello")
        assert handle.wait_for_log('"type": "result"', timeout=5.0)

        kill_calls = []
        log_snapshot = []

        def fake_kill(pid, sig):
            if sig == 0:
                return _real_os_kill(pid, sig)
            if handle.log_path.exists():
                log_snapshot.append(handle.log_path.read_text())
            kill_calls.append((pid, sig))
            handle.stop()

        args = argparse.Namespace(
            name="wrapup-force",
            force=True,
            no_wrap_up=False,
        )
        with patch("claude_worker.cli.os.kill", side_effect=fake_kill):
            cmd_stop(args)

        log_after = log_snapshot[0] if log_snapshot else ""
        assert "[system:stop-requested]" not in log_after

        # SIGKILL should have been sent (force mode)
        assert len(kill_calls) >= 1
        assert kill_calls[0][1] == signal.SIGKILL

    def test_wrapup_timeout_proceeds_to_sigterm(self, running_worker):
        """If the wrap-up wait times out, SIGTERM should still fire."""
        handle = running_worker(
            name="wrapup-timeout",
            initial_message="hello",
            stub_delay_ms=500,
        )
        assert handle.wait_for_log('"type": "result"', timeout=5.0)

        kill_calls = []

        def fake_kill(pid, sig):
            if sig == 0:
                return _real_os_kill(pid, sig)
            kill_calls.append((pid, sig))
            handle.stop()

        args = argparse.Namespace(
            name="wrapup-timeout",
            force=False,
            no_wrap_up=False,
        )
        # Very short timeout to trigger timeout path
        with (
            patch("claude_worker.cli.STOP_WRAPUP_TIMEOUT_SECONDS", 0.1),
            patch("claude_worker.cli.os.kill", side_effect=fake_kill),
        ):
            cmd_stop(args)

        # SIGTERM should still have been sent even though wrap-up timed out
        assert len(kill_calls) >= 1
        assert kill_calls[0][1] == signal.SIGTERM
