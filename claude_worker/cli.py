"""CLI entry point for claude-worker."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

# -- Named constants --

# Timeouts (seconds)
LOG_FILE_WAIT_TIMEOUT_SECONDS: float = 300.0
MANAGER_READY_TIMEOUT_SECONDS: float = 10.0
WORKER_READY_TIMEOUT_SECONDS: float = 30.0
DEFAULT_SETTLE_SECONDS: float = 3.0

# Polling intervals (seconds)
POLL_INTERVAL_SECONDS: float = 0.1
STOP_CLEANUP_DELAY_SECONDS: float = 0.5

# Display
LS_PREVIEW_MAX_CHARS: int = 80
SUMMARY_PREVIEW_MAX_CHARS: int = 80
UUID_SHORT_LENGTH: int = 8

# Queue correlation
QUEUE_WAIT_TIMEOUT_SECONDS: float = 600.0

# Hook installation
HOOK_SCRIPT_SOURCE_NAME: str = "session-uuid-env-injection.sh"
HOOK_SCRIPT_INSTALL_PATH: Path = (
    Path.home() / ".claude" / "hooks" / "session-uuid-env-injection.sh"
)
USER_SETTINGS_PATH: Path = Path.home() / ".claude" / "settings.json"
PROJECT_SETTINGS_RELATIVE_PATH: Path = Path(".claude") / "settings.json"
HOOK_EVENT_NAME: str = "SessionStart"

# Chat routing / PM mode
CHAT_TAG_PREFIX: str = "chat:"
QUEUE_TAG_PREFIX: str = "queue:"
PM_IDENTITY_RESOURCE: str = "pm.md"
PM_INTERNALIZE_MESSAGE: str = (
    "Initialize your PM state. Scan your own conversation history for any "
    "prior [chat:*] messages to recover ongoing consumer state. If this is "
    "a fresh worker, acknowledge readiness. Check for MEMORY.md and "
    "PROJECT.md in the current directory for project context. "
    "Report your initialization status."
)
MISSING_TAG_LOG_NAME: str = "missing-tags.json"
MISSING_TAG_PREVIEW_MAX_CHARS: int = 100

from claude_worker.manager import (
    cleanup_runtime_dir,
    create_runtime_dir,
    get_base_dir,
    get_runtime_dir,
    get_saved_worker,
    run_manager,
    save_worker,
)


def generate_name() -> str:
    """Generate a short random worker name."""
    import secrets

    return f"worker-{secrets.token_hex(2)}"


def resolve_worker(name: str) -> Path:
    """Resolve a worker name to its runtime directory, or error."""
    runtime = get_runtime_dir(name)
    if not runtime.exists():
        print(f"Error: worker '{name}' not found at {runtime}", file=sys.stderr)
        sys.exit(1)
    return runtime


def pid_alive(pid: int) -> bool:
    """Check if a PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_worker_status(runtime: Path) -> tuple[str, float | None]:
    """Determine worker status from PID and log state.

    In -p stream-json mode, each turn emits a `result` message but the process
    stays alive waiting for more input. A claude session never truly "completes" —
    it either idles (waiting), works, or its process dies.

    Returns (status, log_mtime) where log_mtime is the log file's modification
    time as a Unix timestamp, useful for computing idle duration.
    """
    pid_file = runtime / "pid"
    log_file = runtime / "log"

    # Check PID
    if not pid_file.exists():
        return "dead", None
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return "dead", None
    alive = pid_alive(pid)

    # Check last meaningful message in log
    if not log_file.exists():
        return ("starting" if alive else "dead"), None

    log_mtime = log_file.stat().st_mtime

    last_type = None
    last_stop_reason = None
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_type = data.get("type")
                if msg_type == "result":
                    last_type = "result"
                elif msg_type == "assistant":
                    msg = data.get("message", {})
                    sr = msg.get("stop_reason")
                    if sr:
                        last_stop_reason = sr
                        last_type = "assistant"
                elif msg_type == "user":
                    last_stop_reason = None
                    last_type = "user"
    except OSError:
        pass

    if not alive:
        return "dead", log_mtime
    # result with process alive = turn complete, waiting for next input
    if last_type == "result":
        return "waiting", log_mtime
    if last_stop_reason == "end_turn":
        return "waiting", log_mtime
    return "working", log_mtime


# -- Shared helpers --


def _extract_text_preview(data: dict, max_chars: int) -> str:
    """Extract the first line of text content from a JSONL message, truncated.

    Works for both assistant and user messages by inspecting the content blocks.
    Falls back to the raw content string if content is not a list.
    """
    content = data.get("message", {}).get("content", "")
    text = ""
    if isinstance(content, list):
        for block in content:
            if block.get("type") == "text" and block.get("text", "").strip():
                text = block["text"].strip()
                break
    elif isinstance(content, str):
        text = content.strip()

    # Collapse to single line
    text = " ".join(text.split())

    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


def _format_duration_since(mtime: float) -> str:
    """Format a human-readable duration from a Unix timestamp to now."""
    secs = int(time.time() - mtime)
    if secs < 0:
        return ""
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    remaining_mins = mins % 60
    if hours < 24:
        return f"{hours}h{remaining_mins}m" if remaining_mins else f"{hours}h"
    days = hours // 24
    remaining_hours = hours % 24
    return f"{days}d{remaining_hours}h" if remaining_hours else f"{days}d"


