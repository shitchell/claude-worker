"""Tests for wait-for-turn --chat filter (D22).

Verifies that _wait_for_turn with chat_tag only fires on turns
containing the matching [chat:<tag>] string.
"""

from __future__ import annotations

import pytest

from conftest import (
    make_assistant_message,
    make_result_message,
    make_system_init,
    make_user_message,
)


class TestWaitForTurnChatFilter:
    """_wait_for_turn(chat_tag=...) must filter turns by chat tag."""

    def test_matching_tag_fires(self, fake_worker):
        """A completed turn with the matching chat tag should return 0."""
        from claude_worker.cli import _wait_for_turn

        name = fake_worker(
            [
                make_system_init("sys-uuid"),
                make_user_message("[chat:abc] question", "u1"),
                make_assistant_message(
                    "[chat:abc] answer here", "a1", stop_reason="end_turn"
                ),
                make_result_message("r1"),
            ],
            alive=True,
        )
        rc = _wait_for_turn(name, timeout=0.5, chat_tag="abc")
        assert rc == 0

    def test_non_matching_tag_skipped(self, fake_worker):
        """A turn tagged for another consumer should be skipped (timeout)."""
        from claude_worker.cli import _wait_for_turn

        name = fake_worker(
            [
                make_system_init("sys-uuid"),
                make_user_message("[chat:other] question", "u1"),
                make_assistant_message(
                    "[chat:other] answer", "a1", stop_reason="end_turn"
                ),
                make_result_message("r1"),
            ],
            alive=True,
        )
        rc = _wait_for_turn(name, timeout=0.5, chat_tag="abc")
        assert rc == 2  # timeout — no matching turn

    def test_untagged_turn_skipped(self, fake_worker):
        """An untagged turn should be skipped when --chat is set."""
        from claude_worker.cli import _wait_for_turn

        name = fake_worker(
            [
                make_system_init("sys-uuid"),
                make_user_message("question", "u1"),
                make_assistant_message(
                    "answer without tag", "a1", stop_reason="end_turn"
                ),
                make_result_message("r1"),
            ],
            alive=True,
        )
        rc = _wait_for_turn(name, timeout=0.5, chat_tag="abc")
        assert rc == 2  # timeout — no matching turn

    def test_no_chat_filter_fires_on_any(self, fake_worker):
        """Without chat_tag, any turn fires (baseline behavior)."""
        from claude_worker.cli import _wait_for_turn

        name = fake_worker(
            [
                make_system_init("sys-uuid"),
                make_user_message("question", "u1"),
                make_assistant_message("untagged answer", "a1", stop_reason="end_turn"),
                make_result_message("r1"),
            ],
            alive=True,
        )
        rc = _wait_for_turn(name, timeout=0.5)
        assert rc == 0

    def test_composes_with_after_uuid(self, fake_worker):
        """--chat + --after-uuid: match only tagged turns after the marker."""
        from claude_worker.cli import _wait_for_turn

        marker = "r1"
        name = fake_worker(
            [
                make_system_init("sys-uuid"),
                # Turn 1: tagged abc (before marker)
                make_user_message("[chat:abc] q1", "u1"),
                make_assistant_message("[chat:abc] a1", "a1", stop_reason="end_turn"),
                make_result_message(marker),
                # Turn 2: tagged xyz (after marker — wrong tag)
                make_user_message("[chat:xyz] q2", "u2"),
                make_assistant_message("[chat:xyz] a2", "a2", stop_reason="end_turn"),
                make_result_message("r2"),
            ],
            alive=True,
        )
        # After marker, only turn 2 exists — but it's tagged xyz, not abc
        rc = _wait_for_turn(name, timeout=0.5, after_uuid=marker, chat_tag="abc")
        assert rc == 2  # timeout
