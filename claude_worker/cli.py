"""CLI entry point for claude-worker."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

# -- Named constants --

# Timeouts (seconds)
LOG_FILE_WAIT_TIMEOUT_SECONDS: float = 300.0
MANAGER_READY_TIMEOUT_SECONDS: float = 10.0
WORKER_READY_TIMEOUT_SECONDS: float = 30.0
DEFAULT_SETTLE_SECONDS: float = 3.0

# How old a turn-end log entry must be before `get_worker_status` reports
# `waiting` instead of `working`. Prevents false "idle" readings when a
# worker has briefly paused between internal subagent dispatches — the
# same class of false positive `wait-for-turn --settle` guards against,
# but applied passively (via log mtime) instead of actively (via sleep +
# re-check). This is the *display* threshold: `ls`, the REPL idle check,
# and the status lines printed after send/start all see it. `_wait_for_turn`
# itself still uses `--settle` for active debounce.
STATUS_IDLE_THRESHOLD_SECONDS: float = 3.0

# Polling intervals (seconds)
POLL_INTERVAL_SECONDS: float = 0.1
STOP_CLEANUP_DELAY_SECONDS: float = 0.5

# Display
LS_PREVIEW_MAX_CHARS: int = 80
SUMMARY_PREVIEW_MAX_CHARS: int = 80
UUID_SHORT_LENGTH: int = 8

# Reverse log iteration — chunk size for reading JSONL files backwards.
# 8 KiB is the typical stdio buffer and large enough to contain an
# average full-turn message in one read.
LOG_REVERSE_CHUNK_SIZE: int = 8192

# Queue correlation
QUEUE_WAIT_TIMEOUT_SECONDS: float = 600.0

# Stop wrap-up — two-phase shutdown sends a wrap-up message before SIGTERM
STOP_WRAPUP_TIMEOUT_SECONDS: float = 900.0
ANALYZE_SESSION_SKILL_RESOURCE: str = "analyze-session.md"


def _build_stop_wrapup_message() -> str:
    """Build the stop wrap-up message with the bundled skill path."""
    try:
        from importlib.resources import files

        skill_path = files("claude_worker") / "skills" / ANALYZE_SESSION_SKILL_RESOURCE
        skill_hint = (
            f" If the analyze-session skill is available, invoke it. "
            f"Otherwise, read the instructions at {skill_path} and follow them "
            f"to produce a session analysis before wrapping up."
        )
    except Exception:
        skill_hint = ""
    return (
        "[system:stop-requested] Stop has been requested. Please complete your "
        "wrap-up procedure and respond with 'wrap-up complete' when done."
        f"{skill_hint} You have up to 15 minutes."
    )


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
# Cap on the missing-tag dedup log size. When exceeded, the oldest
# entries are evicted (FIFO). 1000 is enough for a realistic day or
# two of distinct misses without growing unbounded; eviction is a
# no-op in normal operation where misses are rare.
MISSING_TAG_LOG_MAX_ENTRIES: int = 1000

# Team Lead (TL) mode
TL_IDENTITY_RESOURCE: str = "technical-lead.md"
TL_INTERNALIZE_MESSAGE: str = (
    "Read the project's documentation (README.md, CLAUDE.md, docs/). "
    "Export and read the GVP library if one exists. Familiarize yourself "
    "with the codebase structure. Run the test suite and report the "
    "baseline state. Then report your readiness to the PM."
)

# REPL
REPL_IDLE_POLL_INTERVAL_SECONDS: float = 0.25
REPL_INPUT_PROMPT: str = "you> "
REPL_EXIT_COMMANDS: frozenset[str] = frozenset({"/exit", "/quit"})

# Notifications — human escalation channel
NOTIFY_COOLDOWN_SECONDS: float = 60.0
NOTIFY_SUBPROCESS_TIMEOUT_SECONDS: float = 10.0

# Replaceme — auto-restart mechanism
REPLACEME_ANCESTOR_WALK_MAX: int = 5
REPLACEME_OLD_MANAGER_WAIT_TIMEOUT: float = 30.0
REPLACEME_OLD_MANAGER_POLL_INTERVAL: float = 0.5
REPLACEME_HANDOFF_MAX_AGE_MINUTES: int = 30

# Context window size detection. Claude Code models with the `[1m]`
# suffix (e.g., `claude-opus-4-6[1m]`) use a 1M context window; all
# others default to the standard 200K. See _detect_context_window_size.
CONTEXT_WINDOW_1M: int = 1_000_000
CONTEXT_WINDOW_DEFAULT: int = 200_000

# Permission grant feature — see claude_worker/permission_grant.py for
# the full mechanism. The grants file sits alongside log/pid/session in
# the worker's runtime dir. The per-worker settings.json wires a
# PreToolUse hook on Edit/Write/MultiEdit that consults the grants file.
GRANTS_FILE_NAME: str = "grants.jsonl"
PERMISSION_SETTINGS_FILE_NAME: str = "settings.json"
GRANT_ID_LENGTH: int = 8
PERMISSION_HOOK_TOOLS: tuple[str, ...] = ("Edit", "Write", "MultiEdit")
# Substring that identifies a sensitive-file denial in a tool_result.
# Used by `grant --last` to locate the most recent denied tool call.
SENSITIVE_DENIAL_MARKER: str = "which is a sensitive file"

from claude_worker.manager import (
    _atomic_write_text,
    _legacy_base_dir,
    archive_runtime_dir,
    cleanup_runtime_dir,
    create_runtime_dir,
    enqueue_message,
    get_base_dir,
    get_runtime_dir,
    get_saved_worker,
    prune_archives,
    run_manager,
    save_worker,
)


def _get_cwork_config() -> dict:
    """Read ~/.cwork/config.yaml, returning {} on missing/error."""
    config_path = Path.home() / ".cwork" / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        print(
            "Warning: pyyaml not installed — cannot read ~/.cwork/config.yaml. "
            "Run: pip install pyyaml",
            file=sys.stderr,
        )
        return {}
    try:
        return yaml.safe_load(config_path.read_text()) or {}
    except Exception:
        return {}


def _get_wrapup_timeout_minimum() -> float:
    """Read the wrap-up timeout minimum from config, defaulting to the constant."""
    config = _get_cwork_config()
    try:
        return float(
            config.get("stop", {}).get(
                "wrap_up_timeout_minimum", STOP_WRAPUP_TIMEOUT_SECONDS
            )
        )
    except (TypeError, ValueError):
        return STOP_WRAPUP_TIMEOUT_SECONDS


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

    A detected turn-end (`result` or `assistant stop_reason=end_turn`) only
    counts as `waiting` if the log file hasn't been touched in the last
    STATUS_IDLE_THRESHOLD_SECONDS — otherwise we might be in the middle of a
    subagent dispatch gap that *looks* like turn-end but is actually still
    live activity. The threshold is applied passively via log mtime (no
    blocking sleep) so this remains a point-in-time read suitable for `ls`
    and the REPL idle check.

    This threshold is the *display* threshold. `_wait_for_turn` itself still
    uses an active `--settle` window for the cases where the caller can
    afford to wait (`wait-for-turn` CLI, but not `send` which wants to return
    promptly).

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

    # Walk the log backwards and stop at the first user/assistant/result.
    # Previously this scanned the entire log forward to find the "last"
    # meaningful message — O(log_size) per ls/status call. The reverse
    # iterator yields from newest to oldest and we short-circuit at the
    # first match, so the cost is O(1) amortized. Streaming assistant
    # messages with stop_reason=None are skipped (matches the forward
    # scan's semantics: only truthy stop_reasons were tracked).
    last_type: str | None = None
    last_stop_reason: str | None = None
    for data in _iter_log_reverse(log_file):
        msg_type = data.get("type")
        if msg_type == "result":
            last_type = "result"
            break
        if msg_type == "user":
            last_type = "user"
            last_stop_reason = None
            break
        if msg_type == "assistant":
            sr = data.get("message", {}).get("stop_reason")
            if sr:
                last_type = "assistant"
                last_stop_reason = sr
                break
            # Streaming chunk with stop_reason=None — skip, keep walking back.

    if not alive:
        return "dead", log_mtime

    # A detected turn-end is only "waiting" if the log has been quiet for
    # at least STATUS_IDLE_THRESHOLD_SECONDS. Fresher turn-ends could be
    # followed by a subagent dispatch any moment — treat as working.
    turn_ended = last_type == "result" or last_stop_reason == "end_turn"
    if turn_ended:
        log_age = time.time() - log_mtime
        if log_age >= STATUS_IDLE_THRESHOLD_SECONDS:
            return "waiting", log_mtime
        return "working", log_mtime

    # No user/assistant/result seen yet — just startup noise (system/init,
    # hooks, etc.). The worker is alive but idle, literally waiting for
    # first input. Previously this fell through to "working" which made
    # background-started workers look busy forever.
    if last_type is None:
        return "waiting", log_mtime
    # Anything else (user with no trailing turn-end, assistant mid-tool_use)
    # is actively in progress.
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


def _render_one_message(data: dict, msg, config, formatter) -> str | None:
    """Render a single parsed message to a prefixed, formatted string.

    Returns the full rendered output (possibly multi-line) with the
    ``[HH:MM:SS uuid]`` prefix applied to the first line, or ``None``
    if the formatter produces no visible output (e.g., an assistant
    message with only tool_use blocks and ``tools`` hidden).

    Used by ``_read_static`` (forward scan and summary branches),
    ``_read_follow`` (live tail), and ``cmd_repl`` (live stream during
    the working phase). Extracted for DRY — Minor-5 from the Round 2
    code review.
    """
    blocks = msg.render(config)
    output = formatter.format(blocks)
    if not output.strip():
        return None
    prefix = _format_msg_prefix(data)
    lines = output.split("\n")
    lines[0] = prefix + lines[0]
    return "\n".join(lines)


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


def _worker_is_pm(name: str) -> bool:
    """Return True if the named worker is marked as a PM worker in metadata."""
    saved = get_saved_worker(name)
    return bool(saved and saved.get("pm"))


def _running_inside_claudecode() -> bool:
    """Return True if we're running inside a Claude Code session.

    Requires ``CLAUDECODE == "1"`` exactly. Claude Code's Bash tool sets
    this env var automatically for every command it runs. Values like
    ``"0"``, ``"true"``, or an empty string do NOT count — only the
    canonical ``"1"``. Used by chat-routing auto-detection and by
    read-output formatter selection.
    """
    return os.environ.get("CLAUDECODE") == "1"


def _env_chat_id() -> str | None:
    """Return the chat ID from the environment, or None.

    A chat ID is inferred from CLAUDE_SESSION_UUID *only* when
    _running_inside_claudecode() is True. CLAUDE_SESSION_UUID is populated
    by the install-hook SessionStart hook.
    """
    if not _running_inside_claudecode():
        return None
    uuid = os.environ.get("CLAUDE_SESSION_UUID", "").strip()
    return uuid or None


def _resolve_chat_id(
    worker_name: str,
    explicit_chat: str | None,
    all_chats: bool,
) -> str | None:
    """Determine the effective chat ID for a send/read operation.

    Returns the chat ID string, or None for no chat routing. Priority:
      1. ``all_chats=True`` → None (explicit opt-out of filtering)
      2. ``explicit_chat`` → use as-is IF the target is a PM worker
         (for non-PM targets: print a stderr warning and return None)
      3. Env-based auto-detection → only applies to PM workers
      4. Otherwise → None

    Prints a warning on stderr if ``--chat`` was passed but the target
    worker is not a PM — per user spec, non-PM targets pass through
    unchanged in that case.
    """
    if all_chats:
        return None

    is_pm = _worker_is_pm(worker_name)

    if explicit_chat:
        if not is_pm:
            print(
                f"Warning: --chat is only applicable to PM workers, "
                f"and '{worker_name}' is not a PM. Passing message through "
                f"unchanged.",
                file=sys.stderr,
            )
            return None
        return explicit_chat

    # Auto-detection: PM workers only
    if is_pm:
        return _env_chat_id()

    return None


def _message_contains_chat_tag(data: dict, chat_id: str) -> bool:
    """Return True if the message's text content contains the chat tag.

    Used by ``read`` chat filtering. Checks both user messages (string
    content) and assistant messages (list of content blocks with text).
    """
    tag = f"[{CHAT_TAG_PREFIX}{chat_id}]"
    content = data.get("message", {}).get("content", "")
    if isinstance(content, str):
        return tag in content
    if isinstance(content, list):
        for block in content:
            if block.get("type") == "text" and tag in block.get("text", ""):
                return True
    return False


def _extract_chat_id_from_message(data: dict) -> str | None:
    """Return the chat ID tag embedded in a message's text content, or None.

    Looks for the first ``[chat:<id>]`` occurrence in the content. The ID
    is whatever follows ``chat:`` up to the closing bracket.
    """
    import re

    content = data.get("message", {}).get("content", "")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if block.get("type") == "text":
                text += block.get("text", "")
    match = re.search(r"\[" + re.escape(CHAT_TAG_PREFIX) + r"([^\]]+)\]", text)
    return match.group(1) if match else None


def _missing_tag_log_path(worker_name: str) -> Path:
    """Path to the per-worker missing-tag dedup log."""
    return get_runtime_dir(worker_name) / MISSING_TAG_LOG_NAME


def _load_missing_tag_log(path: Path) -> dict:
    """Load the missing-tag dedup log as a dict keyed by message UUID.

    Returns an empty dict if the file doesn't exist or can't be parsed.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _handle_missing_tag_reports(worker_name: str, reports: list[dict]) -> None:
    """Append new missing-tag reports to the dedup log and warn on new entries.

    A "report" is a dict with keys: uuid, chat_id, preview. The log is keyed
    by UUID for O(1) dedup. Only new UUIDs trigger a warning.

    The dedup log is capped at MISSING_TAG_LOG_MAX_ENTRIES entries; when the
    cap is exceeded the OLDEST entries are evicted FIFO. Eviction relies
    on Python 3.7+ dict insertion-order guarantees, which is why each new
    entry is inserted at the end and old entries appear first.
    """
    if not reports:
        return
    from datetime import datetime, timezone

    log_path = _missing_tag_log_path(worker_name)
    existing = _load_missing_tag_log(log_path)

    new_entries = []
    for report in reports:
        uuid = report["uuid"]
        if uuid in existing:
            continue
        existing[uuid] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "chat_id": report["chat_id"],
            "preview": report["preview"],
        }
        new_entries.append(report)

    if not new_entries:
        return

    # Enforce the size cap: drop the oldest entries (dict insertion order)
    # until we're back under MISSING_TAG_LOG_MAX_ENTRIES. This is a no-op
    # in normal operation — tagging misses are rare.
    while len(existing) > MISSING_TAG_LOG_MAX_ENTRIES:
        oldest_key = next(iter(existing))
        del existing[oldest_key]

    # Write the updated log atomically so a concurrent read or a crash
    # mid-write doesn't leave a partially-written JSON file that breaks
    # the next invocation's dedup check.
    _atomic_write_text(log_path, json.dumps(existing, indent=2) + "\n")

    # Warn the user on stderr for each new entry
    for report in new_entries:
        uuid_short = report["uuid"][:UUID_SHORT_LENGTH]
        chat_short = report["chat_id"][:UUID_SHORT_LENGTH]
        print(
            f"WARNING: PM response [{uuid_short}] for chat:{chat_short} "
            f"is missing its [chat:] tag — consider raising with the human. "
            f"Preview: {report['preview']}",
            file=sys.stderr,
        )


