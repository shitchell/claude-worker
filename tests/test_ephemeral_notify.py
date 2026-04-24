"""Tests for #084/D104: ephemeral worker completion notification.

Covers:
- _notify_parent_on_exit helper (clean exit, reap, no parent, empty log)
- _last_assistant_text_from_log helper
- End-to-end: ephemeral reap triggers parent notification via thread
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from claude_worker import manager as cw_manager
from claude_worker.thread_store import create_thread, pair_thread_id, read_messages


def _write_assistant_log(path: Path, text: str) -> None:
    """Write a minimal log with one assistant message."""
    entry = json.dumps(
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
    path.write_text(entry + "\n")


class TestLastAssistantText:
    def test_extracts_text(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        _write_assistant_log(log, "hello world")
        assert cw_manager._last_assistant_text_from_log(log) == "hello world"

    def test_missing_log(self, tmp_path: Path) -> None:
        assert cw_manager._last_assistant_text_from_log(tmp_path / "nope") == ""

    def test_empty_log(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        log.write_text("")
        assert cw_manager._last_assistant_text_from_log(log) == ""

    def test_truncates_long_text(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        _write_assistant_log(log, "x" * 500)
        result = cw_manager._last_assistant_text_from_log(log, max_chars=50)
        assert len(result) == 53  # 50 + "..."
        assert result.endswith("...")


class TestNotifyParentOnExit:
    def test_clean_exit_sends_notification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CW_PARENT_WORKER", "pm")
        log = tmp_path / "log"
        _write_assistant_log(log, "Ticket done. Commit abc123.")

        cw_manager._notify_parent_on_exit("impl-021", log, reaped=False)

        tid = pair_thread_id("impl-021", "pm")
        msgs = read_messages(tid)
        assert len(msgs) == 1
        assert "[worker-status]" in msgs[0]["content"]
        assert "impl-021" in msgs[0]["content"]
        assert "clean exit" in msgs[0]["content"]
        assert "Ticket done" in msgs[0]["content"]

    def test_reap_sends_notification_with_idle_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CW_PARENT_WORKER", "tl")
        log = tmp_path / "log"
        _write_assistant_log(log, "Implementation complete.")

        cw_manager._notify_parent_on_exit(
            "ephemeral-worker", log, reaped=True, idle_seconds=300
        )

        tid = pair_thread_id("ephemeral-worker", "tl")
        msgs = read_messages(tid)
        assert len(msgs) == 1
        assert "reaped after 5m idle" in msgs[0]["content"]
        assert "Implementation complete" in msgs[0]["content"]

    def test_no_notification_when_parent_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CW_PARENT_WORKER", raising=False)
        log = tmp_path / "log"
        _write_assistant_log(log, "should not notify")

        cw_manager._notify_parent_on_exit("worker1", log, reaped=False)

        # No thread should have been created
        from claude_worker.thread_store import load_index

        index = load_index()
        # Verify no pair-worker1-* thread exists
        assert not any("worker1" in k for k in index)

    def test_notification_with_empty_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CW_PARENT_WORKER", "pm")
        log = tmp_path / "log"
        log.write_text("")  # empty log

        cw_manager._notify_parent_on_exit("worker2", log, reaped=False)

        tid = pair_thread_id("pm", "worker2")
        msgs = read_messages(tid)
        assert len(msgs) == 1
        assert "[worker-status]" in msgs[0]["content"]
        assert "clean exit" in msgs[0]["content"]
        # No "Last message" line since log was empty
        assert "Last message" not in msgs[0]["content"]


class TestLifecycleEphemeralReapNotify:
    """End-to-end: ephemeral worker reap sends parent notification."""

    def test_reap_notifies_parent_via_thread(
        self,
        running_worker,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cw_manager, "EPHEMERAL_CHECK_INTERVAL_SECONDS", 0.2)
        monkeypatch.setattr(cw_manager, "EPHEMERAL_WRAPUP_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(cw_manager, "EPHEMERAL_WRAPUP_POLL_INTERVAL", 0.1)
        monkeypatch.setenv("CW_PARENT_WORKER", "test-parent")

        def _idempotent_create(name: str) -> Path:
            p = cw_manager.get_base_dir() / name
            p.mkdir(parents=True, exist_ok=True)
            fifo_path = p / "in"
            if not fifo_path.exists():
                os.mkfifo(str(fifo_path))
            return p

        monkeypatch.setattr(cw_manager, "create_runtime_dir", _idempotent_create)

        runtime_dir = cw_manager.create_runtime_dir("eph-notify")
        (runtime_dir / "ephemeral").write_text("0.3\n")

        handle = running_worker(name="eph-notify", initial_message=None)

        # Age the log past idle threshold
        time.sleep(0.2)
        log_path = runtime_dir / "log"
        if log_path.exists():
            past = time.time() - 5.0
            os.utime(log_path, (past, past))

        # Wait for reap
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not handle.thread.is_alive():
                break
            time.sleep(0.1)

        handle.stop()

        # Check the parent's pair thread for the notification
        tid = pair_thread_id("eph-notify", "test-parent")
        msgs = read_messages(tid)
        assert len(msgs) >= 1
        status_msgs = [m for m in msgs if "[worker-status]" in m["content"]]
        assert (
            len(status_msgs) >= 1
        ), f"Expected [worker-status] in thread {tid}, got: " + str(
            [m["content"][:100] for m in msgs]
        )
        assert "eph-notify" in status_msgs[0]["content"]
