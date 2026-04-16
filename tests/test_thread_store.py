"""Tests for the unified thread primitive (D74)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_worker import thread_store
from claude_worker.thread_store import (
    append_message,
    close_thread,
    create_thread,
    get_thread_participants,
    list_threads,
    load_index,
    migrate_from_project,
    read_messages,
)


@pytest.fixture(autouse=True)
def _isolate_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point thread_store at a tmp dir so tests never touch ~/.cwork/."""
    threads_dir = tmp_path / "threads"
    monkeypatch.setattr(thread_store, "_THREADS_DIR_OVERRIDE", threads_dir)


def test_create_thread(tmp_path: Path) -> None:
    """Create thread, verify JSONL file exists, index has correct entry."""
    tid = create_thread(participants=["alice", "bob"], thread_type="chat")

    # JSONL file exists
    threads_dir = tmp_path / "threads"
    thread_file = threads_dir / f"{tid}.jsonl"
    assert thread_file.exists()

    # Index has correct metadata
    index = load_index()
    assert tid in index
    entry = index[tid]
    assert entry["participants"] == ["alice", "bob"]
    assert entry["type"] == "chat"
    assert entry["status"] == "open"
    assert "created" in entry
    assert "last_message" in entry


def test_create_thread_custom_id(tmp_path: Path) -> None:
    """Create with explicit ID, verify it's used."""
    tid = create_thread(participants=[], thread_id="custom-id-123")
    assert tid == "custom-id-123"

    threads_dir = tmp_path / "threads"
    thread_file = threads_dir / "custom-id-123.jsonl"
    assert thread_file.exists()

    index = load_index()
    assert "custom-id-123" in index


