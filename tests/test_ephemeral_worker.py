"""Tests for ephemeral worker auto-reaping (#080, D97).

Covers:
- ``_ephemeral_should_reap`` — pure helper, log mtime vs timeout.
- ``_read_ephemeral_sentinel`` — sentinel-file parser.
- ``save_worker`` — ephemeral flags round-trip in .sessions.json.
- Manager lifecycle — sentinel + short idle timeout triggers reap.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from claude_worker import manager as cw_manager


class TestShouldReap:
    """_ephemeral_should_reap returns True iff the log is stale."""

    def test_fresh_log_not_reaped(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        log.write_text("ok")
        assert cw_manager._ephemeral_should_reap(log, idle_timeout=300.0) is False

    def test_idle_log_reaped(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        log.write_text("ok")
        past = time.time() - 600
        os.utime(log, (past, past))
        assert cw_manager._ephemeral_should_reap(log, idle_timeout=300.0) is True

    def test_missing_log_not_reaped(self, tmp_path: Path) -> None:
        """Missing log file = worker is starting; don't reap."""
        log = tmp_path / "nonexistent"
        assert cw_manager._ephemeral_should_reap(log, idle_timeout=1.0) is False

    def test_now_parameter_respected(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        log.write_text("ok")
        mtime = log.stat().st_mtime
        assert (
            cw_manager._ephemeral_should_reap(log, idle_timeout=300.0, now=mtime + 1000)
            is True
        )
        assert (
            cw_manager._ephemeral_should_reap(log, idle_timeout=300.0, now=mtime)
            is False
        )


class TestReadSentinel:
    """_read_ephemeral_sentinel parses or returns None."""

    def test_missing_sentinel_returns_none(self, tmp_path: Path) -> None:
        assert cw_manager._read_ephemeral_sentinel(tmp_path) is None

    def test_valid_sentinel_returns_timeout(self, tmp_path: Path) -> None:
        (tmp_path / "ephemeral").write_text("180\n")
        assert cw_manager._read_ephemeral_sentinel(tmp_path) == 180.0

    def test_malformed_sentinel_falls_back(self, tmp_path: Path) -> None:
        """Corrupt content returns a safe 300s default — don't strand the worker."""
        (tmp_path / "ephemeral").write_text("not-a-number")
        assert cw_manager._read_ephemeral_sentinel(tmp_path) == 300.0


class TestSaveWorkerMetadata:
    """save_worker round-trips ephemeral fields through .sessions.json."""

    def test_ephemeral_fields_persisted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from claude_worker import cli as cw_cli

        base_dir = tmp_path / "workers"
        base_dir.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)
        monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base_dir)

        cw_manager.save_worker(
            "w1",
            cwd=str(tmp_path),
            ephemeral=True,
            ephemeral_idle_timeout=120,
        )
        sessions = json.loads((base_dir / ".sessions.json").read_text())
        assert sessions["w1"]["ephemeral"] is True
        assert sessions["w1"]["ephemeral_idle_timeout"] == 120


class TestEphemeralLifecycle:
    """End-to-end: stub-claude + ephemeral sentinel + short idle → reap fires."""

    def test_manager_reaps_idle_ephemeral_worker(
        self,
        running_worker,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Shrink the reap cadence so the test doesn't wait 30s.
        monkeypatch.setattr(cw_manager, "EPHEMERAL_CHECK_INTERVAL_SECONDS", 0.2)
        monkeypatch.setattr(cw_manager, "EPHEMERAL_WRAPUP_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(cw_manager, "EPHEMERAL_WRAPUP_POLL_INTERVAL", 0.1)

        # The running_worker fixture calls create_runtime_dir which
        # errors on a pre-existing dir. Make it idempotent so we can
        # pre-seed the ephemeral sentinel before the manager starts.
        def _idempotent_create(name: str) -> Path:
            p = cw_manager.get_base_dir() / name
            p.mkdir(parents=True, exist_ok=True)
            # Recreate FIFOs the real create_runtime_dir makes.
            fifo_path = p / "in"
            if not fifo_path.exists():
                os.mkfifo(str(fifo_path))
            return p

        monkeypatch.setattr(cw_manager, "create_runtime_dir", _idempotent_create)

        # Pre-create the runtime dir and drop the sentinel.
        runtime_dir = cw_manager.create_runtime_dir("eph-live")
        (runtime_dir / "ephemeral").write_text("0.3\n")

        # Start the worker with no initial message — it will idle
        # immediately. The manager's reaper should fire within ~2s.
        handle = running_worker(name="eph-live", initial_message=None)

        # Give the log a moment to receive the init message so the mtime
        # gets established, then artifically age it past the 0.3s idle
        # threshold so the reaper triggers on the next cycle.
        time.sleep(0.2)
        log_path = runtime_dir / "log"
        if log_path.exists():
            past = time.time() - 5.0
            os.utime(log_path, (past, past))

        # Wait up to 10s for the manager thread to observe idleness,
        # SIGTERM the claude subprocess, and exit its main loop.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not handle.thread.is_alive():
                break
            time.sleep(0.1)

        thread_alive = handle.thread.is_alive()
        handle.stop()

        assert not thread_alive, "Manager thread should exit after ephemeral reap fires"
