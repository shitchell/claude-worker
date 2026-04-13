"""Tests for send CLI flag ordering (ticket #058).

argparse's nargs="*" on the message positional absorbs trailing flags
into the message list.  _reparse_send_flags extracts trailing recognized
flags and applies them to the args namespace, so flag ordering is
irrelevant:

    send NAME --queue "msg"       # always worked
    send NAME "msg" --queue       # now also works
    send NAME "msg" --chat abc    # value flags too
"""

from __future__ import annotations

import argparse

import pytest

from claude_worker.cli import _reparse_send_flags


def _base_args(**overrides) -> argparse.Namespace:
    """Build a baseline send Namespace with all flags at their defaults."""
    defaults = dict(
        name="w",
        message=[],
        queue=False,
        dry_run=False,
        verbose=False,
        show_response=False,
        show_full_response=False,
        broadcast=False,
        alive=False,
        all_chats=False,
        chat=None,
        role=None,
        status=None,
        cwd_filter=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestReparseSendFlags:
    """_reparse_send_flags extracts trailing flags from args.message."""

    # -- bool flags --

    def test_trailing_bool_flag(self):
        args = _base_args(message=["hello", "world", "--queue"])
        args = _reparse_send_flags(args)
        assert args.message == ["hello", "world"]
        assert args.queue is True

    def test_multiple_trailing_bool_flags(self):
        args = _base_args(message=["hello", "--queue", "--verbose"])
        args = _reparse_send_flags(args)
        assert args.message == ["hello"]
        assert args.queue is True
        assert args.verbose is True

    def test_all_bool_flags(self):
        """Every recognized bool flag is extracted when trailing."""
        flags = [
            "--queue",
            "--dry-run",
            "--verbose",
            "--show-response",
            "--show-full-response",
            "--broadcast",
            "--alive",
            "--all-chats",
        ]
        args = _base_args(message=["msg"] + flags)
        args = _reparse_send_flags(args)
        assert args.message == ["msg"]
        assert args.queue is True
        assert args.dry_run is True
        assert args.verbose is True
        assert args.show_response is True
        assert args.show_full_response is True
        assert args.broadcast is True
        assert args.alive is True
        assert args.all_chats is True

    # -- value flags --

    def test_trailing_value_flag(self):
        args = _base_args(message=["hello", "--chat", "abc123"])
        args = _reparse_send_flags(args)
        assert args.message == ["hello"]
        assert args.chat == "abc123"

    def test_trailing_role_flag(self):
        args = _base_args(message=["deploy", "--role", "pm"])
        args = _reparse_send_flags(args)
        assert args.message == ["deploy"]
        assert args.role == "pm"

    def test_trailing_status_flag(self):
        args = _base_args(message=["deploy", "--status", "waiting"])
        args = _reparse_send_flags(args)
        assert args.message == ["deploy"]
        assert args.status == "waiting"

    def test_trailing_cwd_flag(self):
        args = _base_args(message=["deploy", "--cwd", "/tmp/proj"])
        args = _reparse_send_flags(args)
        assert args.message == ["deploy"]
        assert args.cwd_filter == "/tmp/proj"

    # -- mixed --

    def test_mixed_bool_and_value_flags(self):
        args = _base_args(message=["fix", "the", "bug", "--queue", "--chat", "xyz"])
        args = _reparse_send_flags(args)
        assert args.message == ["fix", "the", "bug"]
        assert args.queue is True
        assert args.chat == "xyz"

    # -- no-op cases --

    def test_no_message(self):
        args = _base_args(message=[])
        args = _reparse_send_flags(args)
        assert args.message == []
        assert args.queue is False

    def test_no_trailing_flags(self):
        args = _base_args(message=["hello", "world"])
        args = _reparse_send_flags(args)
        assert args.message == ["hello", "world"]
        assert args.queue is False

    def test_flags_before_message_already_parsed(self):
        """When argparse handles flags normally (before message), reparse is a no-op."""
        args = _base_args(message=["hello"], queue=True)
        args = _reparse_send_flags(args)
        assert args.message == ["hello"]
        assert args.queue is True

    # -- flag-like words in message body --

    def test_flag_like_word_mid_message_not_extracted(self):
        """--verbose inside the message (not trailing) is NOT extracted."""
        args = _base_args(message=["use", "--verbose", "for", "details"])
        args = _reparse_send_flags(args)
        assert args.message == ["use", "--verbose", "for", "details"]
        assert args.verbose is False

    def test_flag_like_word_mid_message_with_trailing_flag(self):
        """Flag-like words mid-message are preserved; only trailing is extracted."""
        args = _base_args(
            message=["use", "--verbose", "for", "details", "--queue"]
        )
        args = _reparse_send_flags(args)
        assert args.message == ["use", "--verbose", "for", "details"]
        assert args.queue is True
        assert args.verbose is False

    def test_unknown_flag_stops_extraction(self):
        """An unrecognized flag stops the backward scan."""
        args = _base_args(message=["msg", "--unknown", "--queue"])
        args = _reparse_send_flags(args)
        # --queue is trailing, extracted; --unknown stops scan before it
        assert args.message == ["msg", "--unknown"]
        assert args.queue is True

    def test_value_flag_without_value_stops_extraction(self):
        """A value flag as the only remaining word can't consume a value."""
        args = _base_args(message=["--chat"])
        args = _reparse_send_flags(args)
        # --chat alone has no value to consume, scan stops
        assert args.message == ["--chat"]
        assert args.chat is None

    # -- empty message after extraction --

    def test_only_flags_no_message(self):
        """If message is entirely flags, message becomes empty."""
        args = _base_args(message=["--queue", "--verbose"])
        args = _reparse_send_flags(args)
        assert args.message == []
        assert args.queue is True
        assert args.verbose is True
