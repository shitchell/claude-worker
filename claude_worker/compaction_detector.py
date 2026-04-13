"""SessionStart hook that detects compaction events.

When Claude Code compacts, it fires a SessionStart event with
matcher value "compact". This hook:
1. Echoes a re-bootstrap instruction to stdout
2. Logs the event to .cwork/roles/<role>/LOG.md
3. Fires claude-worker notify if configured
4. Notes that analyze-session and wrap-up were SKIPPED

Usage (wired automatically via per-worker settings.json):

    python -m claude_worker.compaction_detector --identity <name> --cwd <path>
"""

# IMPORTANT: Compaction detection in claude-worker has two layers:
#
# 1. Hook-based (this file): SessionStart fires with matcher_value="compact"
#    when Claude Code compacts. This is the REAL-TIME detection mechanism.
#
# 2. Log-based: The JSONL log contains compact_boundary messages:
#    {"type": "system", "subtype": "compact_boundary",
#     "compactMetadata": {"trigger": "manual"|"auto", "preTokens": <int>}}
#    Use this for post-hoc analysis, not system/init (which fires every turn
#    in -p stream-json mode and is NOT a compaction indicator).

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# Role directory names — mirrors IDENTITY_ROLE_DIRS in cli.py.
# Duplicated here to avoid circular imports (compaction_detector is a
# standalone hook entry point that cannot import from cli).
_ROLE_DIRS: dict[str, str] = {"technical-lead": "tl"}


def _role_dir(identity: str) -> str:
    """Map an identity name to its role directory name."""
    return _ROLE_DIRS.get(identity, identity)


def _log_compaction(cwd: str, identity: str) -> None:
    """Append compaction event to the identity's LOG.md."""
    log_file = Path(cwd) / ".cwork" / "roles" / _role_dir(identity) / "LOG.md"
    if not log_file.parent.exists():
        return
    try:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entry = (
            f"{timestamp} | COMPACTION | Context compacted. "
            f"analyze-session and wrap-up were SKIPPED.\n"
        )
        with open(log_file, "a") as f:
            f.write(entry)
    except OSError:
        pass


def _notify_compaction(identity: str) -> None:
    """Fire claude-worker notify if configured."""
    try:
        subprocess.run(
            [
                "claude-worker",
                "notify",
                f"[compaction] {identity} worker context was compacted. "
                f"Analyze-session and wrap-up were skipped.",
            ],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", default="worker")
    parser.add_argument("--cwd", default=".")
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    matcher_value = payload.get("matcher_value", "")
    if matcher_value != "compact":
        sys.exit(0)

    identity = args.identity

    # Log the compaction event
    _log_compaction(args.cwd, identity)

    # Notify human if configured
    _notify_compaction(identity)

    # Echo re-bootstrap instruction
    print(
        f"[system:compaction-detected] Your context was just compacted. "
        f"Prior conversation content has been compressed. "
        f"IMPORTANT: analyze-session and wrap-up were SKIPPED — they cannot "
        f"run retroactively on compacted context. "
        f"To recover: (1) re-read your identity file, (2) re-read the "
        f"latest handoff at .cwork/roles/{_role_dir(identity)}/handoffs/, (3) verify your "
        f"current work by reading ticket files, (4) report status to confirm "
        f"you've re-bootstrapped successfully."
    )


if __name__ == "__main__":
    main()
