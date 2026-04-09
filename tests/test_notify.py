"""Tests for cmd_notify — human escalation notification channel.

Covers config handling, message substitution, rate limiting, and
subprocess failure. Tests must fail if cmd_notify is removed (G3).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_worker.cli import (
    NOTIFY_COOLDOWN_SECONDS,
    cmd_notify,
)


def _build_notify_args(
    message: list[str] | None = None,
    worker: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        message=message or [],
        worker=worker,
    )


class TestNotifyConfigHandling:
    """cmd_notify must handle missing/disabled/incomplete config gracefully."""

    def test_missing_config_exits_silently(self, capsys):
        """No config file → no crash, no output."""
        with patch("claude_worker.cli._get_cwork_config", return_value={}):
            cmd_notify(_build_notify_args(["hello"]))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_disabled_config_exits_silently(self, capsys):
        """notifications.enabled=false → no crash, no output."""
        config = {"notifications": {"enabled": False, "command": "echo ${MESSAGE}"}}
        with patch("claude_worker.cli._get_cwork_config", return_value=config):
            cmd_notify(_build_notify_args(["hello"]))
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_no_command_warns(self, capsys):
        """notifications.enabled=true but no command → stderr warning."""
        config = {"notifications": {"enabled": True}}
        with patch("claude_worker.cli._get_cwork_config", return_value=config):
            cmd_notify(_build_notify_args(["hello"]))
        captured = capsys.readouterr()
        assert "command not set" in captured.err


class TestNotifyMessageSubstitution:
    """${MESSAGE} in the command template must be replaced with the message."""

    def test_message_substituted_in_command(self, tmp_path: Path, capsys):
        """The command should receive the substituted message."""
        output_file = tmp_path / "notified.txt"
        config = {
            "notifications": {
                "enabled": True,
                "command": f"echo ${{MESSAGE}} > {output_file}",
            }
        }
        # Disable rate limiting by patching cooldown dir
        cooldown_dir = tmp_path / "cooldowns"
        cooldown_dir.mkdir()
        with (
            patch("claude_worker.cli._get_cwork_config", return_value=config),
            patch("claude_worker.cli.Path.home", return_value=tmp_path),
        ):
            cmd_notify(_build_notify_args(["test", "notification"]))

        assert output_file.exists()
        content = output_file.read_text().strip()
        assert "test notification" in content


class TestNotifyRateLimiting:
    """Second notification within cooldown period must be skipped."""

    def test_rate_limited_within_cooldown(self, tmp_path: Path, capsys):
        """Two sends within cooldown → second is skipped."""
        cooldown_dir = tmp_path / ".cwork" / "notify-cooldowns"
        cooldown_dir.mkdir(parents=True)

        config = {
            "notifications": {
                "enabled": True,
                "command": "true",  # no-op command
            }
        }

        with (
            patch("claude_worker.cli._get_cwork_config", return_value=config),
            patch("claude_worker.cli.Path.home", return_value=tmp_path),
        ):
            # First call succeeds
            cmd_notify(_build_notify_args(["first"], worker="test-worker"))
            out1 = capsys.readouterr()
            assert "Notification sent" in out1.out

            # Second call within cooldown → rate-limited
            cmd_notify(_build_notify_args(["second"], worker="test-worker"))
            out2 = capsys.readouterr()
            assert "rate-limited" in out2.err


class TestNotifySubprocessFailure:
    """Subprocess failures must log to stderr, not crash."""

    def test_command_failure_warns(self, tmp_path: Path, capsys):
        """A failing command should produce a stderr warning, not crash."""
        config = {
            "notifications": {
                "enabled": True,
                "command": "exit 1",
            }
        }
        cooldown_dir = tmp_path / ".cwork" / "notify-cooldowns"
        cooldown_dir.mkdir(parents=True)

        with (
            patch("claude_worker.cli._get_cwork_config", return_value=config),
            patch("claude_worker.cli.Path.home", return_value=tmp_path),
        ):
            cmd_notify(_build_notify_args(["hello"], worker="fail-test"))
        captured = capsys.readouterr()
        assert "exited" in captured.err or "Warning" in captured.err
