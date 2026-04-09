"""PostToolUse hook that notifies PM/TL workers when ticket files change.

Fires after Write/Edit/MultiEdit on files under <cwd>/.cwork/tickets/.
Discovers other identity workers (PM/TL) with the same CWD and sends
them a notification via ``claude-worker send --queue``.

Usage (wired automatically by cmd_start via per-worker settings.json):

    python -m claude_worker.ticket_watcher --cwd /path/to/project
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TICKET_CHANGE_COOLDOWN_SECONDS: float = 5.0


def _extract_ticket_info(file_path: str, cwd: str) -> dict | None:
    """Parse ticket metadata from the file path.

    Returns None if the path is not under .cwork/tickets/.
    """
    tickets_dir = os.path.join(cwd, ".cwork", "tickets")
    resolved = os.path.realpath(file_path)
    resolved_tickets = os.path.realpath(tickets_dir)

    if (
        not resolved.startswith(resolved_tickets + os.sep)
        and resolved != resolved_tickets
    ):
        return None

    rel = os.path.relpath(resolved, resolved_tickets)
    parts = rel.split(os.sep)

    if parts[0] == "INDEX.md":
        return {"action": "index updated", "ticket_id": None, "file": "INDEX.md"}

    # Pattern: <id>-<slug>/<filename>
    match = re.match(r"^(\d+)-(.+)$", parts[0])
    if match and len(parts) >= 2:
        ticket_id = match.group(1)
        slug = match.group(2)
        filename = parts[-1]
        if filename == "TICKET.md":
            action = "created or updated"
        elif filename == "TECHNICAL.md":
            action = "technical notes updated"
        elif filename == "REVIEW.md":
            action = "review notes updated"
        else:
            action = f"{filename} updated"
        return {
            "action": action,
            "ticket_id": ticket_id,
            "slug": slug,
            "file": filename,
        }

    return {"action": "file changed", "ticket_id": None, "file": rel}


def _find_notification_targets(cwd: str, exclude_pid: int | None = None) -> list[str]:
    """Find PM/TL workers with the same CWD to notify.

    Scans .sessions.json for workers with pm=True or team_lead=True
    whose CWD matches. Excludes the caller's own worker (by claude-pid).
    """
    from claude_worker.manager import _load_sessions, get_runtime_dir

    sessions = _load_sessions()
    targets = []
    for name, meta in sessions.items():
        if not isinstance(meta, dict):
            continue
        if not (meta.get("pm") or meta.get("team_lead")):
            continue
        worker_cwd = meta.get("cwd", "")
        if os.path.realpath(worker_cwd) != os.path.realpath(cwd):
            continue
        # Exclude self
        if exclude_pid is not None:
            runtime = get_runtime_dir(name)
            claude_pid_file = runtime / "claude-pid"
            if claude_pid_file.exists():
                try:
                    if int(claude_pid_file.read_text().strip()) == exclude_pid:
                        continue
                except (ValueError, OSError):
                    pass
        # Check worker is alive
        pid_file = get_runtime_dir(name) / "pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)  # alive check
                targets.append(name)
            except (ValueError, OSError, ProcessLookupError):
                continue
    return targets


def _check_cooldown(cwd: str, target: str) -> bool:
    """Return True if we should send (cooldown elapsed), False if too recent."""
    cooldown_dir = Path.home() / ".cwork" / "ticket-watch-cooldowns"
    cooldown_dir.mkdir(parents=True, exist_ok=True)
    import hashlib

    key = hashlib.md5(f"{cwd}:{target}".encode()).hexdigest()
    cooldown_file = cooldown_dir / key
    if cooldown_file.exists():
        try:
            last = float(cooldown_file.read_text().strip())
            if time.time() - last < TICKET_CHANGE_COOLDOWN_SECONDS:
                return False
        except (ValueError, OSError):
            pass
    try:
        cooldown_file.write_text(str(time.time()))
    except OSError:
        pass
    return True


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="claude_worker.ticket_watcher")
    parser.add_argument("--cwd", required=True, help="Worker's CWD")
    args = parser.parse_args()

    # Read hook payload
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path", "")
    if not file_path:
        # MultiEdit: check first edit
        edits = tool_input.get("edits", [])
        if edits and isinstance(edits, list) and isinstance(edits[0], dict):
            file_path = edits[0].get("file_path", "")
    if not file_path:
        sys.exit(0)

    # Check if it's a ticket file
    info = _extract_ticket_info(file_path, args.cwd)
    if info is None:
        sys.exit(0)

    # Find ancestor claude PID for self-exclusion
    ancestor_pid = None
    try:
        ppid = os.getppid()
        with open(f"/proc/{ppid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    ancestor_pid = int(line.split()[1])
                    break
    except (OSError, ValueError):
        pass

    targets = _find_notification_targets(args.cwd, exclude_pid=ancestor_pid)
    if not targets:
        sys.exit(0)

    # Build notification message
    if info.get("ticket_id"):
        msg = (
            f"[system:ticket-change] Ticket #{info['ticket_id']} "
            f"({info.get('slug', '')}) was {info['action']}."
        )
    else:
        msg = f"[system:ticket-change] Ticket {info['action']}."

    # Send to each target (fire-and-forget via subprocess)
    for target in targets:
        if not _check_cooldown(args.cwd, target):
            continue
        try:
            subprocess.Popen(
                ["claude-worker", "send", target, "--queue", msg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
