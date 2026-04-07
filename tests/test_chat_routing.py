"""Tests for chat ID resolution and routing (Imp-11 coverage gap).

_resolve_chat_id implements a 4-tier priority:
  1. --all-chats → None (explicit opt-out)
  2. --chat <id> → use as-is if PM worker (else warning + None)
  3. Env-based auto-detection → PM workers only
  4. None otherwise

The bulk of this logic was uncovered before this pass; the new chat
routing tests exercise each tier.
"""

from __future__ import annotations

import pytest


class TestResolveChatIdPriority:
    """_resolve_chat_id must honor the documented priority order."""

    def test_all_chats_overrides_everything(self, fake_worker, monkeypatch, capsys):
        """all_chats=True returns None even if --chat and env are set."""
        from claude_worker.cli import _resolve_chat_id

        name = fake_worker([], pm=True)
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_SESSION_UUID", "env-uuid")

        result = _resolve_chat_id(name, explicit_chat="x", all_chats=True)
        assert result is None

    def test_explicit_chat_on_pm_worker_returns_as_is(self, fake_worker, monkeypatch):
        """--chat <id> on a PM worker: return the explicit value."""
        from claude_worker.cli import _resolve_chat_id

        name = fake_worker([], pm=True)
        # Even with env vars set, explicit wins
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_SESSION_UUID", "env-uuid")

        result = _resolve_chat_id(name, explicit_chat="explicit-abc", all_chats=False)
        assert result == "explicit-abc"

    def test_explicit_chat_on_non_pm_warns_and_returns_none(self, fake_worker, capsys):
        """--chat on a non-PM worker: stderr warning, returns None (pass-through)."""
        from claude_worker.cli import _resolve_chat_id

        name = fake_worker([], pm=False)
        result = _resolve_chat_id(name, explicit_chat="abc", all_chats=False)
        assert result is None
        captured = capsys.readouterr()
        assert "--chat is only applicable" in captured.err
        assert name in captured.err

    def test_env_detection_on_pm_worker(self, fake_worker, monkeypatch):
        """Without --chat, env auto-detection fires on PM workers."""
        from claude_worker.cli import _resolve_chat_id

        name = fake_worker([], pm=True)
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_SESSION_UUID", "env-1234-5678-9abc-def012345678")

        result = _resolve_chat_id(name, explicit_chat=None, all_chats=False)
        assert result == "env-1234-5678-9abc-def012345678"

    def test_env_detection_skipped_on_non_pm_worker(self, fake_worker, monkeypatch):
        """Env auto-detection must NOT apply to non-PM workers. Even with
        both env vars set, a non-PM target gets None (no chat routing)."""
        from claude_worker.cli import _resolve_chat_id

        name = fake_worker([], pm=False)
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_SESSION_UUID", "env-uuid")

        result = _resolve_chat_id(name, explicit_chat=None, all_chats=False)
        assert result is None

    def test_no_chat_and_no_env_returns_none(self, fake_worker, monkeypatch):
        """Base case: nothing set, no chat routing."""
        from claude_worker.cli import _resolve_chat_id

        name = fake_worker([], pm=True)
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_UUID", raising=False)

        result = _resolve_chat_id(name, explicit_chat=None, all_chats=False)
        assert result is None
