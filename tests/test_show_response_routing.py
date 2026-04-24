"""Tests for #085/D102: log-based thread resolution for response tee.

Covers:
- ``_resolve_tee_thread`` — unit tests for log-backward scanning
- Multi-thread race scenario (two threads, verify correct routing)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_worker.manager import _resolve_tee_thread
from claude_worker.thread_store import create_thread, read_messages


def _user_notif(thread_id: str, sender: str = "human") -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": (
                    f"[system:new-message] Thread {thread_id} "
                    f"from {sender}: hello..."
                ),
            },
            "uuid": f"notif-{thread_id}",
        }
    )


def _assistant_line(text: str = "response") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
            },
            "uuid": "asst-1",
        }
    )


def _result_line() -> str:
    return json.dumps({"type": "result", "uuid": "res-1"})


class TestResolveTeeThread:
    def test_missing_log_returns_none(self, tmp_path: Path) -> None:
        assert _resolve_tee_thread(tmp_path / "nope") is None

    def test_empty_log_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        log.write_text("")
        assert _resolve_tee_thread(log) is None

    def test_no_notification_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        log.write_text(_assistant_line() + "\n")
        assert _resolve_tee_thread(log) is None

    def test_extracts_thread_id_from_notification(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        log.write_text(_user_notif("pair-human-pm") + "\n")
        assert _resolve_tee_thread(log) == "pair-human-pm"

    def test_returns_most_recent_notification(self, tmp_path: Path) -> None:
        """When multiple notifications exist, return the newest one."""
        log = tmp_path / "log"
        log.write_text(
            _user_notif("pair-tl-pm")
            + "\n"
            + _assistant_line("first response")
            + "\n"
            + _result_line()
            + "\n"
            + _user_notif("pair-human-pm")
            + "\n"
            + _assistant_line("second response")
            + "\n"
        )
        assert _resolve_tee_thread(log) == "pair-human-pm"

    def test_skips_non_user_lines(self, tmp_path: Path) -> None:
        """Assistant and result lines are ignored; only user messages
        containing the notification pattern match."""
        log = tmp_path / "log"
        log.write_text(
            _user_notif("pair-a-b")
            + "\n"
            + _assistant_line()
            + "\n"
            + _result_line()
            + "\n"
        )
        assert _resolve_tee_thread(log) == "pair-a-b"

    def test_handles_chat_thread_ids(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        log.write_text(_user_notif("chat-abc123") + "\n")
        assert _resolve_tee_thread(log) == "chat-abc123"

    def test_ignores_non_notification_user_messages(self, tmp_path: Path) -> None:
        """A plain user message (not a [system:new-message]) is skipped."""
        log = tmp_path / "log"
        log.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": "just a normal message"},
                    "uuid": "plain",
                }
            )
            + "\n"
        )
        assert _resolve_tee_thread(log) is None


class TestMultiThreadRouting:
    """Verify that when two threads receive messages, the tee routes
    each response to the correct thread — not the globally last-seen one."""

    def test_response_routes_to_triggering_thread(self, tmp_path: Path) -> None:
        """Simulate: human sends, TL sends, PM responds to human.
        The response should go to pair-human-pm, not pair-tl-pm."""
        from claude_worker.manager import _tee_assistant_to_thread

        log = tmp_path / "log"
        create_thread(participants=["pm", "human"], thread_id="pair-human-pm")
        create_thread(participants=["pm", "tl"], thread_id="pair-tl-pm")

        # Human's notification is the MOST RECENT user message in the log.
        # TL's notification came BEFORE it (and was already responded to).
        log.write_text(
            _user_notif("pair-tl-pm", sender="tl")
            + "\n"
            + _assistant_line("answered tl")
            + "\n"
            + _result_line()
            + "\n"
            + _user_notif("pair-human-pm", sender="human")
            + "\n"
            + _assistant_line("answered human")
            + "\n"
        )

        # Tee the "answered human" response — should go to pair-human-pm
        assistant = _assistant_line("answered human")
        teed = _tee_assistant_to_thread(assistant, log, "pm")
        assert teed is True

        human_msgs = read_messages("pair-human-pm")
        assert len(human_msgs) == 1
        assert human_msgs[0]["content"] == "answered human"

        # TL's thread should NOT have the human's response
        tl_msgs = read_messages("pair-tl-pm")
        assert len(tl_msgs) == 0 or all(
            "answered human" not in m["content"] for m in tl_msgs
        )
