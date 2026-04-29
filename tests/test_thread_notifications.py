"""Tests for thread notification injection (Phase 2 of inbox/threads)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_worker import thread_store
from claude_worker.manager import (
    THREAD_NOTIFICATION_PREVIEW_LENGTH,
    _read_new_messages_since_size,
    check_thread_changes,
    snapshot_threads,
)
from claude_worker.thread_store import append_message, create_thread


@pytest.fixture(autouse=True)
def _isolate_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point thread_store at a tmp dir so tests never touch ~/.cwork/."""
    threads_dir = tmp_path / "threads"
    monkeypatch.setattr(thread_store, "_THREADS_DIR_OVERRIDE", threads_dir)


# -- Helpers ---------------------------------------------------------------


def _install_fd_capture(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Monkeypatch os.open/write/close to capture FIFO writes.

    Avoids real FIFO I/O: os.open returns a sentinel fd, os.write on
    that fd appends to the returned list, os.close on it is a no-op.
    Real calls on any other fd pass through unchanged.

    Returns the list that will be populated with captured payloads.
    """
    writes: list[bytes] = []
    real_open = os.open
    real_write = os.write
    real_close = os.close
    fake_fd = 999_999

    def mock_open(path, flags, *args, **kwargs):
        return fake_fd

    def mock_write(fd, data):
        if fd == fake_fd:
            writes.append(data)
            return len(data)
        return real_write(fd, data)

    def mock_close(fd):
        if fd == fake_fd:
            return
        return real_close(fd)

    monkeypatch.setattr(os, "open", mock_open)
    monkeypatch.setattr(os, "write", mock_write)
    monkeypatch.setattr(os, "close", mock_close)
    return writes


# -- snapshot_threads ------------------------------------------------------


def test_snapshot_threads_empty(tmp_path: Path):
    """Missing threads dir returns {}."""
    assert snapshot_threads() == {}


def test_snapshot_threads_captures_files(tmp_path: Path):
    """Each *.jsonl file produces a (mtime, size) entry keyed by stem."""
    t1 = create_thread(participants=["a", "b"])
    t2 = create_thread(participants=["b", "c"])

    snap = snapshot_threads()
    assert set(snap.keys()) == {t1, t2}
    for tid, (mtime, size) in snap.items():
        assert isinstance(mtime, float)
        assert isinstance(size, int)
        assert size >= 0


def test_snapshot_threads_ignores_index_json(tmp_path: Path):
    """Only *.jsonl files are tracked, never index.json."""
    tid = create_thread(participants=["a"])
    snap = snapshot_threads()
    assert tid in snap
    assert "index" not in snap


# -- check_thread_changes --------------------------------------------------


def test_check_thread_changes_first_scan_no_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Empty prev_snapshot -> return new snapshot without injecting."""
    tid = create_thread(participants=["pm", "tl"])
    append_message(tid, sender="pm", content="hello")

    writes = _install_fd_capture(monkeypatch)
    new_snap = check_thread_changes("tl", tmp_path / "in", {})

    assert writes == []
    assert tid in new_snap


def test_check_thread_changes_notifies_participant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Participant receives a [system:new-message] notification."""
    tid = create_thread(participants=["pm", "tl"])
    prev = snapshot_threads()

    append_message(tid, sender="pm", content="hello tl")

    writes = _install_fd_capture(monkeypatch)
    new_snap = check_thread_changes("tl", tmp_path / "in", prev)

    assert len(writes) == 1
    payload = writes[0].decode()
    # The envelope is a JSON line terminated by newline
    assert payload.endswith("\n")
    envelope = json.loads(payload.strip())
    content = envelope["message"]["content"]
    assert "[system:new-message]" in content
    assert f"Thread {tid}" in content
    assert "from pm" in content
    assert "hello tl" in content
    assert new_snap[tid][1] > prev[tid][1]


def test_check_thread_changes_ignores_own_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Worker that sent the message is not notified about it."""
    tid = create_thread(participants=["pm", "tl"])
    prev = snapshot_threads()
    append_message(tid, sender="pm", content="hello tl")

    writes = _install_fd_capture(monkeypatch)
    # Worker is pm — the sender — so no notification
    check_thread_changes("pm", tmp_path / "in", prev)

    assert writes == []


def test_check_thread_changes_ignores_non_participant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Worker not in participants list is never notified."""
    tid = create_thread(participants=["pm", "tl"])
    prev = snapshot_threads()
    append_message(tid, sender="pm", content="hello tl")

    writes = _install_fd_capture(monkeypatch)
    check_thread_changes("rhc", tmp_path / "in", prev)

    assert writes == []


def test_check_thread_changes_preview_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Content longer than PREVIEW_LENGTH is truncated and an explicit
    instruction line is appended (D108)."""
    tid = create_thread(participants=["pm", "tl"])
    prev = snapshot_threads()

    long_content = "x" * (THREAD_NOTIFICATION_PREVIEW_LENGTH + 50)
    append_message(tid, sender="pm", content=long_content)

    writes = _install_fd_capture(monkeypatch)
    check_thread_changes("tl", tmp_path / "in", prev)

    assert len(writes) == 1
    envelope = json.loads(writes[0].decode().strip())
    content = envelope["message"]["content"]
    # Preview cuts off at PREVIEW_LENGTH and is followed by "..." then
    # the explicit truncation instruction line on its own line.
    truncated = "x" * THREAD_NOTIFICATION_PREVIEW_LENGTH
    assert truncated in content
    assert "..." in content
    assert "[truncated" in content
    # The full original string should not appear in the notification
    assert long_content not in content


def test_check_thread_changes_no_truncation_at_exact_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Content at exactly PREVIEW_LENGTH is not truncated."""
    tid = create_thread(participants=["pm", "tl"])
    prev = snapshot_threads()

    exact_content = "y" * THREAD_NOTIFICATION_PREVIEW_LENGTH
    append_message(tid, sender="pm", content=exact_content)

    writes = _install_fd_capture(monkeypatch)
    check_thread_changes("tl", tmp_path / "in", prev)

    assert len(writes) == 1
    envelope = json.loads(writes[0].decode().strip())
    assert "..." not in envelope["message"]["content"]


def test_check_thread_changes_empty_worker_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Empty worker_name returns snapshot but never notifies."""
    tid = create_thread(participants=["pm", "tl"])
    prev = snapshot_threads()
    append_message(tid, sender="pm", content="hello")

    writes = _install_fd_capture(monkeypatch)
    new_snap = check_thread_changes("", tmp_path / "in", prev)

    assert writes == []
    assert tid in new_snap


def test_check_thread_changes_multiple_new_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Each new message since the last snapshot produces one notification."""
    tid = create_thread(participants=["pm", "tl"])
    prev = snapshot_threads()

    append_message(tid, sender="pm", content="first")
    append_message(tid, sender="pm", content="second")
    append_message(tid, sender="pm", content="third")

    writes = _install_fd_capture(monkeypatch)
    check_thread_changes("tl", tmp_path / "in", prev)

    assert len(writes) == 3
    contents = [json.loads(w.decode().strip())["message"]["content"] for w in writes]
    assert any("first" in c for c in contents)
    assert any("second" in c for c in contents)
    assert any("third" in c for c in contents)


def test_check_thread_changes_mixed_senders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Own messages skipped, others notified, across a mixed batch."""
    tid = create_thread(participants=["pm", "tl"])
    prev = snapshot_threads()

    append_message(tid, sender="pm", content="from pm")
    append_message(tid, sender="tl", content="from tl self")
    append_message(tid, sender="pm", content="from pm again")

    writes = _install_fd_capture(monkeypatch)
    check_thread_changes("tl", tmp_path / "in", prev)

    # 2 notifications (pm messages only), tl's own is skipped
    assert len(writes) == 2
    for w in writes:
        envelope = json.loads(w.decode().strip())
        assert "from pm" in envelope["message"]["content"]


def test_check_thread_changes_no_growth_no_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If file size is unchanged, no notification is injected."""
    tid = create_thread(participants=["pm", "tl"])
    append_message(tid, sender="pm", content="already here")
    prev = snapshot_threads()

    writes = _install_fd_capture(monkeypatch)
    new_snap = check_thread_changes("tl", tmp_path / "in", prev)

    assert writes == []
    assert new_snap[tid] == prev[tid]


# -- _read_new_messages_since_size ----------------------------------------


def test_read_new_messages_since_size_partial(tmp_path: Path):
    """Only messages appended after the snapshot size are returned."""
    tid = create_thread(participants=["a"])
    append_message(tid, sender="a", content="one")
    append_message(tid, sender="a", content="two")

    snap = snapshot_threads()
    size_after_two = snap[tid][1]

    append_message(tid, sender="a", content="three")

    new_msgs = _read_new_messages_since_size(tid, size_after_two)
    assert len(new_msgs) == 1
    assert new_msgs[0]["content"] == "three"


def test_read_new_messages_since_size_zero_reads_all(tmp_path: Path):
    """old_size=0 returns every message in the thread."""
    tid = create_thread(participants=["a"])
    append_message(tid, sender="a", content="one")
    append_message(tid, sender="a", content="two")

    new_msgs = _read_new_messages_since_size(tid, 0)
    assert [m["content"] for m in new_msgs] == ["one", "two"]


def test_read_new_messages_since_size_missing_thread(tmp_path: Path):
    """Missing thread file returns []."""
    assert _read_new_messages_since_size("nonexistent", 0) == []


def test_read_new_messages_since_size_skips_corrupt_lines(tmp_path: Path):
    """A malformed JSONL line is skipped, valid lines are returned."""
    tid = create_thread(participants=["a"])
    threads_dir = tmp_path / "threads"
    thread_file = threads_dir / f"{tid}.jsonl"
    # Manually append a garbage line between two valid ones
    with open(thread_file, "a") as f:
        f.write(json.dumps({"id": "1", "sender": "a", "content": "ok1"}) + "\n")
        f.write("this is not json\n")
        f.write(json.dumps({"id": "2", "sender": "a", "content": "ok2"}) + "\n")

    new_msgs = _read_new_messages_since_size(tid, 0)
    assert [m["content"] for m in new_msgs] == ["ok1", "ok2"]


# -- Loud truncation hint (#090, D108) ------------------------------------


def test_long_message_persists_full_content(tmp_path: Path):
    """A >1KB message is persisted to the thread JSONL verbatim — the
    truncation lives in the FIFO notification, not in storage."""
    tid = create_thread(participants=["pm", "tl"])
    long_content = "abcdefghij" * 200  # 2000 chars
    assert len(long_content) > 1024
    append_message(tid, sender="pm", content=long_content)

    # Read the thread file directly and verify the full content survived.
    thread_file = tmp_path / "threads" / f"{tid}.jsonl"
    lines = thread_file.read_text().splitlines()
    assert len(lines) == 1
    msg = json.loads(lines[0])
    assert msg["content"] == long_content


def test_long_message_notification_includes_truncation_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Long-message notification carries the 200-char preview AND an
    explicit instruction line naming the recipient and thread_id."""
    tid = create_thread(participants=["pm", "tl"])
    prev = snapshot_threads()

    long_content = "Z" * 1500
    append_message(tid, sender="pm", content=long_content)

    writes = _install_fd_capture(monkeypatch)
    check_thread_changes("tl", tmp_path / "in", prev)

    assert len(writes) == 1
    envelope = json.loads(writes[0].decode().strip())
    content = envelope["message"]["content"]
    # 200-char preview present
    assert "Z" * THREAD_NOTIFICATION_PREVIEW_LENGTH in content
    # Truncation marker + literal instruction line on its own line
    assert "...\n[truncated" in content
    assert "claude-worker thread read" in content
    # Names the recipient and the thread_id
    assert f"thread read tl --thread {tid}" in content
    # Full content not inlined
    assert long_content not in content


def test_short_message_no_truncation_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A message under PREVIEW_LENGTH gets no truncation instruction line."""
    tid = create_thread(participants=["pm", "tl"])
    prev = snapshot_threads()

    short = "hello, this is a short message"
    assert len(short) < THREAD_NOTIFICATION_PREVIEW_LENGTH
    append_message(tid, sender="pm", content=short)

    writes = _install_fd_capture(monkeypatch)
    check_thread_changes("tl", tmp_path / "in", prev)

    assert len(writes) == 1
    envelope = json.loads(writes[0].decode().strip())
    content = envelope["message"]["content"]
    assert short in content
    assert "..." not in content
    assert "[truncated" not in content
    assert "claude-worker thread read" not in content


def test_stdin_path_long_message(tmp_path: Path):
    """The persistence test (#1) re-cast for the stdin-style send code
    path: append_message is the same primitive both positional-arg and
    stdin paths feed into in _send_to_single_worker. Verifies a >1KB
    message stays intact across the path."""
    tid = create_thread(participants=["sender", "recv"])
    big = ("stdin-payload " * 100).strip()  # ~1.4KB
    assert len(big) > 1024
    append_message(tid, sender="sender", content=big)

    thread_file = tmp_path / "threads" / f"{tid}.jsonl"
    msgs = [json.loads(line) for line in thread_file.read_text().splitlines()]
    assert msgs[-1]["content"] == big


def test_thread_monitor_injection_long_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Direct test of check_thread_changes with a synthetic 5KB thread
    message: assert the FIFO write contains the truncation hint and the
    thread JSONL is intact."""
    tid = create_thread(participants=["pm", "tl"])
    prev = snapshot_threads()

    huge = "Q" * 5000
    append_message(tid, sender="pm", content=huge)

    writes = _install_fd_capture(monkeypatch)
    check_thread_changes("tl", tmp_path / "in", prev)

    # Notification carries the truncation hint
    assert len(writes) == 1
    envelope = json.loads(writes[0].decode().strip())
    content = envelope["message"]["content"]
    assert "[truncated" in content
    assert "claude-worker thread read" in content
    assert f"--thread {tid}" in content

    # Thread JSONL still has the full 5KB content verbatim
    thread_file = tmp_path / "threads" / f"{tid}.jsonl"
    msgs = [json.loads(line) for line in thread_file.read_text().splitlines()]
    assert msgs[-1]["content"] == huge
