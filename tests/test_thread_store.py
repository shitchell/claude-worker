"""Tests for the unified thread primitive (D74)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_worker.thread_store import (
    append_message,
    close_thread,
    create_thread,
    get_thread_participants,
    list_threads,
    load_index,
    read_messages,
)


def test_create_thread(tmp_path: Path) -> None:
    """Create thread, verify JSONL file exists, index has correct entry."""
    cwd = str(tmp_path)
    tid = create_thread(cwd, participants=["alice", "bob"], thread_type="chat")

    # JSONL file exists
    thread_file = tmp_path / ".cwork" / "threads" / f"{tid}.jsonl"
    assert thread_file.exists()

    # Index has correct metadata
    index = load_index(cwd)
    assert tid in index
    entry = index[tid]
    assert entry["participants"] == ["alice", "bob"]
    assert entry["type"] == "chat"
    assert entry["status"] == "open"
    assert "created" in entry
    assert "last_message" in entry


def test_create_thread_custom_id(tmp_path: Path) -> None:
    """Create with explicit ID, verify it's used."""
    cwd = str(tmp_path)
    tid = create_thread(cwd, participants=[], thread_id="custom-id-123")
    assert tid == "custom-id-123"

    thread_file = tmp_path / ".cwork" / "threads" / "custom-id-123.jsonl"
    assert thread_file.exists()

    index = load_index(cwd)
    assert "custom-id-123" in index


