"""PreToolUse hook that denies Write/Edit/MultiEdit calls targeting
files outside the worker's CWD.

Prevents workers from modifying files they don't own. Changes to files
outside the CWD should be routed through the owning PM or done in a
worker whose CWD contains the target.

Reading outside CWD is unrestricted.

Usage (wired automatically by cmd_start via per-worker settings.json):

    python -m claude_worker.cwd_guard --cwd /path/to/worker/cwd

Reads the PreToolUse JSON payload from stdin. If the target file_path
is not within --cwd, prints a deny decision on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_GUARDED_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit"})


def _build_deny_decision(reason: str) -> dict:
    """Build a PreToolUse deny decision."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _is_within(target: Path, cwd: Path) -> bool:
    """Check if target is within the cwd subtree."""
    try:
        target_resolved = target.resolve()
        cwd_resolved = cwd.resolve()
        return target_resolved == cwd_resolved or str(target_resolved).startswith(
            str(cwd_resolved) + os.sep
        )
    except (OSError, ValueError):
        return False


def _get_target_path(tool_name: str, tool_input: dict) -> str | None:
    """Extract the target file path from the tool input."""
    if tool_name in ("Edit", "Write"):
        return tool_input.get("file_path")
    if tool_name == "MultiEdit":
        # MultiEdit has an edits array, each with file_path
        edits = tool_input.get("edits", [])
        if edits and isinstance(edits, list):
            # Check the first edit's path — all edits in a MultiEdit
            # should target the same file, but check all to be safe
            for edit in edits:
                if isinstance(edit, dict):
                    path = edit.get("file_path")
                    if path:
                        return path
    return None


def main(
    argv: list[str] | None = None,
    stdin=None,
    stdout=None,
) -> int:
    """Hook entry point."""
    parser = argparse.ArgumentParser(prog="claude_worker.cwd_guard")
    parser.add_argument(
        "--cwd",
        type=Path,
        required=True,
        help="Worker's CWD — writes outside this subtree are denied",
    )
    args = parser.parse_args(argv)

    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout

    raw = in_stream.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool_name not in _GUARDED_TOOLS:
        return 0

    target_path = _get_target_path(tool_name, tool_input)
    if target_path is None:
        return 0

    if _is_within(Path(target_path), args.cwd):
        return 0

    # Target is outside CWD — deny
    decision = _build_deny_decision(
        f"Write denied: {target_path} is outside this worker's CWD "
        f"({args.cwd}). Route changes to files outside your CWD "
        f"through the owning PM or use a worker whose CWD contains "
        f"the target."
    )
    out_stream.write(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
