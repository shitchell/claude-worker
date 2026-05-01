"""CLI entry point for claude-worker."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from claude_worker import __version__

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

# Grace window after writing to the FIFO before waiting on the log —
# closes the #082 race (D101) where the reverse walk + forward scan
# could both run before the stub/claude response lands in the log.
FIFO_HANDOFF_GRACE_SECONDS: float = 0.2
ANALYZE_SESSION_SKILL_RESOURCE: str = "analyze-session.md"


ANALYSES_DIR: Path = Path.home() / ".cwork" / "analyses"


def _build_stop_wrapup_message() -> str:
    """Build the stop wrap-up message with analyze-session instruction."""
    # Ensure analyses directory exists
    ANALYSES_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from importlib.resources import files

        skill_path = files("claude_worker") / "skills" / ANALYZE_SESSION_SKILL_RESOURCE
        analyze_instruction = (
            f" Before wrapping up, run the analyze-session skill on your own "
            f"session log and save the analysis to {ANALYSES_DIR}/. If the "
            f"analyze-session skill is available, invoke it. Otherwise, read "
            f"the instructions at {skill_path} and follow them."
        )
    except Exception:
        analyze_instruction = (
            f" Before wrapping up, run the analyze-session skill on your own "
            f"session log and save the analysis to {ANALYSES_DIR}/."
        )
    return (
        "[system:stop-requested] Stop has been requested. Read your wrap-up "
        "file (~/.cwork/identities/<your-identity>/wrap-up.md) for the full "
        "procedure, then complete it and respond with 'wrap-up complete' when done."
        f"{analyze_instruction} You have up to 15 minutes."
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
    "Enter Mode 1 (continuous work): read INDEX.md, process the backlog. "
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

# Role directory names — maps identity names to their .cwork/roles/<dir> name.
# "technical-lead" → "tl" (shorter), all others keep their identity name.
IDENTITY_ROLE_DIRS: dict[str, str] = {
    "technical-lead": "tl",
}


def _identity_role_dir(identity: str) -> str:
    """Map an identity name to its role directory name."""
    return IDENTITY_ROLE_DIRS.get(identity, identity)


# REPL
REPL_IDLE_POLL_INTERVAL_SECONDS: float = 0.25
REPL_INPUT_PROMPT: str = "you> "
REPL_EXIT_COMMANDS: frozenset[str] = frozenset({"/exit", "/quit"})

# Thread-watch — blocking tail for interactive thread observers (#076)
THREAD_WATCH_POLL_INTERVAL_SECONDS: float = 0.5

# TUI REPL — async log-tailer poll interval (#077, D96)
REPL_TUI_POLL_INTERVAL_SECONDS: float = 0.1
REPL_TUI_MAX_OUTPUT_LINES: int = 5000

# Ephemeral workers — auto-reaped after idle (#080, D97)
EPHEMERAL_IDLE_TIMEOUT_SECONDS: int = 300
EPHEMERAL_WRAPUP_TIMEOUT_SECONDS: int = 30
EPHEMERAL_SENTINEL_FILENAME: str = "ephemeral"

# Tool-call visibility — ls shows the currently-open tool_use (#081, D98)
TOOL_CALL_PREVIEW_LENGTH: int = 60
TOOL_CALL_SCAN_LINE_LIMIT: int = 200

# Notifications — human escalation channel
NOTIFY_COOLDOWN_SECONDS: float = 60.0
NOTIFY_SUBPROCESS_TIMEOUT_SECONDS: float = 10.0

# Replaceme — auto-restart mechanism
REPLACEME_ANCESTOR_WALK_MAX: int = 5
REPLACEME_OLD_MANAGER_WAIT_TIMEOUT: float = 30.0
REPLACEME_OLD_MANAGER_POLL_INTERVAL: float = 0.5
REPLACEME_HANDOFF_MAX_AGE_MINUTES: int = 30
REPLACEME_ERROR_LOG_SUFFIX: str = ".replaceme.log"

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

# Migration system
MIGRATIONS_DIR: Path = Path.home() / ".cwork" / "migrations"
MIGRATION_VERSION_FILE: str = ".migration-version"
CWORK_VERSION_FILE: str = "version"

# Positional-message shell-hazard detection (#092, D110). The em-/en-dash
# and double-asterisk triggers only fire on bodies of this many tokens or
# more — single-line prose like ``Run the test — verify`` is rare and
# recovers via stdin if false-positive; ≥3 is the markdown-paste signal.
MIN_TOKENS_FOR_MARKDOWN_HEURISTIC: int = 3

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
    write_identity_hash,
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
            # Check FIFO for unread data — if bytes are pending, the
            # manager hasn't drained them yet and the worker is about
            # to start working. Report "working" to prevent false-idle.
            in_fifo = runtime / "in"
            if in_fifo.exists():
                try:
                    fd = os.open(str(in_fifo), os.O_RDONLY | os.O_NONBLOCK)
                    try:
                        import select as _sel

                        ready, _, _ = _sel.select([fd], [], [], 0)
                        if ready:
                            return "working", log_mtime
                    finally:
                        os.close(fd)
                except OSError:
                    pass  # FIFO gone or unreadable — proceed with "waiting"
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


def _get_read_marker_consumer_id(args: argparse.Namespace) -> str:
    """Determine the consumer ID for read markers.

    Uses --chat value, CLAUDE_SESSION_UUID, or falls back to 'cli'.
    """
    chat = getattr(args, "chat", None)
    if chat:
        return chat
    uuid = os.environ.get("CLAUDE_SESSION_UUID", "")
    if uuid:
        return uuid
    return "cli"


def _read_marker_path(runtime: Path, consumer_id: str) -> Path:
    """Return the path for a consumer's read marker file."""
    import hashlib

    marker_dir = runtime / "read-markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(consumer_id.encode()).hexdigest()
    return marker_dir / f"{key}.txt"


def _load_read_marker(runtime: Path, args: argparse.Namespace) -> str | None:
    """Load the last-seen UUID for this consumer, or None."""
    consumer_id = _get_read_marker_consumer_id(args)
    path = _read_marker_path(runtime, consumer_id)
    if path.exists():
        try:
            return path.read_text().strip() or None
        except OSError:
            return None
    return None


def _save_read_marker(runtime: Path, args: argparse.Namespace, uuid: str) -> None:
    """Save the last-seen UUID for this consumer."""
    consumer_id = _get_read_marker_consumer_id(args)
    path = _read_marker_path(runtime, consumer_id)
    try:
        path.write_text(uuid)
    except OSError:
        pass


def _get_internalize_message(identity: str) -> str | None:
    """Load the internalization message for an identity.

    Checks ~/.cwork/identities/<identity>/internalize.md first.
    Falls back to hardcoded constants for pm and technical-lead.
    Returns None for unknown identities without an internalize file.
    """
    user_file = Path.home() / ".cwork" / "identities" / identity / "internalize.md"
    if user_file.exists():
        try:
            return user_file.read_text().strip()
        except OSError:
            pass
    # Fall back to built-in constants
    if identity == "pm":
        return PM_INTERNALIZE_MESSAGE
    if identity == "technical-lead":
        return TL_INTERNALIZE_MESSAGE
    return None


def _build_replaceme_initial_message(identity: str, cwd: str) -> str | None:
    """Build the initial prompt for a fresh replacement worker.

    Extends the internalize message with a pointer to the most recent
    handoff file, which is the continuity mechanism (replaceme gives
    clean context; handoff files carry work state forward).
    """
    internalize = _get_internalize_message(identity)

    # Find the latest handoff file for this identity
    role_dir = _identity_role_dir(identity)
    handoff_dir = Path(cwd) / ".cwork" / "roles" / role_dir / "handoffs"
    handoff_hint = ""
    if handoff_dir.exists():
        try:
            handoffs = sorted(
                [f for f in handoff_dir.iterdir() if f.is_file()],
                key=lambda f: f.name,
                reverse=True,
            )
            if handoffs:
                handoff_hint = (
                    f"\n\nIMPORTANT: You are a fresh replacement. Read the most "
                    f"recent handoff file for your work context: {handoffs[0]}. "
                    f"The prior session's conversation is NOT available — the "
                    f"handoff file is the continuity mechanism."
                )
        except OSError:
            pass

    if internalize:
        return internalize + handoff_hint
    return handoff_hint or None


def _load_identity_config(identity: str) -> dict:
    """Load per-identity config from ~/.cwork/identities/<name>/config.yaml.

    Returns {} if the file doesn't exist or can't be parsed.
    Supported keys:
      claude_args: list[str] — extra args passed to claude
      env: dict[str, str] — extra env vars for the subprocess
      auto_skeleton: bool — whether to scaffold .cwork/<name>/ on start
    """
    config_path = Path.home() / ".cwork" / "identities" / identity / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _get_worker_identity(name: str) -> str:
    """Return the identity name for a worker ('pm', 'technical-lead', 'worker').

    Reads the ``identity`` field from saved metadata. Falls back to
    legacy ``pm``/``team_lead`` booleans for sessions saved before the
    identity field was added.
    """
    saved = get_saved_worker(name)
    if not saved:
        return "worker"
    identity = saved.get("identity")
    if identity:
        return identity
    if saved.get("pm"):
        return "pm"
    if saved.get("team_lead"):
        return "technical-lead"
    return "worker"


def _worker_is_pm(name: str) -> bool:
    """Return True if the named worker is a PM (by identity or legacy flag)."""
    return _get_worker_identity(name) == "pm"


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