def _format_msg_prefix(data: dict) -> str:
    """Format a [HH:MM:SS uuid] prefix from a JSONL message dict."""
    from datetime import datetime, timezone

    uuid = data.get("uuid", "")[:UUID_SHORT_LENGTH]
    ts = ""
    ts_raw = data.get("timestamp", "")
    if ts_raw:
        try:
            parsed = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            # Convert to local timezone
            local = parsed.astimezone()
            ts = local.strftime("%H:%M:%S")
        except ValueError:
            pass
    if ts and uuid:
        return f"[{ts} {uuid}] "
    if uuid:
        return f"[{uuid}] "
    return ""


def _print_worker_status(name: str) -> None:
    """Print a single-worker status line (same format as `list`)."""
    line = _format_worker_line(name)
    if line:
        print(line)


def _wait_for_ready_state(
    name: str, timeout: float = WORKER_READY_TIMEOUT_SECONDS
) -> tuple[str, float | None]:
    """Block while worker is `starting`, return when it reaches a terminal state.

    Terminal states for this helper: `waiting`, `working`, `dead`.
    The `starting` state is transient and means "no log output yet."

    Returns the final (status, log_mtime) tuple. Raises TimeoutError if the
    worker stays in `starting` longer than `timeout` seconds.
    """
    runtime = get_runtime_dir(name)
    deadline = time.monotonic() + timeout
    while True:
        status, log_mtime = get_worker_status(runtime)
        if status != "starting":
            return status, log_mtime
        if time.monotonic() > deadline:
            raise TimeoutError(f"Worker '{name}' stayed in 'starting' for {timeout}s")
        time.sleep(POLL_INTERVAL_SECONDS)


def _generate_queue_id() -> str:
    """Generate a correlation ID for queued messages.

    Uses epoch milliseconds — deterministic without increment state,
    visually distinct from UUIDs, and works across multiple orchestrators.
    Sub-millisecond collisions are acceptable for the current use case.
    """
    return str(int(time.time() * 1000))


def _wait_for_queue_response(
    name: str, queue_id: str, timeout: float = QUEUE_WAIT_TIMEOUT_SECONDS
) -> int:
    """Tail the log waiting for an assistant message containing [queue:{id}].

    Returns 0 if the correlation tag is found, 1 if the worker dies, 2 on timeout.
    """
    runtime = get_runtime_dir(name)
    log_file = runtime / "log"
    pid_file = runtime / "pid"
    tag = f"[{QUEUE_TAG_PREFIX}{queue_id}]"

    def _manager_alive() -> bool:
        try:
            pid = int(pid_file.read_text().strip())
            return pid_alive(pid)
        except (ValueError, OSError):
            return False

    if not log_file.exists():
        log_deadline = time.monotonic() + timeout
        while not log_file.exists():
            if time.monotonic() > log_deadline:
                print("Error: timeout waiting for log file", file=sys.stderr)
                return 2
            time.sleep(POLL_INTERVAL_SECONDS)

    deadline = time.monotonic() + timeout

    # Scan existing log first — the response may have already arrived.
    with open(log_file) as f:
        for line in f:
            if tag in line:
                return 0
        # Tail from current position (end of existing content)
        while True:
            if time.monotonic() > deadline:
                print(f"Error: timeout waiting for {tag}", file=sys.stderr)
                return 2
            line = f.readline()
            if not line:
                if not _manager_alive():
                    print("Error: worker process died", file=sys.stderr)
                    return 1
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            if tag in line:
                return 0


def _settle_is_stable(log_file: Path, settle: float) -> bool:
    """Wait `settle` seconds, return True if no new messages appeared.

    Returns True immediately when ``settle <= 0``. Used by ``_wait_for_turn``
    to debounce the return when a worker briefly idles between internal
    subagent dispatches — a turn boundary that "sticks" for the full settle
    window is considered real, while one that flips back to activity is not.
    """
    if settle <= 0:
        return True
    uuid_before = _get_last_uuid(log_file)
    time.sleep(settle)
    uuid_after = _get_last_uuid(log_file)
    return uuid_after == uuid_before


def _wait_for_turn(
    name: str,
    timeout: float | None = None,
    after_uuid: str | None = None,
    settle: float = 0.0,
) -> int:
    """Block until claude finishes its turn. Returns exit code (0=ready, 1=dead, 2=timeout).

    If ``after_uuid`` is provided, only log entries appearing *after* that
    UUID are considered. This lets callers who just wrote to the FIFO avoid
    a race where the scan finds the PREVIOUS turn's `result` message before
    the new user message has been forwarded to claude.

    If ``settle > 0``, after detecting a turn boundary this function waits
    ``settle`` seconds and confirms no new messages appeared before returning.
    A brief idle flipping back to activity (e.g. a subagent dispatch) restarts
    the wait. The settle duration counts against ``timeout``.
    """
    runtime = get_runtime_dir(name)
    log_file = runtime / "log"
    pid_file = runtime / "pid"

    if not log_file.exists():
        deadline = time.monotonic() + (timeout or LOG_FILE_WAIT_TIMEOUT_SECONDS)
        while not log_file.exists():
            if time.monotonic() > deadline:
                print("Error: timeout waiting for log file", file=sys.stderr)
                return 2
            time.sleep(POLL_INTERVAL_SECONDS)

    deadline = None
    if timeout:
        deadline = time.monotonic() + timeout

    def _manager_alive() -> bool:
        try:
            pid = int(pid_file.read_text().strip())
            return pid_alive(pid)
        except (ValueError, OSError):
            return False

    # Scan existing log to determine current state.
    # Track: after the most recent user message, has a turn boundary appeared?
    # When after_uuid is set, ignore entries up to and including that UUID.
    turn_end_after_last_user = None
    passed_marker = after_uuid is None
    with open(log_file) as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not passed_marker:
                msg_uuid = data.get("uuid", "")
                if after_uuid and _uuid_matches(msg_uuid, after_uuid):
                    passed_marker = True
                continue
            msg_type = data.get("type")
            if msg_type == "user":
                turn_end_after_last_user = None  # reset on new user msg
            elif msg_type == "result":
                turn_end_after_last_user = data
            elif msg_type == "assistant":
                sr = data.get("message", {}).get("stop_reason")
                if sr == "end_turn":
                    turn_end_after_last_user = data

    # Scan found an already-complete turn. Confirm it's stable before returning.
    if turn_end_after_last_user is not None:
        if _settle_is_stable(log_file, settle):
            return 0
        # Fell through: new activity during settle — drop into tail loop to
        # wait for the next turn boundary.

    if not _manager_alive():
        print("Error: worker process died", file=sys.stderr)
        return 1

    with open(log_file) as f:
        f.seek(0, 2)  # seek to end
        while True:
            if deadline and time.monotonic() > deadline:
                print("Error: timeout", file=sys.stderr)
                return 2

            line = f.readline()
            if not line:
                if not _manager_alive():
                    print("Error: worker process died", file=sys.stderr)
                    return 1
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            turn_ended = False
            if msg_type == "result":
                turn_ended = True
            elif msg_type == "assistant":
                sr = data.get("message", {}).get("stop_reason")
                if sr == "end_turn":
                    turn_ended = True

            if turn_ended:
                if _settle_is_stable(log_file, settle):
                    return 0
                # New activity during settle: keep tailing for the next
                # turn boundary. Re-seek to end so we don't re-read the
                # messages that arrived during the settle window.
                f.seek(0, 2)
                continue


