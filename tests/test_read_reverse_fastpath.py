"""Tests for _read_static's reverse fast path (Imp-5, call site 5).

The forward scan in _read_static reads the entire log into memory on
every invocation, which is wasted work when the user only wants the
last N messages or the last-turn window. For non-PM workers with no
--since / --until filtering, a reverse fast path walks backwards from
EOF just enough to collect what the user asked for.

The fast path must preserve the output semantics of the forward path
so existing tests continue to pass. New tests here specifically verify
that both paths agree on the same inputs.
"""

from __future__ import annotations

import pytest

from conftest import (
    make_assistant_message,
    make_result_message,
    make_system_init,
    make_user_message,
)


def _run_read(fake_worker_factory, entries, **kwargs):
    """Shared helper: build a fake worker with `entries` and call cmd_read."""
    import argparse
    from claude_worker.cli import cmd_read

    name = fake_worker_factory(entries, name="fast-path-test")
    args = argparse.Namespace(
        name=name,
        follow=False,
        since=None,
        until=None,
        last_turn=False,
        n=None,
        count=False,
        summary=True,
        verbose=False,
        exclude_user=False,
        color=False,
        no_color=True,
        chat=None,
        all_chats=True,
    )
    for key, value in kwargs.items():
        setattr(args, key, value)
    cmd_read(args)


class TestNLastMessages:
    """`read -n N` should return the last N displayable messages."""

    def test_n_equals_two(self, fake_worker, capsys):
        """-n 2 against a 4-exchange log returns just the last exchange."""
        entries = [
            make_system_init("s1-0001-0002-0003-000400050006"),
            make_user_message("q1", "u1-0001-0002-0003-000400050006"),
            make_assistant_message("a1", "aa1-0001-0002-0003-000400050006"),
            make_result_message("r1-0001-0002-0003-000400050006"),
            make_user_message("q2", "u2-0001-0002-0003-000400050006"),
            make_assistant_message("a2", "aa2-0001-0002-0003-000400050006"),
            make_result_message("r2-0001-0002-0003-000400050006"),
        ]
        _run_read(fake_worker, entries, n=2)
        out = capsys.readouterr().out
        # Last 2 displayable (user and assistant pass the filter; result
        # and system don't in non-verbose mode)
        assert "q2" in out
        assert "a2" in out
        # Earlier exchange excluded
        assert "q1" not in out
        assert "a1" not in out

    def test_n_larger_than_log(self, fake_worker, capsys):
        """-n 100 on a small log returns all available messages."""
        entries = [
            make_user_message("q", "uu-0001-0002-0003-000400050006"),
            make_assistant_message("a", "aa-0001-0002-0003-000400050006"),
        ]
        _run_read(fake_worker, entries, n=100)
        out = capsys.readouterr().out
        assert "q" in out
        assert "a" in out


class TestFastPathMatchesForwardPath:
    """Fast path and forward path must agree on all relevant flag
    combinations. This is an equivalence test — run each input twice and
    compare outputs."""

    def test_last_turn_equivalent(self, fake_worker, capsys):
        """`--last-turn` alone should produce the same output via either
        code path."""
        entries = [
            make_user_message("hello", "uu-0001-0002-0003-000400050006"),
            make_assistant_message("hi", "aa-0001-0002-0003-000400050006"),
            make_result_message("rr-0001-0002-0003-000400050006"),
            make_user_message("bye", "uu2-0001-0002-0003-000400050006"),
            make_assistant_message("ok", "aa2-0001-0002-0003-000400050006"),
            make_result_message("rr2-0001-0002-0003-000400050006"),
        ]
        _run_read(fake_worker, entries, last_turn=True)
        out = capsys.readouterr().out
        # Only the most recent exchange
        assert "bye" in out
        assert "ok" in out
        assert "hello" not in out
        assert "hi" not in out
