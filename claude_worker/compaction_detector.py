"""SessionStart hook that detects compaction events.

When Claude Code compacts, it fires a SessionStart event with
matcher value "compact". This hook echoes a re-bootstrap instruction
to stdout, which Claude sees as hook output.

Usage (wired automatically via per-worker settings.json):

    python -m claude_worker.compaction_detector --identity <name>
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", default="worker")
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # Only fire on compaction events
    # SessionStart matcher values: startup, resume, clear, compact
    # The hook fires for all of them, but we only care about compact
    matcher_value = payload.get("matcher_value", "")
    if matcher_value != "compact":
        sys.exit(0)

    # Compaction detected — instruct the agent to re-bootstrap
    identity = args.identity
    print(
        f"[system:compaction-detected] Your context was just compacted. "
        f"Prior conversation content has been compressed. To maintain "
        f"quality: (1) re-read your identity guidance if you're unsure "
        f"of your behavioral rules, (2) re-read relevant ticket files "
        f"before continuing implementation, (3) verify assumptions by "
        f"reading code rather than relying on conversation memory."
    )


if __name__ == "__main__":
    main()
