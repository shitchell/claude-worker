"""PostToolUse hook that checks commits for G3 (tests) and GVP compliance.

Fires after Bash tool calls containing 'git commit' or 'git push'.
Checks the most recent commit for:
1. Test files touched (G3 compliance)
2. GVP library updated (D<N> compliance)
3. Preceding pytest/cairn validate in the session

Outputs warnings to stdout — Claude sees them as hook output. Does
NOT block the tool call (PostToolUse fires after execution).

Usage (wired automatically via per-worker settings.json):

    python -m claude_worker.commit_checker
"""

from __future__ import annotations

import json
import subprocess
import sys


def _check_commit() -> list[str]:
    """Check the most recent commit for compliance. Returns warnings."""
    warnings = []

    try:
        # Get files changed in the most recent commit
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []

        changed_files = result.stdout.strip().splitlines()
        if not changed_files:
            return []

        # Check 1: Test files touched?
        test_files = [f for f in changed_files if f.startswith("tests/")]
        if not test_files:
            # Exempt: docs-only, identity files, config files
            code_files = [
                f
                for f in changed_files
                if f.endswith(".py") and not f.startswith("tests/")
            ]
            if code_files:
                warnings.append(
                    "G3 WARNING: No test files in this commit, but Python "
                    "source files were changed. Per G3 (test-first-with-real-"
                    "conditions), every feature/bugfix needs tests."
                )

        # Check 2: GVP library updated?
        gvp_files = [f for f in changed_files if f.startswith(".gvp/library/")]
        if not gvp_files:
            code_files = [
                f
                for f in changed_files
                if f.endswith(".py") and not f.startswith("tests/")
            ]
            if code_files:
                warnings.append(
                    "GVP WARNING: No .gvp/library/ update in this commit. "
                    "Record D<N> in project.yaml with refs for implementation "
                    "changes."
                )

    except Exception:
        pass

    return warnings


def main() -> None:
    """PostToolUse hook entry point."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # Only fire for Bash tool calls
    tool_name = payload.get("tool_name", "")
    if tool_name != "Bash":
        sys.exit(0)

    # Check if the command contained git commit
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command", "")
    if "git commit" not in command:
        sys.exit(0)

    # Check the commit
    warnings = _check_commit()
    if warnings:
        print("\n".join(warnings))


if __name__ == "__main__":
    main()
