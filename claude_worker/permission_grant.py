"""PreToolUse hook that applies pre-authorized Edit/Write/MultiEdit
operations on behalf of a worker whose real tool call would be blocked
by Claude Code's sensitive-file gate.

## Why this exists

Claude Code gates Edit/Write tool calls targeting certain paths (notably
``.claude/**`` anywhere on disk) with a "sensitive file" denial. This
gate fires even with ``--dangerously-skip-permissions`` /
``--permission-mode bypassPermissions``, and it runs *after* the normal
PreToolUse ``permissionDecision: "allow"`` path, so it cannot be bypassed
by returning ``allow`` from a hook.

The interactive UI has an approval button the user can click; the
``-p`` stream-json mode used by claude-worker has no such affordance, so
a claude-worker-driven Claude just sees the denial and gives up. This
made batch edits into ``~/.claude/skills/**`` impossible from the
orchestrator.

## The workaround

The sensitive-file gate is inside claude's tool executor — it doesn't
affect filesystem operations performed by *subprocesses* of claude
(like hooks). A PreToolUse hook can therefore apply the edit itself
via normal Python file I/O, then return ``permissionDecision: "deny"``
with a friendly reason like "edit applied by hook, do not retry". Claude
reads the denial-as-tool-result and — empirically — understands what
happened and does not retry. The file ends up modified and the turn
proceeds.

## Security model

This hook does NOT blindly apply every Edit/Write it sees. It consults
a per-worker ``grants.jsonl`` file that the operator populates via
``claude-worker grant``. Only operations that match a non-consumed grant
are applied; anything else falls through to the normal sensitive-file
deny path (the hook exits silently with no JSON on stdout). One-shot
grants (``persistent: false``, the default) are marked consumed after
the first match — so a single ``grant`` call authorizes exactly one
edit.

## Invocation

This module is run as a subprocess by Claude Code's hook runner. Claude
passes the PreToolUse JSON payload on stdin, and the hook prints its
decision JSON on stdout (or nothing, to decline to influence the
decision). The grants file path is passed via ``--grants-file``. Usage::

    python3 -m claude_worker.permission_grant --grants-file <path>

The per-worker ``settings.json`` generated at worker-start resolves the
python executable via ``sys.executable`` at that moment, so the hook
runs under the same interpreter (and the same editable install) as the
claude-worker that spawned it.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import IO

# -- Named constants ---------------------------------------------------------

# Tools that are subject to the sensitive-file gate and thus that this
# hook can meaningfully grant permission for. Bash is NOT in this list
# — Bash writes via shell redirection bypass the gate natively, so
# there's no need to grant anything for Bash.
GRANTABLE_TOOLS: tuple[str, ...] = ("Edit", "Write", "MultiEdit")

# Reason-string templates returned in the deny decision, surfaced to
# Claude as the tool_result content. The wording matters: it must be
# unambiguous enough that Claude reads it as "the edit was applied,
# don't try again" instead of "my edit failed, I should retry".
DENY_REASON_SUCCESS: str = (
    "Permission granted by claude-worker (grant {grant_id}); "
    "the hook applied the {tool_name} operation to {file_path} on your "
    "behalf. The file has been updated as requested. Do not retry."
)
DENY_REASON_FAILURE: str = (
    "Permission granted by claude-worker (grant {grant_id}) but the "
    "hook could not apply the {tool_name} operation to {file_path}: "
    "{error}. The grant was NOT consumed; fix the parameters and "
    "retry, or revoke the grant."
)


# -- Grants file I/O ---------------------------------------------------------


def _load_grants(grants_file: Path) -> list[dict]:
    """Parse the grants file into a list of grant dicts.

    Returns an empty list if the file doesn't exist or is empty.
    Silently skips lines that fail JSON parsing — a malformed line
    shouldn't take down the whole hook, and the operator gets
    feedback from ``claude-worker grants`` showing what parsed.
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