def test_append_message(tmp_path: Path) -> None:
    """Create thread, append message, verify JSONL content."""
    cwd = str(tmp_path)
    tid = create_thread(cwd, participants=["alice"])

    msg = append_message(cwd, tid, sender="alice", content="hello world")

    # Message dict has expected fields
    assert "id" in msg
    assert msg["sender"] == "alice"
    assert msg["content"] == "hello world"
    assert "timestamp" in msg
    assert msg["tags"] == []

    # JSONL file has one line
    thread_file = tmp_path / ".cwork" / "threads" / f"{tid}.jsonl"
    lines = [l for l in thread_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["sender"] == "alice"
    assert parsed["content"] == "hello world"


def test_append_multiple_messages(tmp_path: Path) -> None:
    """Append 3 messages, verify JSONL has 3 lines in order."""
    cwd = str(tmp_path)
    tid = create_thread(cwd, participants=["a", "b"])

    msg1 = append_message(cwd, tid, "a", "first")
    msg2 = append_message(cwd, tid, "b", "second")
    msg3 = append_message(cwd, tid, "a", "third")

    thread_file = tmp_path / ".cwork" / "threads" / f"{tid}.jsonl"
    lines = [l for l in thread_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 3

    parsed = [json.loads(l) for l in lines]
    assert parsed[0]["content"] == "first"
    assert parsed[1]["content"] == "second"
    assert parsed[2]["content"] == "third"


def test_read_messages_all(tmp_path: Path) -> None:
    """Append 3, read all -> returns 3."""
    cwd = str(tmp_path)
    tid = create_thread(cwd, participants=[])
    append_message(cwd, tid, "x", "one")
    append_message(cwd, tid, "x", "two")
    append_message(cwd, tid, "x", "three")

    messages = read_messages(cwd, tid)
    assert len(messages) == 3
    assert messages[0]["content"] == "one"
    assert messages[2]["content"] == "three"


def test_read_messages_since(tmp_path: Path) -> None:
    """Append 3, read since msg[0].id -> returns 2."""
    cwd = str(tmp_path)
    tid = create_thread(cwd, participants=[])
    msg0 = append_message(cwd, tid, "x", "zero")
    append_message(cwd, tid, "x", "one")
    append_message(cwd, tid, "x", "two")

    messages = read_messages(cwd, tid, since_id=msg0["id"])
    assert len(messages) == 2
    assert messages[0]["content"] == "one"
    assert messages[1]["content"] == "two"


def test_read_messages_limit(tmp_path: Path) -> None:
    """Append 5, read with limit=2 -> returns last 2."""
    cwd = str(tmp_path)
    tid = create_thread(cwd, participants=[])
    for i in range(5):
        append_message(cwd, tid, "x", f"msg-{i}")

    messages = read_messages(cwd, tid, limit=2)
    assert len(messages) == 2
    assert messages[0]["content"] == "msg-3"
    assert messages[1]["content"] == "msg-4"


def test_read_nonexistent_thread(tmp_path: Path) -> None:
    """Read from nonexistent thread -> FileNotFoundError."""
    cwd = str(tmp_path)
    with pytest.raises(FileNotFoundError):
        read_messages(cwd, "does-not-exist")


def test_list_threads(tmp_path: Path) -> None:
    """Create 3 threads, list -> returns all 3."""
    cwd = str(tmp_path)
    create_thread(cwd, participants=["a"], thread_id="t1")
    create_thread(cwd, participants=["b"], thread_id="t2")
    create_thread(cwd, participants=["c"], thread_id="t3")

    threads = list_threads(cwd)
    assert len(threads) == 3
    ids = {t["thread_id"] for t in threads}
    assert ids == {"t1", "t2", "t3"}


def test_list_threads_filter_status(tmp_path: Path) -> None:
    """Create 2 open + 1 closed, filter open -> 2."""
    cwd = str(tmp_path)
    create_thread(cwd, participants=[], thread_id="open1")
    create_thread(cwd, participants=[], thread_id="open2")
    create_thread(cwd, participants=[], thread_id="closed1")
    close_thread(cwd, "closed1")

    open_threads = list_threads(cwd, status="open")
    assert len(open_threads) == 2
    ids = {t["thread_id"] for t in open_threads}
    assert ids == {"open1", "open2"}

    closed_threads = list_threads(cwd, status="closed")
    assert len(closed_threads) == 1
    assert closed_threads[0]["thread_id"] == "closed1"


def test_close_thread(tmp_path: Path) -> None:
    """Create, close, verify status in index."""
    cwd = str(tmp_path)
    tid = create_thread(cwd, participants=[])
    assert load_index(cwd)[tid]["status"] == "open"

    close_thread(cwd, tid)
    assert load_index(cwd)[tid]["status"] == "closed"


def test_close_nonexistent(tmp_path: Path) -> None:
    """Close unknown ID -> KeyError."""
    cwd = str(tmp_path)
    with pytest.raises(KeyError):
        close_thread(cwd, "no-such-thread")


def test_get_participants(tmp_path: Path) -> None:
    """Create with participants, verify returned list."""
    cwd = str(tmp_path)
    tid = create_thread(cwd, participants=["alice", "bob", "charlie"])

    result = get_thread_participants(cwd, tid)
    assert result == ["alice", "bob", "charlie"]


def test_get_participants_nonexistent(tmp_path: Path) -> None:
    """Get participants for nonexistent thread -> empty list."""
    cwd = str(tmp_path)
    result = get_thread_participants(cwd, "nope")
    assert result == []


def test_index_survives_crash(tmp_path: Path) -> None:
    """Write index, read back -> identical (tests atomic write)."""
    cwd = str(tmp_path)
    tid = create_thread(
        cwd,
        participants=["a", "b"],
        thread_type="request",
        metadata={"priority": "high"},
    )

    # Read the raw index file and parse it
    index_file = tmp_path / ".cwork" / "threads" / "index.json"
    raw = json.loads(index_file.read_text())

    # Compare with load_index
    loaded = load_index(cwd)
    assert raw == loaded
    assert tid in loaded
    assert loaded[tid]["type"] == "request"
    assert loaded[tid]["metadata"] == {"priority": "high"}


def test_append_to_nonexistent_thread(tmp_path: Path) -> None:
    """Append to missing thread -> FileNotFoundError."""
    cwd = str(tmp_path)
    with pytest.raises(FileNotFoundError):
        append_message(cwd, "ghost-thread", "sender", "content")