def _resolve_sender() -> str:
    """Determine the sender name for thread messaging (D75 participants).

    Priority:
      1. ``CW_WORKER_NAME`` (worker -> worker send, set by the manager)
      2. ``CLAUDE_SESSION_UUID`` (human interactive claude shell)
      3. ``"human"`` (plain terminal)
    """
    return (
        os.environ.get("CW_WORKER_NAME")
        or os.environ.get("CLAUDE_SESSION_UUID")
        or "human"
    )


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
) -> tuple[int, str]:
    """Tail the log waiting for an assistant message containing [queue:{id}].

    Returns a 2-tuple ``(rc, reason)``:

    - ``(0, "echo")`` — the recipient echoed the literal correlation tag.
    - ``(0, "turn-end-fallback")`` — tag never appeared, but the recipient
      produced an assistant turn-end strictly after ``after_uuid``. The
      message landed and the recipient finished its turn; treat as success.
    - ``(1, "stuck")`` — tail loop timed out and no post-marker turn-end
      was found. Recipient is stuck or hung.
    - ``(1, "died")`` — recipient process died before the tag arrived.
    - ``(2, "transport")`` — log file never appeared within ``timeout`` (a
      transport-class failure: the FIFO never produced output).

    If ``after_uuid`` is provided, only log entries appearing *after* that
    UUID are considered. This avoids matching a stale [queue:<id>] string
    from a previous cycle (or a sub-millisecond collision between two
    recent queue IDs) — mirrors the race protection already in
    ``_wait_for_turn``. The same marker also bounds the fallback scan so
    that an assistant turn-end from a prior cycle does not falsely satisfy
    the "delivered" claim (D2).
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
                return (2, "transport")
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
                return (0, "echo")
        # Tail from current position (end of existing content). Everything
        # we tail now is by definition past the marker.
        while True:
            if time.monotonic() > deadline:
                # Tag never echoed. Fall back to: did the recipient
                # produce an assistant turn-end after the marker? If yes,
                # the message was delivered and the turn finished — that
                # is the honest "delivered" signal. (D109)
                turn = _forward_scan_for_turn_end(log_file, after_uuid=after_uuid)
                if turn is not None:
                    return (0, "turn-end-fallback")
                return (1, "stuck")
            line = f.readline()
            if not line:
                if not _manager_alive():
                    return (1, "died")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            if tag in line:
                return (0, "echo")


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


def _forward_scan_for_turn_end(
    log_file: Path,
    after_uuid: str | None,
    chat_tag: str | None = None,
) -> dict | None:
    """Forward-scan the log once, starting after ``after_uuid``, for a turn-end.

    Closes the race in ``_wait_for_turn`` between the reverse walk and
    the tail-poll loop (#082, D101). The reverse walk captures file
    size at open time; if the log writer appends between the reverse
    walk closing and the tail loop opening, the tail's seek-to-end
    misses those appends and polls forever.

    This forward scan runs once, in O(log-size-after-marker), which
    is bounded to a single turn of output for realistic callers
    (marker captured just before the triggering write).

    Returns the turn-end entry (`result` or `assistant` with
    `stop_reason == "end_turn"`) that appears strictly after the
    marker, or None. When ``chat_tag`` is set, turns whose assistant
    content doesn't include the tag are skipped.
    """
    if not log_file.exists():
        return None

    past_marker = after_uuid is None
    last_assistant: dict | None = None
    try:
        with open(log_file) as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not past_marker:
                    if _uuid_matches(data.get("uuid", ""), after_uuid):
                        past_marker = True
                    continue

                msg_type = data.get("type")
                if msg_type == "assistant":
                    last_assistant = data
                    sr = data.get("message", {}).get("stop_reason")
                    if sr == "end_turn":
                        if chat_tag and not _message_has_chat_tag(data, chat_tag):
                            continue
                        return data
                elif msg_type == "result":
                    if chat_tag:
                        check = last_assistant or data
                        if not _message_has_chat_tag(check, chat_tag):
                            continue
                    return data
    except OSError:
        return None

    return None


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

    # Race mitigation (#082, D101): the reverse walk may have run BEFORE
    # the log writer appended the turn-end, while the tail loop below
    # would then seek to end and miss it. Forward-scan the log once from
    # the marker to catch any writes that landed in that window.
    if turn_end_after_last_user is None:
        turn_end_after_last_user = _forward_scan_for_turn_end(
            log_file, after_uuid, chat_tag
        )
        if turn_end_after_last_user is not None:
            if _settle_is_stable(log_file, settle, deadline=deadline):
                return 0

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


def _scaffold_from_skeleton(skeleton_dir: Path, target_dir: Path) -> None:
    """Copy a skeleton directory's structure into the target.

    Only creates directories that don't exist yet — never overwrites
    existing files or directories. Copies the directory tree structure
    but not file contents (skeleton dirs contain empty subdirectories
    as scaffolding).
    """
    if not skeleton_dir.exists():
        return
    for item in skeleton_dir.rglob("*"):
        rel = item.relative_to(skeleton_dir)
        target = target_dir / rel
        if item.is_dir() and not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.copy2(item, target)


def _ensure_cwork_dirs(cwd: str, pm: bool, tl: bool) -> None:
    """Auto-create .cwork/ skeleton directories on first identity-mode start.

    Creates global ~/.cwork/ skeleton, then scaffolds the project-level
    <cwd>/.cwork/<identity>/ from the identity's skeleton directory at
    ~/.cwork/identities/<identity>/skeleton/. Falls back to hardcoded
    directories for pm/tl if no skeleton dir exists.
    """
    if not pm and not tl:
        return

    identity = "pm" if pm else "technical-lead"

    # Global skeleton: ~/.cwork/
    home_cwork = Path.home() / ".cwork"
    for d in (
        home_cwork / "gvp" / "library",
        home_cwork / "identities" / "pm" / "gvp",
        home_cwork / "identities" / "technical-lead",
        home_cwork / "workers",
        home_cwork / "analyses",
        home_cwork / "projects",
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

    # Project skeleton: scaffold from ~/.cwork/identities/<identity>/skeleton/
    project_cwork = Path(cwd) / ".cwork"
    skeleton_dir = home_cwork / "identities" / identity / "skeleton"
    target_dir = project_cwork / "roles" / _identity_role_dir(identity)

    if skeleton_dir.exists():
        _scaffold_from_skeleton(skeleton_dir, target_dir)
    else:
        # Hardcoded fallback for pm/tl (backwards compat)
        if pm:
            for d in (
                target_dir / "chats",
                target_dir / "handoffs",
                target_dir / "gvp",
            ):
                d.mkdir(parents=True, exist_ok=True)
        if tl:
            for d in (
                target_dir / "handoffs",
                target_dir / "notes",
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


def _fix_legacy_paths_in_args(args_list: list[str], worker_name: str) -> list[str]:
    """Replace legacy /tmp/claude-workers/ paths with current runtime dir.

    After migration from /tmp/ to ~/.cwork/workers/, saved claude_args
    may still reference the old path. This fixes them so --resume works.
    """
    import re

    old_pattern = re.compile(
        r"/tmp/claude-workers/\d+/" + re.escape(worker_name) + r"/"
    )
    new_base = str(get_runtime_dir(worker_name)) + "/"
    return [old_pattern.sub(new_base, arg) for arg in args_list]


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
    # Resolve identity: --identity <name>, --pm (→ "pm"), --team-lead (→ "technical-lead")
    # These are mutually exclusive (enforced by argparse).
    identity = getattr(args, "identity", None) or ""
    if args.pm:
        identity = "pm"
    elif args.team_lead:
        identity = "technical-lead"
    if args.resume:
        saved = get_saved_worker(name)
        if not saved or not saved.get("session_id"):
            # Try to recover session_id from the latest archive (#070)
            archive = _find_latest_archive(name)
            if archive:
                session_file = archive / "session"
                if session_file.exists():
                    session_id = session_file.read_text().strip()
                    if session_id:
                        print(
                            f"Recovered session from archive: {archive.name}",
                            file=sys.stderr,
                        )
                        saved = {
                            "session_id": session_id,
                            "claude_args": [],
                            "cwd": "",
                        }
            if not saved or not saved.get("session_id"):
                print(
                    f"Error: no saved session for worker '{name}'",
                    file=sys.stderr,
                )
                sys.exit(1)
        # Restore saved cwd unless explicitly overridden
        if not args.cwd and saved.get("cwd"):
            args.cwd = saved["cwd"]
        # Restore saved claude_args (which already includes --agent, etc.)
        # and append any new args the user provided on this invocation.
        # Fix legacy /tmp/ paths in saved claude_args (#069)
        extra = claude_args
        saved_claude_args = saved.get("claude_args") or []
        saved_claude_args = _fix_legacy_paths_in_args(saved_claude_args, name)
        claude_args = ["--resume", saved["session_id"]] + saved_claude_args + extra
        # Resumed workers inherit identity from saved metadata
        if not identity:
            identity = _get_worker_identity(name)
    else:
        # Build claude_args with --agent etc. (order matters: agent first)
        if args.agent:
            claude_args = ["--agent", args.agent] + claude_args

    # Backwards compat aliases
    pm_mode = identity == "pm"
    tl_mode = identity == "technical-lead"
    identity_mode = identity and identity != "worker"

    # Load per-identity config (claude_args, env vars, etc.)
    identity_config = _load_identity_config(identity) if identity_mode else {}
    # Merge identity claude_args (CLI args take precedence — appended after)
    if identity_config.get("claude_args") and not args.resume:
        claude_args = identity_config["claude_args"] + claude_args

    # Identity mode: inject --append-system-prompt-file pointing at the
    # runtime dir's identity.md.
    identity_path: Path | None = None
    if identity_mode and not args.resume:
        identity_path = get_runtime_dir(name) / "identity.md"
        claude_args = ["--append-system-prompt-file", str(identity_path)] + claude_args
    elif identity_mode and args.resume:
        identity_path = get_runtime_dir(name) / "identity.md"

    # Save startup vars for future --resume (claude_args without --resume prefix)
    saved_args = (
        claude_args if not args.resume else claude_args[2:]
    )  # strip --resume <sid>
    ephemeral = bool(getattr(args, "ephemeral", False))
    ephemeral_idle_timeout = int(
        getattr(args, "ephemeral_idle_timeout", EPHEMERAL_IDLE_TIMEOUT_SECONDS)
    )
    save_worker(
        name,
        cwd=args.cwd or os.getcwd(),
        claude_args=saved_args,
        identity=identity or "worker",
        pm=pm_mode,
        team_lead=tl_mode,
        ephemeral=ephemeral,
        ephemeral_idle_timeout=ephemeral_idle_timeout,
    )

    # Build initial message from prompt-file and/or prompt.
    # For identity workers with no user-provided prompt, inject a canonical
    # internalization message so the worker runs its startup logic.
    parts = []
    if args.prompt_file:
        parts.append(Path(args.prompt_file).read_text())
    if args.prompt:
        parts.append(args.prompt)
    if not parts and identity_mode:
        internalize = _get_internalize_message(identity)
        if internalize:
            parts.append(internalize)
    initial_message = "\n\n".join(parts) if parts else None

    # Auto-create .cwork/ skeleton for identity-mode workers
    resolved_cwd = args.cwd or os.getcwd()
    _ensure_cwork_dirs(resolved_cwd, pm_mode, tl_mode)

    # Auto-register project in the registry for identity-mode workers
    if identity_mode:
        from claude_worker.project_registry import register_project

        register_project(resolved_cwd)

    # Create runtime directory.
    # For --resume: if a stale runtime dir exists (dead worker), archive it
    # first so create_runtime_dir succeeds. Alive workers still error (#068).
    runtime_check = get_runtime_dir(name)
    if args.resume and runtime_check.exists():
        pid_file = runtime_check / "pid"
        alive = False
        if pid_file.exists():
            try:
                alive = pid_alive(int(pid_file.read_text().strip()))
            except (ValueError, OSError):
                pass
        if alive:
            print(
                f"Error: worker '{name}' is still alive. Stop it first.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Dead worker with stale dir — archive it
        archive_runtime_dir(name, reason="stale-resume")
        print(f"Archived stale runtime dir for '{name}'", file=sys.stderr)

    try:
        runtime = create_runtime_dir(name)
    except FileExistsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Ephemeral sentinel — written before the manager fork so the
    # manager's poll loop sees it immediately. Contains the idle
    # timeout in seconds (D97, #080).
    if ephemeral:
        (runtime / EPHEMERAL_SENTINEL_FILENAME).write_text(
            f"{ephemeral_idle_timeout}\n"
        )

    # Write the identity file into the runtime dir. Must happen BEFORE
    # the fork so the manager subprocess sees it when spawning claude.
    # Check user-installed identity first (~/.cwork/identities/<name>/identity.md),
    # fall back to bundled identities for pm and technical-lead.
    if identity_path is not None:
        user_identity = Path.home() / ".cwork" / "identities" / identity / "identity.md"
        if user_identity.exists():
            identity_content = user_identity.read_text()
        elif pm_mode:
            identity_content = _load_bundled_resource(
                "identities", PM_IDENTITY_RESOURCE
            )
        elif tl_mode:
            identity_content = _load_bundled_resource(
                "identities", TL_IDENTITY_RESOURCE
            )
        else:
            print(
                f"Error: identity '{identity}' not found at {user_identity}",
                file=sys.stderr,
            )
            sys.exit(1)
        identity_path.write_text(identity_content)
        # Record the source hash so the manager's drift check has a
        # baseline to compare against (#066).
        write_identity_hash(get_runtime_dir(name), identity_content)

    # Write the per-worker settings.json that wires the PreToolUse
    # permission-grant hook. Must happen BEFORE the fork so the manager
    # subprocess finds the file when it builds the claude command.
    # Gated by --no-permission-hook for tests and for users opting out.
    permission_settings = _maybe_write_permission_settings(
        name=name,
        enabled=not getattr(args, "no_permission_hook", False),
        cwd=args.cwd or os.getcwd(),
        identity=identity if identity_mode else None,
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

    # --foreground and --background are mutually exclusive
    if getattr(args, "foreground", False) and args.background:
        print(
            "Error: --foreground and --background are mutually exclusive",
            file=sys.stderr,
        )
        sys.exit(1)

    # Foreground mode: run the manager directly, no fork.
    # Used by systemd Type=simple and similar process supervisors.
    if getattr(args, "foreground", False):
        print(f"claude-worker: {name} (foreground)", file=sys.stderr)
        if identity_mode:
            print(f"  identity: {identity}", file=sys.stderr)
        print(f"  cwd: {args.cwd or os.getcwd()}", file=sys.stderr)
        print(f"  pid: {os.getpid()}", file=sys.stderr)
        run_manager(
            name=name,
            cwd=args.cwd,
            claude_args=claude_args,
            initial_message=initial_message,
            identity=identity or "worker",
            extra_env=identity_config.get("env"),
            remote=getattr(args, "remote", False),
        )
        sys.exit(0)

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
        identity=identity or "worker",
        extra_env=identity_config.get("env"),
        remote=getattr(args, "remote", False),
    )
    os._exit(0)


def _is_known_thread_participant(name: str) -> bool:
    """Return True if ``name`` appears as a participant in any existing thread.

    Used to allow sends to non-worker targets (e.g., interactive Claude
    Code sessions identified as ``human`` or ``rhc``) when the target
    is a legitimate thread peer. Prevents typos from silently creating
    dead threads while enabling worker → interactive replies (D94, #076).
    """
    from claude_worker.thread_store import load_index

    index = load_index()
    for meta in index.values():
        if name in (meta.get("participants") or []):
            return True
    return False


def _send_to_single_worker(
    name: str,
    content: str,
    args: argparse.Namespace,
) -> int:
    """Send a message to a single worker. Returns exit code.

    Post-Phase-3 (D88): writes the message to a thread (via thread_store)
    instead of the FIFO. The manager's thread monitor notices the append
    within THREAD_MONITOR_INTERVAL_SECONDS and delivers a lightweight
    ``[system:new-message]`` notification to claude via the FIFO. The
    worker reads the full thread on demand.

    Thread selection:
      - ``--chat <id>`` (PM workers) → ``chat-<id>`` (multi-consumer)
      - Else → ``pair-<sender>-<recipient>`` (symmetric, deterministic)

    Non-worker targets (D94): if ``name`` has no runtime dir but is a
    known thread participant (e.g., an interactive Claude Code session),
    the send is thread-only — no status gate, no wait-for-turn.

    Extracted from cmd_send so broadcast can reuse the core send logic
    per target without duplicating the thread write + wait sequence.
    """
    from claude_worker.thread_store import (
        append_message,
        chat_thread_id,
        ensure_thread,
        pair_thread_id,
    )

    runtime = get_runtime_dir(name)
    log_file = runtime / "log"
    runtime_exists = runtime.exists()

    if not runtime_exists:
        if not _is_known_thread_participant(name):
            print(
                f"Error: '{name}' is not a worker and not a known thread "
                f"participant. Check the name or use `claude-worker thread "
                f"list` to see known participants.",
                file=sys.stderr,
            )
            return 1
        # Thread-only target (interactive session, etc.). Fall through;
        # status gate + wait-for-turn are skipped below.

    # Status gate: skip for broadcast (fire-and-forget to all),
    # thread-only targets (no FIFO to gate on), and dry-run/queue.
    if (
        runtime_exists
        and not args.queue
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

    # marker_uuid is only used for _wait_for_turn / _wait_for_queue_response,
    # both of which are skipped for thread-only (non-worker) targets.
    marker_uuid = _get_last_uuid(log_file) if runtime_exists else None

    # Chat routing (PM workers) / sender resolution
    chat_id = _resolve_chat_id(name, args.chat, args.all_chats)
    sender = _resolve_sender()
    tagged_content = content
    if chat_id is not None:
        # Keep the [chat:<id>] prefix in the content for PM parsing /
        # downstream filters that look for the literal tag.
        tagged_content = f"[{CHAT_TAG_PREFIX}{chat_id}] {tagged_content}"

    # Queue correlation
    queue_id: str | None = None
    if args.queue:
        queue_id = _generate_queue_id()
        tagged_content = (
            tagged_content
            + f"\n\n[Please include [{QUEUE_TAG_PREFIX}{queue_id}] literally in your response so the sender can identify it.]"
        )

    # Resolve thread ID + participants
    if chat_id is not None:
        thread_id = chat_thread_id(chat_id)
        participants = [name, chat_id]
    else:
        thread_id = pair_thread_id(sender, name)
        participants = sorted([sender, name])

    if getattr(args, "dry_run", False):
        print(
            json.dumps(
                {
                    "thread_id": thread_id,
                    "sender": sender,
                    "recipient": name,
                    "participants": participants,
                    "content": tagged_content,
                },
                indent=2,
            )
        )
        return 0

    if getattr(args, "verbose", False):
        print(
            f"[verbose] thread={thread_id} sender={sender} recipient={name}",
            file=sys.stderr,
        )
        print(tagged_content, file=sys.stderr)

    try:
        ensure_thread(thread_id, participants)
        append_message(
            thread_id,
            sender=sender,
            content=tagged_content,
        )
    except Exception as exc:
        print(
            f"Error: transport failure — could not write to thread '{thread_id}': {exc}",
            file=sys.stderr,
        )
        return 2

    # For broadcast fire-and-forget, don't wait
    if (
        getattr(args, "broadcast", False)
        and not args.show_response
        and not args.show_full_response
    ):
        return 0

    # Thread-only (non-worker) target: no log to wait on, no turn semantics.
    # The reply, if any, will be appended to the same thread — the caller
    # can observe it via `claude-worker thread watch <thread_id>`.
    if not runtime_exists:
        return 0

    if queue_id is not None:
        rc, reason = _wait_for_queue_response(name, queue_id, after_uuid=marker_uuid)
        # Map the reason to a stderr message. "echo" is the silent happy
        # path; every other reason gets a one-line note so operators can
        # tell which branch fired without reading source (V2). (D109)
        if reason == "turn-end-fallback":
            print(
                f"Note: recipient produced an assistant turn-end after the "
                f"send marker but did not echo [{QUEUE_TAG_PREFIX}{queue_id}]. "
                f"Treating as success.",
                file=sys.stderr,
            )
        elif reason == "stuck":
            print(
                f"Error: delivered, but recipient produced no turn-end "
                f"within {QUEUE_WAIT_TIMEOUT_SECONDS}s for "
                f"[{QUEUE_TAG_PREFIX}{queue_id}].",
                file=sys.stderr,
            )
        elif reason == "died":
            print(
                "Error: delivered, but recipient process died before "
                "producing a response.",
                file=sys.stderr,
            )
        elif reason == "transport":
            print(
                "Error: timeout waiting for recipient log to appear.",
                file=sys.stderr,
            )
    else:
        rc = _wait_for_turn(name, after_uuid=marker_uuid)

    if rc == 0 and args.show_response:
        _show_worker_response(name, last_turn=True)
    elif rc == 0 and args.show_full_response:
        _show_worker_response(name, since_uuid=marker_uuid)

    return rc


# -- send flag reparse --
# Maps trailing flags that argparse may absorb into the message positional
# back to their args namespace attribute. Bool flags set True; value flags
# consume the next word as the value.

_SEND_BOOL_FLAGS: dict[str, str] = {
    "--queue": "queue",
    "--dry-run": "dry_run",
    "--verbose": "verbose",
    "--show-response": "show_response",
    "--show-full-response": "show_full_response",
    "--broadcast": "broadcast",
    "--alive": "alive",
    "--all-chats": "all_chats",
}

_SEND_VALUE_FLAGS: dict[str, str] = {
    "--chat": "chat",
    "--role": "role",
    "--status": "status",
    "--cwd": "cwd_filter",
}


def _reparse_send_flags(args: argparse.Namespace) -> argparse.Namespace:
    """Extract trailing flags that argparse absorbed into the message positional.

    When flags appear after the message body (``send NAME "msg" --queue``),
    argparse's ``nargs="*"`` absorbs them into the message list.  This
    function scans ``args.message`` from the end backwards: any trailing
    sequence of recognized send flags is extracted and applied to the
    *args* namespace.  Everything before that trailing sequence is kept as
    the message, so flag-like words inside the message text are preserved.
    """
    if not args.message:
        return args

    words = args.message[:]

    # Scan backwards: peel off trailing flags until we hit a non-flag word.
    extracted: list[tuple[str, str | None]] = []  # (flag, value_or_None)
    while words:
        last = words[-1]
        if last in _SEND_BOOL_FLAGS:
            words.pop()
            extracted.append((last, None))
        elif len(words) >= 2 and words[-2] in _SEND_VALUE_FLAGS:
            value = words.pop()
            flag = words.pop()
            extracted.append((flag, value))
        else:
            break

    # Apply extracted flags to the namespace
    for flag, value in extracted:
        if flag in _SEND_BOOL_FLAGS:
            setattr(args, _SEND_BOOL_FLAGS[flag], True)
        elif flag in _SEND_VALUE_FLAGS:
            setattr(args, _SEND_VALUE_FLAGS[flag], value)

    args.message = words
    return args


_OPTION_LIKE_TOKEN_RE = re.compile(r"^--[a-zA-Z]")


def _validate_positional_message(message_tokens: list[str]) -> str | None:
    """Detect shell-mangled or risky positional bodies (#092, D110).

    Returns the matched trigger name (e.g. ``"backtick"``, ``"em-dash"``)
    if the message body contains a known-risky pattern, or ``None`` if
    safe. The caller emits a stderr error and exits 1 — the canonical
    fix is the stdin/heredoc form, which bypasses both shell quoting
    and argparse.

    The em-/en-dash and double-asterisk triggers gate on
    ``MIN_TOKENS_FOR_MARKDOWN_HEURISTIC`` to avoid flagging single-line
    prose like ``Run the test — verify``; ≥3 tokens is the
    markdown-paste signal.
    """
    for tok in message_tokens:
        if "`" in tok:
            return "backtick"
        if "$(" in tok or "${" in tok:
            return "shell-substitution"
        if tok == "--":
            return "double-dash-separator"
        if _OPTION_LIKE_TOKEN_RE.match(tok):
            return "option-like-token"
        if "\n" in tok:
            return "embedded-newline"
    if len(message_tokens) >= MIN_TOKENS_FOR_MARKDOWN_HEURISTIC:
        for tok in message_tokens:
            if "—" in tok:
                return "em-dash"
            if "–" in tok:
                return "en-dash"
            if "**" in tok:
                return "double-asterisk"
    return None


def _emit_positional_validation_error(
    trigger: str, *, command: str = "thread send <name>"
) -> None:
    """Print the canonical positional-refusal error to stderr (#092, D110).

    Format is intentionally verbatim across cmd_send and cmd_broadcast
    so operators see the same guidance every time. ``command`` is
    interpolated into the example heredoc lines so the suggestion
    matches the subcommand they actually invoked.
    """
    msg = (
        f"Error: positional message contains characters that may be\n"
        f"shell-mangled (matched: {trigger}).\n"
        f"\n"
        f"Pass the message via stdin instead:\n"
        f"\n"
        f"    cat <<'EOF' | claude-worker {command}\n"
        f"    ...your message...\n"
        f"    EOF\n"
        f"\n"
        f"Or from a file:\n"
        f"\n"
        f"    claude-worker {command} < message.md\n"
        f"\n"
        f"Note the single-quoted EOF: it disables shell interpretation\n"
        f"inside the heredoc, which is what makes long/markdown messages\n"
        f"survive intact."
    )
    print(msg, file=sys.stderr)


def cmd_send(args: argparse.Namespace) -> None:
    """Send a message to a single worker (or known thread participant).

    Default behavior: check worker status first and reject if busy. Use
    ``--queue`` to bypass the busy check and track a specific response via
    a correlation ID embedded in the message. For multi-target delivery,
    use ``cmd_broadcast`` (top-level ``broadcast`` subcommand).

    Exposed as ``claude-worker thread send`` after D95 (#075).
    """
    # Fix flag ordering: extract flags that argparse absorbed into message
    args = _reparse_send_flags(args)

    if args.show_response and args.show_full_response:
        print(
            "Error: --show-response and --show-full-response are mutually exclusive",
            file=sys.stderr,
        )
        sys.exit(1)

    # Get message from arg or stdin. Risky positional bodies are refused
    # here so operators get a clear hint rather than silent shell-mangled
    # delivery (#092, D110).
    if args.message:
        trigger = _validate_positional_message(args.message)
        if trigger is not None:
            _emit_positional_validation_error(trigger, command="thread send <name>")
            sys.exit(1)
        content = " ".join(args.message)
    else:
        content = sys.stdin.read()

    if not content.strip():
        print("Error: empty message", file=sys.stderr)
        sys.exit(1)

    if not args.name:
        print(
            "Error: worker name required (use `claude-worker broadcast` "
            "for multi-target delivery)",
            file=sys.stderr,
        )
        sys.exit(1)

    # Broadcast is never True here post-D95 — the parser no longer sets it
    # on `thread send`. Belt-and-braces: ensure the namespace attr exists
    # for _send_to_single_worker's getattr checks.
    if not hasattr(args, "broadcast"):
        args.broadcast = False

    rc = _send_to_single_worker(args.name, content, args)
    _print_worker_status(args.name)
    sys.exit(rc)


def cmd_broadcast(args: argparse.Namespace) -> None:
    """Send a message to all workers matching the filter flags.

    Top-level subcommand as of D95 (#075). Self-exclusion: the caller is
    dropped from the target list if they're a worker themselves.
    Prints a per-worker summary; exits 0 iff at least one target accepted
    the message.
    """
    args = _reparse_send_flags(args)

    if args.show_response and args.show_full_response:
        print(
            "Error: --show-response and --show-full-response are mutually exclusive",
            file=sys.stderr,
        )
        sys.exit(1)

    # Refuse risky positional bodies before fan-out (#092, D110).
    if args.message:
        trigger = _validate_positional_message(args.message)
        if trigger is not None:
            _emit_positional_validation_error(trigger, command="broadcast")
            sys.exit(1)
        content = " ".join(args.message)
    else:
        content = sys.stdin.read()

    if not content.strip():
        print("Error: empty message", file=sys.stderr)
        sys.exit(1)

    targets = _collect_filtered_workers(args)

    self_name = _find_worker_by_ancestry()
    if self_name:
        targets = [w for w in targets if w["name"] != self_name]

    if not targets:
        print("No matching workers found for broadcast", file=sys.stderr)
        sys.exit(1)

    # Ensure the flag shape expected by _send_to_single_worker is present.
    args.broadcast = True
    args.name = None  # unused by _send_to_single_worker

    names = [w["name"] for w in targets]
    results: list[tuple[str, int]] = []
    for name in names:
        rc = _send_to_single_worker(name, content, args)
        results.append((name, rc))

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


def _resolve_read_thread_id(args: argparse.Namespace) -> str:
    """Determine the thread ID ``cmd_read`` should read from.

    Priority:
      1. ``--thread ID`` explicit override
      2. ``--chat ID`` / auto-detected chat (PM workers) → ``chat-<id>``
      3. ``pair-<sender>-<target>`` where sender is ``_resolve_sender()``,
         with existence-based fallback to common identities (D103, #087).

    The fallback chain (step 3) handles the case where the sender
    identity is ephemeral (e.g., CLAUDE_SESSION_UUID that changes per
    session). If the primary pair-thread doesn't exist in the thread
    index, the fallback tries "human" and then CW_WORKER_NAME. A stderr
    notice is emitted when a fallback fires so the reroute is visible
    (V2 explicit-over-implicit, G2 loud-over-silent).
    """
    from claude_worker.thread_store import chat_thread_id, load_index, pair_thread_id

    override = getattr(args, "thread", None)
    if override:
        return override

    chat_id = _resolve_chat_id(
        args.name,
        getattr(args, "chat", None),
        getattr(args, "all_chats", False),
    )
    if chat_id is not None:
        return chat_thread_id(chat_id)

    sender = _resolve_sender()
    primary = pair_thread_id(sender, args.name)

    # Existence-based fallback (#087, D103): if the primary thread
    # doesn't exist, try common fallback identities. This handles
    # the case where the sender identity changes between sessions
    # (e.g., CLAUDE_SESSION_UUID from Claude Code vs. "human" from
    # a plain terminal).
    index = load_index()
    if primary in index:
        return primary

    # Fallback: "human" (interactive terminal identity). Covers the
    # common case where the sender identity changed between sessions
    # (e.g., CLAUDE_SESSION_UUID from Claude Code vs. "human" from
    # a plain terminal). CW_WORKER_NAME fallback is unnecessary
    # because _resolve_sender() returns it as priority 1 — if it's
    # set, it IS the primary sender, so the primary pair-thread
    # already uses it.
    human_thread = pair_thread_id("human", args.name)
    if human_thread != primary and human_thread in index:
        print(
            f"note: thread read {args.name} -> {human_thread} "
            f"(primary {primary} not found, using 'human' identity)",
            file=sys.stderr,
        )
        return human_thread

    # Nothing matched — return primary (clean "no messages" output)
    return primary


def _format_thread_message(msg: dict) -> str:
    """Render a thread message for ``cmd_read`` output.

    Format::

        [<id> <timestamp> <sender>]
        <content>

    Matches the visual rhythm of claude_logs' rendering: a bracketed
    header on its own line, blank-line separated, then the body.
    """
    mid = str(msg.get("id", ""))[:UUID_SHORT_LENGTH]
    ts = msg.get("timestamp", "")
    sender = msg.get("sender", "?")
    content = msg.get("content", "")
    header = f"[{mid} {ts} {sender}]"
    return f"{header}\n{content}"


def _read_from_thread(
    args: argparse.Namespace,
    runtime: Path,
    fallback_to_log: bool = False,
) -> tuple[str | None, str | None] | None:
    """Read messages from the active thread (post-D88 default).

    When ``fallback_to_log=True``, returns ``None`` to signal the caller
    should continue with the log-based read path whenever the thread
    isn't usable yet. Specifically: ``None`` is returned if the auto-
    detected pair thread is missing OR empty AND no explicit ``--thread``
    was provided. An explicit override always reads the thread (and
    prints "No messages" if it's empty).
    """
    from claude_worker.thread_store import read_messages

    thread_id = _resolve_read_thread_id(args)
    explicit_thread = bool(getattr(args, "thread", None))

    since_id: str | None = None
    if getattr(args, "since", None):
        since_id = str(args.since).strip() or None

    limit: int | None = None
    if getattr(args, "n", None) is not None:
        limit = int(args.n)

    try:
        messages = read_messages(
            thread_id,
            since_id=since_id,
            limit=limit,
        )
    except FileNotFoundError:
        if fallback_to_log and not explicit_thread:
            return None
        print(
            f"No messages. (Thread '{thread_id}' does not exist yet. "
            f"Use --log for the raw session log.)",
            file=sys.stderr,
        )
        return None, None

    if not messages:
        if fallback_to_log and not explicit_thread:
            return None
        if getattr(args, "count", False):
            print(0)
        else:
            print("No messages.", file=sys.stderr)
        return None, None

    if getattr(args, "count", False):
        print(len(messages))
        return None, None

    for msg in messages:
        print(_format_thread_message(msg))
        print()

    first_id = str(messages[0].get("id", "")) or None
    last_id = str(messages[-1].get("id", "")) or None

    # Save read marker if --mark was passed. Mirrors the log-path
    # _render_read_output behavior so --mark works regardless of which
    # read path ran (thread is the default post-D88, log is the
    # --log/--follow/--verbose escape hatch).
    if getattr(args, "mark", False) and last_id:
        _save_read_marker(runtime, args, last_id)

    return first_id, last_id


def cmd_read(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Read worker output.

    Post-Phase-3 (D88): reads from the active thread by default (auto-
    detected as ``pair-<sender>-<target>`` or overridden via ``--thread``).
    Legacy ``--log`` reads the raw claude session log — the debugging
    escape hatch. ``--context`` and ``--follow`` always read the log
    since they're about claude's session state, not messaging.

    Returns (first_uuid, last_uuid) for the messages that were actually
    rendered, which programmatic callers (like --show-response) use to
    display a range hint. The normal CLI invocation ignores the return
    value. For thread reads the "UUIDs" are thread message IDs.
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

    # Thread-first read (default). Falls back to log when:
    #   --log       : explicit opt-out (debugging)
    #   --follow    : live tailing still targets the session log
    #   --verbose   : users want tools/thinking → log has them
    # When no explicit thread override is set, the thread read path
    # gracefully falls back to the session log if the auto-detected
    # pair thread has no messages yet (or doesn't exist). This keeps
    # the CLI useful for brand-new workers before any peer has sent.
    use_log = (
        getattr(args, "log", False)
        or getattr(args, "follow", False)
        or getattr(args, "verbose", False)
    )
    if not use_log:
        thread_result = _read_from_thread(args, runtime, fallback_to_log=True)
        if thread_result is not None:
            return thread_result
        # Fell through — continue into log rendering below.

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

    # Handle --new: load last-seen marker as the --since value
    if getattr(args, "new", False):
        if args.since:
            print("Error: --new and --since are mutually exclusive", file=sys.stderr)
            sys.exit(1)
        marker_uuid = _load_read_marker(runtime, args)
        if marker_uuid:
            args.since = marker_uuid

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
        _read_follow(log_file, config, formatter, since_uuid, since_ts, args, runtime)
        return None, None
    return _read_static(
        log_file, config, formatter, since_uuid, since_ts, args, runtime
    )


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
            f"claude-worker thread read {name} "
            f"--since {first_uuid[:UUID_SHORT_LENGTH]} "
            f"--until {last_uuid[:UUID_SHORT_LENGTH]} "
            f"--exclude-user"
        )


def _count_compactions(log_file: Path) -> list[dict]:
    """Count compact_boundary events in a worker's log.

    Returns a list of compaction records, each containing:
    - line: line number in the log
    - trigger: "manual" or "auto"
    - pre_tokens: token count before compaction

    NOTE: system/init fires every turn in -p stream-json mode and is
    NOT a compaction indicator. Only compact_boundary marks a real
    compaction event. See compaction_detector.py for the full story.
    """
    compactions: list[dict] = []
    line_num = 0
    try:
        with open(log_file) as f:
            for raw_line in f:
                line_num += 1
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    data = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if (
                    data.get("type") == "system"
                    and data.get("subtype") == "compact_boundary"
                ):
                    metadata = data.get("compactMetadata", {})
                    compactions.append(
                        {
                            "line": line_num,
                            "trigger": metadata.get("trigger", "unknown"),
                            "pre_tokens": metadata.get("preTokens", 0),
                        }
                    )
    except OSError:
        pass
    return compactions


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
    messages: list[tuple[int, dict, object]], formatter, config, args, runtime: Path
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
            f"claude-worker thread read {args.name} "
            f"--since {last_uuid[:UUID_SHORT_LENGTH]}{exclude_user_flag}"
        )
    # Save read marker if --mark was passed
    if getattr(args, "mark", False) and last_uuid:
        _save_read_marker(runtime, args, last_uuid)

    return first_uuid, last_uuid


def _read_static(
    log_file, config, formatter, since_uuid, since_ts, args, runtime: Path
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
            return _render_read_output(messages, formatter, config, args, runtime)
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

    return _render_read_output(messages, formatter, config, args, runtime)


def _read_follow(
    log_file, config, formatter, since_uuid, since_ts, args, runtime: Path
):
    """Tail the log file, printing new messages as they appear."""
    from claude_logs import parse_message, should_show_message
    import time as _time

    # First, print existing content
    _read_static(log_file, config, formatter, since_uuid, since_ts, args, runtime)

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
    """Block until a turn boundary or a new thread message (D95, #075).

    Dual semantics based on the positional arg:
      - ``pair-<a>-<b>`` / ``chat-<id>`` → wait for the next message on
        that thread (blocks until a new entry appears in its JSONL).
      - any other value → treat as a worker name; wait for that
        worker's next turn boundary (legacy ``wait-for-turn``).
    """
    target = args.name
    if target.startswith("pair-") or target.startswith("chat-"):
        # Thread-id mode: watch the thread until the first new message.
        rc = _watch_thread(
            target,
            since_id=None,
            timeout=getattr(args, "timeout", None),
            exit_on_first_new=True,
        )
        sys.exit(rc)

    resolve_worker(target)  # validate worker exists
    rc = _wait_for_turn(
        target,
        timeout=args.timeout,
        after_uuid=getattr(args, "after_uuid", None),
        settle=args.settle,
        chat_tag=getattr(args, "chat", None),
    )
    sys.exit(rc)


def _format_tool_call(tool_use: dict) -> str:
    """Render a ``tool_use`` content block for ls display.

    Chooses a short, informative per-tool summary:
      Bash → ``Bash(cmd)``
      Edit/Write/Read/MultiEdit → ``<Tool>(basename)``
      Task/Agent → ``Task("description")``
      Grep/Glob → ``<Tool>(pattern)``
      (other) → bare tool name
    Content longer than ``TOOL_CALL_PREVIEW_LENGTH`` chars is truncated
    with a trailing ellipsis.
    """
    name = tool_use.get("name") or "?"
    inp = tool_use.get("input") or {}
    if not isinstance(inp, dict):
        inp = {}

    def _trim(s: str) -> str:
        s = s.replace("\n", " ").strip()
        if len(s) > TOOL_CALL_PREVIEW_LENGTH:
            return s[: TOOL_CALL_PREVIEW_LENGTH - 1] + "…"
        return s

    if name == "Bash":
        cmd = str(inp.get("command") or "")
        return f"Bash({_trim(cmd)})" if cmd else "Bash"
    if name in ("Edit", "Write", "Read", "MultiEdit"):
        path = str(inp.get("file_path") or "")
        return f"{name}({os.path.basename(path)})" if path else name
    if name in ("Task", "Agent"):
        desc = str(inp.get("description") or inp.get("prompt") or "")
        return f"{name}({_trim(desc)})" if desc else name
    if name in ("Grep", "Glob"):
        pat = str(inp.get("pattern") or "")
        return f"{name}({_trim(pat)})" if pat else name
    return name


def _format_tool_call_duration(seconds: float) -> str:
    """Short human duration: ``(12s)`` / ``(2m 15s)`` / ``(1h 3m)``."""
    secs = int(seconds)
    if secs < 60:
        return f"({secs}s)"
    if secs < 3600:
        return f"({secs // 60}m {secs % 60}s)"
    return f"({secs // 3600}h {(secs % 3600) // 60}m)"


def _find_current_tool_call(log_file: Path, now: float | None = None) -> dict | None:
    """Detect whether the worker is in the middle of a tool call.

    Walks the log backward looking for the most recent assistant
    message containing ``tool_use`` content blocks. For each tool_use,
    scans forward in the already-collected tail looking for a matching
    ``tool_result`` in a subsequent user message. If at least one
    tool_use has no matching result, it is the currently-open call
    and this function returns a summary dict::

        {
          "tool_use_id": str,
          "name": str,
          "display": str,       # via _format_tool_call
          "started_at": float,  # epoch seconds (best-effort from timestamp)
          "duration_seconds": float,
        }

    Returns ``None`` when no open tool_use is found within the last
    ``TOOL_CALL_SCAN_LINE_LIMIT`` log lines. The scan is bounded so
    a very long log doesn't cost O(bytes) per ls call. (#081, D98)

    Extracted as a pure-ish function for testing — only touches the
    filesystem via _iter_log_reverse.
    """
    if now is None:
        now = time.time()

    if not log_file.exists():
        return None

    # Tail lines in reverse order; collect up to TOOL_CALL_SCAN_LINE_LIMIT
    # entries, then process in forward (oldest-first) order to match
    # tool_use -> tool_result pairs correctly.
    tail: list[dict] = []
    for entry in _iter_log_reverse(log_file):
        tail.append(entry)
        if len(tail) >= TOOL_CALL_SCAN_LINE_LIMIT:
            break
    tail.reverse()

    # Map of tool_use_id -> (tool_use_block, assistant_timestamp) for
    # any tool_use seen. Deleted when a matching tool_result appears.
    open_calls: dict[str, tuple[dict, float]] = {}

    def _parse_ts(entry: dict) -> float:
        ts = entry.get("timestamp")
        if isinstance(ts, str):
            try:
                from datetime import datetime

                return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                return now
        return now

    for entry in tail:
        etype = entry.get("type")
        if etype == "assistant":
            ts = _parse_ts(entry)
            content = (entry.get("message") or {}).get("content") or []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tid = block.get("id")
                        if tid:
                            open_calls[tid] = (block, ts)
        elif etype == "user":
            content = (entry.get("message") or {}).get("content") or []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tid = block.get("tool_use_id")
                        if tid and tid in open_calls:
                            del open_calls[tid]

    if not open_calls:
        return None

    # Pick the most recently started open call (highest timestamp).
    best_id, (block, started_at) = max(open_calls.items(), key=lambda kv: kv[1][1])
    return {
        "tool_use_id": best_id,
        "name": block.get("name") or "?",
        "display": _format_tool_call(block),
        "started_at": started_at,
        "duration_seconds": max(0.0, now - started_at),
    }


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

    # Read CWD + identity from saved worker metadata
    cwd = "-"
    saved = get_saved_worker(name)
    worker_identity = _get_worker_identity(name)
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

    # Current tool call (#081, D98) — shown when the worker is mid-
    # tool-call. Line omitted when no open tool_use.
    tool_info = _find_current_tool_call(log_file)
    tool_line = ""
    if tool_info:
        duration = _format_tool_call_duration(tool_info["duration_seconds"])
        tool_line = f"\n    tool: {tool_info['display']}  {duration}"

    _IDENTITY_LABELS = {"pm": "PM", "technical-lead": "TL"}
    identity_tag = ""
    if worker_identity and worker_identity != "worker":
        label = _IDENTITY_LABELS.get(worker_identity, worker_identity.upper())
        identity_tag = f" [{label}]"
    return (
        f"  {name}{identity_tag}\n"
        f"    pid: {pid}  status: {status}{idle_str}  cwd: {cwd}\n"
        f"    session: {session}"
        f"{preview_line}"
        f"{context_line}"
        f"{tool_line}"
    )


def _get_worker_info(name: str) -> dict | None:
    """Collect structured info about a worker for filtering and display."""
    runtime = get_runtime_dir(name)
    if not runtime.exists():
        return None

    saved = get_saved_worker(name)
    raw_cwd = (saved.get("cwd") or "-") if saved else "-"
    worker_identity = _get_worker_identity(name)

    status, log_mtime = get_worker_status(runtime)

    # Map identity to role for filter compatibility
    role = (
        "pm"
        if worker_identity == "pm"
        else "tl" if worker_identity == "technical-lead" else "worker"
    )

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
            # Augment with current-tool-call (#081, D98). Always a key
            # for stable script shape; null when no open tool_use.
            log_file = get_runtime_dir(w["name"]) / "log"
            tool_info = _find_current_tool_call(log_file)
            out["current_tool"] = tool_info
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
        cleanup_runtime_dir(args.name, reason="stop")
        return

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        print("Error: invalid PID file", file=sys.stderr)
        cleanup_runtime_dir(args.name, reason="stop")
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
                # Grace window: let the FIFO pump + claude + log-writer land
                # the response in the log before _wait_for_turn opens it.
                # Without this, the #082 race can leave tail_loop seeking
                # to an "end" that gets overtaken by the writer a few ms
                # later, causing the poll to miss the turn entirely.
                time.sleep(FIFO_HANDOFF_GRACE_SECONDS)
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
        reason = "force-stop" if args.force else "stop"
        cleanup_runtime_dir(args.name, reason=reason)
        print(f"Cleaned up {runtime}")


def _strip_flag_with_value(args: list[str], flag: str) -> list[str]:
    """Remove a flag and its following value from an argument list.

    Handles zero, one, or multiple occurrences. If the flag appears at the
    end of the list with no following value, it is still removed.
    """
    result: list[str] = []
    skip_next = False
    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg == flag:
            # Skip this flag and its value (if present)
            if i + 1 < len(args):
                skip_next = True
            continue
        result.append(arg)
    return result


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


def _find_latest_archive(name: str) -> Path | None:
    """Find the latest archived runtime dir for a worker name.

    Archives are named ``<name>.<timestamp>[.<session-prefix>]``.
    Returns the path to the latest archive, or None.
    """
    base = get_base_dir()
    if not base.exists():
        return None
    prefix = f"{name}."
    archives = sorted(
        [d for d in base.iterdir() if d.is_dir() and d.name.startswith(prefix)],
        key=lambda d: d.name,
        reverse=True,
    )
    return archives[0] if archives else None


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
    wid = _get_worker_identity(name)
    if wid == "pm":
        handoff_dirs.append(Path(cwd) / ".cwork" / "roles" / "pm" / "handoffs")
    if wid == "technical-lead":
        handoff_dirs.append(Path(cwd) / ".cwork" / "roles" / "tl" / "handoffs")

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
    saved_args = saved.get("claude_args") or []
    # Strip --append-system-prompt-file from saved_args to avoid duplicates —
    # the identity file is re-written to the new runtime dir below.
    saved_args = _strip_flag_with_value(saved_args, "--append-system-prompt-file")
    # replaceme ALWAYS starts fresh — no --resume. Clean context is the point.
    # Continuity mechanism is the handoff file, NOT Claude Code's --resume.
    claude_args = list(saved_args)
    replace_identity = _get_worker_identity(worker_name)
    identity_mode = replace_identity and replace_identity != "worker"
    pm_mode = replace_identity == "pm"
    tl_mode = replace_identity == "technical-lead"

    # Load per-identity config (env vars, etc.) — mirrors cmd_start
    identity_config = _load_identity_config(replace_identity) if identity_mode else {}

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

        # 6c. Auto-create .cwork/ skeleton + register project (mirrors cmd_start)
        resolved_cwd = cwd or os.getcwd()
        _ensure_cwork_dirs(resolved_cwd, pm_mode, tl_mode)
        if identity_mode:
            from claude_worker.project_registry import register_project

            register_project(resolved_cwd)

        # 6d. The old manager archived the runtime dir (SIGUSR1 handler).
        # The worker name is now free. Create new runtime dir.
        new_runtime = create_runtime_dir(worker_name)

        # 6e. Write identity file — check user-installed identity first,
        # fall back to bundled (mirrors cmd_start's resolution at lines 1249-1265).
        if identity_mode:
            identity_path = new_runtime / "identity.md"
            user_identity = (
                Path.home() / ".cwork" / "identities" / replace_identity / "identity.md"
            )
            if user_identity.exists():
                identity_content = user_identity.read_text()
            elif pm_mode:
                identity_content = _load_bundled_resource(
                    "identities", PM_IDENTITY_RESOURCE
                )
            elif tl_mode:
                identity_content = _load_bundled_resource(
                    "identities", TL_IDENTITY_RESOURCE
                )
            else:
                raise FileNotFoundError(
                    f"Identity '{replace_identity}' not found at {user_identity}"
                )
            identity_path.write_text(identity_content)
            # Record the source hash so the manager's drift check has a
            # baseline to compare against (#066).
            write_identity_hash(new_runtime, identity_content)
            # Prepend --append-system-prompt-file to claude_args
            claude_args = [
                "--append-system-prompt-file",
                str(identity_path),
            ] + claude_args

        # 6f. Write permission settings if applicable
        permission_settings = _maybe_write_permission_settings(
            name=worker_name, enabled=True, cwd=cwd, identity=replace_identity
        )
        if permission_settings is not None:
            claude_args = claude_args + ["--settings", str(permission_settings)]

        # 6g. Save worker metadata (same as cmd_start).
        # saved_args was stripped of --append-system-prompt-file above to
        # prevent duplicates in claude_args. Re-add it with the NEW runtime
        # path so a future cmd_start --resume can find the identity file.
        resume_saved_args = saved_args[:]
        if identity_mode:
            resume_saved_args = [
                "--append-system-prompt-file",
                str(identity_path),
            ] + resume_saved_args
        save_worker(
            worker_name,
            cwd=cwd or os.getcwd(),
            claude_args=resume_saved_args,
            identity=replace_identity,
            pm=pm_mode,
            team_lead=tl_mode,
        )

        # 6h. Determine initial message for the new session.
        # Fresh session needs to know about the handoff file — that's the
        # continuity mechanism, not conversation preservation.
        initial_message = _build_replaceme_initial_message(
            replace_identity, resolved_cwd
        )

        # 6i. Fork the new manager daemon (same pattern as cmd_start)
        manager_pid = os.fork()
        if manager_pid == 0:
            # Grandchild: the new manager
            run_manager(
                worker_name,
                cwd,
                claude_args,
                initial_message,
                identity=replace_identity,
                extra_env=identity_config.get("env"),
                remote=False,
            )
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
        # Log the traceback to a sidecar file for diagnostics
        import traceback

        error_log = get_base_dir() / f"{worker_name}{REPLACEME_ERROR_LOG_SUFFIX}"
        try:
            error_log.parent.mkdir(parents=True, exist_ok=True)
            error_log.write_text(traceback.format_exc())
        except OSError:
            pass
        sys.exit(1)


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


def _load_identity_hooks(identity: str) -> dict:
    """Load hook definitions from ~/.cwork/identities/<name>/hooks/hooks.json.

    Returns a dict matching the settings.json hooks structure:
    {"PreToolUse": [...], "Stop": [...], etc.}. Returns {} if the
    file doesn't exist or can't be parsed.

    Format: raw settings.json hook fragments — same structure as
    _build_permission_hook_settings produces. This keeps identity
    hooks consistent with standard hooks without inventing a new format.
    """
    hooks_file = (
        Path.home() / ".cwork" / "identities" / identity / "hooks" / "hooks.json"
    )
    if not hooks_file.exists():
        return {}
    try:
        data = json.loads(hooks_file.read_text())
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def _merge_hooks(base: dict, extra: dict) -> dict:
    """Merge extra hook entries into the base hooks dict.

    For each hook type (PreToolUse, Stop, PostToolUse, etc.), extra
    entries are appended to the base list. Base entries are preserved.
    """
    merged = dict(base)
    for hook_type, entries in extra.items():
        if not isinstance(entries, list):
            continue
        if hook_type in merged:
            merged[hook_type] = merged[hook_type] + entries
        else:
            merged[hook_type] = entries
    return merged


def _build_permission_hook_settings(
    grants_path: Path,
    python_executable: str,
    sentinel_dir: Path | None = None,
    cwd: str | None = None,
    identity: str | None = None,
) -> dict:
    """Build the settings dict for per-worker hooks.

    Wires standard hooks plus identity-specific hooks:
    1. PreToolUse — permission grant hook for Edit/Write/MultiEdit
    2. PreToolUse — CWD write guard (if cwd provided)
    3. Stop — context threshold check (if sentinel_dir provided)
    4. PostToolUse — ticket watcher (if cwd provided)
    5. Identity hooks — merged from ~/.cwork/identities/<name>/hooks/hooks.json

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
        if identity:
            context_command += f" --identity {identity}"
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
    posttooluse_entries: list[dict] = []
    if cwd is not None:
        ticket_watcher_command = (
            f"{python_executable} -m claude_worker.ticket_watcher --cwd {cwd}"
        )
        posttooluse_entries.append(
            {
                "matcher": matcher,
                "hooks": [
                    {
                        "type": "command",
                        "command": ticket_watcher_command,
                    }
                ],
            }
        )
    # Commit checker: warns on missing tests/GVP for identity workers
    if identity:
        commit_checker_command = f"{python_executable} -m claude_worker.commit_checker"
        posttooluse_entries.append(
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": commit_checker_command,
                    }
                ],
            }
        )
    if posttooluse_entries:
        hooks["PostToolUse"] = posttooluse_entries
    # Compaction detector: SessionStart hook that fires on compact events
    if identity:
        compaction_command = (
            f"{python_executable} -m claude_worker.compaction_detector "
            f"--identity {identity} --cwd {cwd or '.'}"
        )
        reinjector_command = (
            f"{python_executable} -m claude_worker.identity_reinjector "
            f"--identity {identity} --cwd {cwd or '.'}"
        )
        hooks["SessionStart"] = [
            {
                "matcher": "compact",
                "hooks": [
                    {
                        "type": "command",
                        "command": compaction_command,
                    }
                ],
            },
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": reinjector_command,
                    }
                ],
            },
        ]
    # Merge identity-specific hooks if present
    if identity:
        identity_hooks = _load_identity_hooks(identity)
        if identity_hooks:
            hooks = _merge_hooks(hooks, identity_hooks)

    return {"hooks": hooks}


def _maybe_write_permission_settings(
    name: str,
    enabled: bool,
    cwd: str | None = None,
    identity: str | None = None,
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
        identity=identity,
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

    Equivalent to ``claude-worker thread read NAME --last-turn`` (including
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

    # Compaction history
    compactions = _count_compactions(log_file)
    if compactions:
        print()
        print(f"Compactions: {len(compactions)}")
        for c in compactions:
            trigger = c["trigger"]
            pre = c["pre_tokens"]
            print(f"  line {c['line']}: {trigger} (pre: {pre:,} tokens)")
    else:
        print()
        print("Compactions: 0")


def cmd_projects(args: argparse.Namespace) -> None:
    """List all registered projects with active worker info."""
    from claude_worker.project_registry import format_projects_table, load_registry

    projects = load_registry()
    # Cross-reference with active workers
    workers = _collect_filtered_workers(
        argparse.Namespace(role=None, status=None, alive=False, cwd_filter=None)
    )
    print(format_projects_table(projects, workers))


def cmd_stats(args: argparse.Namespace) -> None:
    """Print summary statistics from the token tracking CSV."""
    from claude_worker.token_tracking import format_stats, read_summary

    rows = read_summary()
    print(format_stats(rows))


# -- subagents subcommand (#083, D100) -----------------------------------
#
# Expose Claude Code's per-session subagent JSONL files as a structured
# summary (what's running, for how long, doing what). Complements D98's
# `ls` tool-call display — ls tells you "what tool", this tells you
# "what's inside the Task subagent". The subagent files live at
#   ~/.claude/projects/<slug>/<session>/subagents/agent-*.{jsonl,meta.json}
# where <slug> is cwd.replace("/", "-").replace(".", "-").


def _cwd_to_project_slug(cwd: str) -> str:
    """Replicate Claude Code's cwd -> project-slug transformation.

    Empirically derived from ``~/.claude/projects/`` contents: every
    ``/`` and ``.`` in the absolute cwd becomes ``-``. Case is
    preserved. Nothing else is transformed.

    Symlinks are NOT resolved — Claude Code uses the literal cwd it
    was given, and so do we (so the stored cwd and the on-disk slug
    stay in sync).
    """
    if not cwd:
        return ""
    return cwd.replace("/", "-").replace(".", "-")


def _resolve_subagents_dir(name: str) -> tuple[Path | None, str | None, str | None]:
    """Return (subagents_dir, session_id, cwd) for a worker, or (None, …).

    Reads ``runtime/session`` and the session's saved ``cwd``; joins
    them via ``_cwd_to_project_slug`` to locate
    ``~/.claude/projects/<slug>/<session>/subagents/``. Returns None
    for ``subagents_dir`` when the session isn't captured yet or the
    directory doesn't exist — the session_id and cwd are still
    returned so callers can render a meaningful "no subagents" line.
    """
    runtime = get_runtime_dir(name)
    if not runtime.exists():
        return None, None, None

    session_file = runtime / "session"
    session_id: str | None = None
    if session_file.exists():
        try:
            session_id = session_file.read_text().strip() or None
        except OSError:
            session_id = None

    saved = get_saved_worker(name) or {}
    cwd = saved.get("cwd") or None

    if not session_id or not cwd:
        return None, session_id, cwd

    slug = _cwd_to_project_slug(cwd)
    subagents_dir = (
        Path.home() / ".claude" / "projects" / slug / session_id / "subagents"
    )
    if not subagents_dir.exists():
        return None, session_id, cwd
    return subagents_dir, session_id, cwd


def _summarize_subagent(
    meta_path: Path, jsonl_path: Path, now: float | None = None
) -> dict:
    """Summarize one (meta.json, jsonl) subagent pair.

    Defensive — all fields degraded gracefully when missing. Returns
    a dict matching the JSON schema documented in the TECHNICAL.md.
    """
    if now is None:
        now = time.time()

    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if not isinstance(meta, dict):
                meta = {}
        except (OSError, json.JSONDecodeError):
            meta = {}

    agent_id = jsonl_path.stem
    if agent_id.startswith("agent-"):
        agent_id = agent_id[len("agent-") :]

    started_at: str | None = None
    last_action_at: str | None = None
    tool_call_count = 0
    last_action: str | None = None
    last_tool_use_block: dict | None = None

    if jsonl_path.exists():
        try:
            with open(jsonl_path) as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = entry.get("timestamp")
                    if isinstance(ts, str):
                        if started_at is None:
                            started_at = ts
                        last_action_at = ts
                    message = entry.get("message") or {}
                    content = message.get("content") or []
                    if isinstance(content, list):
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "tool_use"
                            ):
                                tool_call_count += 1
                                last_tool_use_block = block
        except OSError:
            pass

    if last_tool_use_block is not None:
        last_action = _format_tool_call(last_tool_use_block)

    return {
        "agent_id": agent_id,
        "type": meta.get("agentType") or "unknown",
        "description": meta.get("description") or "",
        "started_at": started_at,
        "last_action_at": last_action_at,
        "tool_call_count": tool_call_count,
        "last_action": last_action,
    }


def _format_subagent_duration_since_iso(ts: str | None, now: float) -> str:
    """Render '2m 14s ago' from an ISO timestamp, or '' if missing."""
    if not ts:
        return ""
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        elapsed = max(0.0, now - dt.timestamp())
    except (ValueError, TypeError):
        return ""
    return _format_tool_call_duration(elapsed).strip("()")


def cmd_subagents(args: argparse.Namespace) -> None:
    """Summarize Claude Code subagents launched by a worker (#083, D100).

    Defaults to text output. ``--format json`` emits a single JSON
    envelope with a ``subagents`` array. ``--limit N`` caps the count.
    """
    subagents_dir, session_id, cwd = _resolve_subagents_dir(args.name)

    if session_id is None:
        print(
            f"Error: worker '{args.name}' has no session yet (not started).",
            file=sys.stderr,
        )
        sys.exit(1)

    fmt = getattr(args, "format", None) or "text"
    limit = getattr(args, "limit", None)

    summaries: list[dict] = []
    if subagents_dir is not None:
        now = time.time()
        metas = sorted(subagents_dir.glob("agent-*.meta.json"))
        for meta_path in metas:
            jsonl_path = meta_path.with_suffix("")  # drop .json
            if jsonl_path.suffix != ".jsonl":
                # meta.json has double-suffix; strip explicitly.
                jsonl_path = meta_path.with_name(
                    meta_path.name.replace(".meta.json", ".jsonl")
                )
            summaries.append(_summarize_subagent(meta_path, jsonl_path, now=now))

        # Also pick up any jsonl files without a sibling meta (rare, but
        # defensively handle partial writes).
        seen_ids = {s["agent_id"] for s in summaries}
        for jsonl_path in sorted(subagents_dir.glob("agent-*.jsonl")):
            aid = jsonl_path.stem.removeprefix("agent-")
            if aid in seen_ids:
                continue
            meta_path = jsonl_path.with_name(
                jsonl_path.name.replace(".jsonl", ".meta.json")
            )
            summaries.append(_summarize_subagent(meta_path, jsonl_path, now=now))

        # Most recent activity first.
        summaries.sort(key=lambda s: s.get("last_action_at") or "", reverse=True)

        if isinstance(limit, int) and limit > 0:
            summaries = summaries[:limit]

    if fmt == "json":
        envelope = {
            "worker": args.name,
            "session": session_id,
            "cwd": cwd,
            "subagents": summaries,
        }
        print(json.dumps(envelope, indent=2))
        return

    print(f"worker: {args.name}")
    print(f"session: {session_id}")
    count = len(summaries)
    if subagents_dir is None:
        print(f"subagents: {count}  (no subagents directory for this session)")
        return
    print(f"subagents: {count}")

    now_float = time.time()
    for s in summaries:
        print()
        print(f"  agent-{s['agent_id']}  {s['type']}")
        if s["description"]:
            print(f"    description: \"{s['description']}\"")
        age = _format_subagent_duration_since_iso(s.get("started_at"), now_float)
        last = s.get("last_action") or "(no tool calls)"
        tc = s["tool_call_count"]
        call_word = "call" if tc == 1 else "calls"
        prefix = f"started {age} ago, " if age else ""
        print(f"    {prefix}{tc} tool {call_word}, last: {last}")


# -- Discoverability commands (#071, D86) --
# Every custom CLI utility should expose:
#   version / --version      → semver
#   changelog [--since V]    → CHANGELOG.md (optionally filtered)
#   docs                     → path to README.md
#   skill                    → path to installed skill
# See main:P10.

# Where the installed skill file lives.
SKILL_INSTALL_PATH: Path = (
    Path.home() / ".claude" / "skills" / "claude-worker" / "SKILL.md"
)


def _find_project_file(filename: str) -> Path | None:
    """Return the path to a project-root file (CHANGELOG.md, README.md) or None.

    Searches first the current working directory (useful for editable
    installs where the user runs inside the repo) and falls back to the
    package's parent directory (editable install: points back at the
    repo root; wheel install: usually absent).
    """
    candidates = [
        Path.cwd() / filename,
        Path(__file__).parent.parent / filename,
    ]
    return next((p for p in candidates if p.exists()), None)


def cmd_version(args: argparse.Namespace) -> None:
    """Print the claude-worker version."""
    print(__version__)


def cmd_changelog(args: argparse.Namespace) -> None:
    """Print CHANGELOG.md, optionally filtered by --since version.

    --since V prints everything up to (but not including) the `## V (...)`
    heading — i.e., the entries newer than V.
    """
    changelog_path = _find_project_file("CHANGELOG.md")
    if changelog_path is None:
        print("No CHANGELOG.md found.", file=sys.stderr)
        sys.exit(1)

    content = changelog_path.read_text()
    since = getattr(args, "since", None)
    if not since:
        print(content, end="")
        return

    lines = content.splitlines(keepends=True)
    output: list[str] = []
    for line in lines:
        if line.startswith(f"## {since} ") or line.startswith(f"## {since}\n"):
            break
        output.append(line)
    print("".join(output), end="")


def cmd_docs(args: argparse.Namespace) -> None:
    """Print path to README.md."""
    readme_path = _find_project_file("README.md")
    if readme_path is None:
        print("README.md not found.", file=sys.stderr)
        sys.exit(1)
    print(readme_path)


def cmd_skill(args: argparse.Namespace) -> None:
    """Print path to the installed claude-worker skill."""
    if not SKILL_INSTALL_PATH.exists():
        print(
            f"Skill not installed at {SKILL_INSTALL_PATH}. "
            "Install it from the project repo.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(SKILL_INSTALL_PATH)


def _repl_continuous(
    name: str,
    log_file: Path,
    in_fifo: Path,
    config,
    formatter,
    chat_id: str | None,
) -> None:
    """Continuous-flow REPL: output streams like tail -f, input on demand.

    Messages flow continuously. Pressing Enter activates the input prompt.
    After submitting, returns to continuous flow. Ctrl-D or /exit quits.
    """
    import select as _select
    import termios
    import tty

    from claude_logs import parse_message, should_show_message

    print("(continuous mode — press Enter to type, Ctrl-D to quit)\n")

    # Save terminal state for restoration
    if not sys.stdin.isatty():
        print("Error: continuous mode requires a TTY", file=sys.stderr)
        return
    old_termios = termios.tcgetattr(sys.stdin)

    # Start position for streaming
    try:
        stream_pos = log_file.stat().st_size
    except OSError:
        stream_pos = 0

    def _stream_output(f, stop_evt):
        """Print new messages from the log file until stop_evt is set."""
        while not stop_evt.is_set():
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

    try:
        while True:
            # FLOWING state: stream output + detect keypress
            stop_event = threading.Event()
            log_f = open(log_file)
            log_f.seek(stream_pos)
            stream_thread = threading.Thread(
                target=_stream_output, args=(log_f, stop_event), daemon=True
            )
            stream_thread.start()

            # Put terminal in raw mode to detect keypress without blocking
            try:
                tty.setcbreak(sys.stdin.fileno())
                while True:
                    ready, _, _ = _select.select([sys.stdin], [], [], 0.5)
                    if ready:
                        ch = sys.stdin.read(1)
                        if ch == "\x04":  # Ctrl-D
                            stop_event.set()
                            stream_thread.join(timeout=1.0)
                            stream_pos = log_f.tell()
                            log_f.close()
                            print()
                            return
                        # Any keypress → transition to INPUTTING
                        break
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_termios)

            # INPUTTING state: stop streaming, show prompt
            stop_event.set()
            stream_thread.join(timeout=1.0)
            stream_pos = log_f.tell()
            log_f.close()

            _flush_stdin()
            try:
                # Prepend the triggering character if it was printable
                prompt_prefix = ch if ch.isprintable() else ""
                user_input = input(f"{REPL_INPUT_PROMPT}{prompt_prefix}")
                if prompt_prefix:
                    user_input = prompt_prefix + user_input
            except EOFError:
                print()
                return
            except KeyboardInterrupt:
                print("\n(Ctrl-C — returning to flow)")
                continue

            stripped_input = user_input.strip()
            if not stripped_input:
                continue
            if stripped_input in REPL_EXIT_COMMANDS:
                return

            # Send the message
            send_content = stripped_input
            if chat_id:
                send_content = f"[{CHAT_TAG_PREFIX}{chat_id}] {send_content}"

            payload = json.dumps(
                {"type": "user", "message": {"role": "user", "content": send_content}}
            )
            try:
                with open(in_fifo, "w") as f:
                    f.write(payload + "\n")
                    f.flush()
            except OSError as exc:
                print(f"\nError writing to worker FIFO: {exc}", file=sys.stderr)
                return

            # Update stream position to current log end
            try:
                stream_pos = log_file.stat().st_size
            except OSError:
                pass

    finally:
        # Restore terminal state no matter what
        try:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_termios)
        except Exception:
            pass


def _tui_classify_line(data: dict, msg) -> str:
    """Classify a parsed log line for TUI display styling.

    Returns one of:
      - 'assistant' (agent output)
      - 'user-input' (our own typed input, echoed back)
      - 'inbound' (message from another sender via thread notification)
      - 'system' (notifications, [system:*] markers)
      - 'skip' (don't display)

    Extracted for testability — no prompt_toolkit dependency.
    """
    msg_type = data.get("type", "")
    role = ""
    try:
        role = getattr(msg, "role", "") or ""
    except AttributeError:
        role = ""

    if msg_type == "assistant" or role == "assistant":
        return "assistant"

    if msg_type == "user" or role == "user":
        content = ""
        try:
            content = getattr(msg, "content", "") or ""
        except AttributeError:
            content = ""
        content_str = str(content)
        if content_str.startswith("[system:"):
            return "system"
        if content_str.startswith("[") and "]" in content_str:
            # "[sender] body" pattern from thread inbound notifications
            return "inbound"
        return "user-input"

    return "skip"


def _tui_format_prefix(kind: str, sender: str | None = None) -> str:
    """Return the display prefix for a TUI line given its classification.

    Extracted for testability.
    """
    if kind == "assistant":
        return ""
    if kind == "user-input":
        return "> "
    if kind == "inbound":
        return f"[{sender}] " if sender else "[inbound] "
    if kind == "system":
        return "· "
    return ""


def _repl_tui(
    name: str,
    log_file: Path,
    chat_id: str | None,
    verbose: bool,
) -> None:
    """Non-blocking TUI REPL (#077, D96).

    Uses prompt_toolkit.Application with:
      - Scrollable read-only output region pinned to the top
      - Single-line input pinned to the bottom
      - Async log tailer: new messages stream into the output region
        without disrupting the input field

    Input is available at all times — the user can type while the worker
    is processing, and incoming messages scroll above without clobbering
    the input buffer. Ctrl-D / /exit / /quit cleanly exits the app.

    TTY-only. Non-TTY callers should use the turn-based REPL.
    """
    import asyncio

    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension

    from claude_logs import (
        ANSIFormatter,
        FilterConfig,
        PlainFormatter,
        RenderConfig,
        parse_message,
        should_show_message,
    )

    if verbose:
        hidden = {"timestamps", "metadata", "progress", "file-history-snapshot"}
        config = RenderConfig(filters=FilterConfig(hidden=hidden))
    else:
        hidden = {"timestamps", "metadata", "thinking", "tools"}
        show_only = {"user", "user-input", "assistant"}
        config = RenderConfig(filters=FilterConfig(show_only=show_only, hidden=hidden))
    formatter = PlainFormatter()  # keep buffer clean; ANSI in Window style

    output_buffer = Buffer(read_only=False, multiline=True)
    input_buffer = Buffer(multiline=False)

    def _append_output(text: str) -> None:
        """Append a line to the output buffer, preserving scroll."""
        if not text:
            return
        current = output_buffer.text
        if current and not current.endswith("\n"):
            current += "\n"
        new_text = current + text
        # Trim to the tail so the buffer doesn't grow unbounded.
        lines = new_text.splitlines()
        if len(lines) > REPL_TUI_MAX_OUTPUT_LINES:
            new_text = "\n".join(lines[-REPL_TUI_MAX_OUTPUT_LINES:])
        output_buffer.set_document(
            Document(new_text, cursor_position=len(new_text)),
            bypass_readonly=True,
        )

    # Layout
    output_window = Window(
        content=BufferControl(buffer=output_buffer, focusable=False),
        wrap_lines=True,
    )
    separator = Window(
        content=FormattedTextControl(text="─" * 80),
        height=Dimension.exact(1),
        style="class:separator",
    )
    input_window = Window(
        content=BufferControl(buffer=input_buffer),
        height=Dimension.exact(1),
        style="class:input",
    )
    root = HSplit([output_window, separator, input_window])

    kb = KeyBindings()

    @kb.add("c-d")
    def _(event):
        event.app.exit()

    @kb.add("c-c")
    def _(event):
        # Clear input on Ctrl-C; don't exit (avoids accidental termination)
        input_buffer.reset()

    @kb.add("enter")
    def _(event):
        text = input_buffer.text.strip()
        input_buffer.reset()
        if not text:
            return
        if text in REPL_EXIT_COMMANDS:
            event.app.exit()
            return
        _append_output(_tui_format_prefix("user-input") + text)
        # Submit via the send path — thread store handles persistence.
        send_args = argparse.Namespace(
            name=name,
            message=[text],
            queue=False,
            broadcast=False,
            dry_run=False,
            verbose=False,
            show_response=False,
            show_full_response=False,
            chat=chat_id,
            all_chats=False,
        )
        try:
            _send_to_single_worker(name, text, send_args)
        except SystemExit:
            # _send_to_single_worker uses sys.exit on some failures; surface
            # as an inline error rather than killing the TUI.
            _append_output("· (send failed — see stderr)")
        except Exception as exc:  # noqa: BLE001
            _append_output(f"· send error: {exc}")

    application = Application(
        layout=Layout(root, focused_element=input_window),
        key_bindings=kb,
        full_screen=True,
        mouse_support=False,
    )

    # Start position: only show messages that arrive after TUI starts.
    try:
        stream_pos = log_file.stat().st_size
    except OSError:
        stream_pos = 0

    async def _log_tailer() -> None:
        pos = stream_pos
        while True:
            try:
                with open(log_file) as f:
                    f.seek(pos)
                    for raw in f:
                        pos = f.tell()
                        stripped = raw.strip()
                        if not stripped:
                            continue
                        try:
                            data = json.loads(stripped)
                        except json.JSONDecodeError:
                            continue
                        msg = parse_message(data)
                        if not should_show_message(msg, data, config):
                            continue
                        kind = _tui_classify_line(data, msg)
                        if kind == "skip" or kind == "user-input":
                            # user-input is echoed synchronously on Enter;
                            # skip the log-echo to avoid duplicate lines.
                            continue
                        rendered = _render_one_message(data, msg, config, formatter)
                        if not rendered:
                            continue
                        prefix = _tui_format_prefix(kind)
                        _append_output(prefix + rendered)
                        application.invalidate()
            except OSError:
                pass
            await asyncio.sleep(REPL_TUI_POLL_INTERVAL_SECONDS)

    banner = f"=== claude-worker TUI REPL: {name} ==="
    if chat_id:
        banner += f" (chat: {chat_id})"
    _append_output(banner)
    _append_output("Type below. Enter to send, /exit or Ctrl-D to quit.\n")

    async def _run() -> None:
        tailer_task = asyncio.create_task(_log_tailer())
        try:
            await application.run_async()
        finally:
            tailer_task.cancel()
            try:
                await tailer_task
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


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

    # TUI mode: non-blocking full-screen layout (D96, #077)
    if getattr(args, "tui", False):
        if not sys.stdout.isatty():
            print(
                "Error: --tui requires a TTY. Omit --tui for the "
                "turn-based REPL on non-interactive stdout.",
                file=sys.stderr,
            )
            sys.exit(1)
        _repl_tui(args.name, log_file, chat_id, verbose)
        return

    # Entry context: last turn, if any
    _repl_print_last_turn(args.name)

    # Continuous mode: tail -f with on-demand input
    if getattr(args, "continuous", False):
        _repl_continuous(args.name, log_file, in_fifo, config, formatter, chat_id)
        return

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


# ---------------------------------------------------------------------------
# Migration system — deterministic versioned scripts across projects
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Migration:
    """A versioned migration script."""

    number: int
    name: str
    path: Path


def _discover_migrations(migrations_dir: Path | None = None) -> list[Migration]:
    """Find all NNN-*.sh files in the migrations directory, sorted by number."""
    d = migrations_dir or MIGRATIONS_DIR
    if not d.exists():
        return []
    migrations: list[Migration] = []
    for f in sorted(d.iterdir()):
        if f.suffix == ".sh" and f.name[0:3].isdigit():
            try:
                number = int(f.name.split("-", 1)[0])
            except ValueError:
                continue
            migrations.append(Migration(number=number, name=f.name, path=f))
    return migrations


def _sync_bundled_migrations(migrations_dir: Path | None = None) -> int:
    """Copy bundled migration scripts to ~/.cwork/migrations/.

    Only copies scripts that don't already exist (won't overwrite
    user-modified scripts). Returns count of scripts synced.
    """
    d = migrations_dir or MIGRATIONS_DIR
    d.mkdir(parents=True, exist_ok=True)
    synced = 0
    try:
        from importlib.resources import files

        bundled = files("claude_worker") / "migrations"
        for resource in bundled.iterdir():
            if hasattr(resource, "name") and resource.name.endswith(".sh"):
                target = d / resource.name
                if not target.exists():
                    target.write_text(resource.read_text())
                    target.chmod(0o755)
                    synced += 1
    except Exception:
        pass
    return synced


def _read_migration_version(project_path: str) -> int:
    """Read the current migration version for a project. Returns 0 if missing."""
    version_file = Path(project_path) / ".cwork" / MIGRATION_VERSION_FILE
    if not version_file.exists():
        return 0
    try:
        return int(version_file.read_text().strip())
    except (ValueError, OSError):
        return 0


def _write_migration_version(project_path: str, version: int) -> None:
    """Write the migration version for a project."""
    cwork = Path(project_path) / ".cwork"
    cwork.mkdir(parents=True, exist_ok=True)
    (cwork / MIGRATION_VERSION_FILE).write_text(str(version) + "\n")


def _update_version_anchor(project_path: str) -> None:
    """Update the .cwork/version anchor after migration."""
    from claude_worker import __init__ as _pkg

    version = getattr(_pkg, "__version__", "unknown")
    cwork = Path(project_path) / ".cwork"
    cwork.mkdir(parents=True, exist_ok=True)
    (cwork / CWORK_VERSION_FILE).write_text(f"claude-worker {version}\n")


def _run_migration(migration: Migration, project_path: str) -> int:
    """Run a migration script against a project. Returns exit code."""
    try:
        result = subprocess.run(
            ["bash", str(migration.path), project_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            print(
                f"    stderr: {result.stderr.strip()}" if result.stderr else "",
                file=sys.stderr,
            )
        return result.returncode
    except subprocess.TimeoutExpired:
        print("    timeout after 60s", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"    error: {exc}", file=sys.stderr)
        return 1


def cmd_migrate(args: argparse.Namespace) -> None:
    """Run pending migrations on registered projects."""
    from claude_worker.project_registry import load_registry

    # Sync bundled migrations first
    synced = _sync_bundled_migrations()
    if synced > 0:
        print(f"Synced {synced} new migration(s) from bundle.")

    migrations = _discover_migrations()
    if not migrations:
        print("No migrations found.")
        return

    if args.list_migrations:
        print(f"Available migrations ({len(migrations)}):")
        for m in migrations:
            print(f"  {m.name}")
        print()

    # Determine target projects
    if args.project:
        projects = [
            {
                "slug": Path(args.project).name,
                "path": os.path.realpath(args.project),
            }
        ]
    else:
        projects = load_registry()

    if not projects:
        print("No registered projects. Use --project PATH or register projects first.")
        return

    if args.list_migrations:
        print(f"Project status ({len(projects)}):")
        for proj in projects:
            v = _read_migration_version(proj["path"])
            latest = migrations[-1].number if migrations else 0
            status = "up to date" if v >= latest else f"{latest - v} pending"
            print(f"  {proj.get('slug', '?')}: v{v} ({status})")
        return

    # Run pending migrations
    for proj in projects:
        slug = proj.get("slug", "?")
        proj_path = proj["path"]
        current = _read_migration_version(proj_path)
        pending = [m for m in migrations if m.number > current]

        if not pending:
            print(f"  {slug}: up to date (v{current})")
            continue

        for migration in pending:
            if args.dry_run:
                print(f"  {slug}: would apply {migration.name}")
                continue

            print(f"  {slug}: applying {migration.name}...", end=" ")
            rc = _run_migration(migration, proj_path)
            if rc != 0:
                print(f"FAILED (exit {rc})")
                print(f"  Migration halted for {slug}.", file=sys.stderr)
                break
            print("ok")
            _write_migration_version(proj_path, migration.number)

        if not args.dry_run:
            _update_version_anchor(proj_path)


def _watch_thread(
    thread_id: str,
    since_id: str | None = None,
    timeout: float | None = None,
    exit_on_first_new: bool = False,
) -> int:
    """Blocking tail on a thread JSONL. Prints new messages as they arrive.

    Returns:
      0 on Ctrl-C / EOF / after the first new message if
        ``exit_on_first_new`` is True (graceful)
      1 if the thread file does not exist
      2 on timeout (when ``timeout`` is set and elapses with no new
        messages appearing)

    Used by ``claude-worker thread watch`` (continuous) and
    ``claude-worker thread wait`` (one-shot) — see D94, D95, #076, #075.
    """
    from claude_worker.thread_store import _threads_dir

    thread_file = _threads_dir() / f"{thread_id}.jsonl"
    if not thread_file.exists():
        print(f"Error: thread '{thread_id}' not found", file=sys.stderr)
        return 1

    printed_ids: set[str] = set()
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
                printed_ids.add(msg.get("id", ""))
                # When --since is given, stop prefilling after the marker
                # so subsequent messages are printed as "new".
                if since_id is not None and _uuid_matches(msg.get("id", ""), since_id):
                    break
    except OSError:
        pass

    deadline = (time.monotonic() + timeout) if timeout is not None else None
    try:
        while True:
            had_new = False
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
                        mid = msg.get("id", "")
                        if mid in printed_ids:
                            continue
                        printed_ids.add(mid)
                        had_new = True
                        print(_format_thread_message(msg), flush=True)
            except OSError:
                pass

            if had_new and exit_on_first_new:
                return 0

            if deadline is not None:
                if had_new:
                    # Reset the deadline on activity — `--timeout` is the
                    # idle ceiling, not a total wall-clock cap.
                    deadline = time.monotonic() + timeout
                elif time.monotonic() >= deadline:
                    return 2
            time.sleep(THREAD_WATCH_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        return 0


def cmd_thread(args: argparse.Namespace) -> None:
    """Manage conversation threads."""
    from claude_worker.thread_store import (
        append_message,
        close_thread,
        create_thread,
        list_threads,
        read_messages,
    )

    action = args.thread_action

    if action == "create":
        participants = [p.strip() for p in args.participants.split(",") if p.strip()]
        tid = create_thread(
            participants=participants,
            thread_type=args.thread_type,
            thread_id=getattr(args, "thread_id", None),
        )
        print(f"Thread created: {tid}")
        if participants:
            print(f"  participants: {', '.join(participants)}")

    elif action == "send":
        # Post-D95: `thread send` is the full-featured worker send.
        cmd_send(args)

    elif action == "read":
        # Post-D95: `thread read` is the full-featured worker read.
        cmd_read(args)

    elif action == "wait":
        # Post-D95: `thread wait` replaces `wait-for-turn` with dual
        # semantics (worker-name vs thread-id).
        cmd_wait_for_turn(args)

    elif action == "list":
        threads = list_threads(status=getattr(args, "status", None))
        if not threads:
            print("No threads.")
            return
        for t in threads:
            tid = t["thread_id"]
            status = t.get("status", "?")
            participants = ", ".join(t.get("participants", []))
            thread_type = t.get("type", "chat")
            last = t.get("last_message", "?")
            print(f"  {tid}  [{thread_type}:{status}]  {participants}  last: {last}")

    elif action == "close":
        close_thread(args.thread_id)
        print(f"Thread {args.thread_id} closed.")

    elif action == "watch":
        rc = _watch_thread(
            args.thread_id,
            since_id=getattr(args, "since", None),
            timeout=getattr(args, "timeout", None),
        )
        sys.exit(rc)

    else:
        print(
            "Usage: claude-worker thread {create|send|read|wait|list|close|watch}",
            file=sys.stderr,
        )
        sys.exit(1)


EXAMPLES = """\
examples:
  # Start a worker — blocks until claude responds, then prints status
  claude-worker start --name researcher --prompt "You are a research assistant"

  # Read the response
  claude-worker thread read researcher --last-turn

  # Send a message — blocks until claude responds
  claude-worker thread send researcher "summarize the architecture of this repo"
  claude-worker thread read researcher --last-turn

  # Follow output in real-time
  claude-worker thread read researcher --follow

  # List all workers
  claude-worker list

  # Chat with the worker interactively (turn-by-turn human REPL)
  claude-worker repl researcher

  # Check token usage (context window + session totals)
  claude-worker tokens researcher

  # Or just the current context window as a scriptable one-liner
  claude-worker thread read researcher --context

  # Stop and clean up
  claude-worker stop researcher

  # Start with a prompt file and extra claude args
  claude-worker start --name coder --cwd /path/to/repo \\
    --prompt-file instructions.md --prompt "begin with step 1" \\
    -- --model sonnet

  # Pipe a message via stdin
  cat question.txt | claude-worker thread send researcher

  # Broadcast to all waiting workers
  claude-worker broadcast --status waiting "heads up: CI is down"

  # Start without blocking
  claude-worker start --name bg-worker --prompt "you are a helper" --background

  # Use a custom agent (from ~/.claude/agents/)
  claude-worker start --name pm --agent project-manager \\
    --prompt "plan the auth module implementation"
"""


_ARGPARSE_UNRECOGNIZED_PREFIX: str = "unrecognized arguments:"

_ARGPARSE_POSITIONAL_POSTSCRIPT: str = (
    "\n\nThis usually means a shell-special character or option-like\n"
    "token leaked into the positional message. Pass the message via\n"
    "stdin to bypass argparse:\n"
    "\n"
    "    cat <<'EOF' | claude-worker thread send <name>\n"
    "    ...\n"
    "    EOF"
)


class ShellAwareParser(argparse.ArgumentParser):
    """ArgumentParser that augments ``unrecognized arguments`` errors with
    a postscript pointing at the canonical stdin/heredoc pattern (#092,
    D110).

    Subclasses ``argparse.ArgumentParser`` so that ``parse_args`` still
    exits 2 via ``self.exit(2, ...)`` and the standard error envelope is
    preserved — only the message text is augmented when the well-known
    "unrecognized arguments:" prefix appears.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        if message.startswith(_ARGPARSE_UNRECOGNIZED_PREFIX):
            message = message + _ARGPARSE_POSITIONAL_POSTSCRIPT
        super().error(message)


# Help-text epilog appended to thread-send and broadcast subparsers.
# ``RawDescriptionHelpFormatter`` preserves newlines so the heredoc
# example stays legible (#092, D110).
_STDIN_HINT_EPILOG: str = (
    "Tip: for messages with backticks, em-dashes, double-asterisks, or\n"
    "multi-line markdown, pass via stdin to avoid shell-quoting\n"
    "surprises:\n"
    "    cat <<'EOF' | claude-worker thread send NAME\n"
    "    ...message...\n"
    "    EOF"
)


def main():
    parser = ShellAwareParser(
        prog="claude-worker",
        description="Launch and communicate with Claude Code subprocess workers",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"claude-worker {__version__}",
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
        "--foreground",
        action="store_true",
        help="Run in the foreground (no daemonize). For systemd Type=simple.",
    )
    p_start.add_argument(
        "--remote",
        action="store_true",
        help="Enable CCR remote control. Injects a control_request after "
        "startup, prints session URL for connecting via the Claude mobile app. "
        "Composes with --foreground for systemd deployment.",
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
        "--identity",
        metavar="NAME",
        help="Launch with an identity from ~/.cwork/identities/<name>/identity.md. "
        "Built-in identities: pm, technical-lead.",
    )
    p_start_identity.add_argument(
        "--pm",
        action="store_true",
        help="Shorthand for --identity pm — loads the PM identity "
        "and enables chat-tag routing for multi-consumer coordination",
    )
    p_start_identity.add_argument(
        "--team-lead",
        action="store_true",
        help="Shorthand for --identity technical-lead — loads the TL identity "
        "for code review and delegation",
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
        "--ephemeral",
        action="store_true",
        help="Mark the worker as short-lived. The manager reaps it after "
        "--ephemeral-idle-timeout seconds of log inactivity (default "
        f"{EPHEMERAL_IDLE_TIMEOUT_SECONDS}s). Use instead of Task tool "
        "for long-running delegation — the delegating worker stays "
        "responsive because claude-worker start is non-blocking.",
    )
    p_start.add_argument(
        "--ephemeral-idle-timeout",
        type=int,
        default=EPHEMERAL_IDLE_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"Idle timeout for --ephemeral workers (default "
        f"{EPHEMERAL_IDLE_TIMEOUT_SECONDS}s). Only meaningful with "
        "--ephemeral.",
    )
    p_start.add_argument(
        "claude_args",
        nargs="*",
        metavar="CLAUDE_ARGS",
        help="Additional args passed to claude (use -- before these)",
    )

    # -- broadcast -- (top-level per D95, extracted from `send --broadcast`)
    p_broadcast = sub.add_parser(
        "broadcast",
        help="Send a message to all workers matching filter flags",
        epilog=_STDIN_HINT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_broadcast.add_argument(
        "message", nargs="*", help="Message text (reads stdin if omitted)"
    )
    p_broadcast.add_argument(
        "--role",
        choices=["pm", "tl", "worker"],
        help="Filter targets by identity role",
    )
    p_broadcast.add_argument(
        "--status",
        choices=["working", "waiting", "dead", "starting"],
        help="Filter targets by status",
    )
    p_broadcast.add_argument(
        "--alive",
        action="store_true",
        help="Exclude dead workers from targets",
    )
    p_broadcast.add_argument(
        "--cwd",
        dest="cwd_filter",
        metavar="PATH",
        help="Filter targets by CWD prefix",
    )
    p_broadcast.add_argument(
        "--queue",
        action="store_true",
        help="Embed a correlation ID and wait for per-target tagged responses",
    )
    p_broadcast.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent without writing to any thread",
    )
    p_broadcast.add_argument(
        "--verbose",
        action="store_true",
        help="Print each per-target envelope to stderr before sending",
    )
    p_broadcast.add_argument(
        "--show-response",
        action="store_true",
        help="After each target's turn completes, print its response",
    )
    p_broadcast.add_argument(
        "--show-full-response",
        action="store_true",
        help="After each target's turn completes, print everything new",
    )
    p_broadcast_chat = p_broadcast.add_mutually_exclusive_group()
    p_broadcast_chat.add_argument(
        "--chat",
        metavar="ID",
        help="Prepend [chat:<id>] to every sent message (PM targets only)",
    )
    p_broadcast_chat.add_argument(
        "--all-chats",
        action="store_true",
        help="Bypass automatic chat tagging",
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
        help="Replace the current worker with a fresh instance "
        "(clean context, no conversation carryover). Auto-detects "
        "which worker is calling via PID ancestry. Continuity is "
        "via the handoff file, not Claude Code --resume.",
    )
    p_replace.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip wrap-up validation checks (handoff file, turn state). "
        "Use when the worker is stuck or the human is supervising.",
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
    p_repl.add_argument(
        "--continuous",
        "-c",
        action="store_true",
        help="Continuous output mode: messages flow like tail -f, press Enter to type. "
        "No prompt shown by default — input appears on demand.",
    )
    p_repl.add_argument(
        "--tui",
        action="store_true",
        help="Non-blocking TUI mode: input pinned at the bottom, output "
        "scrolls above, messages stream while input stays editable. "
        "Requires a TTY. Mutually exclusive with --continuous.",
    )

    # -- tokens --
    p_tokens = sub.add_parser(
        "tokens",
        help="Print token stats for a worker (context window + session totals)",
    )
    p_tokens.add_argument("name", help="Worker name")

    # -- projects --
    sub.add_parser(
        "projects",
        help="List registered projects with active workers and ticket counts",
    )

    # -- stats --
    sub.add_parser(
        "stats",
        help="Print summary statistics from session analyses (cost, tokens, per identity/project)",
    )

    # -- subagents (#083, D100) --
    p_subagents = sub.add_parser(
        "subagents",
        help="Summarize Claude Code subagents launched by a worker's session",
    )
    p_subagents.add_argument("name", help="Worker name")
    p_subagents.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    p_subagents.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Show only the N most-recently-active subagents",
    )

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

    # -- migrate --
    p_migrate = sub.add_parser(
        "migrate", help="Run pending migrations on registered projects"
    )
    p_migrate.add_argument(
        "--project",
        metavar="PATH",
        help="Run migrations on this project only (default: all registered)",
    )
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending migrations without running them",
    )
    p_migrate.add_argument(
        "--list",
        dest="list_migrations",
        action="store_true",
        help="List available migrations and project versions",
    )

    # -- Discoverability commands (#071, D86, main:P10) --
    p_version = sub.add_parser("version", help="Print the claude-worker version")
    p_version.set_defaults(func=cmd_version)

    p_changelog = sub.add_parser("changelog", help="Print the changelog")
    p_changelog.add_argument(
        "--since",
        metavar="VERSION",
        help="Only show entries newer than this version",
    )
    p_changelog.set_defaults(func=cmd_changelog)

    p_docs = sub.add_parser("docs", help="Print path to README.md")
    p_docs.set_defaults(func=cmd_docs)

    p_skill = sub.add_parser(
        "skill", help="Print path to the installed claude-worker skill"
    )
    p_skill.set_defaults(func=cmd_skill)

    # -- thread --
    p_thread = sub.add_parser("thread", help="Manage conversation threads")
    thread_sub = p_thread.add_subparsers(dest="thread_action")

    p_thread_create = thread_sub.add_parser("create", help="Create a new thread")
    p_thread_create.add_argument(
        "--participants",
        "-p",
        help="Comma-separated participant names",
        default="",
    )
    p_thread_create.add_argument(
        "--type",
        dest="thread_type",
        default="chat",
        help="Thread type (chat, request, design)",
    )
    p_thread_create.add_argument(
        "--id",
        dest="thread_id",
        help="Explicit thread ID (auto-generated if omitted)",
    )

    # thread send — full-flag send (post-D95, replaces top-level `send`)
    p_thread_send = thread_sub.add_parser(
        "send",
        help="Send a message to a worker (or known thread participant)",
        epilog=_STDIN_HINT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_thread_send.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Worker name (or known thread participant)",
    )
    p_thread_send.add_argument(
        "message", nargs="*", help="Message text (reads stdin if omitted)"
    )
    p_thread_send.add_argument(
        "--queue",
        action="store_true",
        help="Send even if worker is busy; embed a correlation ID and wait "
        "for the specific tagged response",
    )
    p_thread_send.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the JSON envelope that would be sent without writing",
    )
    p_thread_send.add_argument(
        "--verbose",
        action="store_true",
        help="Print the JSON envelope to stderr before sending",
    )
    p_thread_send.add_argument(
        "--show-response",
        action="store_true",
        help="After the turn completes, print the assistant's response",
    )
    p_thread_send.add_argument(
        "--show-full-response",
        action="store_true",
        help="After the turn completes, print everything new since the send",
    )
    p_thread_send_chat = p_thread_send.add_mutually_exclusive_group()
    p_thread_send_chat.add_argument(
        "--chat",
        metavar="ID",
        help="Prepend [chat:<id>] to the message (PM workers only)",
    )
    p_thread_send_chat.add_argument(
        "--all-chats",
        action="store_true",
        help="Bypass any automatic chat tagging (no-op for non-PM workers)",
    )

    # thread read — full-flag read (post-D95, replaces top-level `read`)
    p_thread_read = thread_sub.add_parser(
        "read", help="Read worker output (thread or raw log)"
    )
    p_thread_read.add_argument("name", help="Worker name")
    p_thread_read.add_argument(
        "--follow", "-f", action="store_true", help="Tail the log"
    )
    p_thread_read.add_argument(
        "--since", help="Show messages after this UUID or timestamp"
    )
    p_thread_read.add_argument(
        "--log",
        action="store_true",
        help="Read the raw claude session log instead of the thread. "
        "Debugging escape hatch — threads are the default (post-D88).",
    )
    p_thread_read.add_argument(
        "--thread",
        metavar="ID",
        help="Override auto-detected thread ID (e.g. 'pair-pm-tl' or 'chat-abc')",
    )
    p_thread_read.add_argument(
        "--until", help="Stop showing messages at this UUID (exclusive)"
    )
    p_thread_read.add_argument(
        "--new",
        action="store_true",
        help="Show only messages after the last --mark (per-consumer)",
    )
    p_thread_read.add_argument(
        "--mark",
        action="store_true",
        help="After displaying, save the last-seen UUID as a read marker",
    )
    p_thread_read.add_argument(
        "--last-turn",
        action="store_true",
        help="Show the most recent conversational exchange",
    )
    p_thread_read.add_argument(
        "--exclude-user",
        action="store_true",
        help="Hide user-input messages from the display (default shows them)",
    )
    p_thread_read.add_argument(
        "-n", type=int, metavar="N", help="Show only the last N messages"
    )
    p_thread_read.add_argument(
        "--count",
        action="store_true",
        help="Print the number of messages instead of content",
    )
    p_thread_read.add_argument(
        "--summary",
        action="store_true",
        help="Show one-line summary per message: [uuid] ROLE: preview",
    )
    p_thread_read.add_argument(
        "--context",
        action="store_true",
        help="Print current context window usage (e.g. '77%% (776k/1M)') and exit",
    )
    p_thread_read.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Include tool calls, tool results, and thinking blocks",
    )
    p_thread_read.add_argument(
        "--color", action="store_true", help="Force ANSI color output"
    )
    p_thread_read.add_argument(
        "--no-color",
        action="store_true",
        help="Force plain text output (default when CLAUDECODE is set)",
    )
    p_thread_read_chat = p_thread_read.add_mutually_exclusive_group()
    p_thread_read_chat.add_argument(
        "--chat",
        metavar="ID",
        help="Filter to messages containing [chat:<id>] (PM workers only)",
    )
    p_thread_read_chat.add_argument(
        "--all-chats",
        action="store_true",
        help="Show all chats — bypass automatic chat filtering",
    )

    # thread wait — dual-semantic wait (post-D95, replaces top-level `wait-for-turn`)
    p_thread_wait = thread_sub.add_parser(
        "wait",
        help="Block until a turn boundary (worker) or next thread message (thread-id)",
    )
    p_thread_wait.add_argument(
        "name", help="Worker name, or thread-id (prefix 'pair-' or 'chat-')"
    )
    p_thread_wait.add_argument("--timeout", type=float, help="Timeout in seconds")
    p_thread_wait.add_argument(
        "--after-uuid",
        metavar="UUID",
        help="Ignore log entries up to and including this UUID",
    )
    p_thread_wait.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE_SECONDS,
        metavar="SECONDS",
        help=f"Settle window after turn boundary (default: {DEFAULT_SETTLE_SECONDS})",
    )
    p_thread_wait.add_argument(
        "--chat",
        metavar="TAG",
        help="Only fire when assistant content contains [chat:<tag>]",
    )

    p_thread_list = thread_sub.add_parser("list", help="List all threads")
    p_thread_list.add_argument("--status", help="Filter by status (open, closed)")

    p_thread_close = thread_sub.add_parser("close", help="Close a thread")
    p_thread_close.add_argument("thread_id", help="Thread ID")

    p_thread_watch = thread_sub.add_parser(
        "watch", help="Tail a thread — block until new messages arrive"
    )
    p_thread_watch.add_argument("thread_id", help="Thread ID")
    p_thread_watch.add_argument(
        "--since",
        help="Resume after this message ID (print messages strictly after it)",
    )
    p_thread_watch.add_argument(
        "--timeout",
        type=float,
        help="Exit with code 2 after this many idle seconds (no new messages)",
    )

    p_thread.set_defaults(func=cmd_thread)

    args = parser.parse_args()

    handlers = {
        "start": cmd_start,
        "broadcast": cmd_broadcast,
        "list": cmd_list,
        "ls": cmd_list,
        "stop": cmd_stop,
        "replaceme": cmd_replaceme,
        "notify": cmd_notify,
        "install-hook": cmd_install_hook,
        "repl": cmd_repl,
        "tokens": cmd_tokens,
        "stats": cmd_stats,
        "subagents": cmd_subagents,
        "projects": cmd_projects,
        "grant": cmd_grant,
        "grants": cmd_grants,
        "revoke": cmd_revoke,
        "migrate": cmd_migrate,
        "thread": cmd_thread,
        "version": cmd_version,
        "changelog": cmd_changelog,
        "docs": cmd_docs,
        "skill": cmd_skill,
    }
    handlers[args.command](args)