def _atomic_rewrite_grants(grants_file: Path, grants: list[dict]) -> None:
    """Rewrite the grants file atomically via sibling-tmp + os.replace.

    Used by the hook (for consume-on-use) and by ``claude-worker revoke``.
    Not used by ``grant`` itself — that uses simple append, which is
    atomic at the OS level for small lines per PIPE_BUF.

    Imported from manager to avoid duplicating the helper; this module
    shouldn't reach across into cli.py but manager.py is fair game.
    """
    # Lazy-import to avoid a hard dependency from the hook process on the
    # rest of claude_worker — if the grants file is writable we don't
    # strictly NEED anything from manager.py beyond the atomic write
    # pattern, but reusing the canonical helper is what the project
    # expects and the import is cheap.
    from claude_worker.manager import _atomic_write_text

    content = "\n".join(json.dumps(g) for g in grants)
    if content:
        content += "\n"
    _atomic_write_text(grants_file, content)


# -- Grant matching ----------------------------------------------------------


def _paths_equal(a: str, b: str) -> bool:
    """Compare two file paths for equality, normalizing away trivial
    differences (resolved absolute form, symlink-free). Both paths are
    expected to exist or at least be well-formed; we fall back to string
    equality if resolution fails."""
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except (OSError, RuntimeError):
        return a == b


def _find_matching_grant(
    grants: list[dict],
    tool_name: str,
    tool_input: dict,
    tool_use_id: str,
) -> dict | None:
    """Return the first non-consumed grant that matches this tool call.

    Matching rules, checked in order:

    1. ``match.tool_use_id`` exact equal to the tool's tool_use_id
    2. ``match.path`` path-equal to ``tool_input.file_path``
    3. ``match.glob`` fnmatch against ``tool_input.file_path``

    The grant's ``tools`` list must include ``tool_name`` (defaults to
    all grantable tools if unset).

    The caller is responsible for consume-on-use; this function is a
    pure matcher.
    """
    file_path = tool_input.get("file_path", "")
    for grant in grants:
        if grant.get("consumed"):
            continue
        tools = grant.get("tools") or list(GRANTABLE_TOOLS)
        if tool_name not in tools:
            continue
        match = grant.get("match") or {}
        if "tool_use_id" in match:
            if match["tool_use_id"] == tool_use_id:
                return grant
            continue
        if "path" in match:
            if file_path and _paths_equal(match["path"], file_path):
                return grant
            continue
        if "glob" in match:
            if file_path and fnmatch.fnmatchcase(file_path, match["glob"]):
                return grant
            continue
    return None


# -- Tool-specific apply helpers ---------------------------------------------


class EditApplyError(Exception):
    """Raised when a granted Edit/Write/MultiEdit cannot be applied."""


def _apply_edit(tool_input: dict) -> None:
    """Apply a single Edit (find-and-replace).

    Raises ``EditApplyError`` if the file can't be read, the old_string
    isn't present, or the write fails. The grant is NOT consumed when
    this raises — the operator gets a chance to fix the parameters.
    """
    file_path = tool_input.get("file_path")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")
    replace_all = bool(tool_input.get("replace_all", False))
    if not file_path:
        raise EditApplyError("missing file_path")
    try:
        original = Path(file_path).read_text()
    except OSError as exc:
        raise EditApplyError(f"could not read {file_path}: {exc}")
    if old_string not in original:
        raise EditApplyError(
            f"old_string not found in {file_path}; the file was not modified"
        )
    if replace_all:
        updated = original.replace(old_string, new_string)
    else:
        updated = original.replace(old_string, new_string, 1)
    try:
        Path(file_path).write_text(updated)
    except OSError as exc:
        raise EditApplyError(f"could not write {file_path}: {exc}")


def _apply_write(tool_input: dict) -> None:
    """Apply a Write (overwrite or create the file with the given content)."""
    file_path = tool_input.get("file_path")
    content = tool_input.get("content", "")
    if not file_path:
        raise EditApplyError("missing file_path")
    try:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    except OSError as exc:
        raise EditApplyError(f"could not write {file_path}: {exc}")


