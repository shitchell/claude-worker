"""Background manager process for a claude worker.

Handles subprocess lifecycle, FIFO plumbing, and log writing.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# -- Named constants --
FIFO_SELECT_TIMEOUT_SECONDS: float = 1.0
FIFO_READ_BUFFER_BYTES: int = 65536
SIGTERM_WAIT_TIMEOUT_SECONDS: float = 10.0
LOG_THREAD_JOIN_TIMEOUT_SECONDS: float = 5.0
QUEUE_DRAIN_INTERVAL_SECONDS: float = 5.0
CWORK_MONITOR_INTERVAL_SECONDS: float = 30.0
THREAD_MONITOR_INTERVAL_SECONDS: float = 5.0
THREAD_NOTIFICATION_PREVIEW_LENGTH: int = 200
PERIODIC_CHECK_INTERVAL_SECONDS: float = 30.0
PERIODIC_SUBPROCESS_TIMEOUT_SECONDS: float = 10.0
REMOTE_CONTROL_TIMEOUT_SECONDS: float = 30.0
REMOTE_CONTROL_POLL_INTERVAL: float = 0.2
IDENTITY_DRIFT_CHECK_INTERVAL_SECONDS: float = 30.0

# Version drift detection (#088, D105): manager stamps its version at
# startup and checks periodically whether the installed code has changed.
VERSION_CHECK_INTERVAL_SECONDS: float = 30.0
VERSION_STAMP_FILENAME: str = "version.json"

# Ephemeral workers (#080, D97): the reaper runs in the same poll
# loop as the cwork/thread/periodic checks. Sentinel file at
# <runtime>/ephemeral contains the idle timeout in seconds.
EPHEMERAL_SENTINEL_FILENAME: str = "ephemeral"
EPHEMERAL_CHECK_INTERVAL_SECONDS: float = 30.0
EPHEMERAL_WRAPUP_TIMEOUT_SECONDS: float = 30.0
EPHEMERAL_WRAPUP_POLL_INTERVAL: float = 0.5

# Identity drift detection (#066): hash of the source identity.md, written
# to runtime/identity.hash at copy time. The manager's poll loop compares
# the stored hash against the current source hash and injects a
# [system:identity-drift] notification if they diverge.
IDENTITY_HASH_FILE: str = "identity.hash"

# Response-tee thread resolution (#085, D102): extract the thread_id
# from the most recent [system:new-message] notification in the log.
# Replaces the old global active-thread sidecar which raced on multi-
# consumer workers. Format: "[system:new-message] Thread <id> from ..."
import re as _re

THREAD_NOTIFICATION_RE: "re.Pattern[str]" = _re.compile(
    r"\[system:new-message\] Thread (\S+) from"
)
TEE_LOG_SCAN_LINES: int = 30

# Env var override for the claude binary path. Tests set this to point at
# a stub-claude script that emits canned JSONL output; production leaves
# it unset and defaults to the literal "claude" on PATH.
CLAUDE_BIN_ENV_VAR: str = "CLAUDE_WORKER_CLAUDE_BIN"
DEFAULT_CLAUDE_BIN: str = "claude"


def _read_ephemeral_sentinel(runtime: Path) -> float | None:
    """Read the ephemeral idle-timeout from ``<runtime>/ephemeral``.

    Returns the timeout in seconds, or ``None`` if the file is absent
    (worker is not ephemeral). Malformed content falls back to 300s
    so a corrupt sentinel doesn't leave an ephemeral worker running
    forever (#080, D97).
    """
    path = runtime / EPHEMERAL_SENTINEL_FILENAME
    if not path.exists():
        return None
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return 300.0


def _ephemeral_should_reap(
    log_path: Path, idle_timeout: float, now: float | None = None
) -> bool:
    """Return True if the ephemeral worker's log has been idle > threshold.

    Idle is measured by the log file's mtime. A missing log returns
    False — the worker is likely still in the `starting` state and
    shouldn't be reaped before writing anything.

    Extracted for unit testing — pure function, no side effects.
    """
    if now is None:
        now = time.time()
    try:
        mtime = log_path.stat().st_mtime
    except OSError:
        return False
    return (now - mtime) > idle_timeout


def _reap_ephemeral_worker(
    name: str,
    proc: "subprocess.Popen",
    in_fifo: Path,
    idle_elapsed_seconds: float,
) -> None:
    """Gracefully terminate an ephemeral worker (#080, D97).

    Sends a ``[system:ephemeral-timeout]`` wrap-up notification via
    the FIFO, waits up to ``EPHEMERAL_WRAPUP_TIMEOUT_SECONDS`` for
    the worker to complete its last turn (or exit), then SIGTERM.
    """
    idle_minutes = max(1, int(idle_elapsed_seconds // 60))
    notification = (
        f"[system:ephemeral-timeout] Worker {name} idle {idle_minutes} "
        f"minutes, terminating."
    )
    payload = json.dumps(
        {"type": "user", "message": {"role": "user", "content": notification}}
    )
    try:
        with open(in_fifo, "w") as f:
            f.write(payload + "\n")
            f.flush()
    except OSError:
        pass

    deadline = time.monotonic() + EPHEMERAL_WRAPUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(EPHEMERAL_WRAPUP_POLL_INTERVAL)

    try:
        proc.terminate()
    except Exception:
        pass


def _last_assistant_text_from_log(log_path: Path, max_chars: int = 160) -> str:
    """Extract the last assistant text from the log (local helper).

    Reads the tail of the log backward looking for an assistant message
    with text content blocks. Returns the concatenated text truncated
    to ``max_chars``, or "" if no assistant message is found.

    NOTE: cli.py has a similar ``_get_last_assistant_preview`` backed by
    ``_iter_log_reverse``. This is a local copy for manager.py to avoid
    a circular import. If a third caller appears, extract to a shared
    ``claude_worker._logutil`` module (per P8 proactive-reusability).
    """
    if not log_path.exists():
        return ""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            tail_size = min(16384, f.tell())
            f.seek(-tail_size, 2)
            raw = f.read()
        for line in reversed(raw.split(b"\n")):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if data.get("type") != "assistant":
                continue
            contents = (data.get("message") or {}).get("content") or []
            if not isinstance(contents, list):
                continue
            text_parts = [
                c.get("text", "")
                for c in contents
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            combined = " ".join(t.strip() for t in text_parts if t.strip())
            if not combined:
                continue
            if len(combined) > max_chars:
                return combined[:max_chars] + "..."
            return combined
    except OSError:
        pass
    return ""


WORKER_STATUS_PREFIX: str = "[worker-status]"


def _notify_parent_on_exit(
    name: str,
    log_path: Path,
    reaped: bool,
    idle_seconds: float | None = None,
) -> None:
    """Send a [worker-status] completion notification to the parent worker.

    Called right before cleanup_runtime_dir so the log is still
    readable for the preview. No-op if CW_PARENT_WORKER is unset
    (human-started workers). Best-effort: never crashes the caller.
    (#084, D104)
    """
    parent = os.environ.get("CW_PARENT_WORKER", "").strip()
    if not parent:
        return

    if reaped and idle_seconds is not None:
        idle_min = max(1, int(idle_seconds // 60))
        reason = f"reaped after {idle_min}m idle"
    elif reaped:
        reason = "reaped (idle timeout)"
    else:
        reason = "clean exit"

    preview = _last_assistant_text_from_log(log_path)
    msg_parts = [f"{WORKER_STATUS_PREFIX} {name} completed ({reason})."]
    if preview:
        msg_parts.append(f'Last message: "{preview}"')
    content = "\n".join(msg_parts)

    try:
        from claude_worker.thread_store import (
            append_message,
            ensure_thread,
            pair_thread_id,
        )

        thread_id = pair_thread_id(name, parent)
        ensure_thread(thread_id, participants=sorted([name, parent]))
        append_message(thread_id, sender=name, content=content)
    except Exception:
        pass  # best-effort


def _compute_version_stamp() -> dict:
    """Build a version stamp dict for the running code.

    Contains ``version`` (from ``claude_worker.__version__``) and
    optionally ``git_hash`` (short HEAD hash if running in a git repo).
    Used both at manager startup (write to runtime/version.json) and
    at check time (compare against the running stamp). (#088, D105)
    """
    import claude_worker

    stamp: dict = {"version": claude_worker.__version__}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(claude_worker.__file__).parent,
        )
        if result.returncode == 0:
            stamp["git_hash"] = result.stdout.strip()
    except Exception:
        pass
    return stamp


def _check_version_drift(running_stamp: dict) -> dict | None:
    """Return the current installed stamp if it differs, else None.

    Checks ``version`` first (catches version bumps), then ``git_hash``
    (catches dev commits without a version bump). Returns None if both
    match or if comparison is impossible (e.g., no git at check time
    AND same version string). (#088, D105)
    """
    current = _compute_version_stamp()
    if current.get("version") != running_stamp.get("version"):
        return current
    running_hash = running_stamp.get("git_hash")
    current_hash = current.get("git_hash")
    if running_hash and current_hash and running_hash != current_hash:
        return current
    return None


def _resolve_claude_bin() -> str:
    """Return the claude binary path, honoring the CLAUDE_WORKER_CLAUDE_BIN
    env var for test injection. Defaults to ``"claude"`` (PATH lookup)."""
    return os.environ.get(CLAUDE_BIN_ENV_VAR) or DEFAULT_CLAUDE_BIN


def _resolve_tee_thread(log_path: Path) -> str | None:
    """Derive the response-tee target from the worker's own log.

    Walks the last ``TEE_LOG_SCAN_LINES`` lines backward looking for a
    ``[system:new-message] Thread <id> from ...`` user message — the
    notification that triggered the current turn. Returns the thread_id
    or None if no match (e.g., the worker is processing an initial
    prompt or a direct FIFO write with no thread context).

    Per-turn, not global — immune to the active-thread sidecar race
    that caused #085. See D102.
    """
    if not log_path.exists():
        return None
    try:
        lines: list[str] = []
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            remaining = f.tell()
            buf = b""
            while remaining > 0 and len(lines) < TEE_LOG_SCAN_LINES:
                chunk_size = min(4096, remaining)
                remaining -= chunk_size
                f.seek(remaining)
                buf = f.read(chunk_size) + buf
                lines = buf.split(b"\n")
            # Walk newest-first
            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if data.get("type") != "user":
                    continue
                content = (data.get("message") or {}).get("content") or ""
                if not isinstance(content, str):
                    continue
                m = THREAD_NOTIFICATION_RE.search(content)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def _tee_assistant_to_thread(
    line: str,
    log_path: Path,
    worker_name: str,
) -> bool:
    """Parse a raw JSONL line; if it's a final-turn assistant text message,
    append its text to the thread that triggered this turn.

    Only tees messages where ``stop_reason == "end_turn"`` so one assistant
    text per turn is appended (mid-turn streaming chunks and tool-use
    pauses are skipped). Text is the concatenation of all ``text`` blocks
    in the message's content list; non-text blocks (tool_use, thinking)
    are ignored.

    The target thread is derived per-turn from the log via
    ``_resolve_tee_thread`` (D102, #085) — NOT from the old global
    active-thread sidecar, which raced on multi-consumer workers.

    Returns True if a message was teed, False otherwise. Best-effort:
    parse errors and missing thread both silently return False.
    """
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    if data.get("type") != "assistant":
        return False
    message = data.get("message") or {}
    if message.get("stop_reason") != "end_turn":
        return False
    contents = message.get("content") or []
    if not isinstance(contents, list):
        return False
    text_parts: list[str] = []
    for c in contents:
        if isinstance(c, dict) and c.get("type") == "text":
            text_parts.append(c.get("text", ""))
    if not text_parts:
        return False
    combined = "\n".join(text_parts).strip()
    if not combined:
        return False
    target_thread = _resolve_tee_thread(log_path)
    if not target_thread:
        return False
    try:
        from claude_worker.thread_store import append_message as _append

        _append(
            thread_id=target_thread,
            sender=worker_name,
            content=combined,
            tags=["assistant"],
        )
        return True
    except Exception:
        return False


def get_base_dir() -> Path:
    """Return ~/.cwork/workers/."""
    return Path.home() / ".cwork" / "workers"


def _legacy_base_dir() -> Path:
    """Return the pre-migration /tmp/claude-workers/{UID}/ path.

    Used for backwards compatibility with workers started before the
    migration from /tmp to ~/.cwork/.
    """
    return Path(f"/tmp/claude-workers/{os.getuid()}")


def get_queue_dir(name: str) -> Path:
    """Return the message queue directory for a named worker."""
    return Path.home() / ".cwork" / "queues" / name


def enqueue_message(worker_name: str, sender: str, content: str) -> Path:
    """Write a message to a worker's queue directory.

    Returns the path of the queue file. Queue files are JSONL with
    timestamp, sender, and content fields. Named with epoch-ns for
    ordering.
    """
    queue_dir = get_queue_dir(worker_name)
    queue_dir.mkdir(parents=True, exist_ok=True)
    msg = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sender": sender,
        "content": content,
    }
    # Epoch nanoseconds for unique, ordered filenames
    filename = f"{time.time_ns()}.json"
    msg_path = queue_dir / filename
    msg_path.write_text(json.dumps(msg))
    return msg_path


def drain_queue(name: str, in_fifo: Path) -> int:
    """Drain pending messages from a worker's queue into its FIFO.

    Reads queue files in order, injects each as a synthetic user message
    via the FIFO, and deletes the file after successful injection.
    Returns the number of messages drained.

    Best-effort: catches exceptions per-message so a corrupt queue file
    doesn't block other messages.
    """
    queue_dir = get_queue_dir(name)
    if not queue_dir.exists():
        return 0

    drained = 0
    for msg_file in sorted(queue_dir.iterdir()):
        if not msg_file.is_file() or not msg_file.name.endswith(".json"):
            continue
        try:
            data = json.loads(msg_file.read_text())
            sender = data.get("sender", "unknown")
            content = data.get("content", "")
            if not content:
                msg_file.unlink(missing_ok=True)
                continue

            # Build the synthetic user message with sender attribution
            envelope = json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": f"[system:queue-drain] [reply-from:{sender}] {content}",
                    },
                }
            )
            # Write to FIFO via a non-blocking fd
            wr = os.open(str(in_fifo), os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(wr, (envelope + "\n").encode())
            finally:
                os.close(wr)

            msg_file.unlink(missing_ok=True)
            drained += 1
        except (json.JSONDecodeError, OSError, BlockingIOError):
            # Skip corrupt or unwritable — will retry next cycle
            continue
    return drained


def snapshot_cwork_dir(cwd: str) -> dict[str, tuple[float, int]]:
    """Snapshot the .cwork/ directory: {relative_path: (mtime, size)}.

    Returns an empty dict if .cwork/ doesn't exist. Only includes
    regular files, not directories.
    """
    cwork_dir = Path(cwd) / ".cwork"
    if not cwork_dir.exists():
        return {}
    result: dict[str, tuple[float, int]] = {}
    try:
        for f in cwork_dir.rglob("*"):
            if f.is_file():
                try:
                    st = f.stat()
                    rel = str(f.relative_to(Path(cwd)))
                    result[rel] = (st.st_mtime, st.st_size)
                except OSError:
                    continue
    except OSError:
        pass
    return result


def diff_cwork_snapshots(
    old: dict[str, tuple[float, int]],
    new: dict[str, tuple[float, int]],
) -> list[str]:
    """Compare two .cwork/ snapshots, return list of changed/added file paths."""
    changed: list[str] = []
    for path, (mtime, size) in new.items():
        old_entry = old.get(path)
        if old_entry is None or old_entry != (mtime, size):
            changed.append(path)
    return changed


def check_cwork_changes(
    cwd: str,
    in_fifo: Path,
    prev_snapshot: dict[str, tuple[float, int]],
) -> dict[str, tuple[float, int]]:
    """Check for .cwork/ changes and inject a notification if any found.

    Returns the new snapshot (to be cached by the caller for the next cycle).
    Best-effort: never crashes the caller.
    """
    try:
        new_snapshot = snapshot_cwork_dir(cwd)
        if not prev_snapshot:
            return new_snapshot  # first scan, no diff

        changed = diff_cwork_snapshots(prev_snapshot, new_snapshot)
        if not changed:
            return new_snapshot

        # Build notification
        file_list = ", ".join(changed[:5])
        if len(changed) > 5:
            file_list += f" (+{len(changed) - 5} more)"
        msg = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": (
                        f"[system:cwork-change] {len(changed)} file(s) "
                        f"modified in .cwork/: {file_list}"
                    ),
                },
            }
        )
        wr = os.open(str(in_fifo), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(wr, (msg + "\n").encode())
        finally:
            os.close(wr)
        return new_snapshot
    except Exception:
        return prev_snapshot


def snapshot_threads() -> dict[str, tuple[float, int]]:
    """Snapshot ~/.cwork/threads/*.jsonl files: {thread_id: (mtime, size)}.

    Returns {} if the threads dir doesn't exist. Non-JSONL files (e.g.
    the index.json) are ignored so the snapshot tracks only message
    streams.
    """
    from claude_worker.thread_store import _threads_dir

    threads_dir = _threads_dir()
    if not threads_dir.exists():
        return {}
    result: dict[str, tuple[float, int]] = {}
    try:
        for f in threads_dir.glob("*.jsonl"):
            try:
                st = f.stat()
                result[f.stem] = (st.st_mtime, st.st_size)
            except OSError:
                continue
    except OSError:
        pass
    return result


def _read_new_messages_since_size(thread_id: str, old_size: int) -> list[dict]:
    """Read messages that appeared after the given file size.

    Reads the JSONL file, skips the first ``old_size`` bytes, parses
    the rest as new messages. Returns a list of message dicts.
    Best-effort: corrupt lines are skipped silently.
    """
    from claude_worker.thread_store import _threads_dir

    thread_file = _threads_dir() / f"{thread_id}.jsonl"
    if not thread_file.exists():
        return []
    messages: list[dict] = []
    try:
        with open(thread_file, "rb") as f:
            f.seek(old_size)
            remainder = f.read().decode("utf-8", errors="replace")
        for line in remainder.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return messages


def check_thread_changes(
    worker_name: str,
    in_fifo: Path,
    prev_snapshot: dict[str, tuple[float, int]],
    seeded: bool = False,
) -> dict[str, tuple[float, int]]:
    """Check for new messages in threads the worker participates in.

    For each new message, inject a ``[system:new-message]`` notification
    to the worker's FIFO. Returns the new snapshot (to be cached by the
    caller for the next cycle).

    Notification format (always-lightweight, per D79/P12)::

        [system:new-message] Thread <id> from <sender>: <first 80 chars>

    The worker reads the full thread on demand. Messages where the
    sender is the worker itself are ignored to avoid self-notification
    loops.

    ``seeded=False`` (default) preserves the defensive first-scan
    behaviour: an empty ``prev_snapshot`` skips notifications so a
    freshly-started caller doesn't flood the worker with history.
    ``seeded=True`` declares "the baseline was already taken" and lets
    new threads trigger notifications even when the prior snapshot was
    empty — used by the manager, which pre-seeds a snapshot at startup
    so writes that land before the first 5s poll are still delivered.

    The response tee now derives its target thread from the log per-turn
    (``_resolve_tee_thread``, D102) instead of a global sidecar, so
    this function no longer maintains an active-thread sidecar.
    """
    try:
        new_snapshot = snapshot_threads()
        if not seeded and not prev_snapshot:
            return new_snapshot  # first scan, no diff

        if not worker_name:
            return new_snapshot  # no worker name, can't filter participation

        # Load the thread index to check participants
        try:
            from claude_worker.thread_store import load_index

            index = load_index()
        except Exception:
            return new_snapshot

        for thread_id, (_mtime, size) in new_snapshot.items():
            old_entry = prev_snapshot.get(thread_id)
            old_size = old_entry[1] if old_entry else 0

            # Only notify if the file grew. For threads present in
            # prev_snapshot, this means new messages were appended.
            # For threads first seen this cycle, old_size defaults to
            # 0 so their initial messages (if any) are delivered.
            if old_entry is not None and size <= old_size:
                continue

            # Check participation — skip threads the worker isn't in
            thread_meta = index.get(thread_id, {})
            participants = thread_meta.get("participants") or []
            if worker_name not in participants:
                continue

            # Read the new messages and notify for each
            new_messages = _read_new_messages_since_size(thread_id, old_size)
            for msg in new_messages:
                sender = msg.get("sender", "?")
                if sender == worker_name:
                    # Don't notify the sender about their own message
                    continue
                content = msg.get("content", "") or ""
                truncated = len(content) > THREAD_NOTIFICATION_PREVIEW_LENGTH
                preview = content[:THREAD_NOTIFICATION_PREVIEW_LENGTH]
                # Loud, self-documenting truncation (D108): when the preview
                # cuts off the body, append an explicit instruction line in
                # the same envelope so the recipient knows there's more and
                # exactly how to fetch it. Replaces the silent "..." that
                # made truncation indistinguishable from a complete message
                # (G2 loud-over-silent, V2 explicit-over-implicit).
                notification_body = (
                    f"[system:new-message] Thread {thread_id} "
                    f"from {sender}: {preview}"
                )
                if truncated:
                    notification_body += (
                        f"...\n[truncated — full message in thread; "
                        f"read with: claude-worker thread read "
                        f"{worker_name} --thread {thread_id}]"
                    )
                notification = json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": notification_body,
                        },
                    }
                )
                try:
                    wr = os.open(str(in_fifo), os.O_WRONLY | os.O_NONBLOCK)
                    try:
                        os.write(wr, (notification + "\n").encode())
                    finally:
                        os.close(wr)
                except OSError:
                    # FIFO not writable (no reader, etc.) — skip,
                    # snapshot still advances so we don't re-notify.
                    pass

        return new_snapshot
    except Exception:
        return prev_snapshot  # be safe, don't replace on error


def load_periodic_config(identity: str) -> dict[str, float]:
    """Load periodic task config from the identity's hooks/periodic/periodic.yaml.

    Returns {script_name: interval_seconds}. Returns {} if missing.

    Example periodic.yaml:
        tasks:
          hourly-check.sh: 3600
          daily-review.sh: 86400
    """
    config_path = (
        Path.home()
        / ".cwork"
        / "identities"
        / identity
        / "hooks"
        / "periodic"
        / "periodic.yaml"
    )
    if not config_path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text())
        if isinstance(data, dict) and isinstance(data.get("tasks"), dict):
            return {k: float(v) for k, v in data["tasks"].items()}
        return {}
    except Exception:
        return {}


def check_periodic_tasks(
    identity: str,
    runtime: Path,
    in_fifo: Path,
) -> None:
    """Run any due periodic tasks and inject output as [system:cron].

    Checks each task's last-run timestamp (stored in runtime/periodic/).
    If the interval has elapsed, runs the script and injects its stdout
    as a synthetic user message. Best-effort: failures logged, never crash.
    """
    tasks = load_periodic_config(identity)
    if not tasks:
        return

    periodic_dir = (
        Path.home() / ".cwork" / "identities" / identity / "hooks" / "periodic"
    )
    timestamps_dir = runtime / "periodic"
    timestamps_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()

    for script_name, interval in tasks.items():
        # Check last run
        ts_file = timestamps_dir / f"{script_name}.last"
        if ts_file.exists():
            try:
                last_run = float(ts_file.read_text().strip())
                if now - last_run < interval:
                    continue
            except (ValueError, OSError):
                pass

        # Run the script
        script_path = periodic_dir / script_name
        if not script_path.exists():
            continue

        try:
            import subprocess as _sp

            result = _sp.run(
                ["bash", str(script_path)],
                capture_output=True,
                text=True,
                timeout=PERIODIC_SUBPROCESS_TIMEOUT_SECONDS,
            )
            output = result.stdout.strip()
            if output:
                msg = json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": f"[system:cron] [{script_name}] {output}",
                        },
                    }
                )
                wr = os.open(str(in_fifo), os.O_WRONLY | os.O_NONBLOCK)
                try:
                    os.write(wr, (msg + "\n").encode())
                finally:
                    os.close(wr)

            # Update timestamp (even on empty output — task ran)
            ts_file.write_text(str(now))
        except Exception:
            pass  # best-effort


# -- Identity drift detection (#066) -------------------------------------
#
# The source identity file (`~/.cwork/identities/<name>/identity.md` or
# bundled) is hashed at copy time and the hash is stored in
# `runtime/identity.hash`. The manager's poll loop periodically re-hashes
# the source and compares; on divergence it injects a one-shot
# [system:identity-drift] notification so the worker can decide whether
# to `replaceme` and pick up the change. No automatic update.


def hash_identity_content(content: str) -> str:
    """Return a short stable hash of identity content.

    Uses the first 16 hex chars of sha256 — long enough to be collision-
    resistant for this domain (small text files), short enough to keep
    the notification message human-readable.
    """
    import hashlib

    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def write_identity_hash(runtime: Path, content: str) -> None:
    """Write the source content hash to ``runtime/identity.hash``.

    Best-effort: a filesystem failure here should never break cmd_start
    or cmd_replaceme. Worst case: no baseline is written and the drift
    check silently no-ops (see ``read_identity_hash``).
    """
    try:
        (runtime / IDENTITY_HASH_FILE).write_text(hash_identity_content(content) + "\n")
    except OSError:
        pass


def read_identity_hash(runtime: Path) -> str | None:
    """Read the stored identity hash. Returns None if missing or unreadable."""
    p = runtime / IDENTITY_HASH_FILE
    if not p.exists():
        return None
    try:
        return p.read_text().strip() or None
    except OSError:
        return None


def _read_source_identity(identity: str) -> str | None:
    """Read the current source identity content. Returns None if not found.

    Mirrors ``cmd_start``'s resolution order: user-installed
    (``~/.cwork/identities/<name>/identity.md``) takes precedence over
    the bundled fallback, which is only defined for ``pm`` and
    ``technical-lead``.
    """
    if not identity or identity == "worker":
        return None
    user_path = Path.home() / ".cwork" / "identities" / identity / "identity.md"
    if user_path.exists():
        try:
            return user_path.read_text()
        except OSError:
            pass
    # Bundled fallback (only pm and technical-lead)
    bundled = {"pm": "pm.md", "technical-lead": "technical-lead.md"}
    resource = bundled.get(identity)
    if resource:
        try:
            from importlib.resources import files

            return (files("claude_worker") / "identities" / resource).read_text()
        except Exception:
            pass
    return None


def check_identity_drift(
    identity: str,
    runtime: Path,
    in_fifo: Path,
    notified: bool,
) -> bool:
    """Compare runtime identity hash to current source hash; notify on drift.

    Returns the new "notified" flag — True if a drift notification has
    already been sent for the CURRENT divergence (prevents spamming the
    worker on every poll). When the source matches the stored hash again
    (e.g. the user reverted the edit), the flag clears so the next
    divergence re-notifies.

    Best-effort throughout: no exception escapes. Failures at any step
    (missing baseline, unreachable source, FIFO write failure) leave the
    caller's state unchanged so the next cycle can retry cleanly.
    """
    try:
        stored = read_identity_hash(runtime)
        if stored is None:
            return notified  # no baseline — nothing to compare
        source_content = _read_source_identity(identity)
        if source_content is None:
            return notified  # source unavailable — can't check
        current = hash_identity_content(source_content)
        if current == stored:
            # Match: clear notified flag for the next divergence cycle
            return False
        if notified:
            # Already notified about this divergence; don't spam
            return True
        # Drift detected — inject one [system:identity-drift] notification
        msg = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": (
                        f"[system:identity-drift] Source identity "
                        f"'{identity}' has changed since this worker "
                        f"started (stored hash: {stored}, source hash: "
                        f"{current}). Consider replaceme to pick up "
                        f"the new identity."
                    ),
                },
            }
        )
        try:
            wr = os.open(str(in_fifo), os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(wr, (msg + "\n").encode())
            finally:
                os.close(wr)
        except OSError:
            return notified  # FIFO write failed; try again next cycle
        return True
    except Exception:
        return notified


def get_runtime_dir(name: str) -> Path:
    """Return the runtime directory for a named worker.

    Checks the current base dir first, then falls back to the legacy
    /tmp/ path for workers started before the migration. Returns the
    new-location path for workers that don't exist yet.
    """
    primary = get_base_dir() / name
    if primary.exists():
        return primary
    legacy = _legacy_base_dir() / name
    if legacy.exists():
        return legacy
    return primary


def create_runtime_dir(name: str) -> Path:
    """Create runtime directory with FIFOs. Returns the path.

    Always creates under the new base dir (~/.cwork/workers/).
    Parent directories are created with mode 700.
    """
    runtime = get_base_dir() / name
    if runtime.exists():
        raise FileExistsError(f"Worker '{name}' already exists at {runtime}")
    # Also check legacy path to prevent name collisions across locations
    legacy = _legacy_base_dir() / name
    if legacy.exists():
        raise FileExistsError(f"Worker '{name}' already exists at {legacy}")
    runtime.mkdir(parents=True, mode=0o700)
    # Ensure the base dir itself is 700 (mkdir parents inherit umask)
    get_base_dir().chmod(0o700)
    os.mkfifo(runtime / "in")
    return runtime


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write text to ``path`` via a sibling .tmp file + os.replace.

    Guarantees that a crash (signal, disk full, power loss) between the
    write and the rename leaves the original file untouched — either the
    new content is fully in place, or the old content remains. Used for
    user-critical files: ~/.claude/settings.json, .sessions.json,
    missing-tags.json.

    Creates the parent directory if it doesn't exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_text(content)
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the tmp file; don't mask the original
        # exception.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_identity_from_sessions(name: str) -> str:
    """Read identity from .sessions.json for a worker. Returns '' if unavailable."""
    try:
        sessions_path = get_base_dir() / ".sessions.json"
        if sessions_path.exists():
            data = json.loads(sessions_path.read_text())
            return data.get(name, {}).get("identity", "")
    except (json.JSONDecodeError, OSError):
        pass
    return ""


def archive_runtime_dir(
    name: str,
    reason: str = "unknown",
    successor: str = "",
) -> Path | None:
    """Rename runtime directory to a timestamped archive path.

    Used by the SIGUSR1 (graceful replace) handler to preserve the
    runtime dir for the replacement manager to read session metadata
    from. The archive path is deterministic from the name + timestamp +
    session ID prefix.

    Writes a ``metadata.json`` to the archive with audit trail info
    (worker name, reason, timestamp, session ID, identity, successor).
    The metadata write is best-effort — failure does not prevent archival.

    Returns the archive path, or None if the runtime dir doesn't exist.
    """
    runtime = get_runtime_dir(name)
    if not runtime.exists():
        return None
    timestamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    session_id = ""
    try:
        session_id = (runtime / "session").read_text().strip()[:8]
    except OSError:
        pass
    suffix = f".{session_id}" if session_id else ""
    archive_name = f"{name}.{timestamp}{suffix}"
    archive_path = runtime.parent / archive_name
    try:
        os.rename(runtime, archive_path)
    except OSError:
        return None

    # Write archive metadata for audit trail
    metadata = {
        "worker_name": name,
        "archive_reason": reason,
        "archive_timestamp": timestamp,
        "session_id": session_id,
        "identity": _read_identity_from_sessions(name),
        "successor": successor,
    }
    try:
        metadata_path = archive_path / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    except OSError:
        pass  # best-effort

    return archive_path


ARCHIVE_RETENTION_DAYS: int = 30


def cleanup_runtime_dir(name: str, reason: str = "stop") -> None:
    """Archive and then remove the runtime directory.

    Archives the log and metadata to a timestamped directory under the
    same base dir before deletion. If archival fails, falls back to
    direct deletion. Idempotent: safe to call on a non-existent
    directory or concurrently from multiple callers.

    Checks both the new (~/.cwork/workers/) and legacy (/tmp/) paths
    to handle workers started before the migration.
    """
    # Try to archive before deleting (best-effort)
    try:
        archive_runtime_dir(name, reason=reason)
    except Exception:
        pass
    # Clean up in both possible locations (archive may have moved the
    # dir, but rmtree with ignore_errors handles non-existent paths)
    for base in (get_base_dir(), _legacy_base_dir()):
        runtime = base / name
        shutil.rmtree(runtime, ignore_errors=True)


def prune_archives(max_age_days: int = ARCHIVE_RETENTION_DAYS) -> int:
    """Remove archived worker directories older than max_age_days.

    Archives are identified by their name pattern: they contain a
    dot-separated timestamp (e.g., ``worker.20260409T010000.abc123``).
    Active worker dirs don't contain dots in their names.

    Returns the number of archives pruned.
    """
    cutoff = time.time() - (max_age_days * 86400)
    pruned = 0
    for base in (get_base_dir(), _legacy_base_dir()):
        if not base.exists():
            continue
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            # Archives have dots in their name (timestamp separator)
            if "." not in entry.name:
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    pruned += 1
            except OSError:
                continue
    return pruned


def get_sessions_file() -> Path:
    """Return path to the persistent name→session_id map."""
    return get_base_dir() / ".sessions.json"


def _load_sessions() -> dict:
    """Load sessions from the current path, merging legacy if needed.

    Reads the new-location .sessions.json first. If it doesn't exist,
    falls back to the legacy /tmp/ path. Legacy entries are included
    but not migrated on disk — the next save_worker call writes to the
    new location, effectively migrating that entry.
    """
    sessions: dict = {}
    # Try legacy first (lower priority — new entries override)
    legacy_path = _legacy_base_dir() / ".sessions.json"
    if legacy_path.exists():
        try:
            sessions = json.loads(legacy_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    # New path overrides legacy entries
    path = get_sessions_file()
    if path.exists():
        try:
            new_sessions = json.loads(path.read_text())
            sessions.update(new_sessions)
        except (json.JSONDecodeError, OSError):
            pass
    return sessions


def save_worker(name: str, **kwargs) -> None:
    """Persist worker metadata (session_id, cwd, agent, claude_args, etc.).

    Merges kwargs into any existing entry for this worker name. Writes
    atomically via _atomic_write_text so a crash during the save doesn't
    leave a truncated .sessions.json that breaks future --resume.
    """
    path = get_sessions_file()
    sessions = _load_sessions()
    # Migrate legacy string entries (old format: name → session_id)
    existing = sessions.get(name)
    if isinstance(existing, str):
        existing = {"session_id": existing}
    elif not isinstance(existing, dict):
        existing = {}
    existing.update(kwargs)
    sessions[name] = existing
    _atomic_write_text(path, json.dumps(sessions, indent=2))


def _manager_thread_panic(log_path: Path, thread_name: str, exc: BaseException) -> None:
    """Handle a fatal exception inside a manager daemon thread.

    Loud failure mode (per Round 3 design): a silently-dead thread leaves
    the worker appearing alive in `ls` while being broken (no log pump, or
    no FIFO pump). Instead:

    1. Best-effort append a sentinel JSONL line so operators reading the
       log see a clear error signal.
    2. Send SIGTERM to the manager's own PID so the worker transitions to
       `dead` in ls output, prompting investigation.

    The SIGTERM step runs even if the sentinel write fails — the operator
    signal is more important than the log entry.
    """
    import traceback

    try:
        sentinel = {
            "type": "manager_error",
            "thread": thread_name,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exception(type(exc), exc, exc.__traceback__),
        }
        with open(log_path, "a") as log:
            log.write(json.dumps(sentinel) + "\n")
    except Exception:
        # Sentinel write failed (disk full, permissions, etc.) — carry on
        # to the SIGTERM step so the operator still sees the dead worker.
        pass
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        # If we can't even signal ourselves, there's nothing more we can do.
        pass


def _run_manager_thread(body: "callable", log_path: Path, thread_name: str) -> None:
    """Run a manager daemon thread body with panic handling.

    Any uncaught exception from ``body`` is routed through
    ``_manager_thread_panic``. Daemon threads otherwise silently die,
    which the project's state-awareness principle explicitly forbids.
    """
    try:
        body()
    except Exception as exc:
        _manager_thread_panic(log_path, thread_name, exc)


def get_saved_worker(name: str) -> dict | None:
    """Look up saved worker metadata by name.

    Returns a dict with keys like session_id, cwd, agent, claude_args.
    Returns None if no entry exists. Checks both new and legacy session
    files via _load_sessions.
    """
    sessions = _load_sessions()
    entry = sessions.get(name)
    if entry is None:
        return None
    # Migrate legacy string entries
    if isinstance(entry, str):
        return {"session_id": entry}
    return entry


def _enable_remote_control(proc: subprocess.Popen, log_path: Path) -> None:
    """Send a control_request to enable CCR remote control.

    Injects a control_request message on claude's stdin, then polls
    the log for the matching control_response containing session_url
    and connect_url. Prints the URLs to stderr.

    Non-fatal: if the request fails or times out, logs a warning
    but lets the worker continue without remote control.
    """
    request_id = f"rc-{uuid.uuid4().hex[:12]}"
    control_req = {
        "type": "control_request",
        "request_id": request_id,
        "request": {
            "subtype": "remote_control",
            "enabled": True,
        },
    }
    try:
        proc.stdin.write((json.dumps(control_req) + "\n").encode())
        proc.stdin.flush()
    except (OSError, BrokenPipeError) as exc:
        sys.stderr.write(f"[remote-control] Failed to send control_request: {exc}\n")
        return

    # Poll log for control_response matching our request_id
    deadline = time.monotonic() + REMOTE_CONTROL_TIMEOUT_SECONDS
    seen_pos = 0
    while time.monotonic() < deadline:
        time.sleep(REMOTE_CONTROL_POLL_INTERVAL)
        if not log_path.exists():
            continue
        try:
            with open(log_path) as f:
                f.seek(seen_pos)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        data.get("type") == "control_response"
                        and data.get("request_id") == request_id
                    ):
                        response = data.get("response", {}) or {}
                        session_url = response.get("session_url")
                        connect_url = response.get("connect_url")
                        env_id = response.get("environment_id", "")
                        sys.stderr.write(
                            "[remote-control] Enabled. Connect via Claude mobile app:\n"
                        )
                        if session_url:
                            sys.stderr.write(f"  session: {session_url}\n")
                        if connect_url:
                            sys.stderr.write(f"  connect: {connect_url}\n")
                        if env_id:
                            sys.stderr.write(f"  env: {env_id}\n")
                        sys.stderr.flush()
                        return
                seen_pos = f.tell()
        except OSError:
            pass

    sys.stderr.write(
        "[remote-control] Timed out waiting for control_response. "
        "Worker continues without remote control.\n"
    )
    sys.stderr.flush()


def run_manager(
    name: str,
    cwd: str | None,
    claude_args: list[str],
    initial_message: str | None,
    identity: str = "worker",
    extra_env: dict[str, str] | None = None,
    remote: bool = False,
) -> None:
    """Run the manager process (called after fork).

    Thin wrapper around ``_run_manager_forkless`` that installs signal
    handlers. Production uses this via cmd_start's fork + setsid +
    fd-redirect sequence. Tests drive ``_run_manager_forkless`` directly
    with ``install_signals=False`` so SIGTERM/SIGINT don't escape into
    the test runner, and so the helper can run in a thread instead of
    a forked process.
    """
    _run_manager_forkless(
        name,
        cwd,
        claude_args,
        initial_message,
        install_signals=True,
        identity=identity,
        extra_env=extra_env,
        remote=remote,
    )


def _run_manager_forkless(
    name: str,
    cwd: str | None,
    claude_args: list[str],
    initial_message: str | None,
    install_signals: bool = True,
    identity: str = "worker",
    extra_env: dict[str, str] | None = None,
    remote: bool = False,
) -> None:
    """Run the manager lifecycle WITHOUT the fork wrapper.

    This is the main loop that:
    1. Launches claude with stream-json I/O (resolved via
       CLAUDE_WORKER_CLAUDE_BIN for test stubbing)
    2. Bridges the `in` FIFO to claude's stdin
    3. Tees claude's stdout to the `log` file
    4. Captures session ID from the init message
    5. Sends initial prompt if provided
    6. Waits for claude to exit, then cleans up

    When ``install_signals=False`` (test mode), SIGTERM/SIGINT handlers
    are NOT registered — the test runner's signal handling stays intact,
    and shutdown is driven by the stub-claude subprocess exiting.
    """
    runtime = get_runtime_dir(name)
    in_fifo = runtime / "in"
    log_path = runtime / "log"
    pid_file = runtime / "pid"
    session_file = runtime / "session"

    # Write manager PID
    pid_file.write_text(str(os.getpid()))

    # Version stamp (#088, D105): written once at startup. The
    # periodic check in the main loop compares the running stamp
    # against a fresh _compute_version_stamp() to detect code drift.
    running_version_stamp = _compute_version_stamp()
    running_version_stamp["started_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )
    version_file = runtime / VERSION_STAMP_FILENAME
    _atomic_write_text(version_file, json.dumps(running_version_stamp, indent=2))

    resolved_cwd = cwd or os.getcwd()

    # Build environment — unset ANTHROPIC_API_KEY to force subscription auth
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    # Worker identity env vars — available to hooks and Bash tool calls
    env["CW_WORKER_NAME"] = name
    env["CW_IDENTITY"] = identity
    env["CW_PARENT_WORKER"] = os.environ.get("CW_WORKER_NAME", "")

    # Ephemeral flag (#080, D97) — readable from hooks/tools so they
    # can tune behavior for short-lived workers.
    env["CW_EPHEMERAL"] = (
        "true" if (runtime / EPHEMERAL_SENTINEL_FILENAME).exists() else "false"
    )

    # Extra env vars from identity config
    if extra_env:
        env.update(extra_env)

    # Build claude command. Binary path is overridable via
    # CLAUDE_WORKER_CLAUDE_BIN for test injection of a stub.
    cmd = [
        _resolve_claude_bin(),
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--replay-user-messages",
        "--dangerously-skip-permissions",
        *claude_args,
    ]

    # Launch claude subprocess
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=resolved_cwd,
    )

    # Write the claude subprocess pid to a sidecar file. This lets test
    # harnesses (which run `_run_manager_forkless` in a thread, not a
    # forked child) discover and signal the stub-claude process directly
    # without walking /proc trees or depending on psutil. Production
    # tooling ignores this file.
    try:
        (runtime / "claude-pid").write_text(str(proc.pid))
    except OSError:
        pass

    # Signal handling — forward SIGTERM to claude, then exit.
    # Wrapped in try/except so a stuck claude (subprocess.TimeoutExpired)
    # doesn't leave the manager tracebacked and the runtime dir uncleaned.
    # On timeout, escalate to SIGKILL and clean up unconditionally.
    def _kill_claude():
        """Terminate the claude subprocess, escalating to SIGKILL on timeout."""
        proc.terminate()
        try:
            proc.wait(timeout=SIGTERM_WAIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=SIGTERM_WAIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass  # claude is truly stuck; cleanup anyway

    def handle_term(signum, frame):
        try:
            _kill_claude()
        finally:
            cleanup_runtime_dir(name)
            sys.exit(0)

    def handle_replace(signum, frame):
        """Graceful replace: kill claude, archive runtime dir, exit.

        Unlike handle_term, does NOT delete the runtime dir — the
        archive preserves the prior session's log and metadata for
        audit / debugging. The replacement manager starts a fresh
        session (replaceme is fresh-start by design — see D90); the
        handoff file carries work state forward, not --resume.
        """
        try:
            _kill_claude()
        finally:
            archive_runtime_dir(name, reason="replaceme")
            sys.exit(0)

    if install_signals:
        signal.signal(signal.SIGTERM, handle_term)
        signal.signal(signal.SIGINT, handle_term)
        signal.signal(signal.SIGUSR1, handle_replace)

    # Session ID capture event
    session_captured = threading.Event()

    # Thread: read claude stdout → log file
    def stdout_to_log_body():
        with open(log_path, "w") as log:
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace")
                log.write(line)
                log.flush()
                # Capture session ID from init message
                if not session_captured.is_set():
                    try:
                        data = json.loads(line)
                        if (
                            data.get("type") == "system"
                            and data.get("subtype") == "init"
                        ):
                            sid = data.get("session_id", "")
                            session_file.write_text(sid)
                            save_worker(name, session_id=sid)
                            session_captured.set()
                    except (json.JSONDecodeError, KeyError):
                        pass
                # Phase 3 response tee: append end_turn assistant text
                # to the active thread, if one is set. Best-effort — any
                # parse or I/O error is swallowed by the helper.
                try:
                    _tee_assistant_to_thread(line, log_path, name)
                except Exception:
                    pass

    log_thread = threading.Thread(
        target=_run_manager_thread,
        args=(stdout_to_log_body, log_path, "stdout_to_log"),
        daemon=True,
    )
    log_thread.start()

    # Thread: read from `in` FIFO → claude stdin
    # Uses a dummy write fd to prevent EOF when writers close.
    # Start this immediately so external senders don't block.
    # Ephemeral-reap state — shared between fifo_to_stdin_body (inner)
    # and _notify_parent_on_exit (outer) via nonlocal (#084, D104).
    ephemeral_reaped = False
    ephemeral_idle_elapsed: float = 0.0

    def fifo_to_stdin_body():
        # Open read end non-blocking first
        rd_fd = os.open(str(in_fifo), os.O_RDONLY | os.O_NONBLOCK)
        # Open write end to keep FIFO alive (prevents EOF)
        wr_fd = os.open(str(in_fifo), os.O_WRONLY)

        last_queue_drain = time.monotonic()
        last_cwork_check = time.monotonic()
        last_thread_check = time.monotonic()
        last_periodic_check = time.monotonic()
        last_identity_drift_check = time.monotonic()
        last_ephemeral_check = time.monotonic()
        last_version_check = time.monotonic()
        # Read the ephemeral idle-timeout once at startup. None means
        # the worker is not ephemeral. (#080, D97)
        ephemeral_idle_timeout = _read_ephemeral_sentinel(runtime)
        # Access the outer-scope reap state so _notify_parent_on_exit
        # can distinguish clean-exit from idle-reap (#084, D104).
        nonlocal ephemeral_reaped, ephemeral_idle_elapsed
        # One-shot dedup flags: set when a drift notification has been
        # delivered for the current divergence. (#066 identity, #088 version)
        identity_drift_notified: bool = False
        version_drift_notified: bool = False
        cwork_snapshot: dict[str, tuple[float, int]] = {}
        # Seed the thread snapshot synchronously at startup so existing
        # threads form the baseline — any file growth from this moment on
        # triggers notifications. Without the pre-seed, writes that land
        # between manager start and the first 5s poll would be absorbed
        # into the baseline and never notify (a genuine delivery bug
        # for tests and fresh workers that are sent to immediately).
        # Migrate per-project threads to global storage before first
        # snapshot. Best-effort: a failure here doesn't block startup.
        try:
            from claude_worker.thread_store import migrate_from_project

            migrated = migrate_from_project(resolved_cwd)
            if migrated > 0:
                import logging

                logging.getLogger(__name__).warning(
                    "Migrated %d thread(s) from %s to global storage",
                    migrated,
                    resolved_cwd,
                )
        except Exception:
            pass

        thread_snapshot: dict[str, tuple[float, int]] = snapshot_threads()
        # Flag: the manager has already taken a baseline snapshot. Passed
        # to check_thread_changes so it doesn't short-circuit on the
        # defensive "empty prev_snapshot → skip notifications" branch.
        thread_baseline_seeded = True

        try:
            while proc.poll() is None:
                # Wait for data on the read fd
                ready, _, _ = select.select(
                    [rd_fd], [], [], FIFO_SELECT_TIMEOUT_SECONDS
                )
                if ready:
                    data = os.read(rd_fd, FIFO_READ_BUFFER_BYTES)
                    if data and proc.stdin:
                        proc.stdin.write(data)
                        proc.stdin.flush()

                now = time.monotonic()

                # Periodic queue drain — deliver pending reply messages
                if now - last_queue_drain >= QUEUE_DRAIN_INTERVAL_SECONDS:
                    last_queue_drain = now
                    try:
                        drain_queue(name, in_fifo)
                    except Exception:
                        pass

                # Periodic .cwork/ directory monitoring
                if now - last_cwork_check >= CWORK_MONITOR_INTERVAL_SECONDS:
                    last_cwork_check = now
                    cwork_snapshot = check_cwork_changes(
                        resolved_cwd, in_fifo, cwork_snapshot
                    )

                # Periodic thread monitoring — inject new-message
                # notifications for threads the worker participates in.
                # ``seeded=True`` tells check_thread_changes the baseline
                # is already established, so it should not skip
                # notifications on an empty snapshot.
                if now - last_thread_check >= THREAD_MONITOR_INTERVAL_SECONDS:
                    last_thread_check = now
                    thread_snapshot = check_thread_changes(
                        name,
                        in_fifo,
                        thread_snapshot,
                        seeded=thread_baseline_seeded,
                    )

                # Periodic identity tasks (cron)
                if (
                    identity != "worker"
                    and now - last_periodic_check >= PERIODIC_CHECK_INTERVAL_SECONDS
                ):
                    last_periodic_check = now
                    try:
                        check_periodic_tasks(identity, runtime, in_fifo)
                    except Exception:
                        pass

                # Identity drift detection (#066) — compare runtime hash
                # to current source hash; notify once per divergence.
                if (
                    identity != "worker"
                    and now - last_identity_drift_check
                    >= IDENTITY_DRIFT_CHECK_INTERVAL_SECONDS
                ):
                    last_identity_drift_check = now
                    identity_drift_notified = check_identity_drift(
                        identity, runtime, in_fifo, identity_drift_notified
                    )

                # Version drift detection (#088, D105). Compare the
                # running stamp against a fresh computation; notify once.
                if (
                    not version_drift_notified
                    and now - last_version_check >= VERSION_CHECK_INTERVAL_SECONDS
                ):
                    last_version_check = now
                    drift = _check_version_drift(running_version_stamp)
                    if drift is not None:
                        version_drift_notified = True
                        running_v = running_version_stamp.get(
                            "git_hash", running_version_stamp.get("version", "?")
                        )
                        current_v = drift.get("git_hash", drift.get("version", "?"))
                        notification = json.dumps(
                            {
                                "type": "user",
                                "message": {
                                    "role": "user",
                                    "content": (
                                        f"[system:manager-outdated] Manager code is "
                                        f"outdated (running {running_v}, installed "
                                        f"{current_v}). Use `claude-worker replaceme` "
                                        f"or `stop + start` to pick up new code."
                                    ),
                                },
                            }
                        )
                        try:
                            wr = os.open(str(in_fifo), os.O_WRONLY | os.O_NONBLOCK)
                            try:
                                os.write(wr, (notification + "\n").encode())
                            finally:
                                os.close(wr)
                        except OSError:
                            pass

                # Ephemeral inactivity reap (#080, D97). Runs only when
                # the ephemeral sentinel was present at startup.
                if (
                    ephemeral_idle_timeout is not None
                    and now - last_ephemeral_check >= EPHEMERAL_CHECK_INTERVAL_SECONDS
                ):
                    last_ephemeral_check = now
                    if _ephemeral_should_reap(log_path, ephemeral_idle_timeout):
                        try:
                            mtime = log_path.stat().st_mtime
                            idle_elapsed = time.time() - mtime
                        except OSError:
                            idle_elapsed = ephemeral_idle_timeout
                        ephemeral_reaped = True
                        ephemeral_idle_elapsed = idle_elapsed
                        _reap_ephemeral_worker(name, proc, in_fifo, idle_elapsed)
                        # The reaper's SIGTERM causes proc.poll() to
                        # transition; the loop condition catches it
                        # on the next iteration.
        except (OSError, BrokenPipeError):
            # These are EXPECTED during normal shutdown (claude exits,
            # FIFO closes). Not a panic condition.
            pass
        finally:
            os.close(rd_fd)
            os.close(wr_fd)

    fifo_thread = threading.Thread(
        target=_run_manager_thread,
        args=(fifo_to_stdin_body, log_path, "fifo_to_stdin"),
        daemon=True,
    )
    fifo_thread.start()

    # Enable CCR remote control if requested
    if remote and proc.stdin:
        _enable_remote_control(proc, log_path)

    # Send initial prompt if provided
    if initial_message and proc.stdin:
        msg = json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": initial_message},
            }
        )
        proc.stdin.write((msg + "\n").encode())
        proc.stdin.flush()

    # Wait for claude to exit
    proc.wait()
    log_thread.join(timeout=LOG_THREAD_JOIN_TIMEOUT_SECONDS)

    # Notify the parent worker that this child has completed (#084, D104).
    # Must happen BEFORE cleanup (log is still on disk for the preview).
    _notify_parent_on_exit(
        name,
        log_path,
        reaped=ephemeral_reaped,
        idle_seconds=ephemeral_idle_elapsed if ephemeral_reaped else None,
    )

    cleanup_runtime_dir(name, reason="exit")
