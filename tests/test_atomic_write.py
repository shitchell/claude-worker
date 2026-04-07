"""Tests for atomic file writes (Imp-8) and its use in install-hook.

The install-hook subcommand previously called settings_path.write_text()
directly — a non-atomic overwrite. If the write was interrupted (disk full,
signal, power loss), the user's ~/.claude/settings.json would be
truncated/corrupted with no rollback. That file is sacred: corrupting it
breaks all claude invocations.

Fix: _atomic_write_text(path, content) writes to a sibling .tmp file and
uses os.replace() for an atomic rename. Reviewed-in Recommendation 2 also
covers .sessions.json and missing-tags.json — folded in opportunistically.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


class TestAtomicWriteText:
    """Basic contract for the helper."""

    def test_writes_content_to_path(self, tmp_path):
        from claude_worker.cli import _atomic_write_text

        target = tmp_path / "target.json"
        _atomic_write_text(target, '{"a": 1}\n')
        assert target.read_text() == '{"a": 1}\n'

    def test_replaces_existing_file(self, tmp_path):
        from claude_worker.cli import _atomic_write_text

        target = tmp_path / "target.json"
        target.write_text("old content\n")
        _atomic_write_text(target, "new content\n")
        assert target.read_text() == "new content\n"

    def test_no_tmp_file_left_behind_after_success(self, tmp_path):
        from claude_worker.cli import _atomic_write_text

        target = tmp_path / "target.json"
        _atomic_write_text(target, "data\n")
        # No .tmp sibling should remain
        assert not (tmp_path / "target.json.tmp").exists()

    def test_crash_mid_write_leaves_original_intact(self, tmp_path, monkeypatch):
        """If the write crashes between tmp-write and os.replace, the
        original file must be untouched."""
        from claude_worker import cli as cw_cli

        target = tmp_path / "target.json"
        target.write_text("original untouched\n")

        def crash_replace(src, dst):
            raise OSError("simulated disk-full or power loss")

        monkeypatch.setattr(os, "replace", crash_replace)

        with pytest.raises(OSError, match="simulated"):
            cw_cli._atomic_write_text(target, "would-be-corruption\n")

        # Original must still be readable and unchanged
        assert target.read_text() == "original untouched\n"


class TestInstallHookUsesAtomicWrite:
    """install-hook must route its settings.json write through the atomic
    helper. A crash mid-install must not corrupt the user's settings."""

    def test_install_hook_crash_preserves_settings(self, tmp_path, monkeypatch):
        """Simulate a crash during the atomic rename step and verify
        the existing settings.json is untouched."""
        import argparse
        from claude_worker import cli as cw_cli

        # Stage an existing user settings.json at a test path
        fake_home_claude = tmp_path / ".claude"
        fake_home_claude.mkdir()
        settings_path = fake_home_claude / "settings.json"
        original_content = (
            '{\n  "permissions": {"deny": ["LSP"]},\n'
            '  "alwaysThinkingEnabled": true\n}\n'
        )
        settings_path.write_text(original_content)

        # Also stage a hooks dir for the script install
        hooks_dir = fake_home_claude / "hooks"
        hooks_dir.mkdir()

        # Monkey-patch the target path constants
        monkeypatch.setattr(cw_cli, "USER_SETTINGS_PATH", settings_path)
        monkeypatch.setattr(
            cw_cli,
            "HOOK_SCRIPT_INSTALL_PATH",
            hooks_dir / "session-uuid-env-injection.sh",
        )

        # Inject a crash into os.replace so the rename step fails
        def crash_replace(src, dst):
            raise OSError("simulated disk-full at rename time")

        monkeypatch.setattr(os, "replace", crash_replace)

        args = argparse.Namespace(project=False, user=True, yes=True, force=False)
        with pytest.raises(OSError, match="simulated"):
            cw_cli.cmd_install_hook(args)

        # The original settings file must still be intact
        assert settings_path.read_text() == original_content
