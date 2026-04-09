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

# Context threshold wakeup — fire a synthetic user message when the
# worker crosses this fraction of its context window. Best-effort:
# never crashes the manager, fires at most once per session (sentinel
# file prevents re-fire).
CONTEXT_WAKEUP_THRESHOLD_PCT: float = 0.80
CONTEXT_WAKEUP_CHECK_INTERVAL_SECONDS: float = 30.0

# Context window size detection. Mirrors the constants in cli.py — we
# duplicate here to avoid a circular import (cli imports from manager).
_CONTEXT_WINDOW_1M: int = 1_000_000
_CONTEXT_WINDOW_DEFAULT: int = 200_000

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


def cleanup_runtime_dir(name: str) -> None:
    """Remove runtime directory and all contents.

    Idempotent: safe to call on a non-existent directory or concurrently
    from multiple callers (the SIGTERM handler, natural manager exit, and
    cmd_stop all race on this). Uses shutil.rmtree(ignore_errors=True)
    instead of iterdir+unlink so subdirectories are handled and a
    concurrent deletion between iterdir and unlink doesn't raise.

    Checks both the new (~/.cwork/workers/) and legacy (/tmp/) paths
    to handle workers started before the migration.
    """
    # Clean up in both possible locations
    for base in (get_base_dir(), _legacy_base_dir()):
        runtime = base / name
        shutil.rmtree(runtime, ignore_errors=True)


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


def _detect_context_window_size(log_file: Path) -> int:
    """Read the log's system/init message to determine context window size.

    Mirrors ``cli._detect_context_window_size`` — duplicated here to avoid
    a circular import (cli imports from manager). Falls back to 1M on any
    error (safer: underestimates percentage).
    """
    try:
        with open(log_file) as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "system" and data.get("subtype") == "init":
                    model = data.get("model", "")
                    if "[1m]" in model:
                        return _CONTEXT_WINDOW_1M
                    return _CONTEXT_WINDOW_DEFAULT
    except OSError:
        pass
    return _CONTEXT_WINDOW_1M


def _check_context_threshold(log_path: Path, runtime: Path, in_fifo: Path) -> None:
    """Fire a one-shot synthetic user message when context usage crosses the threshold.

    Best-effort: catches all exceptions so the manager's FIFO loop is never
    interrupted. Uses a sentinel file (``runtime / "wakeup-context-sent"``)
    to ensure the message fires at most once per session.
    """
    try:
        sentinel = runtime / "wakeup-context-sent"
        if sentinel.exists():
            return

        if not log_path.exists():
            return

        try:
            from claude_logs import compute_context_window_usage
        except ImportError:
            return

        cw = compute_context_window_usage(log_path)
        if cw is None:
            return

        window = _detect_context_window_size(log_path)
        pct = cw.total / window
        if pct < CONTEXT_WAKEUP_THRESHOLD_PCT:
            return

        # Threshold crossed — write the synthetic message to the FIFO
        pct_display = int(pct * 100)
        msg = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": (
                        f"[system:context-threshold] You are at approximately "
                        f"{pct_display}% of your context window. Begin your "
                        f"wrap-up procedure now."
                    ),
                },
            }
        )
        # Use a separate fd so we don't interfere with the FIFO loop's
        # own read/write descriptors. O_WRONLY will block until a reader
        # exists, but the FIFO loop's rd_fd is already open.
        wr = os.open(str(in_fifo), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(wr, (msg + "\n").encode())
        finally:
            os.close(wr)

        sentinel.write_text("")
    except Exception:
        # Best-effort — never crash the manager for a context check.
        pass


def run_manager(
    name: str,
    cwd: str | None,
    claude_args: list[str],
    initial_message: str | None,
) -> None:
    """Run the manager process (called after fork).

    Thin wrapper around ``_run_manager_forkless`` that installs signal
    handlers. Production uses this via cmd_start's fork + setsid +
    fd-redirect sequence. Tests drive ``_run_manager_forkless`` directly
    with ``install_signals=False`` so SIGTERM/SIGINT don't escape into
    the test runner, and so the helper can run in a thread instead of
    a forked process.
    """
    _run_manager_forkless(name, cwd, claude_args, initial_message, install_signals=True)


def _run_manager_forkless(
    name: str,
    cwd: str | None,
    claude_args: list[str],
    initial_message: str | None,
    install_signals: bool = True,
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

    if install_signals:
        signal.signal(signal.SIGTERM, handle_term)
        signal.signal(signal.SIGINT, handle_term)

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

        last_context_check = time.monotonic()

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

                # Periodic context threshold check — piggybacks on the
                # select() timeout so we don't need a separate thread.
                now = time.monotonic()
                if now - last_context_check >= CONTEXT_WAKEUP_CHECK_INTERVAL_SECONDS:
                    last_context_check = now
                    _check_context_threshold(log_path, runtime, in_fifo)
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
