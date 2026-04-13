"""Thread store for claude-worker.

Unified messaging primitive: threads replace FIFO direct writes,
queue dirs, chat transcripts, and per-consumer chat files. Each
thread is an append-only JSONL file + metadata in an atomic index.

Thread files: .cwork/threads/<id>.jsonl
Thread index: .cwork/threads/index.json
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

THREADS_DIR_NAME: str = "threads"
INDEX_FILE_NAME: str = "index.json"
MESSAGE_PREVIEW_LENGTH: int = 80


def _threads_dir(cwd: str) -> Path:
    """Return the threads directory for a project."""
    return Path(cwd) / ".cwork" / THREADS_DIR_NAME


def _index_path(cwd: str) -> Path:
    return _threads_dir(cwd) / INDEX_FILE_NAME


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _generate_thread_id() -> str:
    """Generate a short, unique thread ID."""
    return uuid.uuid4().hex[:12]


def load_index(cwd: str) -> dict:
    """Load the thread index. Returns {} if missing."""
    path = _index_path(cwd)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_index(cwd: str, index: dict) -> None:
    """Atomically save the thread index."""
    path = _index_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write via sibling .tmp file
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(index, indent=2) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def create_thread(
    cwd: str,
    participants: list[str],
    thread_type: str = "chat",
    thread_id: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Create a new thread. Returns the thread ID."""
    tid = thread_id or _generate_thread_id()
    threads_dir = _threads_dir(cwd)
    threads_dir.mkdir(parents=True, exist_ok=True)

    # Create empty JSONL file
    thread_file = threads_dir / f"{tid}.jsonl"
    thread_file.touch()

    # Update index
    index = load_index(cwd)
    index[tid] = {
        "participants": participants,
        "type": thread_type,
        "status": "open",
        "created": _now_iso(),
        "last_message": _now_iso(),
        "metadata": metadata or {},
    }
    _save_index(cwd, index)
    return tid


def append_message(
    cwd: str,
    thread_id: str,
    sender: str,
    content: str,
    tags: list[str] | None = None,
) -> dict:
    """Append a message to a thread. Returns the message dict."""
    threads_dir = _threads_dir(cwd)
    thread_file = threads_dir / f"{thread_id}.jsonl"

    if not thread_file.exists():
        raise FileNotFoundError(f"Thread '{thread_id}' not found")

    message = {
        "id": uuid.uuid4().hex[:16],
        "sender": sender,
        "timestamp": _now_iso(),
        "content": content,
        "tags": tags or [],
    }

    # Append-only write (O_APPEND for atomicity)
    with open(thread_file, "a") as f:
        f.write(json.dumps(message) + "\n")

    # Update index last_message timestamp
    index = load_index(cwd)
    if thread_id in index:
        index[thread_id]["last_message"] = message["timestamp"]
        _save_index(cwd, index)

    return message


def read_messages(
    cwd: str,
    thread_id: str,
    since_id: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Read messages from a thread.

    If since_id is provided, returns messages after that ID.
    If limit is provided, returns the last N messages.
    """
    thread_file = _threads_dir(cwd) / f"{thread_id}.jsonl"
    if not thread_file.exists():
        raise FileNotFoundError(f"Thread '{thread_id}' not found")

    messages: list[dict] = []
    past_marker = since_id is None
    try:
        with open(thread_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not past_marker:
                    if msg.get("id") == since_id:
                        past_marker = True
                    continue
                messages.append(msg)
    except OSError:
        pass

    if limit is not None and len(messages) > limit:
        messages = messages[-limit:]

    return messages


def list_threads(cwd: str, status: str | None = None) -> list[dict]:
    """List all threads, optionally filtered by status.

    Returns list of dicts with thread_id + index metadata.
    """
    index = load_index(cwd)
    result = []
    for tid, meta in index.items():
        if status and meta.get("status") != status:
            continue
        result.append({"thread_id": tid, **meta})
    return result


def close_thread(cwd: str, thread_id: str) -> None:
    """Close a thread (set status to 'closed')."""
    index = load_index(cwd)
    if thread_id not in index:
        raise KeyError(f"Thread '{thread_id}' not in index")
    index[thread_id]["status"] = "closed"
    _save_index(cwd, index)


def get_thread_participants(cwd: str, thread_id: str) -> list[str]:
    """Get the participant list for a thread."""
    index = load_index(cwd)
    if thread_id not in index:
        return []
    return index[thread_id].get("participants", [])
