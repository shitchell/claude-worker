"""Tests for _running_inside_claudecode helper and consistent CLAUDECODE check.

Imp-13: cmd_read and _env_chat_id used different truthiness checks for
the CLAUDECODE env var. cmd_read used `os.environ.get("CLAUDECODE")`
(truthy for any non-empty value including "0" and "false"), while
_env_chat_id required exact match against "1". Inconsistent and
confusing if users set CLAUDECODE=0.

Fix: extract _running_inside_claudecode() that requires exact "1",
use it in both places.
"""

from __future__ import annotations

import pytest


class TestRunningInsideClaudecode:
    """The helper must return True only for CLAUDECODE=1 exactly."""

    def test_unset_returns_false(self, monkeypatch):
        from claude_worker.cli import _running_inside_claudecode

        monkeypatch.delenv("CLAUDECODE", raising=False)
        assert _running_inside_claudecode() is False

    def test_exactly_one_returns_true(self, monkeypatch):
        from claude_worker.cli import _running_inside_claudecode

        monkeypatch.setenv("CLAUDECODE", "1")
        assert _running_inside_claudecode() is True

    def test_zero_returns_false(self, monkeypatch):
        """CLAUDECODE=0 must NOT count as running inside claude code,
        even though the old os.environ.get() truthiness check was True
        for any non-empty value."""
        from claude_worker.cli import _running_inside_claudecode

        monkeypatch.setenv("CLAUDECODE", "0")
        assert _running_inside_claudecode() is False

    def test_true_string_returns_false(self, monkeypatch):
        from claude_worker.cli import _running_inside_claudecode

        monkeypatch.setenv("CLAUDECODE", "true")
        assert _running_inside_claudecode() is False

    def test_empty_returns_false(self, monkeypatch):
        from claude_worker.cli import _running_inside_claudecode

        monkeypatch.setenv("CLAUDECODE", "")
        assert _running_inside_claudecode() is False


class TestEnvChatIdUsesHelper:
    """_env_chat_id must only return a UUID when _running_inside_claudecode
    is True AND CLAUDE_SESSION_UUID is set."""

    def test_returns_uuid_when_both_set(self, monkeypatch):
        from claude_worker.cli import _env_chat_id

        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_SESSION_UUID", "abc-1234-5678-9abc-def012345678")
        assert _env_chat_id() == "abc-1234-5678-9abc-def012345678"

    def test_returns_none_when_claudecode_missing(self, monkeypatch):
        from claude_worker.cli import _env_chat_id

        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_UUID", "abc")
        assert _env_chat_id() is None

    def test_returns_none_when_claudecode_is_zero(self, monkeypatch):
        from claude_worker.cli import _env_chat_id

        monkeypatch.setenv("CLAUDECODE", "0")
        monkeypatch.setenv("CLAUDE_SESSION_UUID", "abc")
        assert _env_chat_id() is None
