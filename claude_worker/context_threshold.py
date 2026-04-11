"""Stop hook that checks context window usage after each turn.

Fires at three thresholds:
- 50%: [system:context-warning] delegation reminder
- 65%: [system:context-warning] stronger delegation + wrap-up recommendation
- 80%: [system:context-threshold] begin wrap-up procedure

Each threshold fires at most once per session (sentinel files).

Usage (wired automatically by cmd_start via per-worker settings.json):

    python -m claude_worker.context_threshold --sentinel-dir /path/to/dir

Reads the Stop hook JSON payload from stdin. Extracts ``transcript_path``
to compute context usage via ``claude_logs.compute_context_window_usage``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Thresholds: (percentage, sentinel_name, message)
THRESHOLDS: list[tuple[float, str, str]] = [
    (
        0.50,
        "context-warning-50",
        "[system:context-warning] You are at approximately {pct}% of your "
        "context window. Delegate more, implement less — each tool call "
        "output consumes context. Delegation costs ~1% of context; direct "
        "implementation costs 5-10%.",
    ),
    (
        0.65,
        "context-warning-65",
        "[system:context-warning] You are at approximately {pct}% of your "
        "context window. Consider wrapping up your current task and "
        "delegating remaining work to a sub-worker. Context pressure "
        "increases error rate and compaction risk.",
    ),
    (
        0.80,
        "wakeup-context-sent",
        "[system:context-threshold] You are at approximately {pct}% of "
        "your context window. Begin your wrap-up procedure now.",
    ),
]

# Context window size detection constants
_CONTEXT_WINDOW_1M: int = 1_000_000
_CONTEXT_WINDOW_DEFAULT: int = 200_000


def _detect_context_window_size(log_file: Path) -> int:
    """Read the log's system/init message to determine context window size."""
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
        help="Directory for the one-shot sentinel files",
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

    transcript_path = payload.get("transcript_path", "")
    if not transcript_path:
        sys.exit(0)

    log_file = Path(transcript_path)
    if not log_file.exists():
        sys.exit(0)

    # Compute context usage
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

    sentinel_dir = Path(args.sentinel_dir)

    # Check thresholds from lowest to highest — fire the highest crossed
    # threshold that hasn't been fired yet
    for threshold_pct, sentinel_name, message_template in THRESHOLDS:
        if pct < threshold_pct:
            continue
        sentinel = sentinel_dir / sentinel_name
        if sentinel.exists():
            continue

        # Threshold crossed — echo warning and write sentinel
        pct_display = int(pct * 100)
        print(message_template.format(pct=pct_display))

        try:
            sentinel_dir.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(str(time.time()))
        except OSError:
            pass

    # Write sentinel to prevent re-fire
    try:
        sentinel_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


if __name__ == "__main__":
    main()
