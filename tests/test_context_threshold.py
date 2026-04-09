"""Tests for context threshold notification (Feature 5).

Verifies that _check_context_threshold fires a synthetic FIFO message
when context usage crosses the threshold, respects the sentinel file,
and never crashes on missing/empty logs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_worker.manager import (
    CONTEXT_WAKEUP_THRESHOLD_PCT,
    _check_context_threshold,
    _detect_context_window_size,
)
from tests.conftest import make_system_init


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cw_usage(total: int):
    """Return a mock ContextWindowUsage with the given total."""
    return SimpleNamespace(
        total=total,
        input_tokens=total,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=0,
        source_uuid="",
        source_message_id="",
        source_line=0,
    )


def _write_log(log_path: Path, entries: list[dict]) -> None:
    with open(log_path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# _detect_context_window_size (manager-local copy)
# ---------------------------------------------------------------------------


class TestDetectContextWindowSize:
    def test_1m_model(self, tmp_path: Path):
        log = tmp_path / "log"
        _write_log(log, [make_system_init("u1", "sess")])
        # Default fixture model is "claude-opus-4-6" (no [1m])
        assert _detect_context_window_size(log) == 200_000

    def test_1m_model_suffix(self, tmp_path: Path):
        log = tmp_path / "log"
        init = make_system_init("u1", "sess")
        init["model"] = "claude-opus-4-6[1m]"
        _write_log(log, [init])
        assert _detect_context_window_size(log) == 1_000_000

    def test_missing_log(self, tmp_path: Path):
        log = tmp_path / "nonexistent"
        assert _detect_context_window_size(log) == 1_000_000

    def test_empty_log(self, tmp_path: Path):
        log = tmp_path / "log"
        log.write_text("")
        assert _detect_context_window_size(log) == 1_000_000


# ---------------------------------------------------------------------------
# _check_context_threshold
# ---------------------------------------------------------------------------


class TestCheckContextThreshold:
    """Test the one-shot context threshold notification."""

    def _setup(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """Create runtime dir with log and FIFO, return (log, runtime, fifo)."""
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        log_path = runtime / "log"
        in_fifo = runtime / "in"
        os.mkfifo(in_fifo)
        # Write a minimal log with a system/init so _detect_context_window_size works
        init = make_system_init("u1", "sess")
        init["model"] = "claude-opus-4-6[1m]"
        _write_log(log_path, [init])
        return log_path, runtime, in_fifo

    def test_below_threshold_no_write(self, tmp_path: Path):
        """When usage is below 80%, no sentinel and no FIFO write."""
        log_path, runtime, in_fifo = self._setup(tmp_path)
        sentinel = runtime / "wakeup-context-sent"

        # 50% of 1M = 500k tokens
        mock_usage = _make_cw_usage(500_000)
        with patch(
            "claude_logs.compute_context_window_usage",
            return_value=mock_usage,
        ):
            _check_context_threshold(log_path, runtime, in_fifo)

        assert not sentinel.exists()

    def test_above_threshold_fires(self, tmp_path: Path):
        """When usage >= 80%, writes to FIFO and creates sentinel."""
        log_path, runtime, in_fifo = self._setup(tmp_path)
        sentinel = runtime / "wakeup-context-sent"

        # 85% of 1M = 850k tokens
        mock_usage = _make_cw_usage(850_000)

        # Open the FIFO read end so the write doesn't block/fail
        rd_fd = os.open(str(in_fifo), os.O_RDONLY | os.O_NONBLOCK)
        try:
            with patch(
                "claude_logs.compute_context_window_usage",
                return_value=mock_usage,
            ):
                _check_context_threshold(log_path, runtime, in_fifo)

            assert sentinel.exists()

            # Read what was written to the FIFO
            data = os.read(rd_fd, 65536)
            msg = json.loads(data.decode())
            assert msg["type"] == "user"
            assert "[system:context-threshold]" in msg["message"]["content"]
            assert "85%" in msg["message"]["content"]
        finally:
            os.close(rd_fd)

    def test_already_fired_no_second_write(self, tmp_path: Path):
        """When sentinel exists, skip even if threshold is crossed."""
        log_path, runtime, in_fifo = self._setup(tmp_path)
        sentinel = runtime / "wakeup-context-sent"
        sentinel.write_text("")

        mock_usage = _make_cw_usage(900_000)

        # The FIFO read end is NOT opened — if a write were attempted
        # with O_NONBLOCK it would raise. The function should bail before
        # reaching the write because the sentinel already exists.
        with patch(
            "claude_logs.compute_context_window_usage",
            return_value=mock_usage,
        ):
            _check_context_threshold(log_path, runtime, in_fifo)

        # No crash, sentinel still exists
        assert sentinel.exists()

    def test_missing_log_no_crash(self, tmp_path: Path):
        """Missing log file → no crash, no sentinel."""
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        log_path = runtime / "log"  # does not exist
        in_fifo = runtime / "in"
        os.mkfifo(in_fifo)

        _check_context_threshold(log_path, runtime, in_fifo)

        assert not (runtime / "wakeup-context-sent").exists()

    def test_empty_log_no_crash(self, tmp_path: Path):
        """Empty log file → no crash, no sentinel."""
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        log_path = runtime / "log"
        log_path.write_text("")
        in_fifo = runtime / "in"
        os.mkfifo(in_fifo)

        with patch(
            "claude_logs.compute_context_window_usage",
            return_value=None,
        ):
            _check_context_threshold(log_path, runtime, in_fifo)

        assert not (runtime / "wakeup-context-sent").exists()

    def test_compute_raises_no_crash(self, tmp_path: Path):
        """If compute_context_window_usage raises, no crash."""
        log_path, runtime, in_fifo = self._setup(tmp_path)

        with patch(
            "claude_logs.compute_context_window_usage",
            side_effect=OSError("disk on fire"),
        ):
            _check_context_threshold(log_path, runtime, in_fifo)

        assert not (runtime / "wakeup-context-sent").exists()