# -- Subcommand handlers --


def cmd_start(args: argparse.Namespace) -> None:
    """Start a new claude worker."""
    name = args.name or generate_name()

    # Handle --resume: restore saved startup vars (cwd, claude_args)
    claude_args = list(args.claude_args or [])
    if args.resume:
        saved = get_saved_worker(name)
        if not saved or not saved.get("session_id"):
            print(f"Error: no saved session for worker '{name}'", file=sys.stderr)
            sys.exit(1)
        # Restore saved cwd unless explicitly overridden
        if not args.cwd and saved.get("cwd"):
            args.cwd = saved["cwd"]
        # Restore saved claude_args (which already includes --agent, etc.)
        # and append any new args the user provided on this invocation
        extra = claude_args
        claude_args = (
            ["--resume", saved["session_id"]] + (saved.get("claude_args") or []) + extra
        )
    else:
        # Build claude_args with --agent etc. (order matters: agent first)
        if args.agent:
            claude_args = ["--agent", args.agent] + claude_args

    # Save startup vars for future --resume (claude_args without --resume prefix)
    saved_args = (
        claude_args if not args.resume else claude_args[2:]
    )  # strip --resume <sid>
    save_worker(
        name,
        cwd=args.cwd or os.getcwd(),
        claude_args=saved_args,
    )

    # Build initial message from prompt-file and/or prompt
    parts = []
    if args.prompt_file:
        parts.append(Path(args.prompt_file).read_text())
    if args.prompt:
        parts.append(args.prompt)
    initial_message = "\n\n".join(parts) if parts else None

    # Create runtime directory
    try:
        runtime = create_runtime_dir(name)
    except FileExistsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # --show-response and --show-full-response are mutually exclusive
    if args.show_response and args.show_full_response:
        print(
            "Error: --show-response and --show-full-response are mutually exclusive",
            file=sys.stderr,
        )
        sys.exit(1)

    # Fork to background
    pid = os.fork()
    if pid > 0:
        # Parent — wait for manager to be ready, then optionally wait for turn
        pid_file = runtime / "pid"

        # Wait for PID file (manager is running)
        deadline = time.monotonic() + MANAGER_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if pid_file.exists():
                break
            time.sleep(POLL_INTERVAL_SECONDS)

        # If we sent a prompt, wait for the turn to complete (unless --background)
        if initial_message and not args.background:
            rc = _wait_for_turn(name)
            # --show-response / --show-full-response: print the response
            # before the status line so status appears at the bottom.
            # There was no "before" marker for start (fresh worker), so
            # --show-full-response means "show everything from the start."
            if rc == 0 and args.show_response:
                _show_worker_response(name, last_turn=True)
            elif rc == 0 and args.show_full_response:
                _show_worker_response(name)
            _print_worker_status(name)
            sys.exit(rc)

        # --background or no prompt: print status and return
        _print_worker_status(name)
        return

    # Child — detach and become manager
    os.setsid()
    # Close inherited fds
    sys.stdin.close()
    sys.stdout.close()
    sys.stderr.close()
    # Redirect std fds to /dev/null
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)

    run_manager(
        name=name,
        cwd=args.cwd,
        claude_args=claude_args,
        initial_message=initial_message,
    )
    os._exit(0)


