"""Tests for compaction resilience: context warnings + identity re-injection."""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_worker.context_threshold import THRESHOLDS, main as threshold_main


def _make_hook_payload(transcript_path: str) -> dict:
    return {
        "session_id": "test",
        "transcript_path": transcript_path,
        "cwd": "/tmp",
        "permission_mode": "bypassPermissions",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }


def _make_cw_usage(total: int):
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


def _run_threshold(payload, sentinel_dir, total_tokens):
    """Run the context threshold hook with mocked usage."""
    stdin_backup = sys.stdin
    stdout_backup = sys.stdout
    argv_backup = sys.argv
    try:
        sys.stdin = StringIO(json.dumps(payload))
        captured = StringIO()
        sys.stdout = captured
        sys.argv = ["context_threshold", "--sentinel-dir", str(sentinel_dir)]
        mock_usage = _make_cw_usage(total_tokens)
        with patch(
            "claude_logs.compute_context_window_usage",
            return_value=mock_usage,
        ):
            try:
                threshold_main()
            except SystemExit:
                pass
        return captured.getvalue()
    finally:
        sys.stdin = stdin_backup
        sys.stdout = stdout_backup
        sys.argv = argv_backup


class TestContextWarningThresholds:
    """Context threshold hook must fire at 50%, 65%, and 80%."""

    def test_50_percent_warning(self, tmp_path: Path):
        log = tmp_path / "log"
        init = {"type": "system", "subtype": "init", "model": "claude-opus-4-6[1m]"}
        log.write_text(json.dumps(init) + "\n")
        sentinel = tmp_path / "sentinel"
        sentinel.mkdir()

        output = _run_threshold(_make_hook_payload(str(log)), sentinel, 500_000)
        assert "[system:context-warning]" in output
        assert "50%" in output
        assert (sentinel / "context-warning-50").exists()

    def test_65_percent_warning(self, tmp_path: Path):
        log = tmp_path / "log"
        init = {"type": "system", "subtype": "init", "model": "claude-opus-4-6[1m]"}
        log.write_text(json.dumps(init) + "\n")
        sentinel = tmp_path / "sentinel"
        sentinel.mkdir()
        # Already fired 50%
        (sentinel / "context-warning-50").write_text("1")

        output = _run_threshold(_make_hook_payload(str(log)), sentinel, 650_000)
        assert "65%" in output
        assert (sentinel / "context-warning-65").exists()

    def test_80_percent_threshold(self, tmp_path: Path):
        log = tmp_path / "log"
        init = {"type": "system", "subtype": "init", "model": "claude-opus-4-6[1m]"}
        log.write_text(json.dumps(init) + "\n")
        sentinel = tmp_path / "sentinel"
        sentinel.mkdir()
        (sentinel / "context-warning-50").write_text("1")
        (sentinel / "context-warning-65").write_text("1")

        output = _run_threshold(_make_hook_payload(str(log)), sentinel, 800_000)
        assert "[system:context-threshold]" in output
        assert "80%" in output

    def test_below_50_no_warning(self, tmp_path: Path):
        log = tmp_path / "log"
        init = {"type": "system", "subtype": "init", "model": "claude-opus-4-6[1m]"}
        log.write_text(json.dumps(init) + "\n")
        sentinel = tmp_path / "sentinel"
        sentinel.mkdir()

        output = _run_threshold(_make_hook_payload(str(log)), sentinel, 400_000)
        assert output == ""


class TestIdentityReinjector:
    """identity_reinjector must echo identity on compact/clear."""

    def test_compact_includes_identity(self, tmp_path: Path, monkeypatch):
        from claude_worker.identity_reinjector import main as reinjector_main

        identity_dir = tmp_path / ".cwork" / "identities" / "pm"
        identity_dir.mkdir(parents=True)
        (identity_dir / "identity.md").write_text("# PM Identity\nYou are a PM.")

        monkeypatch.setattr(
            "claude_worker.identity_reinjector.Path.home", lambda: tmp_path
        )

        payload = {"matcher_value": "compact"}
        stdin_backup = sys.stdin
        stdout_backup = sys.stdout
        argv_backup = sys.argv
        try:
            sys.stdin = StringIO(json.dumps(payload))
            captured = StringIO()
            sys.stdout = captured
            sys.argv = ["reinjector", "--identity", "pm", "--cwd", str(tmp_path)]
            try:
                reinjector_main()
            except SystemExit:
                pass
            output = captured.getvalue()
        finally:
            sys.stdin = stdin_backup
            sys.stdout = stdout_backup
            sys.argv = argv_backup

        assert "IDENTITY GUIDANCE" in output
        assert "You are a PM" in output

    def test_startup_no_identity_text(self, tmp_path: Path, monkeypatch):
        from claude_worker.identity_reinjector import main as reinjector_main

        monkeypatch.setattr(
            "claude_worker.identity_reinjector.Path.home", lambda: tmp_path
        )

        payload = {"matcher_value": "startup"}
        stdin_backup = sys.stdin
        stdout_backup = sys.stdout
        argv_backup = sys.argv
        try:
            sys.stdin = StringIO(json.dumps(payload))
            captured = StringIO()
            sys.stdout = captured
            sys.argv = ["reinjector", "--identity", "pm", "--cwd", str(tmp_path)]
            try:
                reinjector_main()
            except SystemExit:
                pass
            output = captured.getvalue()
        finally:
            sys.stdin = stdin_backup
            sys.stdout = stdout_backup
            sys.argv = argv_backup

        assert "IDENTITY GUIDANCE" not in output
        assert "[system:identity-context]" in output
