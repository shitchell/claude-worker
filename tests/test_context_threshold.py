"""Tests for context threshold notification (Feature 5).

Verifies the context_threshold Stop hook module: _detect_context_window_size,
sentinel one-shot behavior, and threshold detection via the main() entry point.
"""

from __future__ import annotations

import json
import os
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_worker.context_threshold import (
    CONTEXT_WAKEUP_THRESHOLD_PCT,
    _detect_context_window_size,
    main,
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


def _make_hook_payload(transcript_path: str) -> dict:
    """Build a minimal Stop hook JSON payload."""
    return {
        "session_id": "test-session",
        "transcript_path": transcript_path,
        "cwd": "/tmp",
        "permission_mode": "bypassPermissions",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }


def _run_main(payload: dict, sentinel_dir: str) -> str:
    """Run main() with the given payload on stdin, return stdout."""
    stdin_backup = sys.stdin
    stdout_backup = sys.stdout
    argv_backup = sys.argv
    try:
        sys.stdin = StringIO(json.dumps(payload))
        captured = StringIO()
        sys.stdout = captured
        sys.argv = ["context_threshold", "--sentinel-dir", sentinel_dir]
        try:
            main()
        except SystemExit:
            pass
        return captured.getvalue()
    finally:
        sys.stdin = stdin_backup
        sys.stdout = stdout_backup
        sys.argv = argv_backup


# ---------------------------------------------------------------------------
# _detect_context_window_size
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
# main() — Stop hook entry point
# ---------------------------------------------------------------------------


class TestContextThresholdHook:
    """Test the Stop hook via main()."""

    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a log file with system/init, return (log_path, sentinel_dir)."""
        log_path = tmp_path / "log"
        sentinel_dir = tmp_path / "sentinel"
        sentinel_dir.mkdir()
        init = make_system_init("u1", "sess")
        init["model"] = "claude-opus-4-6[1m]"
        _write_log(log_path, [init])
        return log_path, sentinel_dir

    def test_below_threshold_no_output(self, tmp_path: Path):
        """When usage is below 80%, no output and no sentinel."""
        log_path, sentinel_dir = self._setup(tmp_path)
        payload = _make_hook_payload(str(log_path))

        mock_usage = _make_cw_usage(500_000)
        with patch(
            "claude_logs.compute_context_window_usage",
            return_value=mock_usage,
        ):
            output = _run_main(payload, str(sentinel_dir))

        assert output == ""
        assert not (sentinel_dir / "wakeup-context-sent").exists()

    def test_above_threshold_prints_warning(self, tmp_path: Path):
        """When usage >= 80%, prints warning and creates sentinel."""
        log_path, sentinel_dir = self._setup(tmp_path)
        payload = _make_hook_payload(str(log_path))

        mock_usage = _make_cw_usage(850_000)
        with patch(
            "claude_logs.compute_context_window_usage",
            return_value=mock_usage,
        ):
            output = _run_main(payload, str(sentinel_dir))

        assert "[system:context-threshold]" in output
        assert "85%" in output
        assert (sentinel_dir / "wakeup-context-sent").exists()

    def test_sentinel_prevents_second_fire(self, tmp_path: Path):
        """When sentinel exists, skip even if threshold is crossed."""
        log_path, sentinel_dir = self._setup(tmp_path)
        (sentinel_dir / "wakeup-context-sent").write_text("already fired")
        payload = _make_hook_payload(str(log_path))

        mock_usage = _make_cw_usage(900_000)
        with patch(
            "claude_logs.compute_context_window_usage",
            return_value=mock_usage,
        ):
            output = _run_main(payload, str(sentinel_dir))

        assert output == ""

    def test_stop_hook_active_bails(self, tmp_path: Path):
        """When stop_hook_active is true, exit immediately."""
        log_path, sentinel_dir = self._setup(tmp_path)
        payload = _make_hook_payload(str(log_path))
        payload["stop_hook_active"] = True

        output = _run_main(payload, str(sentinel_dir))
        assert output == ""

    def test_missing_transcript_no_crash(self, tmp_path: Path):
        """Missing transcript_path → no output, no crash."""
        sentinel_dir = tmp_path / "sentinel"
        sentinel_dir.mkdir()
        payload = _make_hook_payload("/nonexistent/path")

        output = _run_main(payload, str(sentinel_dir))
        assert output == ""

    def test_compute_raises_no_crash(self, tmp_path: Path):
        """If compute_context_window_usage raises, no crash."""
        log_path, sentinel_dir = self._setup(tmp_path)
        payload = _make_hook_payload(str(log_path))

        with patch(
            "claude_logs.compute_context_window_usage",
            side_effect=OSError("disk on fire"),
        ):
            output = _run_main(payload, str(sentinel_dir))

        assert output == ""
        assert not (sentinel_dir / "wakeup-context-sent").exists()
