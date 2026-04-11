"""Tests for `start --foreground` (ticket #053).

The --foreground flag runs the manager directly in the current process
without forking. This enables systemd Type=simple service management
where the main process must stay alive for the service lifetime.
"""

from __future__ import annotations

import argparse
import os
import sys

import pytest


def _make_start_args(**overrides) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for cmd_start.

    Sets every attribute that cmd_start reads to a safe default,
    then applies overrides.
    """
    defaults = dict(
        name="test-fg",
        cwd=None,
        prompt=None,
        prompt_file=None,
        agent=None,
        resume=False,
        background=False,
        foreground=False,
        show_response=False,
        show_full_response=False,
        pm=False,
        team_lead=False,
        identity=None,
        no_permission_hook=True,
        claude_args=[],
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestForegroundBackgroundMutualExclusion:
    """--foreground and --background cannot be used together."""

    def test_foreground_background_mutually_exclusive(self, fake_worker, capsys):
        from claude_worker.cli import cmd_start

        # Anchor the fake_worker fixture so base dir is patched
        fake_worker([], name="unused-fixture-anchor")

        args = _make_start_args(foreground=True, background=True)
        with pytest.raises(SystemExit) as exc_info:
            cmd_start(args)
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "--foreground" in captured.err
        assert "--background" in captured.err
        assert "mutually exclusive" in captured.err


class TestForegroundRunsManagerDirectly:
    """--foreground calls run_manager in the current process."""

    def test_foreground_runs_manager_directly(self, running_worker):
        """The running_worker fixture already exercises
        _run_manager_forkless (the same path foreground uses).
        Verify the worker comes up: PID file written, session captured.
        """
        handle = running_worker(name="fg-direct")

        # PID file exists (manager wrote it)
        pid_file = handle.runtime_dir / "pid"
        assert pid_file.exists()

        # Session file captured from stub-claude's init message
        assert handle.wait_for_log('"type": "system"', timeout=5.0)


class TestForegroundBanner:
    """--foreground prints a banner to stderr before calling run_manager."""

    def test_foreground_banner_printed(self, fake_worker, monkeypatch, capsys):
        from claude_worker import cli as cw_cli
        from claude_worker.cli import cmd_start

        # Anchor the fake_worker fixture so base dir is patched
        fake_worker([], name="unused-fixture-anchor")

        # Mock run_manager to be a no-op (we only care about the banner)
        called_with: dict = {}

        def mock_run_manager(**kwargs):
            called_with.update(kwargs)

        monkeypatch.setattr(cw_cli, "run_manager", mock_run_manager)

        args = _make_start_args(foreground=True, name="banner-test")
        with pytest.raises(SystemExit) as exc_info:
            cmd_start(args)
        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "banner-test" in captured.err
        assert "(foreground)" in captured.err
        assert "cwd:" in captured.err
        assert f"pid: {os.getpid()}" in captured.err

    def test_foreground_banner_no_identity_line_for_plain_worker(
        self, fake_worker, monkeypatch, capsys
    ):
        """Plain workers (no identity) should NOT print an identity line."""
        from claude_worker import cli as cw_cli
        from claude_worker.cli import cmd_start

        fake_worker([], name="unused-fixture-anchor")

        def mock_run_manager(**kwargs):
            pass

        monkeypatch.setattr(cw_cli, "run_manager", mock_run_manager)

        args = _make_start_args(foreground=True, name="plain-fg")
        with pytest.raises(SystemExit):
            cmd_start(args)

        captured = capsys.readouterr()
        assert "identity:" not in captured.err


class TestForegroundComposesWithIdentity:
    """--foreground + --identity resolves the identity correctly."""

    def test_foreground_composes_with_identity(
        self, fake_worker, monkeypatch, capsys, tmp_path
    ):
        from claude_worker import cli as cw_cli
        from claude_worker.cli import cmd_start

        fake_worker([], name="unused-fixture-anchor")

        # Create a user identity file so cmd_start finds it.
        # cmd_start looks at Path.home() / ".cwork" / "identities" / <name> / "identity.md"
        identity_dir = tmp_path / ".cwork" / "identities" / "custom-id"
        identity_dir.mkdir(parents=True)
        (identity_dir / "identity.md").write_text("# Custom Identity\nTest.")
        monkeypatch.setattr(
            "pathlib.Path.home", lambda: tmp_path
        )

        called_with: dict = {}

        def mock_run_manager(**kwargs):
            called_with.update(kwargs)

        monkeypatch.setattr(cw_cli, "run_manager", mock_run_manager)

        args = _make_start_args(
            foreground=True, name="identity-fg", identity="custom-id"
        )
        with pytest.raises(SystemExit):
            cmd_start(args)

        # Verify identity was resolved and passed to run_manager
        assert called_with.get("identity") == "custom-id"
        # Banner should mention the identity
        captured = capsys.readouterr()
        assert "identity: custom-id" in captured.err


class TestForegroundComposesWithResume:
    """--foreground + --resume builds resume args correctly."""

    def test_foreground_composes_with_resume(self, fake_worker, monkeypatch, capsys):
        from claude_worker import cli as cw_cli
        from claude_worker import manager as cw_manager
        from claude_worker.cli import cmd_start

        fake_worker([], name="unused-fixture-anchor")

        # Save a session so --resume can find it
        base_dir = cw_manager.get_base_dir()
        sessions_path = base_dir / ".sessions.json"
        import json

        sessions_data = {
            "resume-fg": {
                "session_id": "sess-abc-123",
                "cwd": "/tmp/test-cwd",
                "claude_args": ["--model", "opus"],
            }
        }
        sessions_path.write_text(json.dumps(sessions_data))

        called_with: dict = {}

        def mock_run_manager(**kwargs):
            called_with.update(kwargs)

        monkeypatch.setattr(cw_cli, "run_manager", mock_run_manager)

        args = _make_start_args(foreground=True, resume=True, name="resume-fg")
        with pytest.raises(SystemExit):
            cmd_start(args)

        # Verify --resume session ID was injected into claude_args
        assert "--resume" in called_with.get("claude_args", [])
        assert "sess-abc-123" in called_with.get("claude_args", [])
