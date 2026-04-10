"""Tests for identity periodic tasks (cron)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from claude_worker.manager import (
    check_periodic_tasks,
    load_periodic_config,
)


class TestLoadPeriodicConfig:
    """load_periodic_config must read periodic.yaml."""

    def test_missing_file_returns_empty(self):
        result = load_periodic_config("nonexistent-identity-xyz")
        assert result == {}

    def test_loads_tasks(self, tmp_path: Path, monkeypatch):
        periodic_dir = tmp_path / ".cwork" / "identities" / "pm" / "hooks" / "periodic"
        periodic_dir.mkdir(parents=True)
        (periodic_dir / "periodic.yaml").write_text(
            "tasks:\n  hourly.sh: 3600\n  daily.sh: 86400\n"
        )
        monkeypatch.setattr("claude_worker.manager.Path.home", lambda: tmp_path)
        result = load_periodic_config("pm")
        assert result == {"hourly.sh": 3600.0, "daily.sh": 86400.0}

    def test_malformed_returns_empty(self, tmp_path: Path, monkeypatch):
        periodic_dir = tmp_path / ".cwork" / "identities" / "pm" / "hooks" / "periodic"
        periodic_dir.mkdir(parents=True)
        (periodic_dir / "periodic.yaml").write_text("not valid yaml {{")
        monkeypatch.setattr("claude_worker.manager.Path.home", lambda: tmp_path)
        result = load_periodic_config("pm")
        assert result == {}


class TestCheckPeriodicTasks:
    """check_periodic_tasks must run due tasks and inject output."""

    def test_runs_due_task(self, tmp_path: Path, monkeypatch):
        """A task whose interval has elapsed should run."""
        periodic_dir = tmp_path / ".cwork" / "identities" / "pm" / "hooks" / "periodic"
        periodic_dir.mkdir(parents=True)
        (periodic_dir / "periodic.yaml").write_text("tasks:\n  test.sh: 1\n")
        (periodic_dir / "test.sh").write_text("#!/bin/bash\necho 'hello from cron'")
        (periodic_dir / "test.sh").chmod(0o755)

        monkeypatch.setattr("claude_worker.manager.Path.home", lambda: tmp_path)

        # Create FIFO
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        in_fifo = runtime / "in"
        os.mkfifo(in_fifo)

        # Open FIFO reader
        rd_fd = os.open(str(in_fifo), os.O_RDONLY | os.O_NONBLOCK)
        try:
            check_periodic_tasks("pm", runtime, in_fifo)

            import json

            data = os.read(rd_fd, 65536)
            msg = json.loads(data.decode().strip())
            assert "[system:cron]" in msg["message"]["content"]
            assert "hello from cron" in msg["message"]["content"]
        finally:
            os.close(rd_fd)

    def test_skips_recently_run_task(self, tmp_path: Path, monkeypatch):
        """A task run recently should be skipped."""
        periodic_dir = tmp_path / ".cwork" / "identities" / "pm" / "hooks" / "periodic"
        periodic_dir.mkdir(parents=True)
        (periodic_dir / "periodic.yaml").write_text("tasks:\n  test.sh: 3600\n")
        (periodic_dir / "test.sh").write_text("#!/bin/bash\necho 'should not run'")
        (periodic_dir / "test.sh").chmod(0o755)

        monkeypatch.setattr("claude_worker.manager.Path.home", lambda: tmp_path)

        runtime = tmp_path / "runtime"
        runtime.mkdir()
        in_fifo = runtime / "in"
        os.mkfifo(in_fifo)

        # Write a recent timestamp
        ts_dir = runtime / "periodic"
        ts_dir.mkdir()
        (ts_dir / "test.sh.last").write_text(str(time.time()))

        rd_fd = os.open(str(in_fifo), os.O_RDONLY | os.O_NONBLOCK)
        try:
            check_periodic_tasks("pm", runtime, in_fifo)

            # Nothing should have been written
            try:
                data = os.read(rd_fd, 65536)
                assert data == b""
            except BlockingIOError:
                pass  # expected — no data in FIFO
        finally:
            os.close(rd_fd)