def _apply_multi_edit(tool_input: dict) -> None:
    """Apply a MultiEdit (list of sequential find-and-replace edits).

    Applied atomically: if any single edit's old_string isn't present
    in the current buffer state, the whole operation is rolled back and
    the file is left untouched. This matches claude's own MultiEdit
    semantics (partial failures there are also atomic).
    """
    file_path = tool_input.get("file_path")
    edits = tool_input.get("edits") or []
    if not file_path:
        raise EditApplyError("missing file_path")
    if not isinstance(edits, list) or not edits:
        raise EditApplyError("edits list is empty or malformed")
    try:
        buffer = Path(file_path).read_text()
    except OSError as exc:
        raise EditApplyError(f"could not read {file_path}: {exc}")
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise EditApplyError(f"edit #{i} is not an object")
        old_s = edit.get("old_string", "")
        new_s = edit.get("new_string", "")
        replace_all = bool(edit.get("replace_all", False))
        if old_s not in buffer:
            raise EditApplyError(
                f"edit #{i}: old_string not found; rolling back with no changes"
            )
        if replace_all:
            buffer = buffer.replace(old_s, new_s)
        else:
            buffer = buffer.replace(old_s, new_s, 1)
    try:
        Path(file_path).write_text(buffer)
    except OSError as exc:
        raise EditApplyError(f"could not write {file_path}: {exc}")


_APPLY_DISPATCH = {
    "Edit": _apply_edit,
    "Write": _apply_write,
    "MultiEdit": _apply_multi_edit,
}


# -- Decision builders -------------------------------------------------------


def _build_deny_decision(reason: str) -> dict:
    """Build the PreToolUse hook output JSON for a deny decision.

    The deny path is how we signal Claude that the tool call was
    "handled" without executing its Edit/Write. Returning a deny + a
    reason string causes Claude to see the reason as the tool_result
    content with ``is_error=True``.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _now_iso() -> str:
    """UTC ISO 8601 timestamp with trailing Z (matches claude log format)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- Main entry point --------------------------------------------------------


def main(
    argv: list[str] | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    """Hook entry point.

    Reads a PreToolUse payload on ``stdin``, consults the grants file
    named in ``--grants-file``, applies the edit if a grant matches,
    and writes a JSON decision on ``stdout``. Returns 0 on success
    (including no-match), nonzero only on argument errors.

    The ``stdin``/``stdout`` parameters exist so tests can drive the
    hook in-process without spawning a subprocess. Production runs
    use the real stdin/stdout.
    """
    parser = argparse.ArgumentParser(prog="claude_worker.permission_grant")
    parser.add_argument(
        "--grants-file",
        type=Path,
        required=True,
        help="Path to the worker's grants.jsonl file",
    )
    args = parser.parse_args(argv)

    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout

    raw = in_stream.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Malformed stdin — don't crash the hook runner; just decline.
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    tool_use_id = payload.get("tool_use_id", "")

    if tool_name not in _APPLY_DISPATCH:
        # Not a tool we can apply for; let default rules run.
        return 0

    grants = _load_grants(args.grants_file)
    grant = _find_matching_grant(grants, tool_name, tool_input, tool_use_id)
    if grant is None:
        # No matching grant — decline, so the normal sensitive-file
        # deny path runs and Claude sees the familiar error. Important:
        # don't echo "allow" here (that would be ignored by the gate
        # anyway) and don't echo "deny" (we'd mask the real error).
        return 0

    # Apply the tool operation.
    apply_fn = _APPLY_DISPATCH[tool_name]
    try:
        apply_fn(tool_input)
    except EditApplyError as exc:
        # The grant matched but the operation failed. Emit a deny with
        # a failure reason so Claude sees the error and can decide what
        # to do. Don't consume the grant — the operator may want to
        # fix parameters and have the grant still apply on retry.
        reason = DENY_REASON_FAILURE.format(
            grant_id=grant.get("id", "unknown"),
            tool_name=tool_name,
            file_path=tool_input.get("file_path", "<unknown>"),
            error=str(exc),
        )
        out_stream.write(json.dumps(_build_deny_decision(reason)))
        return 0

    # Apply succeeded. Consume the grant unless it's persistent.
    if not grant.get("persistent"):
        grant["consumed"] = True
        grant["consumed_at"] = _now_iso()
        _atomic_rewrite_grants(args.grants_file, grants)

    reason = DENY_REASON_SUCCESS.format(
        grant_id=grant.get("id", "unknown"),
        tool_name=tool_name,
        file_path=tool_input.get("file_path", "<unknown>"),
    )
    out_stream.write(json.dumps(_build_deny_decision(reason)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
