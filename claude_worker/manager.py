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
from pathlib import Path

# -- Named constants --
FIFO_SELECT_TIMEOUT_SECONDS: float = 1.0
FIFO_READ_BUFFER_BYTES: int = 65536
SIGTERM_WAIT_TIMEOUT_SECONDS: float = 10.0
LOG_THREAD_JOIN_TIMEOUT_SECONDS: float = 5.0


def get_base_dir() -> Path:
    """Return /tmp/claude-workers/{UID}/."""
    return Path(f"/tmp/claude-workers/{os.getuid()}")


def get_runtime_dir(name: str) -> Path:
    """Return the runtime directory for a named worker."""
    return get_base_dir() / name


def create_runtime_dir(name: str) -> Path:
    """Create runtime directory with FIFOs. Returns the path."""
    runtime = get_runtime_dir(name)
    if runtime.exists():
        raise FileExistsError(f"Worker '{name}' already exists at {runtime}")
    runtime.mkdir(parents=True)
    os.mkfifo(runtime / "in")
    return runtime


def cleanup_runtime_dir(name: str) -> None:
    """Remove runtime directory and all contents.

    Idempotent: safe to call on a non-existent directory or concurrently
    from multiple callers (the SIGTERM handler, natural manager exit, and
    cmd_stop all race on this). Uses shutil.rmtree(ignore_errors=True)
    instead of iterdir+unlink so subdirectories are handled and a
    concurrent deletion between iterdir and unlink doesn't raise.
    """
    runtime = get_runtime_dir(name)
    shutil.rmtree(runtime, ignore_errors=True)


def get_sessions_file() -> Path:
    """Return path to the persistent name→session_id map."""
    return get_base_dir() / ".sessions.json"


def save_worker(name: str, **kwargs) -> None:
    """Persist worker metadata (session_id, cwd, agent, claude_args, etc.).

    Merges kwargs into any existing entry for this worker name.
    """
    path = get_sessions_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    sessions = {}
    if path.exists():
        try:
            sessions = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    # Migrate legacy string entries (old format: name → session_id)
    existing = sessions.get(name)
    if isinstance(existing, str):
        existing = {"session_id": existing}
    elif not isinstance(existing, dict):
        existing = {}
    existing.update(kwargs)
    sessions[name] = existing
    path.write_text(json.dumps(sessions, indent=2))


def get_saved_worker(name: str) -> dict | None:
    """Look up saved worker metadata by name.

    Returns a dict with keys like session_id, cwd, agent, claude_args.
    Returns None if no entry exists.
    """
    path = get_sessions_file()
    if not path.exists():
        return None
    try:
        sessions = json.loads(path.read_text())
        entry = sessions.get(name)
        if entry is None:
            return None
        # Migrate legacy string entries
        if isinstance(entry, str):
            return {"session_id": entry}
        return entry
    except (json.JSONDecodeError, OSError):
        return None


def run_manager(
    name: str,
    cwd: str | None,
    claude_args: list[str],
    initial_message: str | None,
) -> None:
    """Run the manager process (called after fork).

    This is the main loop that:
    1. Launches claude with stream-json I/O
    2. Bridges the `in` FIFO to claude's stdin
    3. Tees claude's stdout to the `log` file
    4. Captures session ID from the init message
    5. Sends initial prompt if provided
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

    # Build claude command
    cmd = [
        "claude",
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

    # Signal handling — forward SIGTERM to claude, then exit.
    # Wrapped in try/except so a stuck claude (subprocess.TimeoutExpired)
    # doesn't leave the manager tracebacked and the runtime dir uncleaned.
    # On timeout, escalate to SIGKILL and clean up unconditionally.
    def handle_term(signum, frame):
        try:
            proc.terminate()
            try:
                proc.wait(timeout=SIGTERM_WAIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=SIGTERM_WAIT_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass  # claude is truly stuck; cleanup anyway
        finally:
            cleanup_runtime_dir(name)
            sys.exit(0)

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)

    # Session ID capture event
    session_captured = threading.Event()

    # Thread: read claude stdout → log file
    def stdout_to_log():
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

    log_thread = threading.Thread(target=stdout_to_log, daemon=True)
    log_thread.start()

    # Thread: read from `in` FIFO → claude stdin
    # Uses a dummy write fd to prevent EOF when writers close.
    # Start this immediately so external senders don't block.
    def fifo_to_stdin():
        # Open read end non-blocking first
        rd_fd = os.open(str(in_fifo), os.O_RDONLY | os.O_NONBLOCK)
        # Open write end to keep FIFO alive (prevents EOF)
        wr_fd = os.open(str(in_fifo), os.O_WRONLY)

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
        except (OSError, BrokenPipeError):
            pass
        finally:
            os.close(rd_fd)
            os.close(wr_fd)

    fifo_thread = threading.Thread(target=fifo_to_stdin, daemon=True)
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
