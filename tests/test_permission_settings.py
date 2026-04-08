"""Tests for the per-worker settings.json generation that wires the
PreToolUse permission-grant hook into claude.

The generation itself is a pure-data operation: given a runtime dir
and a python executable path, return a settings dict with the hook
command pointing at the worker's grants file. The manager invokes
this before spawning claude and appends ``--settings <runtime>/settings.json``
to the claude command line.

These tests verify the helper's output shape and the --no-permission-hook
opt-out path.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


class TestBuildPermissionSettings:
    """Pure-data test of the settings-dict builder."""

    def test_contains_pretooluse_hook_for_edit_write_multiedit(self):
        from claude_worker import cli as cw_cli

        grants_path = Path("/tmp/claude-workers/1000/foo/grants.jsonl")
        settings = cw_cli._build_permission_hook_settings(
            grants_path=grants_path, python_executable="/usr/bin/python3"
        )
        pre = settings["hooks"]["PreToolUse"]
        assert isinstance(pre, list) and len(pre) >= 1
        matcher = pre[0]["matcher"]
        # Must match Edit, Write, and MultiEdit — the three tools gated
        # by the sensitive-file rule.
        assert "Edit" in matcher
        assert "Write" in matcher
        assert "MultiEdit" in matcher

    def test_command_references_grants_file_and_python(self):
        from claude_worker import cli as cw_cli

        grants_path = Path("/tmp/claude-workers/1000/foo/grants.jsonl")
        settings = cw_cli._build_permission_hook_settings(
            grants_path=grants_path, python_executable="/usr/bin/python3.11"
        )
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "/usr/bin/python3.11" in cmd
        assert "claude_worker.permission_grant" in cmd
        assert str(grants_path) in cmd


class TestMaybeWritePermissionSettings:
    """cmd_start calls _maybe_write_permission_settings before forking;
    the helper is tested directly here (instead of driving the full
    fork + manager spawn) so the test stays hermetic and fast. Coverage
    for the actual cmd_start wiring lives in the integration smoke test
    with real claude."""

    def _patched_base(self, tmp_path, monkeypatch) -> Path:
        from claude_worker import cli as cw_cli
        from claude_worker import manager as cw_manager

        base = tmp_path / "workers"
        base.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base)
        monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base)
        return base

    def test_enabled_writes_settings_file(self, tmp_path, monkeypatch):
        """Enabled path (the default) writes a settings.json whose
        PreToolUse hook command references the runtime's grants file."""
        from claude_worker import cli as cw_cli
        from claude_worker import manager as cw_manager

        self._patched_base(tmp_path, monkeypatch)
        name = "cw-test-settings-on"
        runtime = cw_manager.create_runtime_dir(name)

        result = cw_cli._maybe_write_permission_settings(name=name, enabled=True)
        assert result is not None
        assert result == runtime / "settings.json"
        assert (runtime / "settings.json").exists()

        data = json.loads((runtime / "settings.json").read_text())
        pre = data["hooks"]["PreToolUse"]
        assert pre, "PreToolUse hook entry missing"
        cmd = pre[0]["hooks"][0]["command"]
        # Command must reference this worker's grants file, not a
        # different worker's.
        assert str(runtime / "grants.jsonl") in cmd
        assert "claude_worker.permission_grant" in cmd

    def test_disabled_returns_none_and_writes_nothing(self, tmp_path, monkeypatch):
        """With --no-permission-hook, the helper must NOT create the
        file. This is the kill switch required for tests and for users
        who want old behavior."""
        from claude_worker import cli as cw_cli
        from claude_worker import manager as cw_manager

        self._patched_base(tmp_path, monkeypatch)
        name = "cw-test-noperm"
        runtime = cw_manager.create_runtime_dir(name)

        result = cw_cli._maybe_write_permission_settings(name=name, enabled=False)
        assert result is None
        assert not (runtime / "settings.json").exists()

    def test_settings_file_uses_current_python(self, tmp_path, monkeypatch):
        """sys.executable capture — the hook command must invoke the
        same Python the parent claude-worker was started under, so the
        hook child resolves the matching ``claude_worker`` install
        (editable vs installed, venv vs system). This guards against
        the 'wrong claude_worker module got imported' class of bug we
        had to work around earlier when setting the worktree up."""
        import sys as real_sys
        from claude_worker import cli as cw_cli
        from claude_worker import manager as cw_manager

        self._patched_base(tmp_path, monkeypatch)
        name = "cw-test-syspython"
        runtime = cw_manager.create_runtime_dir(name)

        cw_cli._maybe_write_permission_settings(name=name, enabled=True)
        data = json.loads((runtime / "settings.json").read_text())
        cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert real_sys.executable in cmd
