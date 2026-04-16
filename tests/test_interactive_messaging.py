"""Tests for interactive-session messaging support (#076, D94).

Covers:
- ``_is_known_thread_participant`` — membership check over index
- ``_send_to_single_worker`` — thread-only fallback for non-worker targets
- ``_watch_thread`` — blocking tail of a thread JSONL
- ``claude-worker thread watch`` parser + dispatch
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

import pytest

from claude_worker import cli as cw_cli
from claude_worker.thread_store import (
    append_message,
    create_thread,
    pair_thread_id,
    read_messages,
)


def _build_send_args(
    name: str,
    message: list[str],
    *,
    queue: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        name=name,
        message=message,
        queue=queue,
        show_response=False,
        show_full_response=False,
        chat=None,
        all_chats=False,
        broadcast=False,
        dry_run=False,
        verbose=False,
    )


class TestIsKnownThreadParticipant:
    def test_returns_true_when_in_participant_list(self) -> None:
        create_thread(participants=["worker-a", "rhc"], thread_type="chat")
        assert cw_cli._is_known_thread_participant("rhc") is True
        assert cw_cli._is_known_thread_participant("worker-a") is True

    def test_returns_false_for_unknown_name(self) -> None:
        create_thread(participants=["worker-a", "rhc"], thread_type="chat")
        assert cw_cli._is_known_thread_participant("someone-else") is False

    def test_returns_false_on_empty_index(self) -> None:
        assert cw_cli._is_known_thread_participant("anyone") is False


class TestSendToNonWorkerTarget:
    """``_send_to_single_worker`` must deliver to non-worker thread peers."""

    def test_send_to_thread_participant_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker -> interactive participant: no runtime dir, must still deliver."""
        # Sender is a worker named "worker-a"
        monkeypatch.setenv("CW_WORKER_NAME", "worker-a")

        # Create the pair thread explicitly so "rhc" is a known participant
        tid = pair_thread_id("worker-a", "rhc")
        create_thread(
            participants=["rhc", "worker-a"], thread_id=tid, thread_type="chat"
        )

        args = _build_send_args("rhc", ["hi rhc"])
        rc = cw_cli._send_to_single_worker("rhc", "hi rhc", args)

        assert rc == 0
        msgs = read_messages(tid)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hi rhc"
        assert msgs[0]["sender"] == "worker-a"

    def test_send_to_unknown_target_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Typo'd name that matches neither worker nor thread participant."""
        monkeypatch.setenv("CW_WORKER_NAME", "worker-a")
        # No thread exists containing "nobody"

        args = _build_send_args("nobody", ["hello"])
        rc = cw_cli._send_to_single_worker("nobody", "hello", args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "not a worker" in captured.err
        assert "not a known thread participant" in captured.err

    def test_send_to_non_worker_skips_status_gate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No runtime dir means no status check — must not raise or hang."""
        monkeypatch.setenv("CW_WORKER_NAME", "worker-a")
        tid = pair_thread_id("worker-a", "human")
        create_thread(
            participants=["human", "worker-a"], thread_id=tid, thread_type="chat"
        )

        args = _build_send_args("human", ["quick"])
        # If the status gate were still active this would try to read a
        # non-existent runtime/pid and either hang or crash.
        rc = cw_cli._send_to_single_worker("human", "quick", args)
        assert rc == 0


class TestWatchThread:
    def test_missing_thread_returns_1(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = cw_cli._watch_thread("does-not-exist", timeout=0.1)
        assert rc == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err

    def test_timeout_returns_2_when_idle(self) -> None:
        tid = create_thread(participants=["a", "b"], thread_type="chat")
        start = time.monotonic()
        rc = cw_cli._watch_thread(tid, timeout=0.3)
        elapsed = time.monotonic() - start
        assert rc == 2
        # Should honor the timeout roughly — not hang for seconds.
        assert elapsed < 2.0

    def test_picks_up_new_messages(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Concurrent append must be visible to the watcher."""
        tid = create_thread(participants=["a", "b"], thread_type="chat")

        # Pre-existing messages should NOT appear (we start from end).
        append_message(tid, sender="a", content="before-watch")

        def _delayed_write() -> None:
            time.sleep(0.2)  # let watch start first
            append_message(tid, sender="b", content="after-watch")

        t = threading.Thread(target=_delayed_write, daemon=True)
        t.start()

        # timeout is the idle ceiling — it resets on activity, so we
        # rely on the absence of further writes to exit.
        rc = cw_cli._watch_thread(tid, timeout=1.0)
        t.join(timeout=2.0)

        # Exit via timeout (code 2) is fine — we care about output.
        assert rc == 2
        captured = capsys.readouterr()
        assert "after-watch" in captured.out
        # Pre-existing message must not be re-printed.
        assert "before-watch" not in captured.out

    def test_since_id_resumes_from_marker(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """With --since, messages after the marker reappear; earlier ones don't."""
        tid = create_thread(participants=["a", "b"], thread_type="chat")
        m1 = append_message(tid, sender="a", content="msg1")
        m2 = append_message(tid, sender="a", content="msg2")
        append_message(tid, sender="a", content="msg3")

        rc = cw_cli._watch_thread(tid, since_id=m1["id"], timeout=0.3)
        assert rc == 2  # idle timeout after printing backlog after m1
        captured = capsys.readouterr()
        assert "msg1" not in captured.out  # marker itself excluded
        assert "msg2" in captured.out
        assert "msg3" in captured.out
        # Sanity: m2 came right after m1
        assert m2["id"] != m1["id"]
