"""Tests for FIFO-pending status check in get_worker_status.

When the FIFO has unread bytes (message written but not yet drained
by the manager), get_worker_status should return "working" even if
the log shows an aged turn-end.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from conftest import make_result_message, make_system_init

from claude_worker.cli import STATUS_IDLE_THRESHOLD_SECONDS, get_worker_status


class TestFifoPendingStatus:
    """get_worker_status must detect unread FIFO data as 'working'."""

    def test_aged_result_with_empty_fifo_is_waiting(self, fake_worker):
        """Normal case: aged result + no pending FIFO data → waiting."""
        name = fake_worker(
            [make_system_init("u1"), make_result_message("r1")],
            alive=True,
        )
        runtime = (
            Path(
                os.path.dirname(
                    os.path.dirname(
                        str(
                            fake_worker.__wrapped_base_dir__
                        )  # not accessible, use get_runtime_dir
                    )
                )
            )
            if False
            else None
        )

        # Use get_runtime_dir to find the runtime
        from claude_worker.manager import get_runtime_dir

        runtime = get_runtime_dir(name)

        # Age the log past the threshold
        log_file = runtime / "log"
        old_time = time.time() - STATUS_IDLE_THRESHOLD_SECONDS - 1
        os.utime(log_file, (old_time, old_time))

        # Create FIFO (empty)
        in_fifo = runtime / "in"
        if not in_fifo.exists():
            os.mkfifo(in_fifo)

        status, _ = get_worker_status(runtime)
        assert status == "waiting"

    def test_aged_result_with_pending_fifo_is_working(self, fake_worker):
        """FIFO has unread data → should report working despite aged log."""
        from claude_worker.manager import get_runtime_dir

        name = fake_worker(
            [make_system_init("u1"), make_result_message("r1")],
            alive=True,
        )
        runtime = get_runtime_dir(name)

        # Age the log past the threshold
        log_file = runtime / "log"
        old_time = time.time() - STATUS_IDLE_THRESHOLD_SECONDS - 1
        os.utime(log_file, (old_time, old_time))

        # Create FIFO and write data to it
        in_fifo = runtime / "in"
        if not in_fifo.exists():
            os.mkfifo(in_fifo)

        # Open read end first (non-blocking), then write end, then write data
        rd_fd = os.open(str(in_fifo), os.O_RDONLY | os.O_NONBLOCK)
        wr_fd = os.open(str(in_fifo), os.O_WRONLY)
        try:
            os.write(
                wr_fd,
                b'{"type":"user","message":{"role":"user","content":"pending"}}\n',
            )

            status, _ = get_worker_status(runtime)
            assert status == "working"
        finally:
            os.close(rd_fd)
            os.close(wr_fd)
