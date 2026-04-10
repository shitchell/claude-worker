"""Tests for _get_internalize_message (identity-specific first-turn messages)."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_worker.cli import (
    PM_INTERNALIZE_MESSAGE,
    TL_INTERNALIZE_MESSAGE,
    _get_internalize_message,
)


class TestGetInternalizeMessage:
    """_get_internalize_message must prefer user file over hardcoded constant."""

    def test_pm_falls_back_to_constant(self):
        """Without a user file, PM returns the hardcoded constant."""
        msg = _get_internalize_message("pm")
        assert msg == PM_INTERNALIZE_MESSAGE

    def test_tl_falls_back_to_constant(self):
        """Without a user file, TL returns the hardcoded constant."""
        msg = _get_internalize_message("technical-lead")
        assert msg == TL_INTERNALIZE_MESSAGE

    def test_unknown_identity_returns_none(self):
        """Unknown identity with no user file returns None."""
        msg = _get_internalize_message("nonexistent-identity")
        assert msg is None

    def test_user_file_overrides_constant(self, tmp_path: Path, monkeypatch):
        """User internalize.md takes priority over hardcoded constant."""
        identity_dir = tmp_path / ".cwork" / "identities" / "pm"
        identity_dir.mkdir(parents=True)
        (identity_dir / "internalize.md").write_text("Custom PM startup message")

        monkeypatch.setattr("claude_worker.cli.Path.home", lambda: tmp_path)
        msg = _get_internalize_message("pm")
        assert msg == "Custom PM startup message"

    def test_custom_identity_with_file(self, tmp_path: Path, monkeypatch):
        """Custom identity with an internalize.md file works."""
        identity_dir = tmp_path / ".cwork" / "identities" / "researcher"
        identity_dir.mkdir(parents=True)
        (identity_dir / "internalize.md").write_text("Research mode activated")

        monkeypatch.setattr("claude_worker.cli.Path.home", lambda: tmp_path)
        msg = _get_internalize_message("researcher")
        assert msg == "Research mode activated"
