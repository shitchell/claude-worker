"""CLI entry point for claude-worker."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from claude_worker.manager import (
    cleanup_runtime_dir,
    create_runtime_dir,
    get_base_dir,
    get_runtime_dir,
    run_manager,
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


def get_worker_status(runtime: Path) -> str:
    """Determine worker status from PID and log state.

    In -p stream-json mode, each turn emits a `result` message but the process
    stays alive waiting for more input. So `result` only means "done" if the
    process is dead. If alive, `result` means "waiting" (turn complete).
    """
    pid_file = runtime / "pid"
    log_file = runtime / "log"

    # Check PID
    if not pid_file.exists():
        return "dead"
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return "dead"
    alive = pid_alive(pid)

    # Check last meaningful message in log
    if not log_file.exists():
        return "starting" if alive else "dead"

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
        return "dead"
    # result with process alive = turn complete, waiting for next input
    if last_type == "result":
        return "waiting"
    if last_stop_reason == "end_turn":
        return "waiting"
    return "working"


# -- Subcommand handlers --


def cmd_start(args: argparse.Namespace) -> None:
    """Start a new claude worker."""
    name = args.name or generate_name()

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

    # Fork to background
    pid = os.fork()
    if pid > 0:
        # Parent — wait for session init if we're sending a prompt,
        # otherwise just wait for the PID file and return immediately
        session_file = runtime / "session"
        pid_file = runtime / "pid"

        # Always wait for PID file (manager is running)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pid_file.exists():
                break
            time.sleep(0.1)

        # If we have an initial prompt, wait for session init (init arrives
        # after first user message in stream-json mode)
        if initial_message:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if session_file.exists() and session_file.read_text().strip():
                    break
                time.sleep(0.1)

        session_id = ""
        if session_file.exists():
            session_id = session_file.read_text().strip()

        print(f"{name}")
        print(f"  dir:     {runtime}")
        if session_id:
            print(f"  session: {session_id}")
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
        claude_args=args.claude_args or [],
        initial_message=initial_message,
    )
    os._exit(0)


def cmd_send(args: argparse.Namespace) -> None:
    """Send a message to a worker."""
    runtime = resolve_worker(args.name)
    in_fifo = runtime / "in"

    # Get message from arg or stdin
    if args.message:
        content = " ".join(args.message)
    else:
        content = sys.stdin.read()

    if not content.strip():
        print("Error: empty message", file=sys.stderr)
        sys.exit(1)

    msg = json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": content},
        }
    )

    with open(in_fifo, "w") as f:
        f.write(msg + "\n")
        f.flush()


def cmd_read(args: argparse.Namespace) -> None:
    """Read worker output, formatted via claude_logs."""
    runtime = resolve_worker(args.name)
    log_file = runtime / "log"

    if not log_file.exists():
        print("No log output yet.", file=sys.stderr)
        sys.exit(1)

    from claude_logs import (
        ANSIFormatter,
        FilterConfig,
        RenderConfig,
        parse_message,
        should_show_message,
    )
    from claude_logs.dateparse import parse_datetime

    filters = FilterConfig(
        hidden={"progress", "file-history-snapshot", "last-prompt"},
    )
    config = RenderConfig(filters=filters, timestamp_format="%H:%M:%S")

    # Handle --since
    since_ts = None
    since_uuid = None
    if args.since:
        # Try as UUID first (contains dashes, 36 chars)
        val = args.since.strip()
        if len(val) == 36 and val.count("-") == 4:
            since_uuid = val
        else:
            try:
                since_ts = parse_datetime(val)
            except Exception:
                print(f"Error: cannot parse --since value: {val}", file=sys.stderr)
                sys.exit(1)

    formatter = ANSIFormatter()

    if args.follow:
        _read_follow(log_file, config, formatter, since_uuid, since_ts, args)
    else:
        _read_static(log_file, config, formatter, since_uuid, since_ts, args)


def _read_static(log_file, config, formatter, since_uuid, since_ts, args):
    """Read log file statically."""
    from claude_logs import parse_message, should_show_message
    from datetime import datetime, timezone

    found_since = since_uuid is None and since_ts is None
    messages = []

    with open(log_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Handle --since filtering
            if not found_since:
                if since_uuid and data.get("uuid") == since_uuid:
                    found_since = True
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

            msg = parse_message(data)
            if should_show_message(msg, data, config):
                messages.append((data, msg))

    # Handle --last-turn: find last turn boundary, show everything after
    if args.last_turn:
        last_end = -1
        for i, (data, msg) in enumerate(messages):
            msg_type = data.get("type")
            if msg_type == "result":
                last_end = i
            elif msg_type == "assistant":
                sr = data.get("message", {}).get("stop_reason")
                if sr == "end_turn":
                    last_end = i
        if last_end >= 0:
            messages = messages[last_end:]

    for data, msg in messages:
        uuid = data.get("uuid", "")[:8]
        ts = data.get("timestamp", "")
        if ts:
            # Format timestamp compactly
            try:
                from datetime import datetime as dt

                parsed = dt.fromisoformat(ts.replace("Z", "+00:00"))
                ts = parsed.strftime("%H:%M:%S")
            except ValueError:
                pass
        prefix = f"[{ts} {uuid}] " if uuid else ""

        blocks = msg.render(config)
        output = formatter.format(blocks)
        if output.strip():
            # Prepend timestamp+uuid to first line
            lines = output.split("\n")
            lines[0] = prefix + lines[0]
            print("\n".join(lines))


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
                    _time.sleep(0.1)
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
                    uuid = data.get("uuid", "")[:8]
                    ts = data.get("timestamp", "")
                    if ts:
                        try:
                            from datetime import datetime as dt

                            parsed = dt.fromisoformat(ts.replace("Z", "+00:00"))
                            ts = parsed.strftime("%H:%M:%S")
                        except ValueError:
                            pass
                    prefix = f"[{ts} {uuid}] " if uuid else ""
                    blocks = msg.render(config)
                    output = formatter.format(blocks)
                    if output.strip():
                        lines = output.split("\n")
                        lines[0] = prefix + lines[0]
                        print("\n".join(lines), flush=True)
        except KeyboardInterrupt:
            pass


def cmd_wait_for_turn(args: argparse.Namespace) -> None:
    """Block until claude finishes its turn or the session ends.

    Exit codes:
        0 — turn complete, worker is ready for more input
        1 — worker process died (no more turns possible)
        2 — timeout
    """
    runtime = resolve_worker(args.name)
    log_file = runtime / "log"
    pid_file = runtime / "pid"

    if not log_file.exists():
        # Wait for log to appear
        deadline = time.monotonic() + (args.timeout or 300)
        while not log_file.exists():
            if time.monotonic() > deadline:
                print("Error: timeout waiting for log file", file=sys.stderr)
                sys.exit(2)
            time.sleep(0.1)

    deadline = None
    if args.timeout:
        deadline = time.monotonic() + args.timeout

    def _manager_alive() -> bool:
        try:
            pid = int(pid_file.read_text().strip())
            return pid_alive(pid)
        except (ValueError, OSError):
            return False

    # Scan existing log to determine current state.
    # Track: after the most recent user message, has a turn boundary appeared?
    # A "result" that precedes the last user message doesn't count.
    seen_user = False
    turn_end_after_last_user = None
    with open(log_file) as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            msg_type = data.get("type")
            if msg_type == "user":
                seen_user = True
                turn_end_after_last_user = None  # reset on new user msg
            elif msg_type == "result":
                turn_end_after_last_user = data
            elif msg_type == "assistant":
                sr = data.get("message", {}).get("stop_reason")
                if sr == "end_turn":
                    turn_end_after_last_user = data

    if turn_end_after_last_user is not None:
        # Turn already completed after the last user message
        print(json.dumps(turn_end_after_last_user))
        sys.exit(0)

    if not _manager_alive():
        print("Error: worker process died", file=sys.stderr)
        sys.exit(1)

    with open(log_file) as f:
        f.seek(0, 2)  # seek to end
        while True:
            if deadline and time.monotonic() > deadline:
                print("Error: timeout", file=sys.stderr)
                sys.exit(2)

            line = f.readline()
            if not line:
                # No new data — check if process is still alive
                if not _manager_alive():
                    print("Error: worker process died", file=sys.stderr)
                    sys.exit(1)
                time.sleep(0.1)
                continue
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            # In -p stream-json mode, `result` means turn complete.
            # The process stays alive for more input.
            if msg_type == "result":
                print(json.dumps(data))
                sys.exit(0)

            if msg_type == "assistant":
                sr = data.get("message", {}).get("stop_reason")
                if sr == "end_turn":
                    print(json.dumps(data))
                    sys.exit(0)


def cmd_list(args: argparse.Namespace) -> None:
    """List all workers."""
    base = get_base_dir()
    if not base.exists():
        return

    # Header
    print(f"{'NAME':<20} {'PID':<8} {'STATUS':<10} {'SESSION'}")

    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        pid_file = entry / "pid"
        session_file = entry / "session"

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
                session = sid[:12] + "..." if len(sid) > 12 else sid
            except OSError:
                pass

        status = get_worker_status(entry)
        print(f"{name:<20} {pid:<8} {status:<10} {session}")


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
    time.sleep(0.5)
    if runtime.exists():
        cleanup_runtime_dir(args.name)
        print(f"Cleaned up {runtime}")


EXAMPLES = """\
examples:
  # Start a worker with a system prompt
  claude-worker start --name researcher --prompt "You are a research assistant"

  # Send a message and wait for the response
  claude-worker send researcher "summarize the architecture of this repo"
  claude-worker wait-for-turn researcher

  # Read the latest response
  claude-worker read researcher --last-turn

  # Continue the conversation
  claude-worker send researcher "now focus on the database layer"
  claude-worker wait-for-turn researcher
  claude-worker read researcher --last-turn

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

  # Script: send, wait, and capture the result JSON
  claude-worker send myworker "do the thing"
  result=$(claude-worker wait-for-turn myworker)
  echo "$result" | jq .result
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

    # -- read --
    p_read = sub.add_parser("read", help="Read worker output")
    p_read.add_argument("name", help="Worker name")
    p_read.add_argument("--follow", "-f", action="store_true", help="Tail the log")
    p_read.add_argument("--since", help="Show messages after this UUID or timestamp")
    p_read.add_argument(
        "--last-turn", action="store_true", help="Show only the last assistant turn"
    )

    # -- wait-for-turn --
    p_wait = sub.add_parser(
        "wait-for-turn", help="Block until claude is ready for input"
    )
    p_wait.add_argument("name", help="Worker name")
    p_wait.add_argument("--timeout", type=float, help="Timeout in seconds")

    # -- list --
    sub.add_parser("list", aliases=["ls"], help="List all workers")

    # -- stop --
    p_stop = sub.add_parser("stop", help="Stop a worker")
    p_stop.add_argument("name", help="Worker name")
    p_stop.add_argument(
        "--force", action="store_true", help="Send SIGKILL instead of SIGTERM"
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
    }
    handlers[args.command](args)
