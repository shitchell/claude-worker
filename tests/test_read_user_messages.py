"""Regression tests for `read` showing user-input messages by default.

Bug: `read` previously hid replayed user messages because the ``show_only``
filter used only the subtype ``user-input`` and not the type ``user``.
claugs' ``should_show_message`` checks type visibility first, so the
subtype check never ran.

These tests drive the real ``cmd_read`` pipeline against synthetic JSONL
logs staged under a monkey-patched runtime base dir. They catch regressions
in both the filter config and the ``--last-turn`` walk-back semantics.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from conftest import (
    make_assistant_message,
    make_result_message,
    make_system_init,
    make_user_message,
)


def _build_read_args(
    name: str,
    *,
    exclude_user: bool = False,
    last_turn: bool = False,
    since: str | None = None,
    until: str | None = None,
    summary: bool = False,
    verbose: bool = False,
    all_chats: bool = True,
) -> argparse.Namespace:
    """Build an argparse.Namespace matching what the read subparser would produce."""
    return argparse.Namespace(
        name=name,
        follow=False,
        since=since,
        until=until,
        last_turn=last_turn,
        n=None,
        count=False,
        summary=summary,
        verbose=verbose,
        exclude_user=exclude_user,
        color=False,
        no_color=True,
        chat=None,
        all_chats=all_chats,
    )


def _capture_read(
    name: str, capsys: pytest.CaptureFixture, **kwargs
) -> str:
    """Invoke cmd_read with the given flags and return captured stdout."""
    from claude_worker.cli import cmd_read

    args = _build_read_args(name, **kwargs)
    cmd_read(args)
    return capsys.readouterr().out


# -- Default read shows user messages -----------------------------------------


class TestReadShowsUserMessagesByDefault:
    """Default `read` must show user-input messages alongside assistant."""

    def test_user_message_appears_in_summary(self, fake_worker, capsys):
        """Summary mode should list both the user and the assistant message."""
        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("what is 2+2", "uuid-user-0001-0002-000300040005"),
                make_assistant_message("4", "uuid-asst-0001-0002-000300040005"),
                make_result_message("uuid-rslt-0001-0002-000300040005"),
            ]
        )
        out = _capture_read(name, capsys, summary=True)
        assert "user: what is 2+2" in out
        assert "assistant: 4" in out

    def test_user_message_appears_in_default_rendering(self, fake_worker, capsys):
        """Normal rendering (not summary) should include the user message text."""
        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("what is 2+2", "uuid-user-0001-0002-000300040005"),
                make_assistant_message("4", "uuid-asst-0001-0002-000300040005"),
                make_result_message("uuid-rslt-0001-0002-000300040005"),
            ]
        )
        out = _capture_read(name, capsys)
        assert "what is 2+2" in out
        assert "4" in out

    def test_exclude_user_flag_hides_user_messages(self, fake_worker, capsys):
        """--exclude-user should hide user-input messages (old default)."""
        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("what is 2+2", "uuid-user-0001-0002-000300040005"),
                make_assistant_message("4", "uuid-asst-0001-0002-000300040005"),
                make_result_message("uuid-rslt-0001-0002-000300040005"),
            ]
        )
        out = _capture_read(name, capsys, exclude_user=True)
        assert "what is 2+2" not in out
        assert "4" in out


# -- --last-turn walk-back semantics ------------------------------------------


class TestLastTurnWalkBack:
    """--last-turn should find the window by walking back to last user+assistant."""

    def test_most_recent_is_assistant(self, fake_worker, capsys):
        """If the most recent message is an assistant turn, window starts at
        the last user message before it."""
        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("a1", "a1xx-0001-0002-0003-000400050006"),
                make_result_message("r1xx-0001-0002-0003-000400050006"),
                make_user_message("q2", "u2xx-0001-0002-0003-000400050006"),
                make_assistant_message("a2", "a2xx-0001-0002-0003-000400050006"),
                make_result_message("r2xx-0001-0002-0003-000400050006"),
            ]
        )
        out = _capture_read(name, capsys, last_turn=True, summary=True)
        # Should include the most recent exchange only
        assert "q2" in out
        assert "a2" in out
        # Should NOT include the earlier exchange
        assert "q1" not in out
        assert "a1" not in out

    def test_most_recent_is_user(self, fake_worker, capsys):
        """If the most recent message is a user message (no assistant reply
        yet), window starts at the last assistant message before it."""
        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("a1", "a1xx-0001-0002-0003-000400050006"),
                make_result_message("r1xx-0001-0002-0003-000400050006"),
                make_user_message("q2", "u2xx-0001-0002-0003-000400050006"),
            ]
        )
        out = _capture_read(name, capsys, last_turn=True, summary=True)
        # Should include the last assistant AND the pending user question
        assert "a1" in out
        assert "q2" in out
        # Should NOT include q1 (the earlier user turn)
        assert "q1" not in out

    def test_exclude_user_preserves_turn_boundary(self, fake_worker, capsys):
        """--exclude-user with --last-turn: the turn window is still located
        via user-input boundaries, but the user message itself is hidden from
        display."""
        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("a1", "a1xx-0001-0002-0003-000400050006"),
                make_result_message("r1xx-0001-0002-0003-000400050006"),
                make_user_message("q2", "u2xx-0001-0002-0003-000400050006"),
                make_assistant_message("a2", "a2xx-0001-0002-0003-000400050006"),
                make_result_message("r2xx-0001-0002-0003-000400050006"),
            ]
        )
        out = _capture_read(
            name, capsys, last_turn=True, exclude_user=True, summary=True
        )
        # Window is still [u2 ... r2], but user is hidden
        assert "a2" in out
        assert "q2" not in out
        # And earlier turn is excluded
        assert "a1" not in out
        assert "q1" not in out

    def test_no_user_messages_shows_all(self, fake_worker, capsys):
        """If there are no user messages at all (fresh log), --last-turn
        should degrade gracefully — don't drop everything."""
        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_assistant_message("a1", "a1xx-0001-0002-0003-000400050006"),
                make_result_message("r1xx-0001-0002-0003-000400050006"),
            ]
        )
        out = _capture_read(name, capsys, last_turn=True, summary=True)
        # Assistant message should still appear
        assert "a1" in out
