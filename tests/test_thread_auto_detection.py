"""Tests for #087/D103: thread auto-detection with existence-based fallback.

Covers _resolve_read_thread_id's fallback chain:
  primary (sender-based) → "human" fallback → CW_WORKER_NAME fallback
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from claude_worker import cli as cw_cli
from claude_worker import thread_store
from claude_worker.thread_store import create_thread, pair_thread_id


def _read_args(name: str, **overrides) -> argparse.Namespace:
    defaults = dict(
        name=name,
        thread=None,
        chat=None,
        all_chats=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestResolveReadThreadId:
    def test_resolves_to_existing_pair_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plain terminal (sender=human), pair-gvp-human exists → match."""
        monkeypatch.delenv("CW_WORKER_NAME", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_UUID", raising=False)
        monkeypatch.delenv("CLAUDECODE", raising=False)

        create_thread(participants=["human", "gvp"], thread_id="pair-gvp-human")

        args = _read_args("gvp")
        tid = cw_cli._resolve_read_thread_id(args)
        assert tid == "pair-gvp-human"

    def test_falls_back_to_human_when_uuid_thread_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Running from Claude Code (sender=UUID), UUID thread doesn't
        exist, but pair-gvp-human does → falls back with stderr notice."""
        monkeypatch.delenv("CW_WORKER_NAME", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_UUID", "abc-session-uuid")
        monkeypatch.delenv("CLAUDECODE", raising=False)

        create_thread(participants=["human", "gvp"], thread_id="pair-gvp-human")
        # NOTE: pair-abc-session-uuid-gvp does NOT exist

        args = _read_args("gvp")
        tid = cw_cli._resolve_read_thread_id(args)
        assert tid == "pair-gvp-human"

        captured = capsys.readouterr()
        assert "note:" in captured.err
        assert "pair-gvp-human" in captured.err
        assert "not found" in captured.err

    def test_uuid_thread_preferred_when_exists(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Both UUID-based and human threads exist → primary (UUID) wins.
        No fallback notice."""
        monkeypatch.delenv("CW_WORKER_NAME", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_UUID", "my-uuid")
        monkeypatch.delenv("CLAUDECODE", raising=False)

        uuid_thread = pair_thread_id("my-uuid", "gvp")
        create_thread(participants=["my-uuid", "gvp"], thread_id=uuid_thread)
        create_thread(participants=["human", "gvp"], thread_id="pair-gvp-human")

        args = _read_args("gvp")
        tid = cw_cli._resolve_read_thread_id(args)
        assert tid == uuid_thread

        captured = capsys.readouterr()
        assert "note:" not in captured.err

    def test_explicit_thread_overrides_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--thread pair-x-y always wins regardless of existence."""
        monkeypatch.delenv("CW_WORKER_NAME", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_UUID", raising=False)
        monkeypatch.delenv("CLAUDECODE", raising=False)

        create_thread(participants=["human", "gvp"], thread_id="pair-gvp-human")

        args = _read_args("gvp", thread="pair-x-y")
        tid = cw_cli._resolve_read_thread_id(args)
        assert tid == "pair-x-y"

    def test_worker_to_worker_resolves_correctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CW_WORKER_NAME=pm, target=tl, pair-pm-tl exists → match."""
        monkeypatch.setenv("CW_WORKER_NAME", "pm")
        monkeypatch.delenv("CLAUDE_SESSION_UUID", raising=False)
        monkeypatch.delenv("CLAUDECODE", raising=False)

        create_thread(participants=["pm", "tl"], thread_id="pair-pm-tl")

        args = _read_args("tl")
        tid = cw_cli._resolve_read_thread_id(args)
        assert tid == "pair-pm-tl"

    def test_no_thread_exists_returns_primary(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No matching threads at all → returns primary pair-thread-id.
        No fallback notice (nothing to fall back to)."""
        monkeypatch.delenv("CW_WORKER_NAME", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_UUID", raising=False)
        monkeypatch.delenv("CLAUDECODE", raising=False)

        args = _read_args("gvp")
        tid = cw_cli._resolve_read_thread_id(args)
        assert tid == "pair-gvp-human"

        captured = capsys.readouterr()
        assert "note:" not in captured.err

    def test_no_fallback_when_sender_is_human_already(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Sender is already 'human' (primary = pair-gvp-human), thread
        doesn't exist. No fallback fires, no notice emitted."""
        monkeypatch.delenv("CW_WORKER_NAME", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_UUID", raising=False)
        monkeypatch.delenv("CLAUDECODE", raising=False)

        # No threads exist at all
        args = _read_args("gvp")
        tid = cw_cli._resolve_read_thread_id(args)
        assert tid == "pair-gvp-human"  # primary IS the human thread

        captured = capsys.readouterr()
        assert "note:" not in captured.err  # no fallback to announce
