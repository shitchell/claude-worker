"""Tests for get_worker_status — the "working forever" bug.

Bug: a worker started with `--background` and no `--prompt` showed
`working` forever in `ls`. Root cause: get_worker_status iterates the
log tracking last_type for user/assistant/result messages. If the log
contains only system/init messages (no user input sent yet), last_type
stays None and the function falls through to the "working" default at
the bottom. An alive, idle worker is genuinely waiting — not working.

Fix: when the process is alive and no user/assistant/result has landed,
return "waiting" instead of "working". The worker is literally idling.
"""

from __future__ import annotations

import os

import pytest

from conftest import (
    make_assistant_message,
    make_result_message,
    make_system_init,
    make_user_message,
)


class TestIdleWorkerStatus:
    """Alive workers with no user input should show `waiting`, not `working`."""

    def test_log_with_only_system_init_returns_waiting(self, fake_worker):
        """Worker is alive, log has only system/init — should be waiting."""
        from claude_worker.cli import get_worker_status
        from claude_worker.manager import get_runtime_dir

        name = fake_worker(
            [make_system_init("sys-uuid-0001-0002-000300040005")],
            alive=True,
        )
        runtime = get_runtime_dir(name)
        status, _ = get_worker_status(runtime)
        assert status == "waiting"

    def test_log_with_no_alive_process_returns_dead(self, fake_worker):
        """No PID file → dead, regardless of log contents."""
        from claude_worker.cli import get_worker_status
        from claude_worker.manager import get_runtime_dir

        name = fake_worker(
            [make_system_init("sys-uuid-0001-0002-000300040005")],
            alive=False,  # no pid file
        )
        runtime = get_runtime_dir(name)
        status, _ = get_worker_status(runtime)
        assert status == "dead"

    def test_completed_turn_returns_waiting_when_aged(self, fake_worker):
        """A completed turn counts as waiting only if the log has been
        quiet for at least STATUS_IDLE_THRESHOLD_SECONDS. We simulate
        the quiet period by backdating the log file's mtime via
        os.utime.
        """
        import os as _os
        from claude_worker.cli import (
            STATUS_IDLE_THRESHOLD_SECONDS,
            get_worker_status,
        )
        from claude_worker.manager import get_runtime_dir

        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("q", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("a", "a1xx-0001-0002-0003-000400050006"),
                make_result_message("r1xx-0001-0002-0003-000400050006"),
            ],
            alive=True,
        )
        runtime = get_runtime_dir(name)

        # Age the log well past the threshold
        log_path = runtime / "log"
        old_time = log_path.stat().st_mtime - (STATUS_IDLE_THRESHOLD_SECONDS + 1.0)
        _os.utime(log_path, (old_time, old_time))

        status, _ = get_worker_status(runtime)
        assert status == "waiting"

    def test_completed_turn_returns_working_when_fresh(self, fake_worker):
        """A completed turn with a FRESH log mtime is treated as working,
        because a subagent dispatch could still be coming. Regression
        test for the STATUS_IDLE_THRESHOLD_SECONDS behavior introduced
        for REPL and `ls`."""
        from claude_worker.cli import get_worker_status
        from claude_worker.manager import get_runtime_dir

        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("q", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("a", "a1xx-0001-0002-0003-000400050006"),
                make_result_message("r1xx-0001-0002-0003-000400050006"),
            ],
            alive=True,
        )
        # Log was just written by the fixture — mtime is within the
        # threshold, so status should report working, not waiting.
        runtime = get_runtime_dir(name)
        status, _ = get_worker_status(runtime)
        assert status == "working"

    def test_mid_turn_user_message_returns_working(self, fake_worker):
        """A user message without a trailing turn-end is actively being
        processed — status should be working."""
        from claude_worker.cli import get_worker_status
        from claude_worker.manager import get_runtime_dir

        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("q", "u1xx-0001-0002-0003-000400050006"),
                # No assistant response or result yet
            ],
            alive=True,
        )
        runtime = get_runtime_dir(name)
        status, _ = get_worker_status(runtime)
        assert status == "working"
