"""Tests for install-hook happy paths and idempotency (Imp-11 coverage).

The install-hook command has several distinct behaviors beyond the
atomic-write crash handling tested in test_atomic_write.py:
- Creating a settings.json from scratch
- Merging into existing settings.json with other hooks
- Idempotency: second run detects existing entry and skips
- --force: bypass idempotency
- Hook script copied to install path with executable bit
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

import pytest


def _build_install_hook_args(
    *, user: bool = True, project: bool = False, yes: bool = True, force: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(user=user, project=project, yes=yes, force=force)


class TestInstallHookFreshInstall:
    """Installing into a nonexistent settings.json."""

    def test_creates_settings_file_and_hook_script(self, tmp_path, monkeypatch):
        from claude_worker import cli as cw_cli

        settings_path = tmp_path / "settings.json"
        hook_path = tmp_path / "hooks" / "session-uuid-env-injection.sh"

        monkeypatch.setattr(cw_cli, "USER_SETTINGS_PATH", settings_path)
        monkeypatch.setattr(cw_cli, "HOOK_SCRIPT_INSTALL_PATH", hook_path)

        cw_cli.cmd_install_hook(_build_install_hook_args())

        # Both files should now exist
        assert settings_path.exists()
        assert hook_path.exists()

        # Hook script should be executable
        mode = hook_path.stat().st_mode
        assert mode & stat.S_IXUSR

        # Settings should contain a SessionStart entry referencing the hook
        data = json.loads(settings_path.read_text())
        session_start = data["hooks"]["SessionStart"]
        assert len(session_start) == 1
        command = session_start[0]["hooks"][0]["command"]
        assert str(hook_path) in command


class TestInstallHookMergesWithExisting:
    """Installing into a settings.json that already has other hooks."""

    def test_preserves_existing_hooks(self, tmp_path, monkeypatch):
        from claude_worker import cli as cw_cli

        settings_path = tmp_path / "settings.json"
        hook_path = tmp_path / "hooks" / "session-uuid-env-injection.sh"

        # Stage a settings.json with a pre-existing different SessionStart hook
        existing = {
            "permissions": {"deny": ["LSP"]},
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "bash /some/other/hook.sh",
                            }
                        ]
                    }
                ]
            },
        }
        settings_path.write_text(json.dumps(existing, indent=2))

        monkeypatch.setattr(cw_cli, "USER_SETTINGS_PATH", settings_path)
        monkeypatch.setattr(cw_cli, "HOOK_SCRIPT_INSTALL_PATH", hook_path)

        cw_cli.cmd_install_hook(_build_install_hook_args())

        data = json.loads(settings_path.read_text())
        # Original permissions block preserved
        assert data["permissions"] == {"deny": ["LSP"]}
        # Both hooks present
        session_start = data["hooks"]["SessionStart"]
        assert len(session_start) == 2
        all_commands = [h["command"] for entry in session_start for h in entry["hooks"]]
        assert any("/some/other/hook.sh" in c for c in all_commands)
        assert any(str(hook_path) in c for c in all_commands)


class TestInstallHookIdempotency:
    """Second run should detect the existing entry and skip."""

    def test_second_run_does_not_add_duplicate(self, tmp_path, monkeypatch):
        from claude_worker import cli as cw_cli

        settings_path = tmp_path / "settings.json"
        hook_path = tmp_path / "hooks" / "session-uuid-env-injection.sh"

        monkeypatch.setattr(cw_cli, "USER_SETTINGS_PATH", settings_path)
        monkeypatch.setattr(cw_cli, "HOOK_SCRIPT_INSTALL_PATH", hook_path)

        cw_cli.cmd_install_hook(_build_install_hook_args())
        first_content = settings_path.read_text()

        # Second run without --force should be a no-op
        cw_cli.cmd_install_hook(_build_install_hook_args())
        second_content = settings_path.read_text()

        assert first_content == second_content
        data = json.loads(second_content)
        assert len(data["hooks"]["SessionStart"]) == 1  # still just one

    def test_force_adds_duplicate_entry(self, tmp_path, monkeypatch):
        from claude_worker import cli as cw_cli

        settings_path = tmp_path / "settings.json"
        hook_path = tmp_path / "hooks" / "session-uuid-env-injection.sh"

        monkeypatch.setattr(cw_cli, "USER_SETTINGS_PATH", settings_path)
        monkeypatch.setattr(cw_cli, "HOOK_SCRIPT_INSTALL_PATH", hook_path)

        cw_cli.cmd_install_hook(_build_install_hook_args())
        cw_cli.cmd_install_hook(_build_install_hook_args(force=True))

        data = json.loads(settings_path.read_text())
        assert len(data["hooks"]["SessionStart"]) == 2
