"""Stop hook that checks context window usage after each turn.

Fires once per session when usage crosses CONTEXT_WAKEUP_THRESHOLD_PCT.
Echoes a warning to stdout which Claude Code shows to the agent.

Usage (wired automatically by cmd_start via per-worker settings.json):

    python -m claude_worker.context_threshold --sentinel-dir /path/to/dir

Reads the Stop hook JSON payload from stdin. Extracts ``transcript_path``
to compute context usage via ``claude_logs.compute_context_window_usage``.

Exit codes:
    0 — success (stdout may contain a warning message)
    Other — non-blocking error (Claude Code ignores and continues)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

CONTEXT_WAKEUP_THRESHOLD_PCT: float = 0.80

# Context window size detection constants
_CONTEXT_WINDOW_1M: int = 1_000_000
_CONTEXT_WINDOW_DEFAULT: int = 200_000


def _detect_context_window_size(log_file: Path) -> int:
    """Read the log's system/init message to determine context window size.

    Models with ``[1m]`` suffix use a 1M context window; all others
    default to 200K. Falls back to 1M on error (underestimates
    percentage, which is the safer failure mode).
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sentinel-dir",
        required=True,
        help="Directory for the one-shot sentinel file",
    )
    args = parser.parse_args()

    # Read hook input from stdin
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # Prevent Stop hook loops
    if payload.get("stop_hook_active"):
        sys.exit(0)

    sentinel_dir = Path(args.sentinel_dir)
    sentinel = sentinel_dir / "wakeup-context-sent"

    # One-shot: bail if already fired
    if sentinel.exists():
        sys.exit(0)

    transcript_path = payload.get("transcript_path", "")
    if not transcript_path:
        sys.exit(0)

    log_file = Path(transcript_path)
    if not log_file.exists():
        sys.exit(0)

    # Compute context usage — best-effort, never crash the hook
    try:
        from claude_logs import compute_context_window_usage
    except ImportError:
        sys.exit(0)

    try:
        cw = compute_context_window_usage(log_file)
    except Exception:
        sys.exit(0)
    if cw is None:
        sys.exit(0)

    window = _detect_context_window_size(log_file)
    pct = cw.total / window
    if pct < CONTEXT_WAKEUP_THRESHOLD_PCT:
        sys.exit(0)

    # Threshold crossed — echo warning to stdout (Claude sees it)
    pct_display = int(pct * 100)
    print(
        f"[system:context-threshold] You are at approximately "
        f"{pct_display}% of your context window. Begin your "
        f"wrap-up procedure now."
    )

    # Write sentinel to prevent re-fire
    try:
        sentinel_dir.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(str(time.time()))
    except OSError:
        pass


if __name__ == "__main__":
    main()