def cmd_send(args: argparse.Namespace) -> None:
    """Send a message to a worker.

    Default behavior: check worker status first and reject if busy. Use
    ``--queue`` to bypass the busy check and track a specific response via a
    correlation ID embedded in the message.
    """
    runtime = resolve_worker(args.name)
    in_fifo = runtime / "in"
    log_file = runtime / "log"

    # --queue + --background is incoherent: the whole point of queue is
    # correlation tracking, which requires waiting for the tagged response.
    if args.queue and args.background:
        print("Error: --queue and --background are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    if args.show_response and args.show_full_response:
        print(
            "Error: --show-response and --show-full-response are mutually exclusive",
            file=sys.stderr,
        )
        sys.exit(1)

    # Get message from arg or stdin
    if args.message:
        content = " ".join(args.message)
    else:
        content = sys.stdin.read()

    if not content.strip():
        print("Error: empty message", file=sys.stderr)
        sys.exit(1)

    # Status gate: refuse to send to a busy worker unless --queue was passed.
    # `starting` is transient — wait for it to clear. `dead` is fatal.
    if not args.queue:
        try:
            status, _ = _wait_for_ready_state(args.name)
        except TimeoutError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if status == "dead":
            print(
                f"Error: worker '{args.name}' is dead. "
                f"Use `claude-worker start --resume --name {args.name}` to restart.",
                file=sys.stderr,
            )
            sys.exit(1)
        if status == "working":
            print(
                f"Error: worker '{args.name}' is busy. "
                f"Use `--queue` to send anyway with correlation tracking.",
                file=sys.stderr,
            )
            sys.exit(1)
        # status == "waiting" → proceed

    # Remember the last UUID BEFORE writing to the FIFO. This marker serves
    # two purposes:
    #   1. `--show-full-response` uses it as a `--since` marker to render
    #      everything new since the send.
    #   2. `_wait_for_turn` uses it to ignore the prior turn's `result`
    #      message — otherwise the scan phase would find the OLD turn
    #      boundary and return immediately before the new user message
    #      reaches claude (race condition).
    marker_uuid = _get_last_uuid(log_file)

    # If --queue, append a correlation instruction so we can detect the
    # specific response that corresponds to THIS send.
    queue_id: str | None = None
    if args.queue:
        queue_id = _generate_queue_id()
        content = (
            content
            + f"\n\n[Please include [{QUEUE_TAG_PREFIX}{queue_id}] literally in your response so the sender can identify it.]"
        )

    msg = json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": content},
        }
    )

    with open(in_fifo, "w") as f:
        f.write(msg + "\n")
        f.flush()

    if args.background:
        _print_worker_status(args.name)
        return

    if queue_id is not None:
        rc = _wait_for_queue_response(args.name, queue_id)
    else:
        rc = _wait_for_turn(args.name, after_uuid=marker_uuid)

    # Print response BEFORE the status line so the status appears at the
    # bottom (last line the user sees) — matching the style of cmd_start.
    if rc == 0 and args.show_response:
        _show_worker_response(args.name, last_turn=True)
    elif rc == 0 and args.show_full_response:
        _show_worker_response(args.name, since_uuid=marker_uuid)

    _print_worker_status(args.name)
    sys.exit(rc)