def _generate_queue_id() -> str:
    """Generate a collision-resistant correlation ID for queued messages.

    Uses random hex (8 chars from 4 bytes) — no sub-millisecond collision
    risk between concurrent senders. Combined with the after_uuid marker
    (D2), this eliminates all known queue tag match races.
    """
    import secrets

    return secrets.token_hex(4)


def _wait_for_queue_response(
    name: str,
    queue_id: str,
    timeout: float = QUEUE_WAIT_TIMEOUT_SECONDS,
    after_uuid: str | None = None,
) -> int:
    """Tail the log waiting for an assistant message containing [queue:{id}].

    Returns 0 if the correlation tag is found, 1 if the worker dies, 2 on timeout.

    If ``after_uuid`` is provided, only log entries appearing *after* that
    UUID are considered. This avoids matching a stale [queue:<id>] string
    from a previous cycle (or a sub-millisecond collision between two
    recent queue IDs) — mirrors the race protection already in
    ``_wait_for_turn``.
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
    # When after_uuid is set, ignore everything up to and including the
    # marker line (matched on the parsed "uuid" JSON field, robust to
    # whitespace differences in serialization style).
    passed_marker = after_uuid is None
    with open(log_file) as f:
        for line in f:
            if not passed_marker:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if after_uuid and _uuid_matches(data.get("uuid", ""), after_uuid):
                    passed_marker = True
                continue
            if tag in line:
                return 0
        # Tail from current position (end of existing content). Everything
        # we tail now is by definition past the marker.
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


def _settle_is_stable(
    log_file: Path, settle: float, deadline: float | None = None
) -> bool:
    """Wait up to `settle` seconds, return True if no new messages appeared.

    Returns True immediately when ``settle <= 0``. Used by ``_wait_for_turn``
    to debounce the return when a worker briefly idles between internal
    subagent dispatches — a turn boundary that "sticks" for the full settle
    window is considered real, while one that flips back to activity is not.

    When ``deadline`` (an absolute time.monotonic() value) is provided, the
    sleep is capped at min(settle, remaining_time). If the deadline has
    already passed, skip the sleep entirely and return False so the caller
    can fall through to its own timeout check. This prevents `--settle 3
    --timeout 5` from blowing past the user's total budget.
    """
    if settle <= 0:
        return True
    effective_settle = settle
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Already past the deadline — no point sleeping. The caller's
            # next deadline check will catch this and return timeout.
            return False
        effective_settle = min(settle, remaining)
    uuid_before = _get_last_uuid(log_file)
    time.sleep(effective_settle)
    uuid_after = _get_last_uuid(log_file)
    return uuid_after == uuid_before


def _message_has_chat_tag(data: dict, chat_tag: str) -> bool:
    """Check if an assistant message contains [chat:<tag>] in its content."""
    msg = data.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return f"[{CHAT_TAG_PREFIX}{chat_tag}]" in content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                if f"[{CHAT_TAG_PREFIX}{chat_tag}]" in block.get("text", ""):
                    return True
    return False


def _wait_for_turn(
    name: str,
    timeout: float | None = None,
    after_uuid: str | None = None,
    settle: float = 0.0,
    chat_tag: str | None = None,
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

    If ``chat_tag`` is provided, only fire when the turn's assistant content
    contains ``[chat:<tag>]``. Turns without the tag are skipped.
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

    # Walk the log backwards looking for the current turn state.
    # Question: "is there a turn-end message AFTER the most recent
    # user message (and, if after_uuid is set, AFTER the marker)?"
    # Reverse-walking lets us answer in O(1) amortized instead of
    # O(log_size) per wait-for-turn call.
    #
    # Decision rules while walking newest → oldest:
    # - Hit after_uuid marker: stop. Everything before the marker is
    #   out of scope; whatever we found above it wins.
    # - Hit a `result` or `assistant` with stop_reason=end_turn: that's
    #   our turn-end (it's newer than any user message we haven't seen
    #   yet going backwards). Record it and stop.
    # - Hit a `user` message: the turn is in progress — no turn-end
    #   exists newer than this user message. Stop without recording.
    turn_end_after_last_user = None
    last_assistant_in_turn = None
    for data in _iter_log_reverse(log_file):
        if after_uuid:
            msg_uuid = data.get("uuid", "")
            if _uuid_matches(msg_uuid, after_uuid):
                break  # reached the marker; everything before is stale
        msg_type = data.get("type")
        if msg_type == "result":
            turn_end_after_last_user = data
            # Keep walking to find the assistant message for chat tag check
            continue
        if msg_type == "assistant":
            sr = data.get("message", {}).get("stop_reason")
            if turn_end_after_last_user is not None:
                # We already found a result — this is the assistant for that turn
                last_assistant_in_turn = data
                break
            if sr == "end_turn":
                turn_end_after_last_user = data
                last_assistant_in_turn = data
                break
            # Streaming chunk: keep walking back.
            continue
        if msg_type == "user":
            # Reached a user message before any turn-end — turn is
            # still in progress.
            break

    # Scan found an already-complete turn. Check chat tag if filtering.
    # Use the assistant message (not result) for tag checking since result
    # messages don't contain assistant content.
    if turn_end_after_last_user is not None:
        tag_check_msg = last_assistant_in_turn or turn_end_after_last_user
        if chat_tag and not _message_has_chat_tag(tag_check_msg, chat_tag):
            # Turn doesn't match the chat filter — fall through to tail loop
            turn_end_after_last_user = None
    if turn_end_after_last_user is not None:
        if _settle_is_stable(log_file, settle, deadline=deadline):
            return 0
        # Fell through: new activity during settle — drop into tail loop to
        # wait for the next turn boundary.

    if not _manager_alive():
        print("Error: worker process died", file=sys.stderr)
        return 1

    tail_last_assistant: dict = {}
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

            # Track the last assistant message for chat tag checking
            # (result messages don't contain assistant content)
            if msg_type == "assistant":
                tail_last_assistant = data

            turn_ended = False
            if msg_type == "result":
                turn_ended = True
            elif msg_type == "assistant":
                sr = data.get("message", {}).get("stop_reason")
                if sr == "end_turn":
                    turn_ended = True

            if turn_ended:
                # Chat tag filter: check assistant content, not result
                if chat_tag:
                    check_msg = tail_last_assistant if msg_type == "result" else data
                    if not _message_has_chat_tag(check_msg, chat_tag):
                        continue
                if _settle_is_stable(log_file, settle, deadline=deadline):
                    return 0
                # New activity during settle: keep tailing for the next
                # turn boundary. Re-seek to end so we don't re-read the
                # messages that arrived during the settle window.
                f.seek(0, 2)
                continue


# -- Subcommand handlers --


def _ensure_cwork_dirs(cwd: str, pm: bool, tl: bool) -> None:
    """Auto-create .cwork/ skeleton directories on first identity-mode start.

    Creates both the global ~/.cwork/ skeleton and the project-level
    <cwd>/.cwork/ skeleton. Idempotent — mkdir(exist_ok=True) everywhere.
    Only runs for --pm or --team-lead workers.
    """
    if not pm and not tl:
        return

    # Global skeleton: ~/.cwork/
    home_cwork = Path.home() / ".cwork"
    for d in (
        home_cwork / "gvp" / "library",
        home_cwork / "identities" / "pm" / "gvp",
        home_cwork / "identities" / "technical-lead",
        home_cwork / "workers",
    ):
        d.mkdir(parents=True, exist_ok=True)

    # Create CATEGORIES.md manifest if missing
    categories_md = home_cwork / "gvp" / "library" / "CATEGORIES.md"
    if not categories_md.exists():
        categories_md.write_text(
            "# Categories\n\n"
            "- [code](code/) — Coding principles: DRY, naming, style, circular imports\n"
            "- [identities](identities/) — Role-specific guidance for PM and TL workers\n"
        )

    # Project skeleton: <cwd>/.cwork/
    project_cwork = Path(cwd) / ".cwork"
    if pm:
        for d in (
            project_cwork / "pm" / "chats",
            project_cwork / "pm" / "handoffs",
            project_cwork / "pm" / "gvp",
        ):
            d.mkdir(parents=True, exist_ok=True)
    if tl:
        for d in (
            project_cwork / "technical-lead" / "handoffs",
            project_cwork / "technical-lead" / "notes",
        ):
            d.mkdir(parents=True, exist_ok=True)

    # Shared project dirs
    tickets_dir = project_cwork / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    index = tickets_dir / "INDEX.md"
    if not index.exists():
        index.write_text(
            "# Tickets\n\n"
            "| ID | Slug | Status | Priority | Assigned | Consumer | Blocked-by |\n"
            "|----|------|--------|----------|----------|----------|------------|\n"
        )


def cmd_start(args: argparse.Namespace) -> None:
    """Start a new claude worker."""
    # --resume requires an explicit --name. Without it, the old code
    # would silently generate a random name, then fail with "no saved
    # session for worker 'worker-ab12'" — referring to a name the user
    # never provided. Fail cleanly instead.
    if args.resume and not args.name:
        print(
            "Error: --resume requires --name (the name of the worker to resume)",
            file=sys.stderr,
        )
        sys.exit(1)

    name = args.name or generate_name()

    # Handle --resume: restore saved startup vars (cwd, claude_args)
    claude_args = list(args.claude_args or [])
    # Determine whether this worker is a PM or TL worker. For new workers,
    # the --pm / --team-lead flag drives this. For resumes, we check the
    # saved metadata. These are mutually exclusive (enforced by argparse).
    pm_mode = args.pm
    tl_mode = args.team_lead
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
        # Resumed workers inherit identity mode from saved metadata
        if saved.get("pm"):
            pm_mode = True
        if saved.get("team_lead"):
            tl_mode = True
    else:
        # Build claude_args with --agent etc. (order matters: agent first)
        if args.agent:
            claude_args = ["--agent", args.agent] + claude_args

    # Identity mode (--pm or --team-lead): inject --append-system-prompt-file
    # pointing at the runtime dir's identity.md. The runtime dir path is
    # deterministic from `name` so we can reference it before creating the
    # dir; we write the identity file after create_runtime_dir but before
    # the fork, so the manager subprocess always finds it when spawning claude.
    identity_mode = pm_mode or tl_mode
    identity_path: Path | None = None
    if identity_mode and not args.resume:
        identity_path = get_runtime_dir(name) / "identity.md"
        claude_args = ["--append-system-prompt-file", str(identity_path)] + claude_args
    elif identity_mode and args.resume:
        # On resume, recompute the identity path so we can rewrite the file
        # (the previous runtime dir was cleaned up on stop)
        identity_path = get_runtime_dir(name) / "identity.md"

    # Save startup vars for future --resume (claude_args without --resume prefix)
    saved_args = (
        claude_args if not args.resume else claude_args[2:]
    )  # strip --resume <sid>
    save_worker(
        name,
        cwd=args.cwd or os.getcwd(),
        claude_args=saved_args,
        pm=pm_mode,
        team_lead=tl_mode,
    )

    # Build initial message from prompt-file and/or prompt.
    # For identity workers with no user-provided prompt, inject a canonical
    # internalization message so the worker runs its startup logic.
    parts = []
    if args.prompt_file:
        parts.append(Path(args.prompt_file).read_text())
    if args.prompt:
        parts.append(args.prompt)
    if not parts and pm_mode:
        parts.append(PM_INTERNALIZE_MESSAGE)
    if not parts and tl_mode:
        parts.append(TL_INTERNALIZE_MESSAGE)
    initial_message = "\n\n".join(parts) if parts else None

    # Auto-create .cwork/ skeleton for identity-mode workers
    _ensure_cwork_dirs(args.cwd or os.getcwd(), pm_mode, tl_mode)

    # Create runtime directory
    try:
        runtime = create_runtime_dir(name)
    except FileExistsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Write the identity file into the runtime dir. Must happen BEFORE
    # the fork so the manager subprocess sees it when spawning claude.
    if identity_path is not None:
        if tl_mode:
            identity_resource = TL_IDENTITY_RESOURCE
        else:
            identity_resource = PM_IDENTITY_RESOURCE
        identity_content = _load_bundled_resource("identities", identity_resource)
        identity_path.write_text(identity_content)

    # Write the per-worker settings.json that wires the PreToolUse
    # permission-grant hook. Must happen BEFORE the fork so the manager
    # subprocess finds the file when it builds the claude command.
    # Gated by --no-permission-hook for tests and for users opting out.
    permission_settings = _maybe_write_permission_settings(
        name=name,
        enabled=not getattr(args, "no_permission_hook", False),
        cwd=args.cwd or os.getcwd(),
    )
    if permission_settings is not None:
        # Append --settings to the claude args so claude merges this
        # settings file with the user's existing settings. The flag is
        # additive: user settings stay in effect, we just add the hook.
        claude_args = claude_args + ["--settings", str(permission_settings)]

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


def _send_to_single_worker(
    name: str,
    content: str,
    args: argparse.Namespace,
) -> int:
    """Send a message to a single worker. Returns exit code.

    Extracted from cmd_send so broadcast can reuse the core send logic
    per target without duplicating the FIFO write + wait sequence.
    """
    runtime = get_runtime_dir(name)
    in_fifo = runtime / "in"
    log_file = runtime / "log"

    if not runtime.exists():
        print(f"Error: worker '{name}' not found at {runtime}", file=sys.stderr)
        return 1

    # Status gate: skip for broadcast (fire-and-forget to all)
    if (
        not args.queue
        and not getattr(args, "broadcast", False)
        and not getattr(args, "dry_run", False)
    ):
        try:
            status, _ = _wait_for_ready_state(name)
        except TimeoutError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if status == "dead":
            print(
                f"Error: worker '{name}' is dead. "
                f"Use `claude-worker start --resume --name {name}` to restart.",
                file=sys.stderr,
            )
            return 1
        if status == "working":
            print(
                f"Error: worker '{name}' is busy. "
                f"Use `--queue` to send anyway with correlation tracking.",
                file=sys.stderr,
            )
            return 1

    marker_uuid = _get_last_uuid(log_file)

    # Chat routing
    chat_id = _resolve_chat_id(name, args.chat, args.all_chats)
    tagged_content = content
    if chat_id is not None:
        tagged_content = f"[{CHAT_TAG_PREFIX}{chat_id}] {tagged_content}"

    # Queue correlation
    queue_id: str | None = None
    if args.queue:
        queue_id = _generate_queue_id()
        tagged_content = (
            tagged_content
            + f"\n\n[Please include [{QUEUE_TAG_PREFIX}{queue_id}] literally in your response so the sender can identify it.]"
        )

    msg = json.dumps(
        {"type": "user", "message": {"role": "user", "content": tagged_content}}
    )

    if getattr(args, "dry_run", False):
        print(json.dumps(json.loads(msg), indent=2))
        return 0

    if getattr(args, "verbose", False):
        print(json.dumps(json.loads(msg), indent=2), file=sys.stderr)

    with open(in_fifo, "w") as f:
        f.write(msg + "\n")
        f.flush()

    # For broadcast fire-and-forget, don't wait
    if (
        getattr(args, "broadcast", False)
        and not args.show_response
        and not args.show_full_response
    ):
        return 0

    if queue_id is not None:
        rc = _wait_for_queue_response(name, queue_id, after_uuid=marker_uuid)
    else:
        rc = _wait_for_turn(name, after_uuid=marker_uuid)

    if rc == 0 and args.show_response:
        _show_worker_response(name, last_turn=True)
    elif rc == 0 and args.show_full_response:
        _show_worker_response(name, since_uuid=marker_uuid)

    return rc


def cmd_send(args: argparse.Namespace) -> None:
    """Send a message to a worker, or broadcast to multiple workers.

    Default behavior: check worker status first and reject if busy. Use
    ``--queue`` to bypass the busy check and track a specific response via a
    correlation ID embedded in the message. Use ``--broadcast`` with filter
    flags to send to multiple workers matching a filter.
    """
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

    # Broadcast mode: send to all matching workers
    if getattr(args, "broadcast", False):
        targets = _collect_filtered_workers(args)

        # Self-exclusion
        self_name = _find_worker_by_ancestry()
        if self_name:
            targets = [w for w in targets if w["name"] != self_name]

        if not targets:
            print("No matching workers found for broadcast", file=sys.stderr)
            sys.exit(1)

        names = [w["name"] for w in targets]
        results: list[tuple[str, int]] = []
        for name in names:
            rc = _send_to_single_worker(name, content, args)
            results.append((name, rc))

        # Summary
        sent = [n for n, rc in results if rc == 0]
        failed = [n for n, rc in results if rc != 0]
        if sent:
            print(f"Broadcast sent to {len(sent)} workers: {', '.join(sent)}")
        if failed:
            print(
                f"Failed for {len(failed)} workers: {', '.join(failed)}",
                file=sys.stderr,
            )
        sys.exit(1 if failed and not sent else 0)

    # Single-target mode
    if not args.name:
        print(
            "Error: worker name required (or use --broadcast with filters)",
            file=sys.stderr,
        )
        sys.exit(1)

    rc = _send_to_single_worker(args.name, content, args)
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

    # Short-circuit for --context: just print the context-window label
    # and exit. This is an alternative output mode, not a filter, so it
    # skips the rest of the read pipeline.
    if getattr(args, "context", False):
        label = _format_context_window_label(log_file)
        if label is None:
            print("—")
        else:
            print(label)
        return None, None

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
        # Default: conversational messages only — whitelist user (type) +
        # user-input (subtype) + assistant, hiding tool results, system, etc.
        # Both "user" and "user-input" are required: claugs checks type
        # visibility first, so show_only must include "user" or ALL user
        # messages are hidden regardless of subtype. The "user-input" subtype
        # then narrows to real human input (excluding tool-result,
        # subagent-result, system-meta, local-command).
        # Also hide tool/thinking content blocks from assistant messages.
        hidden |= {"thinking", "tools"}
        show_only = {"user", "user-input", "assistant", "queue-operation"}
        if getattr(args, "exclude_user", False):
            show_only -= {"user", "user-input"}
        filters = FilterConfig(show_only=show_only, hidden=hidden)

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
            except ValueError:
                print(f"Error: cannot parse --since value: {val}", file=sys.stderr)
                sys.exit(1)

    # Resolve effective chat ID from --chat, --all-chats, or env-based
    # auto-detection (PM workers only). Stash on args so _read_static
    # can consult it without a separate parameter.
    args.chat_id = _resolve_chat_id(
        args.name,
        getattr(args, "chat", None),
        getattr(args, "all_chats", False),
    )

    # Use markdown when running inside Claude Code — supervisor claudes
    # parse markdown better than ANSI or plain text. ANSI colors for
    # human terminals. Override with --color/--no-color.
    if args.color:
        formatter = ANSIFormatter()
    elif args.no_color:
        formatter = PlainFormatter()
    elif _running_inside_claudecode():
        formatter = MarkdownFormatter()
    else:
        formatter = ANSIFormatter()

    if args.follow:
        _read_follow(log_file, config, formatter, since_uuid, since_ts, args)
        return None, None
    return _read_static(log_file, config, formatter, since_uuid, since_ts, args)


def _uuid_matches(msg_uuid: str, target: str) -> bool:
    """Case-insensitive UUID prefix match.

    Defensive against empty target: an empty string would otherwise be a
    prefix of every UUID and match-all-lines. Returns False for empty
    inputs on either side.
    """
    if not msg_uuid or not target:
        return False
    return msg_uuid.lower().startswith(target.lower())


def _is_user_input_raw(data: dict) -> bool:
    """Lightweight classifier: is this raw JSONL entry a user-input message?

    Mirrors claugs' ``UserMessage.get_subtype() == "user-input"`` without
    instantiating a UserMessage. Used by ``--last-turn`` walk-back tracking
    during the scan loop, where we need to identify turn boundaries on raw
    data *before* display filtering (which may hide user messages via
    ``--exclude-user`` but still needs them to locate the window).

    A user-input message is:
    - type == "user"
    - has no toolUseResult (not a tool-result or subagent-result)
    - isMeta is not set (not a system-injected meta message)
    - content does not start with <command-name> / <local-command-stdout>
    - content is not a list containing tool_result blocks
    """
    if data.get("type") != "user":
        return False
    if data.get("toolUseResult") is not None:
        return False
    if data.get("isMeta"):
        return False
    content = data.get("message", {}).get("content", "")
    if isinstance(content, str):
        return not (
            content.startswith("<command-name>")
            or content.startswith("<local-command-stdout>")
        )
    if isinstance(content, list):
        # A list that contains any tool_result block is a tool-result message
        return not any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    return False


def _has_assistant_text(data: dict) -> bool:
    """Return True if this raw entry is an assistant message with non-empty text.

    Used alongside ``_is_user_input_raw`` for ``--last-turn`` walk-back
    boundary detection. Assistant messages that only contain tool_use blocks
    (no text) don't count as a conversational turn endpoint.
    """
    if data.get("type") != "assistant":
        return False
    content = data.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and block.get("text", "").strip()
        for block in content
    )


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
        # The orchestrator just sent the message; echoing its own text back
        # is noise. Force --exclude-user for show-response output. The
        # --last-turn window still uses user-input messages for boundary
        # detection (see _is_user_input_raw), so hiding them doesn't break
        # the walk-back.
        exclude_user=True,
        color=False,
        no_color=False,
        no_hint=True,
        # Chat filtering is handled by the --last-turn / --since window
        # boundary (the caller just sent the message, so last-turn locates
        # it). Disable per-message chat filtering to avoid dropping the
        # caller's own response when chat_id was also embedded.
        chat=None,
        all_chats=True,
    )
    first_uuid, last_uuid = cmd_read(namespace)
    if first_uuid and last_uuid:
        # Hint uses --exclude-user to match the view we just rendered, so
        # re-running the suggested command produces the same output.
        print(
            f"\nTo see this window again or expand: "
            f"claude-worker read {name} "
            f"--since {first_uuid[:UUID_SHORT_LENGTH]} "
            f"--until {last_uuid[:UUID_SHORT_LENGTH]} "
            f"--exclude-user"
        )


def _iter_log_reverse(log_file: Path, chunk_size: int = LOG_REVERSE_CHUNK_SIZE):
    """Yield parsed JSONL entries from a log file, newest to oldest.

    Reads the file from EOF backwards in chunks of ``chunk_size`` bytes.
    Buffers incomplete lines across chunk boundaries so a line split by
    a chunk boundary is correctly reassembled before parsing.

    Yields dicts. Silently skips empty lines and lines that fail JSON
    parsing. Yields nothing if the file doesn't exist or is empty.

    This is the hot path for "what's the last thing in the log?" queries
    (last uuid, last assistant preview, worker status). The old forward
    scan was O(log_size) per call; this is O(chunk_size) amortized for
    callers that stop after the first few matches.

    The generator owns the file handle and closes it when exhausted or
    when the caller calls ``.close()`` explicitly. Lazy — stopping the
    iteration after the first yield only reads enough chunks to deliver
    that yield.
    """
    if not log_file.exists():
        return
    try:
        f = open(log_file, "rb")
    except OSError:
        return
    try:
        f.seek(0, 2)  # SEEK_END
        remaining = f.tell()
        buffer = b""
        while remaining > 0:
            read_size = min(chunk_size, remaining)
            remaining -= read_size
            f.seek(remaining)
            chunk = f.read(read_size)
            buffer = chunk + buffer
            # Split on newlines. The FIRST fragment may be an incomplete
            # line continuing from further back in the file — keep it
            # in the buffer for the next iteration. Yield the rest in
            # reverse order (newest first).
            parts = buffer.split(b"\n")
            buffer = parts[0]
            complete_lines = parts[1:]
            for line in reversed(complete_lines):
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        # Final buffer holds the very first line of the file (or the
        # only line if the file had no newlines). Yield it last.
        if buffer:
            try:
                yield json.loads(buffer)
            except json.JSONDecodeError:
                pass
    finally:
        f.close()


def _get_last_uuid(log_file: Path) -> str | None:
    """Return the UUID of the most recent message in the log, or None.

    Used as a marker before sending so the caller can later use --since
    to show everything that arrived after this point. Uses
    ``_iter_log_reverse`` so we only read the last chunk of the file
    instead of scanning forward from byte 0.
    """
    for data in _iter_log_reverse(log_file):
        uuid = data.get("uuid", "")
        if uuid:
            return uuid
    return None


def _get_last_assistant_preview(log_file: Path, max_chars: int) -> str:
    """Return a single-line preview of the most recent assistant text message.

    Returns the empty string if the log does not exist or no assistant text
    message is found. Used by `ls` to show "what's the worker doing" at a
    glance. Uses ``_iter_log_reverse`` to short-circuit at the first match.
    """
    for data in _iter_log_reverse(log_file):
        if data.get("type") != "assistant":
            continue
        preview = _extract_text_preview(data, max_chars)
        if preview:
            return preview
    return ""


def _detect_context_window_size(log_file: Path) -> int:
    """Read the log's system/init message to determine context window size.

    Claude Code model identifiers encode the context window in a suffix:
    ``claude-opus-4-6[1m]`` → 1,000,000 tokens;
    ``claude-opus-4-6`` (no suffix) → 200,000 tokens (default).

    The ``system/init`` message is always written first by claude, so a
    short forward scan reaches it in O(1) for real logs. Falls back to
    CONTEXT_WINDOW_1M on any error — 1M is the more common ceiling for
    long-running workers, and guessing too-high underestimates the
    percentage (which is a safer failure mode than over-reporting).
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
                        return CONTEXT_WINDOW_1M
                    return CONTEXT_WINDOW_DEFAULT
    except OSError:
        pass
    return CONTEXT_WINDOW_1M


