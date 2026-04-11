"""Tests for wrap-up file injection at the 80% context threshold."""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_worker.context_threshold import (
    _load_wrapup_file,
    main,
)


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


def _run_main(payload: dict, sentinel_dir: str, extra_args: list[str] | None = None) -> str:
    """Run main() with the given payload on stdin, return stdout."""
    stdin_backup = sys.stdin
    stdout_backup = sys.stdout
    argv_backup = sys.argv
    try:
        sys.stdin = StringIO(json.dumps(payload))
        captured = StringIO()
        sys.stdout = captured
        argv = ["context_threshold", "--sentinel-dir", sentinel_dir]
        if extra_args:
            argv.extend(extra_args)
        sys.argv = argv
        try:
            main()
        except SystemExit:
            pass
        return captured.getvalue()
    finally:
        sys.stdin = stdin_backup
        sys.stdout = stdout_backup
        sys.argv = argv_backup


def _setup_log(tmp_path: Path) -> tuple[Path, Path]:
    """Create a 1M-context log + sentinel dir, return (log_path, sentinel_dir)."""
    log_path = tmp_path / "log"
    sentinel_dir = tmp_path / "sentinel"
    sentinel_dir.mkdir()
    init = {
        "type": "system",
        "subtype": "init",
        "model": "claude-opus-4-6[1m]",
        "session_id": "sess",
        "uuid": "u1",
        "cwd": "/tmp",
        "tools": [],
        "mcp_servers": [],
    }
    log_path.write_text(json.dumps(init) + "\n")
    return log_path, sentinel_dir


# ---------------------------------------------------------------------------
# _load_wrapup_file
# ---------------------------------------------------------------------------


class TestLoadWrapupFile:
    def test_load_wrapup_bundled_pm(self):
        result = _load_wrapup_file("pm")
        assert result is not None
        assert "handoff" in result.lower()

    def test_load_wrapup_bundled_tl(self):
        result = _load_wrapup_file("technical-lead")
        assert result is not None
        assert "handoff" in result.lower()

    def test_load_wrapup_unknown_identity(self):
        result = _load_wrapup_file("unknown-identity")
        assert result is None

    def test_load_wrapup_empty_identity(self):
        result = _load_wrapup_file("")
        assert result is None

    def test_load_wrapup_user_installed_overrides(self, tmp_path: Path, monkeypatch):
        user_wrapup = tmp_path / ".cwork" / "identities" / "pm" / "wrap-up.md"
        user_wrapup.parent.mkdir(parents=True)
        user_wrapup.write_text("Custom user wrap-up for PM")

        monkeypatch.setattr("claude_worker.context_threshold.Path.home", lambda: tmp_path)

        result = _load_wrapup_file("pm")
        assert result == "Custom user wrap-up for PM"


# ---------------------------------------------------------------------------
# main() integration — wrap-up injection
# ---------------------------------------------------------------------------


class TestWrapupInjection:
    def test_80_threshold_injects_wrapup(self, tmp_path: Path):
        """At 80%+ with --identity pm, output includes wrap-up procedure."""
        log_path, sentinel_dir = _setup_log(tmp_path)
        # Pre-fire lower thresholds so only 80% fires
        (sentinel_dir / "context-warning-50").write_text("1")
        (sentinel_dir / "context-warning-65").write_text("1")
        (sentinel_dir / "context-warning-70").write_text("1")
        payload = _make_hook_payload(str(log_path))

        mock_usage = _make_cw_usage(850_000)
        with patch(
            "claude_logs.compute_context_window_usage",
            return_value=mock_usage,
        ):
            output = _run_main(
                payload, str(sentinel_dir), ["--identity", "pm"]
            )

        assert "[system:context-threshold]" in output
        assert "WRAP-UP PROCEDURE" in output
        assert "handoff" in output.lower()

    def test_50_threshold_no_wrapup_injection(self, tmp_path: Path):
        """At 50% with --identity pm, no wrap-up injection."""
        log_path, sentinel_dir = _setup_log(tmp_path)
        payload = _make_hook_payload(str(log_path))

        mock_usage = _make_cw_usage(500_000)
        with patch(
            "claude_logs.compute_context_window_usage",
            return_value=mock_usage,
        ):
            output = _run_main(
                payload, str(sentinel_dir), ["--identity", "pm"]
            )

        assert "[system:context-warning]" in output
        assert "WRAP-UP PROCEDURE" not in output

    def test_80_threshold_no_identity_no_wrapup(self, tmp_path: Path):
        """At 80%+ without --identity, threshold fires but no wrap-up injected."""
        log_path, sentinel_dir = _setup_log(tmp_path)
        (sentinel_dir / "context-warning-50").write_text("1")
        (sentinel_dir / "context-warning-65").write_text("1")
        (sentinel_dir / "context-warning-70").write_text("1")
        payload = _make_hook_payload(str(log_path))

        mock_usage = _make_cw_usage(850_000)
        with patch(
            "claude_logs.compute_context_window_usage",
            return_value=mock_usage,
        ):
            output = _run_main(payload, str(sentinel_dir))

        assert "[system:context-threshold]" in output
        assert "WRAP-UP PROCEDURE" not in output
