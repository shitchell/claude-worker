"""SessionStart hook that re-injects identity context.

On compact/clear: echoes the FULL identity.md + ticket summary + GVP count.
On startup/resume: echoes lighter summary (tickets + GVP only).

Usage (wired automatically via per-worker settings.json):

    python -m claude_worker.identity_reinjector --identity <name> --cwd <path>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_identity_text(identity: str) -> str:
    """Load identity.md from user dir or bundled."""
    user_path = Path.home() / ".cwork" / "identities" / identity / "identity.md"
    if user_path.exists():
        return user_path.read_text()
    try:
        from importlib.resources import files

        bundled = {"pm": "pm.md", "technical-lead": "technical-lead.md"}
        if identity in bundled:
            return (
                files("claude_worker") / "identities" / bundled[identity]
            ).read_text()
    except Exception:
        pass
    return ""


def _get_ticket_summary(cwd: str) -> str:
    """Count open tickets from INDEX.md."""
    index = Path(cwd) / ".cwork" / "tickets" / "INDEX.md"
    if not index.exists():
        return "No ticket system found."
    try:
        todo = active = done = 0
        for line in index.read_text().splitlines():
            if "| todo |" in line:
                todo += 1
            elif "| active |" in line:
                active += 1
            elif "| done |" in line:
                done += 1
        return f"Tickets: {todo} todo, {active} active, {done} done"
    except OSError:
        return "Could not read INDEX.md"


def _get_gvp_summary(cwd: str) -> str:
    """Count GVP elements."""
    gvp_file = Path(cwd) / ".gvp" / "library" / "project.yaml"
    if not gvp_file.exists():
        return ""
    try:
        text = gvp_file.read_text()
        decisions = text.count("  - id: D")
        goals = text.count("  - id: G")
        principles = text.count("  - id: P")
        values = text.count("  - id: V")
        return f"GVP: {goals}G, {values}V, {principles}P, {decisions}D"
    except OSError:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", required=True)
    parser.add_argument("--cwd", required=True)
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    event_type = payload.get("matcher_value", "startup")
    ticket_summary = _get_ticket_summary(args.cwd)
    gvp_summary = _get_gvp_summary(args.cwd)

    lines = [
        f"[system:identity-context] Identity: {args.identity} | Event: {event_type}",
        ticket_summary,
    ]
    if gvp_summary:
        lines.append(gvp_summary)

    # Full identity re-injection on compact/clear (context was lost)
    if event_type in ("compact", "clear"):
        identity_text = _load_identity_text(args.identity)
        if identity_text:
            lines.append("")
            lines.append("=== IDENTITY GUIDANCE (re-injected after context change) ===")
            lines.append(identity_text)

    print("\n".join(lines))


if __name__ == "__main__":
    main()
