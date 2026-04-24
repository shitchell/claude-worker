"""Tests for #086/D106: UUID prefix matching in --since/--until.

Covers:
- _id_prefix_matches helper
- read_messages with prefix since_id
- _watch_thread with prefix since_id
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_worker import cli as cw_cli
from claude_worker.thread_store import (
    _id_prefix_matches,
    append_message,
    create_thread,
    read_messages,
)


class TestIdPrefixMatches:
    def test_exact_match(self) -> None:
        assert _id_prefix_matches("abc123def456", "abc123def456") is True

    def test_prefix_match(self) -> None:
        assert _id_prefix_matches("abc123def456", "abc123de") is True

    def test_case_insensitive(self) -> None:
        assert _id_prefix_matches("ABC123def456", "abc123de") is True

    def test_no_match(self) -> None:
        assert _id_prefix_matches("abc123def456", "xyz") is False

    def test_empty_target_returns_false(self) -> None:
        assert _id_prefix_matches("abc123", "") is False

    def test_empty_id_returns_false(self) -> None:
        assert _id_prefix_matches("", "abc") is False


class TestReadMessagesPrefix:
    def test_prefix_since_skips_to_marker(self) -> None:
        tid = create_thread(participants=["a", "b"], thread_id="t1")
        m1 = append_message(tid, "a", "first")
        m2 = append_message(tid, "a", "second")
        m3 = append_message(tid, "a", "third")

        # Use 8-char prefix of m1's id
        prefix = m1["id"][:8]
        msgs = read_messages(tid, since_id=prefix)
        # Should return messages AFTER m1 (i.e., m2 + m3)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "second"
        assert msgs[1]["content"] == "third"

    def test_full_id_still_works(self) -> None:
        tid = create_thread(participants=["a", "b"], thread_id="t2")
        m1 = append_message(tid, "a", "first")
        m2 = append_message(tid, "a", "second")

        msgs = read_messages(tid, since_id=m1["id"])
        assert len(msgs) == 1
        assert msgs[0]["content"] == "second"

    def test_no_match_returns_empty(self) -> None:
        tid = create_thread(participants=["a", "b"], thread_id="t3")
        append_message(tid, "a", "msg")

        msgs = read_messages(tid, since_id="nonexistent")
        assert msgs == []

    def test_none_since_returns_all(self) -> None:
        tid = create_thread(participants=["a", "b"], thread_id="t4")
        append_message(tid, "a", "m1")
        append_message(tid, "a", "m2")

        msgs = read_messages(tid, since_id=None)
        assert len(msgs) == 2


class TestWatchThreadPrefix:
    def test_since_prefix_in_watch(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_watch_thread with a prefix since_id should skip messages
        up to and including the match, only printing later ones."""
        tid = create_thread(participants=["a", "b"], thread_id="tw1")
        m1 = append_message(tid, "a", "before-marker")
        m2 = append_message(tid, "a", "after-marker")
        m3 = append_message(tid, "a", "also-after")

        prefix = m1["id"][:8]
        # Use timeout to exit after printing existing messages
        rc = cw_cli._watch_thread(tid, since_id=prefix, timeout=0.3)
        assert rc == 2  # timeout exit
        captured = capsys.readouterr()
        assert "before-marker" not in captured.out
        assert "after-marker" in captured.out
        assert "also-after" in captured.out