def test_append_message(tmp_path: Path) -> None:
    """Create thread, append message, verify JSONL content."""
    tid = create_thread(participants=["alice"])

    msg = append_message(tid, sender="alice", content="hello world")

    # Message dict has expected fields
    assert "id" in msg
    assert msg["sender"] == "alice"
    assert msg["content"] == "hello world"
    assert "timestamp" in msg
    assert msg["tags"] == []

    # JSONL file has one line
    threads_dir = tmp_path / "threads"
    thread_file = threads_dir / f"{tid}.jsonl"
    lines = [l for l in thread_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["sender"] == "alice"
    assert parsed["content"] == "hello world"


def test_append_multiple_messages(tmp_path: Path) -> None:
    """Append 3 messages, verify JSONL has 3 lines in order."""
    tid = create_thread(participants=["a", "b"])

    msg1 = append_message(tid, "a", "first")
    msg2 = append_message(tid, "b", "second")
    msg3 = append_message(tid, "a", "third")

    threads_dir = tmp_path / "threads"
    thread_file = threads_dir / f"{tid}.jsonl"
    lines = [l for l in thread_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 3

    parsed = [json.loads(l) for l in lines]
    assert parsed[0]["content"] == "first"
    assert parsed[1]["content"] == "second"
    assert parsed[2]["content"] == "third"


def test_read_messages_all(tmp_path: Path) -> None:
    """Append 3, read all -> returns 3."""
    tid = create_thread(participants=[])
    append_message(tid, "x", "one")
    append_message(tid, "x", "two")
    append_message(tid, "x", "three")

    messages = read_messages(tid)
    assert len(messages) == 3
    assert messages[0]["content"] == "one"
    assert messages[2]["content"] == "three"


def test_read_messages_since(tmp_path: Path) -> None:
    """Append 3, read since msg[0].id -> returns 2."""
    tid = create_thread(participants=[])
    msg0 = append_message(tid, "x", "zero")
    append_message(tid, "x", "one")
    append_message(tid, "x", "two")

    messages = read_messages(tid, since_id=msg0["id"])
    assert len(messages) == 2
    assert messages[0]["content"] == "one"
    assert messages[1]["content"] == "two"


def test_read_messages_limit(tmp_path: Path) -> None:
    """Append 5, read with limit=2 -> returns last 2."""
    tid = create_thread(participants=[])
    for i in range(5):
        append_message(tid, "x", f"msg-{i}")

    messages = read_messages(tid, limit=2)
    assert len(messages) == 2
    assert messages[0]["content"] == "msg-3"
    assert messages[1]["content"] == "msg-4"


def test_read_nonexistent_thread(tmp_path: Path) -> None:
    """Read from nonexistent thread -> FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        read_messages("does-not-exist")


def test_list_threads(tmp_path: Path) -> None:
    """Create 3 threads, list -> returns all 3."""
    create_thread(participants=["a"], thread_id="t1")
    create_thread(participants=["b"], thread_id="t2")
    create_thread(participants=["c"], thread_id="t3")

    threads = list_threads()
    assert len(threads) == 3
    ids = {t["thread_id"] for t in threads}
    assert ids == {"t1", "t2", "t3"}


def test_list_threads_filter_status(tmp_path: Path) -> None:
    """Create 2 open + 1 closed, filter open -> 2."""
    create_thread(participants=[], thread_id="open1")
    create_thread(participants=[], thread_id="open2")
    create_thread(participants=[], thread_id="closed1")
    close_thread("closed1")

    open_threads = list_threads(status="open")
    assert len(open_threads) == 2
    ids = {t["thread_id"] for t in open_threads}
    assert ids == {"open1", "open2"}

    closed_threads = list_threads(status="closed")
    assert len(closed_threads) == 1
    assert closed_threads[0]["thread_id"] == "closed1"


def test_close_thread(tmp_path: Path) -> None:
    """Create, close, verify status in index."""
    tid = create_thread(participants=[])
    assert load_index()[tid]["status"] == "open"

    close_thread(tid)
    assert load_index()[tid]["status"] == "closed"


def test_close_nonexistent(tmp_path: Path) -> None:
    """Close unknown ID -> KeyError."""
    with pytest.raises(KeyError):
        close_thread("no-such-thread")


def test_get_participants(tmp_path: Path) -> None:
    """Create with participants, verify returned list."""
    tid = create_thread(participants=["alice", "bob", "charlie"])

    result = get_thread_participants(tid)
    assert result == ["alice", "bob", "charlie"]


def test_get_participants_nonexistent(tmp_path: Path) -> None:
    """Get participants for nonexistent thread -> empty list."""
    result = get_thread_participants("nope")
    assert result == []


def test_index_survives_crash(tmp_path: Path) -> None:
    """Write index, read back -> identical (tests atomic write)."""
    tid = create_thread(
        participants=["a", "b"],
        thread_type="request",
        metadata={"priority": "high"},
    )

    # Read the raw index file and parse it
    index_file = tmp_path / "threads" / "index.json"
    raw = json.loads(index_file.read_text())

    # Compare with load_index
    loaded = load_index()
    assert raw == loaded
    assert tid in loaded
    assert loaded[tid]["type"] == "request"
    assert loaded[tid]["metadata"] == {"priority": "high"}


def test_append_to_nonexistent_thread(tmp_path: Path) -> None:
    """Append to missing thread -> FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        append_message("ghost-thread", "sender", "content")


# -- Migration tests -------------------------------------------------------


def test_migrate_from_project(tmp_path: Path) -> None:
    """Migrate per-project threads to global storage."""
    # Create per-project thread structure
    project_dir = tmp_path / "project"
    project_threads = project_dir / ".cwork" / "threads"
    project_threads.mkdir(parents=True)

    # Write a thread file
    (project_threads / "pair-a-b.jsonl").write_text(
        json.dumps({"id": "m1", "sender": "a", "content": "hello"}) + "\n"
    )
    # Write an index
    project_index = {
        "pair-a-b": {
            "participants": ["a", "b"],
            "type": "chat",
            "status": "open",
            "created": "2026-04-15T00:00:00Z",
            "last_message": "2026-04-15T00:00:00Z",
            "metadata": {},
        }
    }
    (project_threads / "index.json").write_text(json.dumps(project_index))

    count = migrate_from_project(str(project_dir))
    assert count == 1

    # Verify the file was moved to global dir
    global_dir = tmp_path / "threads"
    assert (global_dir / "pair-a-b.jsonl").exists()

    # Verify the index was merged
    index = load_index()
    assert "pair-a-b" in index
    assert index["pair-a-b"]["participants"] == ["a", "b"]

    # Verify the project threads dir was cleaned up
    assert not project_threads.exists()


def test_migrate_idempotent(tmp_path: Path) -> None:
    """Re-running migration after files are already global skips them."""
    # Pre-create the global thread file
    global_dir = tmp_path / "threads"
    global_dir.mkdir(parents=True)
    (global_dir / "pair-a-b.jsonl").write_text("existing content\n")

    # Create a project thread with the same ID
    project_dir = tmp_path / "project"
    project_threads = project_dir / ".cwork" / "threads"
    project_threads.mkdir(parents=True)
    (project_threads / "pair-a-b.jsonl").write_text("project content\n")
    (project_threads / "index.json").write_text("{}")

    count = migrate_from_project(str(project_dir))
    assert count == 0  # skipped

    # Global file should still have original content
    assert (global_dir / "pair-a-b.jsonl").read_text() == "existing content\n"


def test_migrate_no_project_threads(tmp_path: Path) -> None:
    """No project threads dir -> returns 0."""
    count = migrate_from_project(str(tmp_path / "nonexistent"))
    assert count == 0
