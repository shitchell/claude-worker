"""Tests for identity-specific hooks loading and merging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_worker.cli import _load_identity_hooks, _merge_hooks


class TestLoadIdentityHooks:
    """_load_identity_hooks must load hooks.json from identity directory."""

    def test_missing_file_returns_empty(self):
        result = _load_identity_hooks("nonexistent-identity-xyz")
        assert result == {}

    def test_loads_valid_hooks(self, tmp_path: Path, monkeypatch):
        hooks_dir = tmp_path / ".cwork" / "identities" / "researcher" / "hooks"
        hooks_dir.mkdir(parents=True)
        hooks_data = {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo hello"}]}]
        }
        (hooks_dir / "hooks.json").write_text(json.dumps(hooks_data))

        monkeypatch.setattr("claude_worker.cli.Path.home", lambda: tmp_path)
        result = _load_identity_hooks("researcher")
        assert "SessionStart" in result
        assert len(result["SessionStart"]) == 1

    def test_malformed_json_returns_empty(self, tmp_path: Path, monkeypatch):
        hooks_dir = tmp_path / ".cwork" / "identities" / "broken" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text("{not valid json")

        monkeypatch.setattr("claude_worker.cli.Path.home", lambda: tmp_path)
        result = _load_identity_hooks("broken")
        assert result == {}


class TestMergeHooks:
    """_merge_hooks must append extra entries to base without overwriting."""

    def test_empty_extra_returns_base(self):
        base = {"PreToolUse": [{"hooks": []}]}
        result = _merge_hooks(base, {})
        assert result == base

    def test_new_hook_type_added(self):
        base = {"PreToolUse": [{"hooks": []}]}
        extra = {"SessionStart": [{"hooks": [{"type": "command", "command": "echo"}]}]}
        result = _merge_hooks(base, extra)
        assert "PreToolUse" in result
        assert "SessionStart" in result

    def test_same_hook_type_appended(self):
        base = {"PreToolUse": [{"hooks": [{"type": "command", "command": "base"}]}]}
        extra = {"PreToolUse": [{"hooks": [{"type": "command", "command": "extra"}]}]}
        result = _merge_hooks(base, extra)
        assert len(result["PreToolUse"]) == 2

    def test_base_not_mutated(self):
        base = {"PreToolUse": [{"hooks": []}]}
        extra = {"PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}]}
        _merge_hooks(base, extra)
        assert len(base["PreToolUse"]) == 1  # original unchanged