def cmd_read(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Read worker output, formatted via claude_logs.

    Returns (first_uuid, last_uuid) for the messages that were actually
    rendered, which programmatic callers (like --show-response) use to
    display a range hint. The normal CLI invocation ignores the return value.
    """
    runtime = resolve_worker(args.name)
    log_file = runtime / "log"

    if not log_file.exists():
        print("No log output yet.", file=sys.stderr)
        sys.exit(1)

    from claude_logs import (
        ANSIFormatter,
        FilterConfig,
        MarkdownFormatter,
        PlainFormatter,
        RenderConfig,
    )
    from claude_logs.dateparse import parse_datetime

    # We always hide these — we render our own prefix with uuid + local time
    hidden = {
        "timestamps",
        "metadata",
    }

    if args.verbose:
        # Show everything meaningful
        hidden |= {"progress", "file-history-snapshot", "last-prompt"}
        filters = FilterConfig(hidden=hidden)
    else:
        # Default: conversational messages only — whitelist user-input (human-
        # typed messages) and assistant, hiding tool results, system, etc.
        # Also hide tool/thinking content blocks from assistant messages.
        hidden |= {"thinking", "tools"}
        filters = FilterConfig(
            show_only={"user-input", "assistant", "queue-operation"},
            hidden=hidden,
        )

    config = RenderConfig(filters=filters)

    # Handle --since
    since_ts = None
    since_uuid = None
    if args.since:
        val = args.since.strip()
        # Accept full UUIDs (36 chars) or short prefixes (like the 8-char
        # IDs shown in read output). Hex-only strings are treated as UUID
        # prefixes; anything else is parsed as a timestamp.
        hex_val = val.replace("-", "")
        if hex_val and all(c in "0123456789abcdefABCDEF" for c in hex_val):
            since_uuid = val
        else:
            try:
                since_ts = parse_datetime(val)
            except Exception:
                print(f"Error: cannot parse --since value: {val}", file=sys.stderr)
                sys.exit(1)

    # Use markdown when running inside Claude Code (CLAUDECODE env var) —
    # supervisor claudes parse markdown better than ANSI or plain text.
    # ANSI colors for human terminals. Override with --color/--no-color.
    if args.color:
        formatter = ANSIFormatter()
    elif args.no_color:
        formatter = PlainFormatter()
    elif os.environ.get("CLAUDECODE"):
        formatter = MarkdownFormatter()
    else:
        formatter = ANSIFormatter()

    if args.follow:
        _read_follow(log_file, config, formatter, since_uuid, since_ts, args)
        return None, None
    return _read_static(log_file, config, formatter, since_uuid, since_ts, args)


def _uuid_matches(msg_uuid: str, target: str) -> bool:
    """Case-insensitive UUID prefix match."""
    return msg_uuid.lower().startswith(target.lower())


def _show_worker_response(
    name: str,
    last_turn: bool = False,
    since_uuid: str | None = None,
) -> None:
    """Print a worker's response by invoking cmd_read programmatically.

    Used by `send --show-response` / `start --show-response` and their
    `--show-full-response` variants. Mutually exclusive flags at the caller
    decide which window to show:

    - ``last_turn=True``: equivalent to `read --last-turn` — just the
      assistant's turn after the user's last message.
    - ``since_uuid=X``: equivalent to `read --since X` — everything newer
      than the given marker UUID, including the echoed user message.

    After rendering, prints a hint with the first/last UUIDs of the shown
    window so the caller can re-query that exact range.
    """
    namespace = argparse.Namespace(
        name=name,
        follow=False,
        since=since_uuid,
        until=None,
        last_turn=last_turn,
        n=None,
        count=False,
        summary=False,
        verbose=False,
        color=False,
        no_color=False,
        no_hint=True,
    )
    first_uuid, last_uuid = cmd_read(namespace)
    if first_uuid and last_uuid:
        print(
            f"\nTo see this window again or expand: "
            f"claude-worker read {name} "
            f"--since {first_uuid[:UUID_SHORT_LENGTH]} "
            f"--until {last_uuid[:UUID_SHORT_LENGTH]}"
        )


def _get_last_uuid(log_file: Path) -> str | None:
    """Return the UUID of the most recent message in the log, or None.

    Used as a marker before sending so the caller can later use --since
    to show everything that arrived after this point.
    """
    if not log_file.exists():
        return None
    last_uuid: str | None = None
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                uuid = data.get("uuid", "")
                if uuid:
                    last_uuid = uuid
    except OSError:
        pass
    return last_uuid


def _get_last_assistant_preview(log_file: Path, max_chars: int) -> str:
    """Return a single-line preview of the most recent assistant text message.

    Returns the empty string if the log does not exist or no assistant text
    message is found. Used by `ls` to show "what's the worker doing" at a
    glance.
    """
    if not log_file.exists():
        return ""
    last_preview = ""
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("type") != "assistant":
                    continue
                preview = _extract_text_preview(data, max_chars)
                if preview:
                    last_preview = preview
    except OSError:
        pass
    return last_preview


def _read_static(
    log_file, config, formatter, since_uuid, since_ts, args
) -> tuple[str | None, str | None]:
    """Read log file statically.

    Returns (first_uuid, last_uuid) for the messages actually printed, so
    callers (like --show-response) can display a range hint. Returns
    (None, None) if nothing was printed.
    """
    from claude_logs import parse_message, should_show_message
    from datetime import datetime, timezone

    found_since = since_uuid is None and since_ts is None
    messages = []
    total_scanned = 0
    # Remember the --since marker message so we can show its content if the
    # result set is empty ("No new messages since [abc12345]: ...")
    since_marker_data: dict | None = None

    with open(log_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            total_scanned += 1

            # Handle --since filtering
            if not found_since:
                if since_uuid:
                    msg_uuid = data.get("uuid", "")
                    if _uuid_matches(msg_uuid, since_uuid):
                        found_since = True
                        since_marker_data = data
                        continue  # skip the since message itself
                if since_ts:
                    ts_str = data.get("timestamp", "")
                    if ts_str:
                        try:
                            msg_ts = datetime.fromisoformat(
                                ts_str.replace("Z", "+00:00")
                            )
                            if msg_ts >= since_ts.replace(tzinfo=timezone.utc):
                                found_since = True
                        except ValueError:
                            pass
                if not found_since:
                    continue

            # Handle --until filtering
            if hasattr(args, "until") and args.until:
                msg_uuid = data.get("uuid", "")
                if _uuid_matches(msg_uuid, args.until):
                    break

            # Handle --last-turn: reset the message list every time we hit a
            # raw user message. This must run BEFORE display filtering because
            # claude_logs classifies replayed user messages as non-"user-input"
            # and drops them — so we cannot rely on the filtered list to
            # locate turn boundaries.
            if (
                hasattr(args, "last_turn")
                and args.last_turn
                and data.get("type") == "user"
            ):
                messages = []

            msg = parse_message(data)
            if not should_show_message(msg, data, config):
                continue

            # In non-verbose mode, skip messages with no text content
            # (e.g. tool_use-only assistant turns, tool_result-only user turns)
            if not args.verbose:
                content = data.get("message", {}).get("content", [])
                if isinstance(content, list):
                    has_text = any(
                        c.get("type") == "text" and c.get("text", "").strip()
                        for c in content
                    )
                    if not has_text:
                        continue

            messages.append((data, msg))

    # Warn when --since UUID was not found in the log
    if (since_uuid or since_ts) and not found_since:
        target = since_uuid or str(since_ts)
        print(
            f"Warning: --since '{target}' not found in log ({total_scanned} messages scanned)",
            file=sys.stderr,
        )
        return None, None

    # Handle -n: keep only the last N messages
    if hasattr(args, "n") and args.n is not None:
        messages = messages[-args.n :]

    if not messages and found_since and (since_uuid or since_ts):
        if since_marker_data is not None:
            marker_uuid_short = since_marker_data.get("uuid", "")[:UUID_SHORT_LENGTH]
            marker_preview = _extract_text_preview(
                since_marker_data, SUMMARY_PREVIEW_MAX_CHARS
            )
            print(
                f"No new messages since [{marker_uuid_short}]: {marker_preview}",
                file=sys.stderr,
            )
        else:
            print("No new messages after that point.", file=sys.stderr)
        return None, None

    # Alternative output modes: --count and --summary
    if hasattr(args, "count") and args.count:
        print(len(messages))
        return None, None

    if hasattr(args, "summary") and args.summary:
        first_uuid: str | None = None
        last_uuid: str | None = None
        for data, msg in messages:
            uuid_short = data.get("uuid", "")[:UUID_SHORT_LENGTH]
            role = data.get("type", "?")
            text = _extract_text_preview(data, SUMMARY_PREVIEW_MAX_CHARS)
            print(f"[{uuid_short}] {role}: {text}")
            uuid = data.get("uuid", "")
            if uuid:
                if first_uuid is None:
                    first_uuid = uuid
                last_uuid = uuid
        return first_uuid, last_uuid

    last_uuid = None
    first_uuid = None
    for data, msg in messages:
        blocks = msg.render(config)
        output = formatter.format(blocks)
        if output.strip():
            prefix = _format_msg_prefix(data)
            lines = output.split("\n")
            lines[0] = prefix + lines[0]
            print("\n".join(lines))
            uuid = data.get("uuid", "")
            if uuid:
                if first_uuid is None:
                    first_uuid = uuid
                last_uuid = uuid

    # The bottom-of-output "to see new messages" hint is only shown when
    # called directly from `read` — programmatic callers (like --show-response)
    # set args.no_hint and print their own hint using the returned UUIDs.
    suppress_hint = getattr(args, "no_hint", False)
    if last_uuid and not suppress_hint:
        print(
            f"\nTo see NEW messages after this point: "
            f"claude-worker read {args.name} --since {last_uuid[:UUID_SHORT_LENGTH]}"
        )

    return first_uuid, last_uuid


def _read_follow(log_file, config, formatter, since_uuid, since_ts, args):
    """Tail the log file, printing new messages as they appear."""
    from claude_logs import parse_message, should_show_message
    import time as _time

    # First, print existing content
    _read_static(log_file, config, formatter, since_uuid, since_ts, args)

    # Then tail
    with open(log_file) as f:
        f.seek(0, 2)  # seek to end
        try:
            while True:
                line = f.readline()
                if not line:
                    _time.sleep(POLL_INTERVAL_SECONDS)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = parse_message(data)
                if should_show_message(msg, data, config):
                    blocks = msg.render(config)
                    output = formatter.format(blocks)
                    if output.strip():
                        prefix = _format_msg_prefix(data)
                        lines = output.split("\n")
                        lines[0] = prefix + lines[0]
                        print("\n".join(lines), flush=True)
        except KeyboardInterrupt:
            pass


def cmd_wait_for_turn(args: argparse.Namespace) -> None:
    """Block until claude finishes its turn or the session ends."""
    resolve_worker(args.name)  # validate worker exists
    rc = _wait_for_turn(args.name, timeout=args.timeout, settle=args.settle)
    sys.exit(rc)


def _format_worker_line(name: str) -> str | None:
    """Format a single worker status line. Returns None if not a valid worker dir."""
    runtime = get_runtime_dir(name)
    if not runtime.exists():
        return None

    pid_file = runtime / "pid"
    session_file = runtime / "session"

    pid = "-"
    if pid_file.exists():
        try:
            pid = pid_file.read_text().strip()
        except OSError:
            pass

    session = "-"
    if session_file.exists():
        try:
            sid = session_file.read_text().strip()
            session = sid
        except OSError:
            pass

    # Read CWD from saved worker metadata
    cwd = "-"
    saved = get_saved_worker(name)
    if saved and saved.get("cwd"):
        home = os.path.expanduser("~")
        if saved["cwd"].startswith(home):
            cwd = "~" + saved["cwd"][len(home) :]
        else:
            cwd = saved["cwd"]

    status, log_mtime = get_worker_status(runtime)
    idle_str = ""
    if log_mtime is not None and status in ("waiting", "dead"):
        idle_str = _format_duration_since(log_mtime)
        if idle_str:
            idle_str = f"  idle: {idle_str}"

    # "Last assistant text" preview — answers "what's the worker doing?"
    # without requiring a separate `read` call.
    log_file = runtime / "log"
    preview = _get_last_assistant_preview(log_file, LS_PREVIEW_MAX_CHARS)
    preview_line = f"\n    last: {preview}" if preview else ""

    return (
        f"  {name}\n"
        f"    pid: {pid}  status: {status}{idle_str}  cwd: {cwd}\n"
        f"    session: {session}"
        f"{preview_line}"
    )


def cmd_list(args: argparse.Namespace) -> None:
    """List all workers."""
    base = get_base_dir()
    if not base.exists():
        return

    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        line = _format_worker_line(entry.name)
        if line:
            print(line)


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop a worker."""
    runtime = resolve_worker(args.name)
    pid_file = runtime / "pid"

    if not pid_file.exists():
        print(f"No PID file for worker '{args.name}'", file=sys.stderr)
        cleanup_runtime_dir(args.name)
        return

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        print("Error: invalid PID file", file=sys.stderr)
        cleanup_runtime_dir(args.name)
        sys.exit(1)

    sig = signal.SIGKILL if args.force else signal.SIGTERM
    try:
        os.kill(pid, sig)
        print(f"Sent {'SIGKILL' if args.force else 'SIGTERM'} to {pid}")
    except ProcessLookupError:
        print(f"Process {pid} already dead")
    except PermissionError:
        print(f"Error: permission denied killing {pid}", file=sys.stderr)
        sys.exit(1)

    # Wait briefly for cleanup, then force-clean if needed
    time.sleep(STOP_CLEANUP_DELAY_SECONDS)
    if runtime.exists():
        cleanup_runtime_dir(args.name)
        print(f"Cleaned up {runtime}")


def _load_bundled_resource(subdir: str, filename: str) -> str:
    """Return the text contents of a resource bundled with the package.

    Uses importlib.resources so it works whether the package is installed
    from wheel, sdist, or in editable mode.
    """
    from importlib.resources import files

    return (files("claude_worker") / subdir / filename).read_text()


def _format_settings_json(settings: dict) -> str:
    """Serialize settings dict the way Claude Code does: 2-space indent + newline."""
    return json.dumps(settings, indent=2) + "\n"


def _hook_already_installed(settings: dict, hook_command_fragment: str) -> bool:
    """Check whether a SessionStart hook referencing the given command exists."""
    session_start = settings.get("hooks", {}).get(HOOK_EVENT_NAME, [])
    if not isinstance(session_start, list):
        return False
    for entry in session_start:
        hooks = entry.get("hooks", []) if isinstance(entry, dict) else []
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            if hook.get("type") != "command":
                continue
            if hook_command_fragment in hook.get("command", ""):
                return True
    return False


def _merge_session_start_hook(settings: dict, hook_command: str) -> dict:
    """Return a new settings dict with the SessionStart hook appended.

    Preserves any existing SessionStart entries; adds a new entry alongside.
    """
    merged = json.loads(json.dumps(settings))  # deep copy via round-trip
    hooks = merged.setdefault("hooks", {})
    session_start = hooks.setdefault(HOOK_EVENT_NAME, [])
    if not isinstance(session_start, list):
        session_start = []
        hooks[HOOK_EVENT_NAME] = session_start
    session_start.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": hook_command,
                }
            ]
        }
    )
    return merged


