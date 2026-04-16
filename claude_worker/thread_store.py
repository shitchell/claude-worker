"""Thread store for claude-worker.

Unified messaging primitive: threads replace FIFO direct writes,
queue dirs, chat transcripts, and per-consumer chat files. Each
thread is an append-only JSONL file + metadata in an atomic index.

Thread files: ~/.cwork/threads/<id>.jsonl
Thread index: ~/.cwork/threads/index.json
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

THREADS_DIR_NAME: str = "threads"
INDEX_FILE_NAME: str = "index.json"
MESSAGE_PREVIEW_LENGTH: int = 80

# Test-injection override: when set, _threads_dir() returns this path
# instead of the real ~/.cwork/threads/.  Same pattern as get_base_dir
# for worker dirs.
_THREADS_DIR_OVERRIDE: Path | None = None


def _threads_dir() -> Path:
    """Return the global threads directory (~/.cwork/threads/)."""
    if _THREADS_DIR_OVERRIDE is not None:
        return _THREADS_DIR_OVERRIDE
    return Path.home() / ".cwork" / THREADS_DIR_NAME


def _index_path() -> Path:
    return _threads_dir() / INDEX_FILE_NAME


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _generate_thread_id() -> str:
    """Generate a short, unique thread ID."""
    return uuid.uuid4().hex[:12]


def load_index() -> dict:
    """Load the thread index. Returns {} if missing."""
    path = _index_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_index(index: dict) -> None:
    """Atomically save the thread index."""
    path = _index_path()
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
    participants: list[str],
    thread_type: str = "chat",
    thread_id: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Create a new thread. Returns the thread ID."""
    tid = thread_id or _generate_thread_id()
    threads_dir = _threads_dir()
    threads_dir.mkdir(parents=True, exist_ok=True)

    # Create empty JSONL file
    thread_file = threads_dir / f"{tid}.jsonl"
    thread_file.touch()

    # Update index
    index = load_index()
    index[tid] = {
        "participants": participants,
        "type": thread_type,
        "status": "open",
        "created": _now_iso(),
        "last_message": _now_iso(),
        "metadata": metadata or {},
    }
    _save_index(index)
    return tid


def append_message(
    thread_id: str,
    sender: str,
    content: str,
    tags: list[str] | None = None,
) -> dict:
    """Append a message to a thread. Returns the message dict."""
    threads_dir = _threads_dir()
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
    index = load_index()
    if thread_id in index:
        index[thread_id]["last_message"] = message["timestamp"]
        _save_index(index)

    return message


def read_messages(
    thread_id: str,
    since_id: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Read messages from a thread.

    If since_id is provided, returns messages after that ID.
    If limit is provided, returns the last N messages.
    """
    thread_file = _threads_dir() / f"{thread_id}.jsonl"
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


def list_threads(status: str | None = None) -> list[dict]:
    """List all threads, optionally filtered by status.

    Returns list of dicts with thread_id + index metadata.
    """
    index = load_index()
    result = []
    for tid, meta in index.items():
        if status and meta.get("status") != status:
            continue
        result.append({"thread_id": tid, **meta})
    return result


def close_thread(thread_id: str) -> None:
    """Close a thread (set status to 'closed')."""
    index = load_index()
    if thread_id not in index:
        raise KeyError(f"Thread '{thread_id}' not in index")
    index[thread_id]["status"] = "closed"
    _save_index(index)


def get_thread_participants(thread_id: str) -> list[str]:
    """Get the participant list for a thread."""
    index = load_index()
    if thread_id not in index:
        return []
    return index[thread_id].get("participants", [])


# -- Active-thread routing helpers (Phase 3) -------------------------------


def pair_thread_id(sender: str, recipient: str) -> str:
    """Deterministic thread ID for a sender-recipient pair.

    Sorted so the ID is symmetric: ``pair_thread_id(A, B) ==
    pair_thread_id(B, A)``. Gives every pair of workers exactly one
    shared thread for direct messaging.
    """
    a, b = sorted([sender or "?", recipient or "?"])
    return f"pair-{a}-{b}"


def chat_thread_id(chat_id: str) -> str:
    """Thread ID for a PM chat tag (multi-consumer routing)."""
    return f"chat-{chat_id}"


def ensure_thread(
    thread_id: str,
    participants: list[str],
    thread_type: str = "chat",
) -> str:
    """Create a thread if missing, else extend participants if new.

    Idempotent — safe to call on every send. Returns the thread ID.
    When the thread already exists, any participants not already in
    the stored list are appended (order-preserving).
    """
    index = load_index()
    if thread_id in index:
        existing = index[thread_id].get("participants") or []
        updated = list(existing)
        for p in participants:
            if p not in updated:
                updated.append(p)
        if updated != existing:
            index[thread_id]["participants"] = updated
            _save_index(index)
        return thread_id
    return create_thread(
        participants=participants,
        thread_type=thread_type,
        thread_id=thread_id,
    )


# -- Migration helper (per-project → global) ------------------------------


def migrate_from_project(project_cwd: str) -> int:
    """Migrate per-project threads to global storage.

    Moves .jsonl files and merges index.json. Returns the number
    of threads migrated. Idempotent — skips files that already exist
    in the global store.
    """
    project_threads = Path(project_cwd) / ".cwork" / THREADS_DIR_NAME
    if not project_threads.exists():
        return 0

    global_dir = _threads_dir()
    global_dir.mkdir(parents=True, exist_ok=True)

    # Merge index entries
    project_index_path = project_threads / INDEX_FILE_NAME
    project_index: dict = {}
    if project_index_path.exists():
        try:
            project_index = json.loads(project_index_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    global_index = load_index()
    merged_count = 0

    for jsonl_file in project_threads.glob("*.jsonl"):
        thread_id = jsonl_file.stem
        global_file = global_dir / jsonl_file.name
        if global_file.exists():
            continue  # idempotent — skip already-migrated
        shutil.move(str(jsonl_file), str(global_file))
        merged_count += 1

        # Merge this thread's index entry if present
        if thread_id in project_index and thread_id not in global_index:
            global_index[thread_id] = project_index[thread_id]

    if merged_count > 0:
        _save_index(global_index)

    # Clean up the project threads directory
    try:
        shutil.rmtree(project_threads)
    except OSError:
        pass

    return merged_count
