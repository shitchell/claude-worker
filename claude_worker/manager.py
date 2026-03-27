"""Background manager process for a claude worker.

Handles subprocess lifecycle, FIFO plumbing, and log writing.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
from pathlib import Path


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
    """Remove runtime directory and all contents."""
    runtime = get_runtime_dir(name)
    if runtime.exists():
        for f in runtime.iterdir():
            f.unlink()
        runtime.rmdir()


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

    # Build environment — unset ANTHROPIC_API_KEY to force subscription auth
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    # Build claude command
    cmd = [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
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
        cwd=cwd or os.getcwd(),
    )

    # Signal handling — forward SIGTERM to claude, then exit
    def handle_term(signum, frame):
        proc.terminate()
        proc.wait(timeout=10)
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
                ready, _, _ = select.select([rd_fd], [], [], 1.0)
                if ready:
                    data = os.read(rd_fd, 65536)
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
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": initial_message},
        })
        proc.stdin.write((msg + "\n").encode())
        proc.stdin.flush()

    # Wait for claude to exit
    proc.wait()
    log_thread.join(timeout=5)
    cleanup_runtime_dir(name)