def _render_settings_diff(before: str, after: str, path: Path) -> str:
    """Return a unified diff between two settings.json serializations."""
    import difflib

    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{path} (current)",
            tofile=f"{path} (proposed)",
            n=3,
        )
    )


def cmd_install_hook(args: argparse.Namespace) -> None:
    """Install the SessionStart hook that sets CLAUDE_SESSION_UUID.

    Writes the hook script to ``~/.claude/hooks/session-uuid-env-injection.sh``
    and merges a SessionStart hook entry into the target settings file.
    Idempotent: detects an existing installation and skips unless --force.
    """
    # Resolve target settings path
    if args.project:
        settings_path = Path.cwd() / PROJECT_SETTINGS_RELATIVE_PATH
    else:
        settings_path = USER_SETTINGS_PATH

    # 1. Write the hook script itself (always — it's outside settings.json)
    HOOK_SCRIPT_INSTALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    script_source = _load_bundled_resource("hooks", HOOK_SCRIPT_SOURCE_NAME)
    script_already_current = (
        HOOK_SCRIPT_INSTALL_PATH.exists()
        and HOOK_SCRIPT_INSTALL_PATH.read_text() == script_source
    )
    if not script_already_current:
        HOOK_SCRIPT_INSTALL_PATH.write_text(script_source)
        HOOK_SCRIPT_INSTALL_PATH.chmod(0o755)
        print(f"Wrote hook script: {HOOK_SCRIPT_INSTALL_PATH}")
    else:
        print(f"Hook script already up to date: {HOOK_SCRIPT_INSTALL_PATH}")

    # 2. Load existing settings (or start fresh)
    if settings_path.exists():
        try:
            current_settings = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Error: could not parse {settings_path}: {exc}", file=sys.stderr)
            sys.exit(1)
        current_text = _format_settings_json(current_settings)
    else:
        current_settings = {}
        current_text = "(file does not exist)\n"

    # 3. Idempotency check — the hook command references the install path
    hook_command = f"bash {HOOK_SCRIPT_INSTALL_PATH}"
    if _hook_already_installed(current_settings, str(HOOK_SCRIPT_INSTALL_PATH)):
        if not args.force:
            print(
                f"Hook already installed in {settings_path}. "
                f"Use --force to add a duplicate entry.",
                file=sys.stderr,
            )
            print(
                f'\nTest with: claude -p "echo $CLAUDE_SESSION_UUID"',
                file=sys.stderr,
            )
            return

    # 4. Build the proposed settings and show diff
    proposed_settings = _merge_session_start_hook(current_settings, hook_command)
    proposed_text = _format_settings_json(proposed_settings)
    diff = _render_settings_diff(current_text, proposed_text, settings_path)
    print("\nProposed changes:")
    print(diff if diff else "(no changes)")

    # 5. Confirm unless --yes
    if not args.yes:
        try:
            response = input("\nApply these changes? [y/N] ").strip().lower()
        except EOFError:
            response = ""
        if response not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            sys.exit(1)

    # 6. Write the settings file
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(proposed_text)
    print(f"Updated {settings_path}")
    print(f'\nTest with: claude -p "echo $CLAUDE_SESSION_UUID"')