def _format_token_count_short(n: int) -> str:
    """Format a token count for display. Examples:
    763716 → "764k", 1234567 → "1.2M", 1000000 → "1M", 42 → "42".
    """
    if n >= 1_000_000:
        # Drop trailing ".0" for round millions (1M not 1.0M).
        m = n / 1_000_000
        if m == int(m):
            return f"{int(m)}M"
        return f"{m:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def _format_context_window_label(log_file: Path) -> str | None:
    """Return a compact context-window label for display, or None.

    Examples: ``"77% (764k/1M)"``, ``"2% (4k/200k)"``. Returns ``None``
    when the log doesn't exist, isn't readable, or has no assistant
    messages with usage data yet (e.g., fresh worker before its first
    turn completes).
    """
    if not log_file.exists():
        return None
    try:
        from claude_logs import compute_context_window_usage
    except ImportError:
        return None
    try:
        cw = compute_context_window_usage(log_file)
    except OSError:
        return None
    if cw is None:
        return None
    window = _detect_context_window_size(log_file)
    pct = cw.total / window
    total_display = _format_token_count_short(cw.total)
    window_display = _format_token_count_short(window)
    return f"{pct:.0%} ({total_display}/{window_display})"


def _passes_display_filters(
    data: dict, config, args, chat_id: str | None
) -> "object | None":
    """Apply the chat + claugs + non-verbose text filter chain.

    Returns the parsed Message object if the entry should be displayed,
    or None if it should be filtered out. Centralizes the logic so the
    forward scan in _read_static and the reverse fast path both agree
    on what counts as a displayable message.
    """
    from claude_logs import parse_message, should_show_message

    if chat_id and not _message_contains_chat_tag(data, chat_id):
        return None

    msg = parse_message(data)
    if not should_show_message(msg, data, config):
        return None

    if not getattr(args, "verbose", False):
        content = data.get("message", {}).get("content", [])
        if isinstance(content, list):
            has_text = any(
                c.get("type") == "text" and c.get("text", "").strip() for c in content
            )
            if not has_text:
                return None
    return msg


