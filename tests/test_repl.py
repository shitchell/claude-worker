"""Tests for the REPL subcommand.

The REPL is a turn-by-turn interactive loop:
1. Wait for worker to be idle
2. Flush stdin and prompt for input
3. Send the message + live-stream the response
4. Loop

Tests use the stub-claude harness (running_worker fixture) so the
full pipeline is exercised — the manager spins up a real subprocess,
the FIFO plumbing runs, and the stream thread tails the live log.
``input()`` is monkey-patched to return canned strings.
"""

from __future__ import annotations

import argparse
import os
import time

import pytest


def _build_repl_args(name: str, chat: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(name=name, chat=chat)


def _scripted_inputs(*responses: str):
    """Build a fake input() that returns each response in sequence and
    raises EOFError after the list is exhausted (mimicking Ctrl-D)."""
    responses_iter = iter(responses)

    def fake_input(prompt=""):
        try:
            return next(responses_iter)
        except StopIteration:
            raise EOFError

    return fake_input


# -- Helper unit tests -------------------------------------------------------


class TestComputeReplChatId:
    """The chat ID auto-derivation should be deterministic for a given
    process and gracefully degrade when stdin is not a TTY."""

    def test_returns_pid_based_id(self):
        from claude_worker.cli import _compute_repl_chat_id

        result = _compute_repl_chat_id()
        assert result.startswith("repl-")
        assert str(os.getpid()) in result

    def test_two_calls_same_process_match(self):
        from claude_worker.cli import _compute_repl_chat_id

        a = _compute_repl_chat_id()
        b = _compute_repl_chat_id()
        assert a == b


# -- End-to-end REPL tests using the stub harness ----------------------------


class TestReplBasicFlow:
    """The REPL loop should send a message, stream the response, and
    exit cleanly when stdin returns EOF (Ctrl-D)."""

    def test_single_turn_round_trip(self, running_worker, monkeypatch, capsys):
        """Spawn a worker, REPL sends one message, response is streamed
        to stdout, then stdin EOF exits the loop."""
        from claude_worker import cli as cw_cli

        handle = running_worker(name="repl-1", initial_message="prime")
        # Wait for the initial prime turn so the worker is actually idle
        # before the REPL takes over (otherwise the REPL's first idle
        # check might fire before the prime turn even started).
        assert handle.wait_for_log("stub response to: prime", timeout=5.0)

        # Age the log so STATUS_IDLE_THRESHOLD_SECONDS doesn't make
        # the REPL block on idle-wait. The threshold is 3s; backdate
        # by 5s for safety.
        log_path = handle.log_path
        old_time = log_path.stat().st_mtime - 5.0
        os.utime(log_path, (old_time, old_time))

        # Patch input() to return one message then EOF
        monkeypatch.setattr("builtins.input", _scripted_inputs("hello from REPL"))

        # The stream thread sleeps in increments of POLL_INTERVAL_SECONDS
        # (0.1s); make sure the stub responds quickly enough that the
        # REPL doesn't sit waiting forever. We bump the threshold check
        # via the same os.utime trick after each turn won't apply here
        # because the response will reset the mtime — instead we rely
        # on the stub being fast (no CLAUDE_STUB_DELAY_MS) and the post-
        # turn idle check eventually flipping past STATUS_IDLE_THRESHOLD.
        #
        # Actually, the REPL will block in _wait_for_worker_idle after
        # the send because mtime is fresh again. Lower the threshold
        # for the test so we don't have to actually wait 3 real seconds.
        monkeypatch.setattr(cw_cli, "STATUS_IDLE_THRESHOLD_SECONDS", 0.2)

        cw_cli.cmd_repl(_build_repl_args(handle.name))

        captured = capsys.readouterr()
        # Banner is present
        assert "claude-worker REPL" in captured.out
        assert handle.name in captured.out
        # Stub's response to our REPL message landed on stdout
        assert "stub response to: hello from REPL" in captured.out
        handle.stop()

    def test_eof_on_empty_prompt_exits(self, running_worker, monkeypatch, capsys):
        """Hitting Ctrl-D (EOFError) at an empty prompt exits without
        sending anything new."""
        from claude_worker import cli as cw_cli

        # No initial_message: avoid producing any "stub response to:"
        # text that would muddy the assertion below.
        handle = running_worker(name="repl-eof")
        # Wait for the system/init so the log file exists
        assert handle.wait_for_log('"type": "system"', timeout=5.0)

        # Age log past threshold
        old_time = handle.log_path.stat().st_mtime - 5.0
        os.utime(handle.log_path, (old_time, old_time))

        # First call to input() raises EOFError immediately
        def fake_input(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr(cw_cli, "STATUS_IDLE_THRESHOLD_SECONDS", 0.2)

        # Should return without raising
        cw_cli.cmd_repl(_build_repl_args(handle.name))

        captured = capsys.readouterr()
        assert "claude-worker REPL" in captured.out
        # No "stub response to:" — we never sent anything and the
        # worker had no prior turns either
        assert "stub response to:" not in captured.out
        handle.stop()

    def test_exit_command_quits(self, running_worker, monkeypatch, capsys):
        """Typing /exit at the prompt exits the loop."""
        from claude_worker import cli as cw_cli

        handle = running_worker(name="repl-exit-cmd", initial_message="prime")
        assert handle.wait_for_log("stub response", timeout=5.0)

        old_time = handle.log_path.stat().st_mtime - 5.0
        os.utime(handle.log_path, (old_time, old_time))

        monkeypatch.setattr("builtins.input", _scripted_inputs("/exit"))
        monkeypatch.setattr(cw_cli, "STATUS_IDLE_THRESHOLD_SECONDS", 0.2)

        cw_cli.cmd_repl(_build_repl_args(handle.name))

        captured = capsys.readouterr()
        # /exit was consumed but no message was sent
        assert "stub response to: /exit" not in captured.out
        handle.stop()


class TestReplLastTurnContext:
    """REPL entry should print the worker's last turn (if any) so the
    human reading sees what's already happened."""

    def test_prior_turn_shown_at_entry(self, running_worker, monkeypatch, capsys):
        from claude_worker import cli as cw_cli

        handle = running_worker(
            name="repl-context",
            initial_message="from-the-past",
        )
        assert handle.wait_for_log("stub response to: from-the-past", timeout=5.0)

        old_time = handle.log_path.stat().st_mtime - 5.0
        os.utime(handle.log_path, (old_time, old_time))

        # EOF immediately so REPL prints banner + last turn + exits
        def fake_input(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr(cw_cli, "STATUS_IDLE_THRESHOLD_SECONDS", 0.2)

        cw_cli.cmd_repl(_build_repl_args(handle.name))

        captured = capsys.readouterr()
        # The prior assistant response should appear in the entry context
        assert "stub response to: from-the-past" in captured.out
        handle.stop()


class TestReplPmAutoChatId:
    """For PM workers, the REPL banner should show the auto-derived chat
    ID and the sent message should carry the corresponding chat tag."""

    def test_pm_worker_banner_shows_chat_id(self, running_worker, monkeypatch, capsys):
        from claude_worker import cli as cw_cli

        # PM mode requires save_worker(pm=True) — running_worker doesn't
        # do this for us, but cmd_start would. We can mark the worker as
        # PM by writing the metadata directly.
        handle = running_worker(name="repl-pm", initial_message="prime")
        assert handle.wait_for_log("stub response", timeout=5.0)

        # Mark as PM in .sessions.json (the same way fake_worker does it)
        from claude_worker.manager import get_sessions_file
        import json as _json

        sessions_path = get_sessions_file()
        sessions_data = {}
        if sessions_path.exists():
            sessions_data = _json.loads(sessions_path.read_text())
        sessions_data[handle.name] = {"pm": True}
        sessions_path.write_text(_json.dumps(sessions_data))

        old_time = handle.log_path.stat().st_mtime - 5.0
        os.utime(handle.log_path, (old_time, old_time))

        def fake_input(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr(cw_cli, "STATUS_IDLE_THRESHOLD_SECONDS", 0.2)

        cw_cli.cmd_repl(_build_repl_args(handle.name))

        captured = capsys.readouterr()
        # Banner shows the auto-derived chat ID
        assert "PM chat: repl-" in captured.out
        handle.stop()