EXAMPLES = """\
examples:
  # Start a worker — blocks until claude responds, then prints status
  claude-worker start --name researcher --prompt "You are a research assistant"

  # Read the response
  claude-worker read researcher --last-turn

  # Send a message — blocks until claude responds
  claude-worker send researcher "summarize the architecture of this repo"
  claude-worker read researcher --last-turn

  # Fire-and-forget with --background
  claude-worker send researcher "do something long" --background
  # ... do other work ...
  claude-worker wait-for-turn researcher

  # Follow output in real-time
  claude-worker read researcher --follow

  # List all workers
  claude-worker list

  # Stop and clean up
  claude-worker stop researcher

  # Start with a prompt file and extra claude args
  claude-worker start --name coder --cwd /path/to/repo \\
    --prompt-file instructions.md --prompt "begin with step 1" \\
    -- --model sonnet

  # Pipe a message via stdin
  cat question.txt | claude-worker send researcher

  # Start without blocking
  claude-worker start --name bg-worker --prompt "you are a helper" --background

  # Use a custom agent (from ~/.claude/agents/)
  claude-worker start --name pm --agent project-manager \\
    --prompt "plan the auth module implementation"
"""


def main():
    parser = argparse.ArgumentParser(
        prog="claude-worker",
        description="Launch and communicate with Claude Code subprocess workers",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- start --
    p_start = sub.add_parser("start", help="Start a new claude worker")
    p_start.add_argument("--name", "-n", help="Worker name (auto-generated if omitted)")
    p_start.add_argument("--cwd", help="Working directory for claude")
    p_start.add_argument("--prompt-file", help="File to send as initial prompt content")
    p_start.add_argument("--prompt", help="String to send as initial prompt")
    p_start.add_argument(
        "--agent", help="Agent for the current session. Overrides the 'agent' setting."
    )
    p_start.add_argument(
        "--resume",
        action="store_true",
        help="Resume a previous session with the same worker name",
    )
    p_start.add_argument(
        "--background",
        action="store_true",
        help="Return immediately without waiting for claude's response",
    )
    p_start.add_argument(
        "--show-response",
        action="store_true",
        help="After the initial turn completes, print the assistant's response",
    )
    p_start.add_argument(
        "--show-full-response",
        action="store_true",
        help="After the initial turn completes, print everything from the log",
    )
    p_start.add_argument(
        "claude_args",
        nargs="*",
        metavar="CLAUDE_ARGS",
        help="Additional args passed to claude (use -- before these)",
    )

    # -- send --
    p_send = sub.add_parser("send", help="Send a message to a worker")
    p_send.add_argument("name", help="Worker name")
    p_send.add_argument(
        "message", nargs="*", help="Message text (reads stdin if omitted)"
    )
    p_send.add_argument(
        "--background",
        action="store_true",
        help="Return immediately without waiting for claude's response",
    )
    p_send.add_argument(
        "--queue",
        action="store_true",
        help="Send even if worker is busy; embed a correlation ID and wait "
        "for the specific tagged response",
    )
    p_send.add_argument(
        "--show-response",
        action="store_true",
        help="After the turn completes, print the assistant's response "
        "(equivalent to `read --last-turn`)",
    )
    p_send.add_argument(
        "--show-full-response",
        action="store_true",
        help="After the turn completes, print everything new since the send "
        "(equivalent to `read --since <marker>`)",
    )

    # -- read --
    p_read = sub.add_parser("read", help="Read worker output")
    p_read.add_argument("name", help="Worker name")
    p_read.add_argument("--follow", "-f", action="store_true", help="Tail the log")
    p_read.add_argument("--since", help="Show messages after this UUID or timestamp")
    p_read.add_argument(
        "--until", help="Stop showing messages at this UUID (exclusive)"
    )
    p_read.add_argument(
        "--last-turn",
        action="store_true",
        help="Show everything since the last user message",
    )
    p_read.add_argument(
        "-n",
        type=int,
        metavar="N",
        help="Show only the last N messages",
    )
    p_read.add_argument(
        "--count",
        action="store_true",
        help="Print the number of messages instead of content",
    )
    p_read.add_argument(
        "--summary",
        action="store_true",
        help="Show one-line summary per message: [uuid] ROLE: preview",
    )
    p_read.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Include tool calls, tool results, and thinking blocks",
    )
    p_read.add_argument(
        "--color",
        action="store_true",
        help="Force ANSI color output",
    )
    p_read.add_argument(
        "--no-color",
        action="store_true",
        help="Force plain text output (default when CLAUDECODE is set)",
    )

    # -- wait-for-turn --
    p_wait = sub.add_parser(
        "wait-for-turn", help="Block until claude is ready for input"
    )
    p_wait.add_argument("name", help="Worker name")
    p_wait.add_argument("--timeout", type=float, help="Timeout in seconds")
    p_wait.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE_SECONDS,
        metavar="SECONDS",
        help=(
            f"After detecting a turn boundary, wait this many seconds and "
            f"confirm no new messages appeared (default: {DEFAULT_SETTLE_SECONDS}). "
            f"Prevents false positives when the worker briefly idles between "
            f"internal subagent dispatches. Set to 0 to disable."
        ),
    )

    # -- list --
    sub.add_parser("list", aliases=["ls"], help="List all workers")

    # -- stop --
    p_stop = sub.add_parser("stop", help="Stop a worker")
    p_stop.add_argument("name", help="Worker name")
    p_stop.add_argument(
        "--force", action="store_true", help="Send SIGKILL instead of SIGTERM"
    )

    # -- install-hook --
    p_hook = sub.add_parser(
        "install-hook",
        help="Install SessionStart hook that sets CLAUDE_SESSION_UUID",
    )
    hook_scope = p_hook.add_mutually_exclusive_group()
    hook_scope.add_argument(
        "--user",
        action="store_true",
        help="Install into ~/.claude/settings.json (default)",
    )
    hook_scope.add_argument(
        "--project",
        action="store_true",
        help="Install into ./.claude/settings.json",
    )
    p_hook.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    p_hook.add_argument(
        "--force",
        action="store_true",
        help="Add hook entry even if one already exists",
    )

    args = parser.parse_args()

    handlers = {
        "start": cmd_start,
        "send": cmd_send,
        "read": cmd_read,
        "wait-for-turn": cmd_wait_for_turn,
        "list": cmd_list,
        "ls": cmd_list,
        "stop": cmd_stop,
        "install-hook": cmd_install_hook,
    }
    handlers[args.command](args)
