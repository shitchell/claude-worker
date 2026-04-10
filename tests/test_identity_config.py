"""Tests for _load_identity_config (per-identity config.yaml)."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_worker.cli import _load_identity_config


class TestLoadIdentityConfig:
    """_load_identity_config must load YAML config with fallback to {}."""

    def test_missing_file_returns_empty(self):
        """No config file → empty dict."""
        result = _load_identity_config("nonexistent-identity-xyz")
        assert result == {}

    def test_loads_claude_args(self, tmp_path: Path, monkeypatch):
        """claude_args key is returned as a list."""
        identity_dir = tmp_path / ".cwork" / "identities" / "researcher"
        identity_dir.mkdir(parents=True)
        (identity_dir / "config.yaml").write_text(
            "claude_args:\n  - --model\n  - haiku\n"
        )
        monkeypatch.setattr("claude_worker.cli.Path.home", lambda: tmp_path)
        config = _load_identity_config("researcher")
        assert config["claude_args"] == ["--model", "haiku"]

    def test_loads_env_vars(self, tmp_path: Path, monkeypatch):
        """env key is returned as a dict."""
        identity_dir = tmp_path / ".cwork" / "identities" / "builder"
        identity_dir.mkdir(parents=True)
        (identity_dir / "config.yaml").write_text(
            "env:\n  PROJECT_TYPE: backend\n  LOG_LEVEL: debug\n"
        )
        monkeypatch.setattr("claude_worker.cli.Path.home", lambda: tmp_path)
        config = _load_identity_config("builder")
        assert config["env"] == {"PROJECT_TYPE": "backend", "LOG_LEVEL": "debug"}

    def test_malformed_yaml_returns_empty(self, tmp_path: Path, monkeypatch):
        """Bad YAML → empty dict, no crash."""
        identity_dir = tmp_path / ".cwork" / "identities" / "broken"
        identity_dir.mkdir(parents=True)
        (identity_dir / "config.yaml").write_text("{{not valid yaml")
        monkeypatch.setattr("claude_worker.cli.Path.home", lambda: tmp_path)
        config = _load_identity_config("broken")
        assert config == {}
