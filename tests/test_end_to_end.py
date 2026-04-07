"""End-to-end tests covering the full send → manager → claude → log
pipeline using the stub-claude harness.

Before Round 4, these flows were only exercised manually. The stub
unlocks:
- Live log tailing (`_read_follow`)
- Concurrent FIFO writes with real log pump feedback
- `wait-for-turn` against a log being actively written
- Session ID capture on first init message
- End-to-end multi-turn conversations

Tests use the ``running_worker`` fixture, which runs
``_run_manager_forkless`` in a thread with the stub-claude harness.
"""

from __future__ import annotations

import json
import threading
import time

import pytest


class TestSessionCapture:
    """The manager should capture the session ID from the first init
    message and persist it to runtime/session + .sessions.json."""

    def test_session_file_written_after_init(self, running_worker):
        sid = "aaaa1111-2222-3333-4444-555566667777"
        handle = running_worker(
            name="sess-capture",
            initial_message="ping",
            stub_session_id=sid,
        )
        # Wait for the session file to be written by the log-pump thread
        deadline = time.monotonic() + 5.0
        session_file = handle.runtime_dir / "session"
        while time.monotonic() < deadline:
            if session_file.exists():
                break
            time.sleep(0.02)
        assert session_file.exists()
        assert session_file.read_text().strip() == sid
        handle.stop()

    def test_session_persisted_to_sessions_json(self, running_worker):
        """save_worker should fire during init capture and update
        .sessions.json so --resume can find the session later."""
        from claude_worker.manager import get_sessions_file

        sid = "bbbb1111-2222-3333-4444-555566667777"
        handle = running_worker(
            name="sess-persist",
            initial_message="ping",
            stub_session_id=sid,
        )
        # Wait for session_file as a proxy for "init processed"
        session_file = handle.runtime_dir / "session"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not session_file.exists():
            time.sleep(0.02)

        sessions_path = get_sessions_file()
        assert sessions_path.exists()
        sessions = json.loads(sessions_path.read_text())
        assert "sess-persist" in sessions
        assert sessions["sess-persist"].get("session_id") == sid
        handle.stop()


class TestFifoSendRoundtrip:
    """A message written to the `in` FIFO should reach the stub and
    produce a response in the log file."""

    def test_single_send_produces_response(self, running_worker):
        handle = running_worker(name="fifo-1")
        # Give the manager a moment to get its FIFO reader ready
        assert handle.wait_for_log('"type": "system"', timeout=5.0)

        # Write a user message to the FIFO (same format cmd_send uses)
        payload = json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "what's up"},
            }
        )
        with open(handle.runtime_dir / "in", "w") as f:
            f.write(payload + "\n")
            f.flush()

        assert handle.wait_for_log("stub response to: what's up", timeout=5.0)
        handle.stop()

    def test_multiple_sends_produce_multiple_responses(self, running_worker):
        handle = running_worker(name="fifo-multi")
        assert handle.wait_for_log('"type": "system"', timeout=5.0)

        fifo = handle.runtime_dir / "in"
        for i, content in enumerate(["first", "second", "third"]):
            payload = json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": content},
                }
            )
            with open(fifo, "w") as f:
                f.write(payload + "\n")
                f.flush()
            assert handle.wait_for_log(f"stub response to: {content}", timeout=5.0)
        handle.stop()


class TestWaitForTurnAgainstLiveLog:
    """_wait_for_turn against a real live log: block until the stub
    emits a result message, return 0."""

    def test_wait_returns_zero_on_stub_result(self, running_worker):
        from claude_worker.cli import _wait_for_turn

        handle = running_worker(
            name="wait-live",
            initial_message="produce a result",
            stub_delay_ms=50,  # ensure wait has to actually wait
        )
        rc = _wait_for_turn(handle.name, timeout=5.0)
        assert rc == 0
        handle.stop()