def _read_static_fast_path(
    log_file: Path, config, args, chat_id: str | None
) -> list[tuple[int, dict, object]] | None:
    """Reverse-iterate fast path for _read_static.

    Applicable when the query only needs the tail of the log:
    - no --since / --until (they imply forward position tracking)
    - not a PM worker (PM monitoring needs the full forward scan to
      track turn boundaries in order)
    - either --last-turn or -n N is set (otherwise there's no end
      condition for the reverse walk)
    - chat filtering is handled natively (skip non-matching entries)

    Returns a list of ``(raw_idx, data, msg)`` tuples matching the
    shape _read_static builds, or None if the query doesn't qualify
    for the fast path (caller should fall through to the forward scan).

    The raw_idx values here count matched displayable messages
    backwards from the end — they're only used for the --last-turn
    window filter, which is already applied inside this helper, so
    downstream consumers should not rely on their absolute values.
    """
    has_last_turn = getattr(args, "last_turn", False)
    n_limit = getattr(args, "n", None)
    if not (has_last_turn or n_limit is not None):
        return None

    collected_reverse: list[tuple[int, dict, object]] = []
    seen_user = False
    seen_asst = False
    idx = 0
    for data in _iter_log_reverse(log_file):
        # Boundary detection for --last-turn runs on RAW classification
        # (before display filtering) so --exclude-user doesn't break
        # the walk-back — matches the forward scan's approach of using
        # _is_user_input_raw / _has_assistant_text rather than the
        # filtered message list.
        if has_last_turn:
            if _is_user_input_raw(data):
                seen_user = True
            elif _has_assistant_text(data):
                seen_asst = True

        msg = _passes_display_filters(data, config, args, chat_id)
        if msg is not None:
            # Fake raw_idx — not meaningful in reverse, but downstream
            # code expects a 3-tuple shape.
            collected_reverse.append((idx, data, msg))
            idx += 1

        # --last-turn termination: stop once we've seen both types in
        # the RAW log (even if display-filtered out).
        if has_last_turn and seen_user and seen_asst:
            break

        # -n N termination: stop once we've collected N displayable
        # messages. --last-turn takes precedence if both are set.
        if (
            not has_last_turn
            and n_limit is not None
            and len(collected_reverse) >= n_limit
        ):
            break

    return list(reversed(collected_reverse))


def _render_read_output(
    messages: list[tuple[int, dict, object]], formatter, config, args
) -> tuple[str | None, str | None]:
    """Render a prepared messages list according to args and return
    (first_uuid, last_uuid) of what was emitted.

    Handles the --count, --summary, and normal-render output modes plus
    the bottom-of-output "to see new messages" hint. Called by both the
    forward scan and the reverse fast path inside _read_static.
    """
    # Alternative output modes: --count and --summary
    if hasattr(args, "count") and args.count:
        print(len(messages))
        return None, None

    if hasattr(args, "summary") and args.summary:
        first_uuid: str | None = None
        last_uuid: str | None = None
        for _raw_idx, data, _msg in messages:
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
    for _raw_idx, data, msg in messages:
        rendered = _render_one_message(data, msg, config, formatter)
        if rendered is None:
            continue
        print(rendered)
        uuid = data.get("uuid", "")
        if uuid:
            if first_uuid is None:
                first_uuid = uuid
            last_uuid = uuid

    # The bottom-of-output "to see new messages" hint is only shown when
    # called directly from `read` — programmatic callers (like --show-response)
    # set args.no_hint and print their own hint using the returned UUIDs.
    # Preserve --exclude-user in the suggestion so re-running produces the
    # same view the user is currently seeing.
    suppress_hint = getattr(args, "no_hint", False)
    if last_uuid and not suppress_hint:
        exclude_user_flag = (
            " --exclude-user" if getattr(args, "exclude_user", False) else ""
        )
        print(
            f"\nTo see NEW messages after this point: "
            f"claude-worker read {args.name} "
            f"--since {last_uuid[:UUID_SHORT_LENGTH]}{exclude_user_flag}"
        )
    return first_uuid, last_uuid


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
    # messages stores (raw_idx, data, msg) so --last-turn can filter by
    # raw-scan position after computing the walk-back window. raw_idx is
    # total_scanned at the time of append.
    messages: list[tuple[int, dict, object]] = []
    total_scanned = 0
    # Remember the --since marker message so we can show its content if the
    # result set is empty ("No new messages since [abc12345]: ...")
    since_marker_data: dict | None = None

    # -- Missing chat tag monitoring state (PM workers only) --
    # Tracks each turn's chat_id from the user message and the final
    # assistant message so we can check tagging discipline on the result
    # boundary. Only activated for PM workers.
    monitor_pm_tags = _worker_is_pm(args.name)

    # -- Reverse-walk fast path (Imp-5) --
    # When the query only needs the tail of the log (--last-turn or -n N)
    # AND we don't need full-log forward state (no --since / --until, no
    # PM tag monitoring), skip the O(log_size) forward scan and reverse-
    # iterate only as far back as needed.
    fast_path_eligible = (
        not monitor_pm_tags
        and since_uuid is None
        and since_ts is None
        and not (hasattr(args, "until") and args.until)
    )
    if fast_path_eligible:
        chat_id_for_fast = getattr(args, "chat_id", None)
        fast_messages = _read_static_fast_path(log_file, config, args, chat_id_for_fast)
        if fast_messages is not None:
            messages = fast_messages
            # Jump past the forward scan and the post-scan filters that
            # were already applied inside the fast path.
            return _render_read_output(messages, formatter, config, args)
    current_turn_chat_id: str | None = None
    last_assistant_in_turn: dict | None = None
    missing_tag_reports: list[dict] = []

    # -- --last-turn walk-back state --
    # Tracks the raw-scan index of the most recent user-input and assistant
    # messages. After the scan, the turn window starts at the EARLIER of the
    # two (i.e. the message that completed the "one of each" pair going
    # backwards from the end of the log). Runs on raw data before display
    # filtering so --exclude-user doesn't break boundary detection.
    last_user_raw_idx = -1
    last_asst_raw_idx = -1

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

            # -- PM tag monitoring: track turn boundaries BEFORE any display
            # filter. Runs for all chats (not gated on the user's --chat
            # filter) because a missing tag in ANY chat is worth surfacing.
            msg_type_raw = data.get("type")
            if monitor_pm_tags:
                if msg_type_raw == "user":
                    current_turn_chat_id = _extract_chat_id_from_message(data)
                    last_assistant_in_turn = None
                elif msg_type_raw == "assistant":
                    last_assistant_in_turn = data
                elif msg_type_raw == "result":
                    if current_turn_chat_id and last_assistant_in_turn is not None:
                        if not _message_contains_chat_tag(
                            last_assistant_in_turn, current_turn_chat_id
                        ):
                            missing_uuid = last_assistant_in_turn.get("uuid", "")
                            if missing_uuid:
                                missing_tag_reports.append(
                                    {
                                        "uuid": missing_uuid,
                                        "chat_id": current_turn_chat_id,
                                        "preview": _extract_text_preview(
                                            last_assistant_in_turn,
                                            MISSING_TAG_PREVIEW_MAX_CHARS,
                                        ),
                                    }
                                )
                    current_turn_chat_id = None
                    last_assistant_in_turn = None

            # Walk-back tracking for --last-turn. Runs on RAW data (before
            # display filtering) so --exclude-user can still compute the
            # correct window boundary via user-input messages.
            if hasattr(args, "last_turn") and args.last_turn:
                if _is_user_input_raw(data):
                    last_user_raw_idx = total_scanned
                elif _has_assistant_text(data):
                    last_asst_raw_idx = total_scanned

            # Chat routing filter: keep only messages containing [chat:<id>].
            # args.chat_id is set by cmd_read after resolving --chat / env.
            # Applied AFTER --last-turn's reset so last-turn can still find
            # the turn boundary correctly, but BEFORE display filtering so
            # we don't waste work on messages we'll drop anyway.
            chat_id = getattr(args, "chat_id", None)
            if chat_id and not _message_contains_chat_tag(data, chat_id):
                continue

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

            messages.append((total_scanned, data, msg))

    # Handle missing-tag reports collected during the scan (PM workers only).
    # Runs before the --since-not-found early return because missing tags
    # are worth surfacing regardless of user filter results.
    if monitor_pm_tags and missing_tag_reports:
        _handle_missing_tag_reports(args.name, missing_tag_reports)

    # Warn when --since UUID was not found in the log
    if (since_uuid or since_ts) and not found_since:
        target = since_uuid or str(since_ts)
        print(
            f"Warning: --since '{target}' not found in log ({total_scanned} messages scanned)",
            file=sys.stderr,
        )
        return None, None

    # Handle --last-turn: filter messages to the walk-back window. Window
    # starts at the EARLIER of (last user-input raw index, last assistant
    # raw index) and runs to the end of the scan. Degrades gracefully if
    # only one type is present — in that case show everything we collected.
    if hasattr(args, "last_turn") and args.last_turn:
        if last_user_raw_idx >= 0 and last_asst_raw_idx >= 0:
            window_start = min(last_user_raw_idx, last_asst_raw_idx)
            messages = [m for m in messages if m[0] >= window_start]

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

    return _render_read_output(messages, formatter, config, args)


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
                    rendered = _render_one_message(data, msg, config, formatter)
                    if rendered is not None:
                        print(rendered, flush=True)
        except KeyboardInterrupt:
            pass


def cmd_wait_for_turn(args: argparse.Namespace) -> None:
    """Block until claude finishes its turn or the session ends."""
    resolve_worker(args.name)  # validate worker exists
    rc = _wait_for_turn(
        args.name,
        timeout=args.timeout,
        after_uuid=getattr(args, "after_uuid", None),
        settle=args.settle,
        chat_tag=getattr(args, "chat", None),
    )
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

    # Read CWD + identity flags from saved worker metadata
    cwd = "-"
    saved = get_saved_worker(name)
    is_pm = bool(saved and saved.get("pm"))
    is_tl = bool(saved and saved.get("team_lead"))
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

    # Context window usage from the most recent assistant turn. Silent
    # when the worker hasn't produced a turn yet or claugs isn't
    # available; otherwise shows "N% (Nk/1M)" next to the preview.
    context_label = _format_context_window_label(log_file)
    context_line = f"\n    context: {context_label}" if context_label else ""

    identity_tag = " [PM]" if is_pm else " [TL]" if is_tl else ""
    return (
        f"  {name}{identity_tag}\n"
        f"    pid: {pid}  status: {status}{idle_str}  cwd: {cwd}\n"
        f"    session: {session}"
        f"{preview_line}"
        f"{context_line}"
    )


