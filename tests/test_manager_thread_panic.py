"""Tests for manager thread panic handling (Imp-9).

The manager's stdout_to_log and fifo_to_stdin threads previously caught
only the specific exceptions they expected (JSONDecodeError, BrokenPipeError).
Any other exception (OSError on log write, unicode errors, unexpected
subprocess state) would silently kill the daemon thread. The manager
process kept running but was observably broken: `ls` shows the worker
as alive, `read` shows no new messages, and operators have no signal
to investigate.

Fix (loud): wrap each thread body in try/except Exception, write a
sentinel JSONL line describing the failure, then send SIGTERM to the
manager's own PID so the worker transitions to `dead` in ls output.
Half-working is worse than dead.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest


class TestManagerThreadPanic:
    """The _manager_thread_panic helper writes a sentinel and SIGTERMs."""

    def test_writes_sentinel_line_to_log(self, tmp_path, monkeypatch):
        """On panic, a JSONL line with type=manager_error is appended."""
        from claude_worker import manager as cw_manager

        log_path = tmp_path / "log"
        log_path.write_text('{"type":"system"}\n')

        # Mock os.kill so the test process doesn't actually SIGTERM itself
        killed: list = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        exc = RuntimeError("simulated thread panic")
        cw_manager._manager_thread_panic(log_path, "stdout_to_log", exc)

        lines = log_path.read_text().splitlines()
        assert len(lines) == 2
        sentinel = json.loads(lines[1])
        assert sentinel["type"] == "manager_error"
        assert sentinel["thread"] == "stdout_to_log"
        assert "simulated thread panic" in sentinel["error"]

    def test_sends_sigterm_to_own_pid(self, tmp_path, monkeypatch):
        """Panic must escalate to SIGTERM on the manager's own PID."""
        from claude_worker import manager as cw_manager

        log_path = tmp_path / "log"
        log_path.write_text("")

        killed: list = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        cw_manager._manager_thread_panic(log_path, "fifo_to_stdin", RuntimeError("x"))

        assert len(killed) == 1
        pid, sig = killed[0]
        assert pid == os.getpid()
        assert sig == signal.SIGTERM

    def test_panic_during_sentinel_write_still_signals(self, tmp_path, monkeypatch):
        """Even if the sentinel write itself fails (e.g., disk full),
        the SIGTERM must still happen — we can't recover either way,
        but the operator needs the dead-worker signal."""
        from claude_worker import manager as cw_manager

        # Point log_path at a location that will refuse writes
        readonly_parent = tmp_path / "readonly"
        readonly_parent.mkdir()
        readonly_parent.chmod(0o500)  # r-x only
        log_path = readonly_parent / "log"

        killed: list = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        try:
            cw_manager._manager_thread_panic(
                log_path, "stdout_to_log", RuntimeError("x")
            )
        finally:
            readonly_parent.chmod(0o700)  # restore so tmp_path cleanup works

        # SIGTERM still delivered despite the sentinel write failure
        assert len(killed) == 1
        assert killed[0][1] == signal.SIGTERM


class TestThreadBodyCallsPanicHandler:
    """When a thread body raises, the panic handler must be invoked."""

    def test_stdout_to_log_panic_on_write_error(self, tmp_path, monkeypatch):
        """Simulate a thread body that raises — verify panic handler runs.

        We can't easily run the full run_manager, so we test the wrapper
        pattern directly: a helper _run_manager_thread(fn, log_path, name)
        that calls fn and routes exceptions through _manager_thread_panic.
        """
        from claude_worker import manager as cw_manager

        log_path = tmp_path / "log"
        log_path.write_text("")

        panics: list = []

        def fake_panic(log_path_arg, thread_name, exc):
            panics.append((str(log_path_arg), thread_name, type(exc).__name__))

        monkeypatch.setattr(cw_manager, "_manager_thread_panic", fake_panic)

        def broken_body():
            raise OSError("simulated log write error")

        cw_manager._run_manager_thread(broken_body, log_path, "stdout_to_log")

        assert len(panics) == 1
        assert panics[0][1] == "stdout_to_log"
        assert panics[0][2] == "OSError"

    def test_clean_exit_does_not_trigger_panic(self, tmp_path, monkeypatch):
        """A thread body that returns cleanly must not call the panic handler."""
        from claude_worker import manager as cw_manager

        log_path = tmp_path / "log"
        log_path.write_text("")

        panics: list = []
        monkeypatch.setattr(
            cw_manager,
            "_manager_thread_panic",
            lambda *a, **kw: panics.append(a),
        )

        def clean_body():
            return None  # simulate normal thread exit

        cw_manager._run_manager_thread(clean_body, log_path, "stdout_to_log")

        assert panics == []