class TestReadFollow:
    """_read_follow should tail an actively-written log and print new
    assistant messages as they appear."""

    def test_follow_prints_new_assistant_messages(self, running_worker, capsys):
        """Start a worker, launch read --follow in a thread, send
        messages via FIFO, verify they appear in the followed output."""
        from claude_worker.cli import (
            cmd_read,
        )
        import argparse

        handle = running_worker(
            name="follow-1",
            initial_message="initial",
        )
        # Wait for the initial response to land so --follow has a
        # baseline log to tail from EOF
        assert handle.wait_for_log("stub response to: initial", timeout=5.0)

        # Run cmd_read with --follow in a background thread. It blocks
        # indefinitely, so we'll interrupt it with a KeyboardInterrupt
        # after our test messages have been produced.
        read_args = argparse.Namespace(
            name=handle.name,
            follow=True,
            since=None,
            until=None,
            last_turn=False,
            n=None,
            count=False,
            summary=True,  # one-liner per message → easier to scan
            verbose=False,
            exclude_user=False,
            color=False,
            no_color=True,
            chat=None,
            all_chats=True,
        )

        read_done = threading.Event()

        def run_read():
            try:
                cmd_read(read_args)
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                read_done.set()

        read_thread = threading.Thread(target=run_read, daemon=True)
        read_thread.start()

        # Give the read thread time to reach its tail loop. The
        # follow loop polls at POLL_INTERVAL_SECONDS (0.1s), so 0.5s
        # gives multiple poll cycles of headroom before we write.
        time.sleep(0.5)

        # Send a new message via the FIFO
        payload = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "follow-check-abcdef",
                },
            }
        )
        with open(handle.runtime_dir / "in", "w") as f:
            f.write(payload + "\n")
            f.flush()

        # Wait for the stub to produce the response in the log
        assert handle.wait_for_log("stub response to: follow-check-abcdef", timeout=5.0)
        # Give the follow loop a moment to pick it up (multiple
        # POLL_INTERVAL_SECONDS cycles of headroom)
        time.sleep(0.5)

        # Stop the worker → closing FIFO writers → proc.wait returns →
        # cleanup runs → log file disappears → read_follow's readline
        # returns empty → it loops sleeping. The read thread doesn't
        # naturally exit; we need to interrupt it.
        handle.stop()

        # Now capture stdout and verify the follow output contained
        # our message. cmd_read with --follow prints as it goes, which
        # capsys captures.
        captured = capsys.readouterr()
        assert "follow-check-abcdef" in captured.out, (
            f"--follow output did not contain the streamed message. "
            f"Captured stdout:\n{captured.out}"
        )


class TestCmdSendEndToEnd:
    """cmd_send against a live worker: the full argparse → status gate →
    FIFO write → wait_for_turn → response roundtrip."""

    def test_cmd_send_produces_response_in_log(self, running_worker):
        import argparse
        from claude_worker.cli import cmd_send

        handle = running_worker(name="cmd-send-1")
        # Wait for the init so status gate sees the worker as ready
        assert handle.wait_for_log('"type": "system"', timeout=5.0)

        args = argparse.Namespace(
            name=handle.name,
            message=["hello", "from", "cmd_send"],
            background=False,
            queue=False,
            show_response=False,
            show_full_response=False,
            chat=None,
            all_chats=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_send(args)
        assert exc_info.value.code == 0

        # Verify the stub's echo landed in the log
        assert handle.wait_for_log("stub response to: hello from cmd_send", timeout=5.0)
        handle.stop()


class TestShutdownCleanup:
    """Manager should clean up its runtime dir on clean exit."""

    def test_runtime_dir_removed_after_stop(self, running_worker):
        handle = running_worker(name="cleanup-1", initial_message="hi")
        assert handle.wait_for_log("stub response", timeout=5.0)

        runtime_dir = handle.runtime_dir
        assert runtime_dir.exists()

        handle.stop()
        # Allow the manager thread to finish cleanup after proc.wait
        # returns
        deadline = time.monotonic() + 2.0
        while runtime_dir.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not runtime_dir.exists()