def _get_worker_info(name: str) -> dict | None:
    """Collect structured info about a worker for filtering and display."""
    runtime = get_runtime_dir(name)
    if not runtime.exists():
        return None

    saved = get_saved_worker(name)
    is_pm = bool(saved and saved.get("pm"))
    is_tl = bool(saved and saved.get("team_lead"))
    raw_cwd = (saved.get("cwd") or "-") if saved else "-"

    status, log_mtime = get_worker_status(runtime)

    role = "pm" if is_pm else "tl" if is_tl else "worker"

    return {
        "name": name,
        "role": role,
        "status": status,
        "cwd": raw_cwd,
        "log_mtime": log_mtime,
    }


def _collect_filtered_workers(args: argparse.Namespace) -> list[dict]:
    """Scan all workers and apply filter flags. Shared by cmd_list and --broadcast."""
    workers: list[dict] = []
    seen: set[str] = set()
    for base in (get_base_dir(), _legacy_base_dir()):
        if not base.exists():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name in seen:
                continue
            if "." in entry.name:
                continue
            seen.add(entry.name)
            info = _get_worker_info(entry.name)
            if info is not None:
                workers.append(info)

    role_filter = getattr(args, "role", None)
    status_filter = getattr(args, "status", None)
    alive_filter = getattr(args, "alive", False)
    cwd_filter = getattr(args, "cwd_filter", None)

    if role_filter:
        workers = [w for w in workers if w["role"] == role_filter]
    if status_filter:
        workers = [w for w in workers if w["status"] == status_filter]
    if alive_filter:
        workers = [w for w in workers if w["status"] != "dead"]
    if cwd_filter:
        resolved_filter = str(Path(cwd_filter).resolve())
        workers = [
            w
            for w in workers
            if w["cwd"] != "-"
            and str(Path(w["cwd"]).resolve()).startswith(resolved_filter)
        ]
    return workers


def cmd_list(args: argparse.Namespace) -> None:
    """List all workers with optional filters.

    Scans both the new (~/.cwork/workers/) and legacy (/tmp/) base
    directories. Filters are composable with AND logic. Prunes old
    archives (>30 days) as a side effect.
    """
    prune_archives()
    workers = _collect_filtered_workers(args)

    format_mode = getattr(args, "format", None)
    if format_mode == "json":
        for w in workers:
            out = {k: v for k, v in w.items() if k != "log_mtime"}
            print(json.dumps(out))
    else:
        for w in workers:
            line = _format_worker_line(w["name"])
            if line:
                print(line)


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop a worker.

    Two-phase shutdown (default): if the worker is alive, write a wrap-up
    message to the FIFO, wait for the turn to complete (up to
    STOP_WRAPUP_TIMEOUT_SECONDS), then send SIGTERM. ``--no-wrap-up``
    or ``--force`` skip straight to the signal.
    """
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

    # Two-phase wrap-up: if the worker is alive and wrap-up is enabled,
    # send a wrap-up message and wait for the turn to complete before
    # sending SIGTERM.
    wrap_up = not args.force and not args.no_wrap_up and pid_alive(pid)
    if wrap_up:
        in_fifo = runtime / "in"
        log_file = runtime / "log"
        if in_fifo.exists():
            # Resolve effective timeout: CLI flag → config minimum → default
            minimum = _get_wrapup_timeout_minimum()
            timeout = getattr(args, "wrap_up_timeout", None) or minimum
            if timeout < minimum:
                print(
                    f"Warning: wrap-up timeout must be at minimum {minimum}s, "
                    f"using {minimum}s",
                    file=sys.stderr,
                )
                timeout = minimum
            try:
                # Capture the last UUID before writing so _wait_for_turn
                # skips the prior turn's result (same race guard as cmd_send).
                marker_uuid = _get_last_uuid(log_file) if log_file.exists() else None
                msg = json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": _build_stop_wrapup_message(),
                        },
                    }
                )
                # Use O_NONBLOCK to avoid hanging if the worker dies between
                # the pid_alive check and here.
                wr = os.open(str(in_fifo), os.O_WRONLY | os.O_NONBLOCK)
                try:
                    os.write(wr, (msg + "\n").encode())
                finally:
                    os.close(wr)
                print(
                    f"Sent wrap-up message to '{args.name}', waiting for completion..."
                )
                _wait_for_turn(
                    args.name,
                    timeout=timeout,
                    settle=0,
                    after_uuid=marker_uuid,
                )
            except BlockingIOError:
                # No reader on the FIFO — worker likely died between
                # pid_alive check and here. Proceed to SIGTERM.
                pass
            except Exception:
                # Wrap-up is best-effort — proceed to SIGTERM on any failure
                pass

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


def _get_ppid(pid: int) -> int | None:
    """Read the parent PID of a process from /proc.

    Returns None if the process doesn't exist or /proc is unavailable.
    Linux/WSL2 only — macOS would need ``ps -o ppid= -p PID``.
    """
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def _find_worker_by_ancestry() -> str | None:
    """Walk the process ancestry looking for a claude-pid match.

    Claude Code Bash tool invocations create this process tree:
    claude (Node.js) → bash -c '...' → command. The ``claude-pid``
    file in each worker's runtime dir stores the claude process PID.
    This function walks up from the current process, checking each
    ancestor against all workers' claude-pid files.

    Bounded to REPLACEME_ANCESTOR_WALK_MAX levels to prevent runaway
    walks. Handles subshells and pipes (which add extra levels).
    """
    pid = os.getpid()
    for _ in range(REPLACEME_ANCESTOR_WALK_MAX):
        pid = _get_ppid(pid)
        if pid is None or pid <= 1:
            return None
        # Scan all worker runtime dirs for a matching claude-pid
        for base in (get_base_dir(), _legacy_base_dir()):
            if not base.exists():
                continue
            for entry in base.iterdir():
                if not entry.is_dir():
                    continue
                claude_pid_file = entry / "claude-pid"
                if claude_pid_file.exists():
                    try:
                        if int(claude_pid_file.read_text().strip()) == pid:
                            return entry.name
                    except (ValueError, OSError):
                        continue
    return None


def _validate_wrapup(name: str, runtime: Path) -> str | None:
    """Check that wrap-up is complete. Returns an error message or None.

    Tier 1 (universal): worker must be idle (turn complete).
    Tier 2 (identity-specific): PM/TL workers must have a recent
    handoff file.
    """
    status, _ = get_worker_status(runtime)
    if status == "working":
        return (
            f"Worker '{name}' is still working. Wait for the current turn to complete."
        )
    if status == "dead":
        return f"Worker '{name}' is dead. Nothing to replace."

    # Identity-specific checks
    saved = get_saved_worker(name)
    if not saved:
        return None  # no metadata = plain worker, skip identity checks

    cwd = saved.get("cwd", "")
    if not cwd:
        return None

    handoff_dirs: list[Path] = []
    if saved.get("pm"):
        handoff_dirs.append(Path(cwd) / ".cwork" / "pm" / "handoffs")
    if saved.get("team_lead"):
        handoff_dirs.append(Path(cwd) / ".cwork" / "technical-lead" / "handoffs")

    for handoff_dir in handoff_dirs:
        if not handoff_dir.exists():
            return (
                f"No handoff directory at {handoff_dir}. "
                f"Complete your wrap-up procedure before calling replaceme."
            )
        handoff_files = sorted(handoff_dir.iterdir())
        if not handoff_files:
            return (
                f"No handoff files in {handoff_dir}. "
                f"Write a handoff file before calling replaceme."
            )
        newest = handoff_files[-1]
        age_minutes = (time.time() - newest.stat().st_mtime) / 60
        if age_minutes > REPLACEME_HANDOFF_MAX_AGE_MINUTES:
            return (
                f"Most recent handoff ({newest.name}) is {int(age_minutes)} minutes old. "
                f"Write a fresh handoff before calling replaceme."
            )

    return None


def cmd_replaceme(args: argparse.Namespace) -> None:
    """Replace the current worker with a fresh instance.

    Auto-detects which worker is calling by walking the process
    ancestry and matching against claude-pid files. Forks a detached
    replacer process that sends SIGUSR1 to the old manager, waits
    for it to archive and exit, then starts a new manager with the
    same identity and session.
    """
    # 1. Auto-detect which worker we belong to
    worker_name = _find_worker_by_ancestry()
    if worker_name is None:
        print(
            "Error: could not determine which worker this command is running in. "
            "replaceme must be called from inside a worker's Bash tool.",
            file=sys.stderr,
        )
        sys.exit(1)

    runtime = get_runtime_dir(worker_name)
    print(f"Detected worker: {worker_name}")

    # 2. Read worker metadata
    saved = get_saved_worker(worker_name)
    if not saved:
        print(f"Error: no saved metadata for worker '{worker_name}'", file=sys.stderr)
        sys.exit(1)

    session_id = saved.get("session_id")
    if not session_id:
        print(f"Error: no session_id for worker '{worker_name}'", file=sys.stderr)
        sys.exit(1)

    # 3. Validate wrap-up (unless --skip-validation)
    if not args.skip_validation:
        error = _validate_wrapup(worker_name, runtime)
        if error:
            print(f"Error: {error}", file=sys.stderr)
            print("Use --skip-validation to override.", file=sys.stderr)
            sys.exit(1)

    # 4. Read old manager PID
    pid_file = runtime / "pid"
    try:
        old_manager_pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        print("Error: cannot read manager PID", file=sys.stderr)
        sys.exit(1)

    if not pid_alive(old_manager_pid):
        print(f"Error: manager process {old_manager_pid} is not alive", file=sys.stderr)
        sys.exit(1)

    # 5. Prepare replacement parameters
    cwd = saved.get("cwd")
    # Reconstruct claude_args: saved args + --resume
    saved_args = saved.get("claude_args") or []
    claude_args = ["--resume", session_id] + saved_args
    pm_mode = saved.get("pm", False)
    tl_mode = saved.get("team_lead", False)

    print(f"Forking replacer process (old manager PID: {old_manager_pid})...")

    # 6. Fork a detached replacer process BEFORE signaling.
    # The replacer survives the old claude process's death.
    child_pid = os.fork()
    if child_pid > 0:
        # Parent (the Bash tool invocation) — exit immediately.
        # The replacer runs independently.
        print(f"Replacer forked (PID: {child_pid}). Replacement in progress.")
        return

    # --- Child: detached replacer process ---
    os.setsid()
    # Redirect stdio to /dev/null so the orphaned process doesn't
    # hold open any pipes to the dying claude process.
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    try:
        # 6a. Send SIGUSR1 to old manager
        os.kill(old_manager_pid, signal.SIGUSR1)

        # 6b. Wait for old manager to die
        deadline = time.monotonic() + REPLACEME_OLD_MANAGER_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            if not pid_alive(old_manager_pid):
                break
            time.sleep(REPLACEME_OLD_MANAGER_POLL_INTERVAL)
        else:
            # Timeout — escalate to SIGTERM
            try:
                os.kill(old_manager_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(2.0)

        # 6c. The old manager archived the runtime dir (SIGUSR1 handler).
        # The worker name is now free. Create new runtime dir.
        new_runtime = create_runtime_dir(worker_name)

        # 6d. Write identity file if PM or TL
        if pm_mode or tl_mode:
            identity_path = new_runtime / "identity.md"
            if tl_mode:
                identity_resource = TL_IDENTITY_RESOURCE
            else:
                identity_resource = PM_IDENTITY_RESOURCE
            identity_content = _load_bundled_resource("identities", identity_resource)
            identity_path.write_text(identity_content)
            # Prepend --append-system-prompt-file to claude_args
            claude_args = [
                "--append-system-prompt-file",
                str(identity_path),
            ] + claude_args

        # 6e. Write permission settings if applicable
        permission_settings = _maybe_write_permission_settings(
            name=worker_name, enabled=True, cwd=cwd
        )
        if permission_settings is not None:
            claude_args = ["--settings", str(permission_settings)] + claude_args

        # 6f. Save worker metadata (same as cmd_start)
        save_worker(
            worker_name,
            cwd=cwd or os.getcwd(),
            claude_args=saved_args,  # save without --resume prefix
            pm=pm_mode,
            team_lead=tl_mode,
        )

        # 6g. Determine initial message for the new session
        initial_message = None
        if pm_mode:
            initial_message = PM_INTERNALIZE_MESSAGE
        elif tl_mode:
            initial_message = TL_INTERNALIZE_MESSAGE

        # 6h. Fork the new manager daemon (same pattern as cmd_start)
        manager_pid = os.fork()
        if manager_pid == 0:
            # Grandchild: the new manager
            run_manager(worker_name, cwd, claude_args, initial_message)
            sys.exit(0)
        # Replacer: wait for the new manager's PID file, then exit
        new_pid_file = new_runtime / "pid"
        pid_deadline = time.monotonic() + 10.0
        while time.monotonic() < pid_deadline:
            if new_pid_file.exists():
                break
            time.sleep(0.1)
        sys.exit(0)
    except Exception:
        # Best-effort: if anything goes wrong, don't leave the
        # replacer process lingering.
        sys.exit(1)


def cmd_reply(args: argparse.Namespace) -> None:
    """Send a reply to a worker's message queue.

    Unlike ``send``, this writes to a persistent queue directory, not a
    FIFO. The recipient's manager drains the queue on each poll cycle and
    injects replies as synthetic user messages. No status gate — the reply
    is stored even if the worker is busy or temporarily dead.

    Designed for the callback pattern: a worker sends a question with
    ``[reply-to:<name>]``, the recipient calls ``claude-worker reply
    <name> "answer"`` when ready.
    """
    if args.message:
        content = " ".join(args.message)
    else:
        content = sys.stdin.read()

    if not content.strip():
        print("Error: empty reply message", file=sys.stderr)
        sys.exit(1)

    sender = args.sender or _find_worker_by_ancestry() or "unknown"

    msg_path = enqueue_message(args.name, sender, content)
    print(f"Reply queued for '{args.name}' (from: {sender})")


def cmd_notify(args: argparse.Namespace) -> None:
    """Send a notification to the human via the configured channel.

    Reads ``notifications.command`` from ``~/.cwork/config.yaml``,
    substitutes ``${MESSAGE}`` with the notification text, and runs it
    via subprocess. Best-effort: logs failures to stderr, never crashes.
    Rate-limited per caller to NOTIFY_COOLDOWN_SECONDS.
    """
    config = _get_cwork_config()
    notif = config.get("notifications", {})
    if not isinstance(notif, dict):
        return

    if not notif.get("enabled", False):
        return

    command_template = notif.get("command")
    if not command_template:
        print(
            "Warning: notifications.command not set in ~/.cwork/config.yaml",
            file=sys.stderr,
        )
        return

    message = " ".join(args.message) if args.message else sys.stdin.read()
    if not message.strip():
        print("Error: empty notification message", file=sys.stderr)
        sys.exit(1)

    # Rate limiting via cooldown file
    cooldown_dir = Path.home() / ".cwork" / "notify-cooldowns"
    cooldown_dir.mkdir(parents=True, exist_ok=True)
    import hashlib

    caller_id = getattr(args, "worker", None) or "cli"
    cooldown_file = cooldown_dir / hashlib.md5(caller_id.encode()).hexdigest()
    if cooldown_file.exists():
        try:
            last_sent = float(cooldown_file.read_text().strip())
            if time.time() - last_sent < NOTIFY_COOLDOWN_SECONDS:
                print(
                    f"Notification rate-limited (cooldown {NOTIFY_COOLDOWN_SECONDS}s)",
                    file=sys.stderr,
                )
                return
        except (ValueError, OSError):
            pass

    # Substitute and run
    command = command_template.replace("${MESSAGE}", message.replace("'", "'\\''"))
    try:
        import subprocess

        result = subprocess.run(
            command,
            shell=True,
            timeout=NOTIFY_SUBPROCESS_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"Warning: notification command exited {result.returncode}: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except subprocess.TimeoutExpired:
        print("Warning: notification command timed out", file=sys.stderr)
    except Exception as exc:
        print(f"Warning: notification failed: {exc}", file=sys.stderr)

    # Update cooldown timestamp
    try:
        cooldown_file.write_text(str(time.time()))
    except OSError:
        pass

    print(f"Notification sent: {message[:80]}...")


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

    # 6. Write the settings file atomically — ~/.claude/settings.json is
    # the user's Claude Code config and must not be corrupted by a partial
    # write if the disk fills or a signal arrives mid-write.
    _atomic_write_text(settings_path, proposed_text)
    print(f"Updated {settings_path}")
    print(f'\nTest with: claude -p "echo $CLAUDE_SESSION_UUID"')


# -- Permission grant --------------------------------------------------------
#
# See claude_worker/permission_grant.py for the hook side of the feature.
# These helpers manage the grants.jsonl file and the per-worker settings
# that wire the hook into claude at worker start.


def _grants_file(name: str) -> Path:
    """Path to the worker's grants.jsonl."""
    return get_runtime_dir(name) / GRANTS_FILE_NAME


