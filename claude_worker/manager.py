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
from pathlib import Path

# -- Named constants --
FIFO_SELECT_TIMEOUT_SECONDS: float = 1.0
FIFO_READ_BUFFER_BYTES: int = 65536
SIGTERM_WAIT_TIMEOUT_SECONDS: float = 10.0
LOG_THREAD_JOIN_TIMEOUT_SECONDS: float = 5.0
QUEUE_DRAIN_INTERVAL_SECONDS: float = 5.0
CWORK_MONITOR_INTERVAL_SECONDS: float = 30.0

# Env var override for the claude binary path. Tests set this to point at
# a stub-claude script that emits canned JSONL output; production leaves
# it unset and defaults to the literal "claude" on PATH.
CLAUDE_BIN_ENV_VAR: str = "CLAUDE_WORKER_CLAUDE_BIN"
DEFAULT_CLAUDE_BIN: str = "claude"


def _resolve_claude_bin() -> str:
    """Return the claude binary path, honoring the CLAUDE_WORKER_CLAUDE_BIN
    env var for test injection. Defaults to ``"claude"`` (PATH lookup)."""
    return os.environ.get(CLAUDE_BIN_ENV_VAR) or DEFAULT_CLAUDE_BIN


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


def archive_runtime_dir(name: str) -> Path | None:
    """Rename runtime directory to a timestamped archive path.

    Used by the SIGUSR1 (graceful replace) handler to preserve the
    runtime dir for the replacement manager to read session metadata
    from. The archive path is deterministic from the name + timestamp +
    session ID prefix.

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
    return archive_path


ARCHIVE_RETENTION_DAYS: int = 30


def cleanup_runtime_dir(name: str) -> None:
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
        archive_runtime_dir(name)
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


def run_manager(
    name: str,
    cwd: str | None,
    claude_args: list[str],
    initial_message: str | None,
    identity: str = "worker",
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
    )


def _run_manager_forkless(
    name: str,
    cwd: str | None,
    claude_args: list[str],
    initial_message: str | None,
    install_signals: bool = True,
    identity: str = "worker",
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
    resolved_cwd = cwd or os.getcwd()

    # Build environment — unset ANTHROPIC_API_KEY to force subscription auth
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    # Worker identity env vars — available to hooks and Bash tool calls
    env["CW_WORKER_NAME"] = name
    env["CW_IDENTITY"] = identity
    env["CW_PARENT_WORKER"] = os.environ.get("CW_WORKER_NAME", "")

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

        Unlike handle_term, does NOT delete the runtime dir. The
        replacement manager needs the session file for --resume.
        """
        try:
            _kill_claude()
        finally:
            archive_runtime_dir(name)
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

    log_thread = threading.Thread(
        target=_run_manager_thread,
        args=(stdout_to_log_body, log_path, "stdout_to_log"),
        daemon=True,
    )
    log_thread.start()

    # Thread: read from `in` FIFO → claude stdin
    # Uses a dummy write fd to prevent EOF when writers close.
    # Start this immediately so external senders don't block.
    def fifo_to_stdin_body():
        # Open read end non-blocking first
        rd_fd = os.open(str(in_fifo), os.O_RDONLY | os.O_NONBLOCK)
        # Open write end to keep FIFO alive (prevents EOF)
        wr_fd = os.open(str(in_fifo), os.O_WRONLY)

        last_queue_drain = time.monotonic()
        last_cwork_check = time.monotonic()
        cwork_snapshot: dict[str, tuple[float, int]] = {}

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
    cleanup_runtime_dir(name)