def _permission_settings_file(name: str) -> Path:
    """Path to the per-worker settings.json that hosts the permission hook."""
    return get_runtime_dir(name) / PERMISSION_SETTINGS_FILE_NAME


def _generate_grant_id() -> str:
    """Short random grant identifier, e.g. ``grant-abc12345``.

    Not cryptographic — grants are local to one worker's runtime dir
    and the ID is just for human reference in `grants` listings and
    the hook's deny-reason string.
    """
    import secrets

    return f"grant-{secrets.token_hex(GRANT_ID_LENGTH // 2)}"


def _now_iso() -> str:
    """UTC ISO 8601 timestamp matching the hook module's format.

    Kept local (instead of importing from permission_grant) so
    cli.py doesn't pull in the hook module on every CLI invocation.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_grants(grants_file: Path) -> list[dict]:
    """Parse the worker's grants.jsonl into a list of grant dicts.

    Mirrors the loader in ``permission_grant._load_grants``. The two
    copies exist so the hook module has zero dependencies on cli.py —
    the hook runs in a claude-subprocess context and should stay small.
    """
    if not grants_file.exists():
        return []
    grants: list[dict] = []
    try:
        raw = grants_file.read_text()
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            grants.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return grants


def _append_grant(grants_file: Path, grant: dict) -> None:
    """Append a new grant to grants.jsonl via a simple append open.

    JSONL append of a small line is atomic at the OS level for writes
    under PIPE_BUF (4KiB on Linux), and our grant records are well
    under that. Rewrites (revoke, consume) go through
    ``_atomic_write_text`` in `_rewrite_grants` instead.
    """
    grants_file.parent.mkdir(parents=True, exist_ok=True)
    with open(grants_file, "a") as f:
        f.write(json.dumps(grant) + "\n")


def _rewrite_grants(grants_file: Path, grants: list[dict]) -> None:
    """Rewrite the grants file atomically via sibling-tmp + os.replace.

    Used by ``revoke`` (and by the hook's consume-on-use path, which
    has its own copy in permission_grant.py for the same reason
    _load_grants is duplicated).
    """
    content = "\n".join(json.dumps(g) for g in grants)
    if content:
        content += "\n"
    _atomic_write_text(grants_file, content)


def _find_last_denial(log_file: Path) -> dict | None:
    """Walk the worker's log backwards for the most recent sensitive-file
    denial and return the triple needed to build a grant.

    Returns a dict with keys ``tool_name``, ``file_path``, ``tool_use_id``,
    or None if the log contains no sensitive-file denial.

    The scan uses ``_iter_log_reverse`` so we only read the tail of
    the file. We first find the tool_result with the denial marker,
    then continue walking back for the paired ``tool_use`` assistant
    message with the same tool_use_id (needed for the file_path and
    tool_name). If the pair can't be resolved, we return None.
    """
    if not log_file.exists():
        return None

    target_tool_use_id: str | None = None
    for data in _iter_log_reverse(log_file):
        msg_type = data.get("type")
        if target_tool_use_id is None:
            # Phase 1: find the denial tool_result.
            if msg_type != "user":
                continue
            content = data.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") != "tool_result":
                    continue
                if not part.get("is_error"):
                    continue
                body = part.get("content", "")
                if isinstance(body, str) and SENSITIVE_DENIAL_MARKER in body:
                    tid = part.get("tool_use_id")
                    if tid:
                        target_tool_use_id = tid
                        break
            if target_tool_use_id is None:
                continue
            # Fall through to phase 2 on the next iteration.
            continue
        # Phase 2: find the assistant tool_use with this tool_use_id.
        if msg_type != "assistant":
            continue
        content = data.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "tool_use":
                continue
            if part.get("id") != target_tool_use_id:
                continue
            tool_input = part.get("input") or {}
            return {
                "tool_name": part.get("name", ""),
                "file_path": tool_input.get("file_path", ""),
                "tool_use_id": target_tool_use_id,
            }
    return None


def cmd_grant(args: argparse.Namespace) -> None:
    """Add a permission grant for a worker.

    Grants live in ``<runtime>/grants.jsonl`` and are consulted by the
    PreToolUse hook at tool-call time. See
    ``claude_worker/permission_grant.py`` for the hook semantics.
    """
    runtime = resolve_worker(args.name)
    grants_file = runtime / GRANTS_FILE_NAME

    # Determine the match spec — exactly one of path/glob/tool_use_id/last
    match: dict | None = None
    source_tool_use_id: str | None = None
    tools = args.tool or list(PERMISSION_HOOK_TOOLS)

    if args.last:
        log_file = runtime / "log"
        denial = _find_last_denial(log_file)
        if denial is None:
            print(
                f"Error: no recent sensitive-file denial found in worker "
                f"'{args.name}' log. Use --path/--glob/--tool-use-id to "
                f"grant explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)
        match = {"path": denial["file_path"]}
        source_tool_use_id = denial["tool_use_id"]
        # If the user didn't override --tool, scope the grant to the
        # specific tool that was denied, so a grant for an Edit doesn't
        # also auto-approve an unrelated Write that happens to target
        # the same file.
        if args.tool is None and denial["tool_name"]:
            tools = [denial["tool_name"]]
    elif args.path:
        match = {"path": args.path}
    elif args.glob:
        match = {"glob": args.glob}
    elif args.tool_use_id:
        match = {"tool_use_id": args.tool_use_id}
    else:
        print(
            "Error: exactly one of --path, --glob, --tool-use-id, or --last "
            "is required.",
            file=sys.stderr,
        )
        sys.exit(1)

    grant: dict = {
        "id": _generate_grant_id(),
        "match": match,
        "tools": tools,
        "persistent": bool(args.persistent),
        "consumed": False,
        "created_at": _now_iso(),
    }
    if args.reason:
        grant["reason"] = args.reason
    if source_tool_use_id is not None:
        grant["source_tool_use_id"] = source_tool_use_id

    _append_grant(grants_file, grant)

    # Friendly summary
    match_desc = next(iter(match.items()))
    persistent_note = " (persistent)" if args.persistent else ""
    print(
        f"Granted {grant['id']} for worker '{args.name}'{persistent_note}: "
        f"{match_desc[0]}={match_desc[1]} tools={','.join(tools)}"
    )


def cmd_grants(args: argparse.Namespace) -> None:
    """List active (non-consumed) grants for a worker."""
    runtime = resolve_worker(args.name)
    grants_file = runtime / GRANTS_FILE_NAME
    all_grants = _load_grants(grants_file)
    active = [g for g in all_grants if not g.get("consumed")]
    if not active:
        print(f"No active grants for worker '{args.name}'.")
        return
    for g in active:
        match = g.get("match", {})
        match_desc = next(iter(match.items())) if match else ("?", "?")
        tools = ",".join(g.get("tools") or [])
        tag = "[persistent]" if g.get("persistent") else "[one-shot]"
        reason = f" # {g['reason']}" if g.get("reason") else ""
        print(
            f"{g.get('id', '?')} {tag} {match_desc[0]}={match_desc[1]} "
            f"tools={tools}{reason}"
        )


def cmd_revoke(args: argparse.Namespace) -> None:
    """Revoke one or all grants for a worker.

    With ``--all``, removes every grant (including consumed history).
    With a GRANT_ID argument, removes that specific grant. Rewrites
    the grants file atomically.
    """
    runtime = resolve_worker(args.name)
    grants_file = runtime / GRANTS_FILE_NAME
    grants = _load_grants(grants_file)
    if getattr(args, "all", False):
        _rewrite_grants(grants_file, [])
        print(f"Revoked all grants for worker '{args.name}' ({len(grants)} removed).")
        return
    if not args.grant_id:
        print(
            "Error: specify a grant id, or pass --all to clear every grant.",
            file=sys.stderr,
        )
        sys.exit(1)
    target_id = args.grant_id
    remaining = [g for g in grants if g.get("id") != target_id]
    if len(remaining) == len(grants):
        print(
            f"Error: grant '{target_id}' not found for worker '{args.name}'.",
            file=sys.stderr,
        )
        sys.exit(1)
    _rewrite_grants(grants_file, remaining)
    print(f"Revoked grant '{target_id}' for worker '{args.name}'.")


def _build_permission_hook_settings(
    grants_path: Path,
    python_executable: str,
    sentinel_dir: Path | None = None,
    cwd: str | None = None,
) -> dict:
    """Build the settings dict for per-worker hooks.

    Wires up to three hooks:
    1. PreToolUse — permission grant hook for Edit/Write/MultiEdit
    2. PreToolUse — CWD write guard (if cwd provided)
    3. Stop — context threshold check after each turn (if sentinel_dir provided)

    This is pure data — no I/O — so tests can assert on the shape
    without touching the filesystem.
    """
    matcher = "|".join(PERMISSION_HOOK_TOOLS)
    permission_command = (
        f"{python_executable} -m claude_worker.permission_grant "
        f"--grants-file {grants_path}"
    )
    pretooluse_entries: list[dict] = [
        {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": permission_command,
                }
            ],
        }
    ]
    if cwd is not None:
        cwd_guard_command = (
            f"{python_executable} -m claude_worker.cwd_guard --cwd {cwd}"
        )
        pretooluse_entries.append(
            {
                "matcher": matcher,
                "hooks": [
                    {
                        "type": "command",
                        "command": cwd_guard_command,
                    }
                ],
            }
        )
    hooks: dict = {"PreToolUse": pretooluse_entries}
    if sentinel_dir is not None:
        context_command = (
            f"{python_executable} -m claude_worker.context_threshold "
            f"--sentinel-dir {sentinel_dir}"
        )
        hooks["Stop"] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": context_command,
                    }
                ],
            }
        ]
    if cwd is not None:
        ticket_watcher_command = (
            f"{python_executable} -m claude_worker.ticket_watcher --cwd {cwd}"
        )
        hooks["PostToolUse"] = [
            {
                "matcher": matcher,
                "hooks": [
                    {
                        "type": "command",
                        "command": ticket_watcher_command,
                    }
                ],
            }
        ]
    return {"hooks": hooks}


def _maybe_write_permission_settings(
    name: str, enabled: bool, cwd: str | None = None
) -> Path | None:
    """Generate the per-worker settings.json for worker hooks.

    Called by ``cmd_start`` just after ``create_runtime_dir`` and before
    the fork, so the manager subprocess can pick up the file when it
    spawns claude. Returns the settings.json path when written (so
    cmd_start can pass ``--settings <path>`` to claude), or None when
    disabled via ``--no-permission-hook``.
    """
    if not enabled:
        return None
    runtime = get_runtime_dir(name)
    settings_path = runtime / PERMISSION_SETTINGS_FILE_NAME
    grants_path = runtime / GRANTS_FILE_NAME
    settings = _build_permission_hook_settings(
        grants_path=grants_path,
        python_executable=sys.executable,
        sentinel_dir=runtime,
        cwd=cwd,
    )
    _atomic_write_text(settings_path, json.dumps(settings, indent=2) + "\n")
    return settings_path


# -- REPL --------------------------------------------------------------------


def _compute_repl_chat_id() -> str:
    """Build a deterministic chat ID for REPL sessions on PM workers.

    PM workers need chat tags to route responses correctly, but the REPL
    runs from a human terminal — not inside Claude Code — so the usual
    CLAUDE_SESSION_UUID auto-detection path doesn't fire.

    Fallback: derive a stable-ish ID from the REPL process's PID and
    controlling TTY. Same shell window → same PID → same chat ID for
    the lifetime of the REPL session. Different terminal windows get
    different IDs.

    Format: ``repl-<pid>-<tty_basename>`` (e.g. ``repl-12345-pts3``).
    If the TTY isn't available (e.g. stdin is a pipe), falls back to
    just ``repl-<pid>``.
    """
    pid = os.getpid()
    tty_component = ""
    try:
        tty_name = os.ttyname(sys.stdin.fileno())
        # Reduce "/dev/pts/3" → "pts3" for readability
        tty_component = tty_name.replace("/dev/", "").replace("/", "")
    except (OSError, ValueError, AttributeError):
        pass
    if tty_component:
        return f"repl-{pid}-{tty_component}"
    return f"repl-{pid}"


def _flush_stdin() -> None:
    """Discard any bytes the user typed while we weren't reading stdin.

    Called just before showing the REPL prompt, so keystrokes made
    while the worker was still processing (which would otherwise be
    mixed into the next turn's input) are silently dropped. Uses
    termios.tcflush when stdin is a TTY; no-op when stdin is a pipe
    (e.g., under pytest or in a script) because there's nothing to
    flush.
    """
    try:
        import termios

        if sys.stdin.isatty():
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except (ImportError, OSError, ValueError):
        # Non-Unix, closed stdin, or some other oddity — nothing to do.
        pass


def _wait_for_worker_idle(
    name: str,
    poll_interval: float = REPL_IDLE_POLL_INTERVAL_SECONDS,
) -> str:
    """Block until the worker's status is `waiting` or `dead`.

    Re-uses the same point-in-time ``get_worker_status`` check that
    `ls` uses, which applies STATUS_IDLE_THRESHOLD_SECONDS via the log
    mtime. Polls every ``poll_interval`` seconds.

    Returns the final status string (``"waiting"`` or ``"dead"``).
    Interrupted by KeyboardInterrupt, which propagates to the caller.
    """
    runtime = get_runtime_dir(name)
    while True:
        status, _ = get_worker_status(runtime)
        if status in ("waiting", "dead"):
            return status
        time.sleep(poll_interval)


def _repl_stream_new_messages(
    log_file: Path,
    config,
    formatter,
    start_position: int,
    stop_event: threading.Event,
) -> None:
    """Tail the log from ``start_position`` and print new messages.

    Runs until ``stop_event`` is set. Each new JSONL line is parsed,
    filtered through the same display config ``read`` uses, and
    printed via ``_render_one_message``.

    Used by the REPL's working-phase display so the user sees assistant
    text streaming in as claude responds. The stop_event is set by the
    REPL loop when the worker transitions back to idle, so the stream
    thread winds down cleanly.
    """
    from claude_logs import parse_message, should_show_message

    with open(log_file) as f:
        f.seek(start_position)
        while not stop_event.is_set():
            line = f.readline()
            if not line:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            msg = parse_message(data)
            if not should_show_message(msg, data, config):
                continue
            rendered = _render_one_message(data, msg, config, formatter)
            if rendered is not None:
                print(rendered, flush=True)


def _repl_print_last_turn(name: str) -> None:
    """Print the worker's last-turn context at REPL entry.

    Equivalent to ``claude-worker read NAME --last-turn`` (including
    user messages — this is for a human reading the screen, not an
    orchestrator). Silent when the worker has no prior context.
    """
    namespace = argparse.Namespace(
        name=name,
        follow=False,
        since=None,
        until=None,
        last_turn=True,
        n=None,
        count=False,
        summary=False,
        verbose=False,
        exclude_user=False,  # human reader wants to see their own prior messages
        color=False,
        no_color=False,
        chat=None,
        all_chats=True,
        no_hint=True,
    )
    try:
        cmd_read(namespace)
    except SystemExit:
        # cmd_read exits 1 when the log doesn't exist yet (fresh worker).
        # That's fine — the REPL should just continue with no context banner.
        pass


def cmd_tokens(args: argparse.Namespace) -> None:
    """Print token stats for a worker's session.

    Shows two views:
    1. **Context window** — the current in-flight input footprint, as
       reported by the most recent assistant turn's ``usage`` block.
       This matches what Claude Code's UI status line displays.
    2. **Session totals** — cumulative tokens across every API call
       since the session started, deduped by ``message.id`` to avoid
       the streaming-chunk double-count.

    Output format is designed to be both human-readable and cheap to
    parse with grep/awk. Example::

        $ claude-worker tokens cw-dev
        Worker: cw-dev
        Session: 86c9ce5a-8223-4164-a794-48a3b89a4901

        Context window:        79% (791k/1M)
          input:                   1
          cache_creation:        243
          cache_read:          788,443
          output:                  72
          source_line:            2271

        Session totals (deduped by message.id):
          input_tokens:             3,659
          output_tokens:          472,855
          cache_creation:       3,538,286
          cache_read:         334,867,411
          total_tokens:       338,882,211
          unique_api_calls:           773
          messages_considered:      1,235
    """
    runtime = resolve_worker(args.name)
    log_file = runtime / "log"

    if not log_file.exists():
        print(f"Worker '{args.name}' has no log output yet.", file=sys.stderr)
        sys.exit(1)

    try:
        from claude_logs import compute_context_window_usage, compute_token_stats
    except ImportError as exc:
        print(
            f"Error: claugs (claude_logs) is required for `tokens`: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Session ID for the banner
    session_file = runtime / "session"
    session_id = ""
    if session_file.exists():
        try:
            session_id = session_file.read_text().strip()
        except OSError:
            pass

    print(f"Worker: {args.name}")
    if session_id:
        print(f"Session: {session_id}")
    print()

    # Context window
    cw = compute_context_window_usage(log_file)
    if cw is None:
        print("Context window:        (no assistant turns yet)")
    else:
        label = _format_context_window_label(log_file) or "—"
        print(f"Context window:        {label}")
        print(f"  input:               {cw.input_tokens:>12,}")
        print(f"  cache_creation:      {cw.cache_creation_input_tokens:>12,}")
        print(f"  cache_read:          {cw.cache_read_input_tokens:>12,}")
        print(f"  output:              {cw.output_tokens:>12,}")
        print(f"  source_line:         {cw.source_line:>12,}")
    print()

    # Session totals
    stats = compute_token_stats(log_file)
    print("Session totals (deduped by message.id):")
    print(f"  input_tokens:        {stats.input_tokens:>12,}")
    print(f"  output_tokens:       {stats.output_tokens:>12,}")
    print(f"  cache_creation:      {stats.cache_creation_input_tokens:>12,}")
    print(f"  cache_read:          {stats.cache_read_input_tokens:>12,}")
    print(f"  total_tokens:        {stats.total_tokens:>12,}")
    print(f"  unique_api_calls:    {stats.unique_api_calls:>12,}")
    print(f"  messages_considered: {stats.messages_considered:>12,}")
    if stats.unknown_token_fields:
        print()
        print("Unknown token fields (not yet classified):")
        for field, value in stats.unknown_token_fields.items():
            print(f"  {field}: {value:,}")


def cmd_repl(args: argparse.Namespace) -> None:
    """Interactive human-facing REPL for a claude worker.

    Turn-by-turn chat loop:
        1. On entry, print the worker's last turn for context.
        2. Wait for the worker to be idle (status == waiting).
        3. Flush any stdin bytes the user typed during idle-wait.
        4. Prompt the user for input.
        5. Send the message. Note the pre-send log position.
        6. Live-stream new log content until the worker is idle again.
        7. Loop.

    Ctrl-D on an empty prompt, Ctrl-C twice in a row, or typing
    ``/exit`` or ``/quit`` exits the REPL. The worker stays alive —
    exiting the REPL does NOT stop the worker.
    """
    runtime = resolve_worker(args.name)
    log_file = runtime / "log"
    in_fifo = runtime / "in"

    # Resolve chat ID: explicit --chat > REPL auto ID on PM workers > None
    is_pm = _worker_is_pm(args.name)
    chat_id: str | None = None
    if args.chat:
        if is_pm:
            chat_id = args.chat
        else:
            print(
                f"Warning: --chat is only applicable to PM workers, "
                f"and '{args.name}' is not a PM. Messages will pass "
                f"through unchanged.",
                file=sys.stderr,
            )
    elif is_pm:
        chat_id = _compute_repl_chat_id()

    # Build the display config once — every turn uses the same rendering
    # settings (human mode: ANSI if a TTY, plain otherwise).
    from claude_logs import (
        ANSIFormatter,
        FilterConfig,
        PlainFormatter,
        RenderConfig,
    )

    verbose = getattr(args, "verbose", False)
    if verbose:
        hidden = {
            "timestamps",
            "metadata",
            "progress",
            "file-history-snapshot",
            "last-prompt",
        }
        config = RenderConfig(filters=FilterConfig(hidden=hidden))
    else:
        hidden = {"timestamps", "metadata", "thinking", "tools"}
        show_only = {"user", "user-input", "assistant", "queue-operation"}
        config = RenderConfig(filters=FilterConfig(show_only=show_only, hidden=hidden))
    formatter: object
    if sys.stdout.isatty():
        formatter = ANSIFormatter()
    else:
        formatter = PlainFormatter()

    # Print banner with context window usage (if available)
    banner_suffix_parts = []
    if chat_id:
        banner_suffix_parts.append(f"PM chat: {chat_id}")
    context_label = _format_context_window_label(log_file)
    if context_label:
        banner_suffix_parts.append(f"context: {context_label}")
    banner_suffix = (
        f" ({' | '.join(banner_suffix_parts)})" if banner_suffix_parts else ""
    )
    print(
        f"=== claude-worker REPL: {args.name}{banner_suffix} ===\n"
        f"Type your message at the prompt. /exit or Ctrl-D to quit.\n"
        f"The worker stays running after you exit.\n"
    )

    # Entry context: last turn, if any
    _repl_print_last_turn(args.name)

    consecutive_sigint_count = 0

    while True:
        # --- Phase 1: wait until worker is idle ---
        try:
            status = _wait_for_worker_idle(args.name)
        except KeyboardInterrupt:
            print("\n(interrupted while waiting for worker — exiting REPL)")
            return
        if status == "dead":
            print(f"\nWorker '{args.name}' has died. Exiting REPL.")
            return

        # --- Phase 2: flush stdin and prompt the user ---
        _flush_stdin()
        try:
            user_input = input(REPL_INPUT_PROMPT)
            consecutive_sigint_count = 0
        except KeyboardInterrupt:
            consecutive_sigint_count += 1
            if consecutive_sigint_count >= 2:
                print("\n(two Ctrl-C in a row — exiting REPL)")
                return
            print("\n(Ctrl-C — press again to exit, or type a message)")
            continue
        except EOFError:
            # Ctrl-D on empty prompt
            print()
            return

        stripped_input = user_input.strip()
        if not stripped_input:
            continue
        if stripped_input in REPL_EXIT_COMMANDS:
            return

        # --- Phase 3: send the message ---
        # Prepend chat tag if routing is active (mirrors cmd_send)
        send_content = stripped_input
        if chat_id:
            send_content = f"[{CHAT_TAG_PREFIX}{chat_id}] {send_content}"

        # Capture the log position AND the most recent UUID BEFORE writing.
        # The position lets the stream thread tail only new content; the
        # marker UUID lets _wait_for_turn skip past the prior turn's
        # `result` message and actually wait for the NEW turn's result.
        # Without the marker, the post-send wait would consult
        # get_worker_status which is mtime-based — and right after the
        # FIFO write the log mtime is still from the prior turn (which
        # has aged past STATUS_IDLE_THRESHOLD_SECONDS, so status reports
        # `waiting`), causing the wait to return immediately before
        # claude has even started responding.
        try:
            start_position = log_file.stat().st_size
        except OSError:
            start_position = 0
        marker_uuid = _get_last_uuid(log_file)

        payload = json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": send_content},
            }
        )
        try:
            with open(in_fifo, "w") as f:
                f.write(payload + "\n")
                f.flush()
        except OSError as exc:
            print(f"\nError writing to worker FIFO: {exc}", file=sys.stderr)
            return

        # --- Phase 4: live-stream new log content during the working phase ---
        stop_event = threading.Event()

        def stream_until_idle():
            try:
                _repl_stream_new_messages(
                    log_file, config, formatter, start_position, stop_event
                )
            except Exception:  # pragma: no cover — defensive
                pass

        stream_thread = threading.Thread(target=stream_until_idle, daemon=True)
        stream_thread.start()

        # Wait for the new turn to complete. Uses _wait_for_turn with the
        # pre-send marker — same race protection cmd_send already uses for
        # its own post-write wait. _wait_for_turn returns 0 on success,
        # 1 if the worker died, 2 on timeout (we don't set one).
        try:
            rc = _wait_for_turn(args.name, after_uuid=marker_uuid)
        except KeyboardInterrupt:
            # User hit Ctrl-C during the working phase. Don't interrupt
            # claude — just exit the REPL cleanly. The worker keeps
            # processing in the background.
            stop_event.set()
            stream_thread.join(timeout=1.0)
            print("\n(interrupted — worker is still processing in the background)")
            return

        # Give the stream thread a moment to flush any trailing messages
        # that landed right at the turn boundary, then shut it down.
        time.sleep(POLL_INTERVAL_SECONDS * 2)
        stop_event.set()
        stream_thread.join(timeout=1.0)

        if rc == 1:
            print(f"\nWorker '{args.name}' has died. Exiting REPL.")
            return

        print()  # blank line before next prompt for readability


EXAMPLES = """\
examples:
  # Start a worker — blocks until claude responds, then prints status
  claude-worker start --name researcher --prompt "You are a research assistant"

  # Read the response
  claude-worker read researcher --last-turn

  # Send a message — blocks until claude responds
  claude-worker send researcher "summarize the architecture of this repo"
  claude-worker read researcher --last-turn

  # Follow output in real-time
  claude-worker read researcher --follow

  # List all workers
  claude-worker list

  # Chat with the worker interactively (turn-by-turn human REPL)
  claude-worker repl researcher

  # Check token usage (context window + session totals)
  claude-worker tokens researcher

  # Or just the current context window as a scriptable one-liner
  claude-worker read researcher --context

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
    p_start_identity = p_start.add_mutually_exclusive_group()
    p_start_identity.add_argument(
        "--pm",
        action="store_true",
        help="Launch as a Project Manager worker — loads the PM identity "
        "via --append-system-prompt-file and enables chat-tag routing for "
        "multi-consumer coordination",
    )
    p_start_identity.add_argument(
        "--team-lead",
        action="store_true",
        help="Launch as a Technical Lead worker — loads the TL identity "
        "via --append-system-prompt-file for code review and delegation",
    )
    p_start.add_argument(
        "--no-permission-hook",
        action="store_true",
        help="Disable the PreToolUse permission-grant hook. By default, "
        "claude-worker wires a hook that lets `claude-worker grant` "
        "pre-authorize Edit/Write/MultiEdit calls that would otherwise "
        "hit the sensitive-file denial. Use this flag to opt out (e.g. "
        "for tests or if the hook itself misbehaves).",
    )
    p_start.add_argument(
        "claude_args",
        nargs="*",
        metavar="CLAUDE_ARGS",
        help="Additional args passed to claude (use -- before these)",
    )

    # -- send --
    p_send = sub.add_parser("send", help="Send a message to a worker")
    p_send.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Worker name (omit when using --broadcast)",
    )
    p_send.add_argument(
        "message", nargs="*", help="Message text (reads stdin if omitted)"
    )
    p_send.add_argument(
        "--broadcast",
        action="store_true",
        help="Send to all workers matching filter flags (--role, --status, --alive, --cwd)",
    )
    p_send.add_argument(
        "--role",
        choices=["pm", "tl", "worker"],
        help="Filter targets by identity role (broadcast only)",
    )
    p_send.add_argument(
        "--status",
        choices=["working", "waiting", "dead", "starting"],
        help="Filter targets by status (broadcast only)",
    )
    p_send.add_argument(
        "--alive",
        action="store_true",
        help="Exclude dead workers from broadcast targets",
    )
    p_send.add_argument(
        "--cwd",
        dest="cwd_filter",
        metavar="PATH",
        help="Filter targets by CWD prefix (broadcast only)",
    )
    p_send.add_argument(
        "--queue",
        action="store_true",
        help="Send even if worker is busy; embed a correlation ID and wait "
        "for the specific tagged response",
    )
    p_send.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the JSON envelope that would be sent (with chat/queue "
        "tags applied) without writing to the FIFO. Zero side effects.",
    )
    p_send.add_argument(
        "--verbose",
        action="store_true",
        help="Print the JSON envelope to stderr before sending. "
        "The message is still sent normally.",
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
    p_send_chat = p_send.add_mutually_exclusive_group()
    p_send_chat.add_argument(
        "--chat",
        metavar="ID",
        help="Prepend [chat:<id>] to the message (PM workers only). "
        "Auto-detected from CLAUDE_SESSION_UUID when running under "
        "CLAUDECODE=1 against a PM worker.",
    )
    p_send_chat.add_argument(
        "--all-chats",
        action="store_true",
        help="Bypass any automatic chat tagging (no-op for non-PM workers)",
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
        help="Show the most recent conversational exchange: walks backwards "
        "from the end of the log until at least one user-input AND one "
        "assistant message have been seen, then shows everything from the "
        "earlier of the two to the end",
    )
    p_read.add_argument(
        "--exclude-user",
        action="store_true",
        help="Hide user-input messages from the display (default shows them)",
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
        "--context",
        action="store_true",
        help="Print the current context window usage (e.g. '77%% (776k/1M)') "
        "and exit. Bypasses all other read flags.",
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
    p_read_chat = p_read.add_mutually_exclusive_group()
    p_read_chat.add_argument(
        "--chat",
        metavar="ID",
        help="Filter to messages containing [chat:<id>] (PM workers only). "
        "Auto-detected from CLAUDE_SESSION_UUID when running under "
        "CLAUDECODE=1 against a PM worker.",
    )
    p_read_chat.add_argument(
        "--all-chats",
        action="store_true",
        help="Show all chats — bypass automatic chat filtering",
    )

    # -- wait-for-turn --
    p_wait = sub.add_parser(
        "wait-for-turn", help="Block until claude is ready for input"
    )
    p_wait.add_argument("name", help="Worker name")
    p_wait.add_argument("--timeout", type=float, help="Timeout in seconds")
    p_wait.add_argument(
        "--after-uuid",
        metavar="UUID",
        help=(
            "Only consider log entries appearing AFTER this UUID. Pass the "
            "last log UUID captured before sending, so wait-for-turn "
            "doesn't match the prior turn's `result` message before the "
            "new input reaches claude."
        ),
    )
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
    p_wait.add_argument(
        "--chat",
        metavar="TAG",
        help="Only fire when the turn's assistant content contains [chat:<tag>]. "
        "Skips untagged turns and turns for other consumers.",
    )

    # -- list --
    p_list = sub.add_parser("list", aliases=["ls"], help="List all workers")
    p_list.add_argument(
        "--role",
        choices=["pm", "tl", "worker"],
        help="Filter by identity role",
    )
    p_list.add_argument(
        "--status",
        choices=["working", "waiting", "dead", "starting"],
        help="Filter by current status",
    )
    p_list.add_argument(
        "--alive",
        action="store_true",
        help="Show only non-dead workers (shorthand for excluding dead)",
    )
    p_list.add_argument(
        "--cwd",
        dest="cwd_filter",
        metavar="PATH",
        help="Filter by CWD (prefix match)",
    )
    p_list.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: text (default) or json (one JSON object per line)",
    )

    # -- stop --
    p_stop = sub.add_parser("stop", help="Stop a worker")
    p_stop.add_argument("name", help="Worker name")
    p_stop.add_argument(
        "--force", action="store_true", help="Send SIGKILL instead of SIGTERM"
    )
    p_stop.add_argument(
        "--no-wrap-up",
        action="store_true",
        help="Skip the wrap-up message and go straight to SIGTERM",
    )
    p_stop.add_argument(
        "--wrap-up-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Maximum time to wait for wrap-up before sending SIGTERM. "
        "Must be >= the configured minimum (default: 900s). "
        "Values below the minimum are clamped with a warning.",
    )

    # -- replaceme --
    p_replace = sub.add_parser(
        "replaceme",
        help="Replace the current worker with a fresh instance. "
        "Auto-detects which worker is calling via PID ancestry.",
    )
    p_replace.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip wrap-up validation checks (handoff file, turn state). "
        "Use when the worker is stuck or the human is supervising.",
    )

    # -- reply --
    p_reply = sub.add_parser(
        "reply",
        help="Send a reply to a worker's message queue (persistent, no FIFO needed)",
    )
    p_reply.add_argument("name", help="Recipient worker name")
    p_reply.add_argument(
        "message", nargs="*", help="Reply text (reads stdin if omitted)"
    )
    p_reply.add_argument(
        "--sender",
        help="Sender identity (auto-detected from PID ancestry if omitted)",
    )

    # -- notify --
    p_notify = sub.add_parser(
        "notify",
        help="Send a notification to the human via configured channel",
    )
    p_notify.add_argument(
        "message",
        nargs="*",
        help="Notification text (reads stdin if omitted)",
    )
    p_notify.add_argument(
        "--worker",
        help="Worker name for rate-limiting context (auto-detected if inside a worker)",
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

    # -- repl --
    p_repl = sub.add_parser(
        "repl",
        help="Interactive turn-by-turn chat with a running worker",
    )
    p_repl.add_argument("name", help="Worker name")
    p_repl.add_argument(
        "--chat",
        metavar="ID",
        help="Override the auto-generated chat ID for PM workers. By "
        "default the REPL uses 'repl-<pid>-<tty>' as the chat tag on "
        "PM workers; pass --chat to use a specific identity instead.",
    )
    p_repl.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show tool calls, thinking, and metadata (matches read --verbose output)",
    )

    # -- tokens --
    p_tokens = sub.add_parser(
        "tokens",
        help="Print token stats for a worker (context window + session totals)",
    )
    p_tokens.add_argument("name", help="Worker name")

    # -- grant --
    p_grant = sub.add_parser(
        "grant",
        help="Pre-authorize a sensitive-file Edit/Write/MultiEdit call for "
        "a worker (bypasses Claude Code's sensitive-file denial)",
    )
    p_grant.add_argument("name", help="Worker name")
    p_grant_match = p_grant.add_mutually_exclusive_group()
    p_grant_match.add_argument(
        "--path",
        metavar="PATH",
        help="Grant for this exact file path (matched after path resolution)",
    )
    p_grant_match.add_argument(
        "--glob",
        metavar="PATTERN",
        help="Grant for any file_path matching this fnmatch pattern "
        "(e.g. '/home/foo/.claude/skills/**/*.md')",
    )
    p_grant_match.add_argument(
        "--tool-use-id",
        metavar="ID",
        help="Grant for the exact tool_use_id (as emitted in the log)",
    )
    p_grant_match.add_argument(
        "--last",
        action="store_true",
        help="Grant the most recent sensitive-file denial in the worker's "
        "log — the ergonomic default for 'I saw the worker hit a denial, "
        "authorize exactly that edit'. Auto-scopes --tool to the denied "
        "tool so a granted Edit doesn't silently authorize a Write.",
    )
    p_grant.add_argument(
        "--tool",
        action="append",
        metavar="TOOL",
        help="Restrict the grant to specific tools (Edit|Write|MultiEdit). "
        "Repeatable. Default: all three.",
    )
    p_grant.add_argument(
        "--persistent",
        action="store_true",
        help="Keep the grant active after its first use. Default is "
        "one-shot (consumed on first match).",
    )
    p_grant.add_argument(
        "--reason",
        metavar="TEXT",
        help="Optional audit note stored in the grant record",
    )

    # -- grants --
    p_grants = sub.add_parser(
        "grants",
        help="List active permission grants for a worker",
    )
    p_grants.add_argument("name", help="Worker name")

    # -- revoke --
    p_revoke = sub.add_parser(
        "revoke",
        help="Revoke a permission grant (by id, or --all)",
    )
    p_revoke.add_argument("name", help="Worker name")
    p_revoke.add_argument(
        "grant_id",
        nargs="?",
        metavar="GRANT_ID",
        help="Grant id to remove (omit when using --all)",
    )
    p_revoke.add_argument(
        "--all",
        dest="all",
        action="store_true",
        help="Remove every grant for this worker (active and consumed)",
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
        "replaceme": cmd_replaceme,
        "notify": cmd_notify,
        "reply": cmd_reply,
        "install-hook": cmd_install_hook,
        "repl": cmd_repl,
        "tokens": cmd_tokens,
        "grant": cmd_grant,
        "grants": cmd_grants,
        "revoke": cmd_revoke,
    }
    handlers[args.command](args)
