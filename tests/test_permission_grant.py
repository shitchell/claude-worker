"""Tests for the PreToolUse permission-grant hook.

The hook catches Edit/Write/MultiEdit tool calls targeting sensitive
files, consults a per-worker grants.jsonl file, and — when a matching
grant exists — applies the edit itself via normal filesystem ops (which
bypass Claude Code's in-CLI sensitive-file gate) and returns a
``permissionDecision: "deny"`` result whose reason tells Claude the
edit was applied on its behalf. See the hook module docstring for why
"deny with a friendly message" is the right shape here.

These tests drive the hook module directly: they feed synthetic
PreToolUse JSON on stdin and assert both the stdout decision and the
resulting on-disk state. No real claude subprocess is involved.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest


def _run_hook(
    tmp_path: Path,
    grants_file: Path,
    stdin_payload: dict,
) -> dict | None:
    """Run the hook module's main() against the given grants file and
    stdin payload. Returns the parsed stdout JSON, or None if the hook
    exited silently (no grant matched).
    """
    from claude_worker import permission_grant

    stdin = io.StringIO(json.dumps(stdin_payload))
    stdout = io.StringIO()
    rc = permission_grant.main(
        argv=["--grants-file", str(grants_file)],
        stdin=stdin,
        stdout=stdout,
    )
    assert rc == 0, f"hook exited with rc={rc}"
    out = stdout.getvalue().strip()
    if not out:
        return None
    return json.loads(out)


def _pretooluse_payload(
    tool_name: str,
    tool_input: dict,
    tool_use_id: str = "toolu_test01",
) -> dict:
    """Build a synthetic PreToolUse hook stdin payload."""
    return {
        "session_id": "00000000-0000-0000-0000-000000000000",
        "transcript_path": "/tmp/fake-transcript.jsonl",
        "cwd": "/tmp",
        "permission_mode": "bypassPermissions",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
    }


def _load_grants(grants_file: Path) -> list[dict]:
    """Read and parse the grants JSONL file. Returns [] if missing."""
    if not grants_file.exists():
        return []
    return [
        json.loads(line)
        for line in grants_file.read_text().splitlines()
        if line.strip()
    ]


class TestNoMatch:
    """When no grant matches, the hook exits silently so the normal
    sensitive-file deny path runs unchanged."""

    def test_empty_grants_file_exits_silently(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("original\n")
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "original",
                    "new_string": "modified",
                    "replace_all": False,
                },
            ),
        )
        assert out is None
        # File was NOT modified
        assert target.read_text() == "original\n"

    def test_missing_grants_file_exits_silently(self, tmp_path):
        grants_file = tmp_path / "does-not-exist.jsonl"
        target = tmp_path / "target.md"
        target.write_text("original\n")
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "original",
                    "new_string": "modified",
                    "replace_all": False,
                },
            ),
        )
        assert out is None
        assert target.read_text() == "original\n"

    def test_grant_for_different_path_does_not_match(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-other",
                    "match": {"path": "/tmp/other-file.md"},
                    "tools": ["Edit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        target = tmp_path / "target.md"
        target.write_text("original\n")
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "original",
                    "new_string": "modified",
                    "replace_all": False,
                },
            ),
        )
        assert out is None
        assert target.read_text() == "original\n"


class TestEditGrant:
    """Edit tool matching by exact path."""

    def test_match_applies_edit_and_denies(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("original content\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-abc",
                    "match": {"path": str(target)},
                    "tools": ["Edit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "original",
                    "new_string": "modified",
                    "replace_all": False,
                },
            ),
        )
        # Hook emitted a deny decision
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert "grant-abc" in hso["permissionDecisionReason"]
        assert "Do not retry" in hso["permissionDecisionReason"]
        # File WAS modified
        assert target.read_text() == "modified content\n"

    def test_replace_all_false_only_replaces_first(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("foo bar foo baz foo\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-abc",
                    "match": {"path": str(target)},
                    "tools": ["Edit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "foo",
                    "new_string": "FOO",
                    "replace_all": False,
                },
            ),
        )
        # Only the first "foo" was replaced
        assert target.read_text() == "FOO bar foo baz foo\n"

    def test_replace_all_true_replaces_all(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("foo bar foo baz foo\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-abc",
                    "match": {"path": str(target)},
                    "tools": ["Edit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "foo",
                    "new_string": "FOO",
                    "replace_all": True,
                },
            ),
        )
        assert target.read_text() == "FOO bar FOO baz FOO\n"

    def test_old_string_not_found_reports_error(self, tmp_path):
        """If the old_string isn't in the file, the hook must NOT silently
        succeed — it should deny with an error reason so Claude sees the
        failure (not a false success)."""
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("hello world\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-abc",
                    "match": {"path": str(target)},
                    "tools": ["Edit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "nonexistent",
                    "new_string": "replacement",
                    "replace_all": False,
                },
            ),
        )
        # Still a deny, but the reason indicates failure, not success
        assert out is not None
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert (
            "not found" in reason.lower()
            or "failed" in reason.lower()
            or "error" in reason.lower()
        )
        # File is UNCHANGED
        assert target.read_text() == "hello world\n"
        # And the grant is NOT consumed (so the user can fix their grant
        # without losing it)
        grants = _load_grants(grants_file)
        assert grants[0]["consumed"] is False


class TestGlobGrant:
    """Grants with a glob pattern match any file under the pattern."""

    def test_glob_matches_nested_file(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        skills_dir = tmp_path / "skills" / "foo"
        skills_dir.mkdir(parents=True)
        target = skills_dir / "SKILL.md"
        target.write_text("original\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-glob",
                    "match": {"glob": str(tmp_path / "skills" / "**" / "*.md")},
                    "tools": ["Edit"],
                    "persistent": True,  # persistent batch grant
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "original",
                    "new_string": "modified",
                    "replace_all": False,
                },
            ),
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert target.read_text() == "modified\n"

    def test_glob_non_match_exits_silently(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "other.txt"
        target.write_text("original\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-glob",
                    "match": {"glob": str(tmp_path / "skills" / "**" / "*.md")},
                    "tools": ["Edit"],
                    "persistent": True,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "original",
                    "new_string": "modified",
                    "replace_all": False,
                },
            ),
        )
        assert out is None
        assert target.read_text() == "original\n"


class TestToolUseIdGrant:
    """Grants that match a specific tool_use_id."""

    def test_tool_use_id_exact_match(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("original\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-byid",
                    "match": {"tool_use_id": "toolu_01SpecificId"},
                    "tools": ["Edit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "original",
                    "new_string": "modified",
                    "replace_all": False,
                },
                tool_use_id="toolu_01SpecificId",
            ),
        )
        assert out is not None
        assert target.read_text() == "modified\n"

    def test_tool_use_id_mismatch_exits_silently(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("original\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-byid",
                    "match": {"tool_use_id": "toolu_01SpecificId"},
                    "tools": ["Edit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "original",
                    "new_string": "modified",
                    "replace_all": False,
                },
                tool_use_id="toolu_01DifferentId",
            ),
        )
        assert out is None


class TestWriteGrant:
    """Grants for the Write tool."""

    def test_write_creates_new_file(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "new-file.md"  # doesn't exist yet
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-write",
                    "match": {"path": str(target)},
                    "tools": ["Write"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "brand new content\n",
                },
            ),
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert target.read_text() == "brand new content\n"

    def test_write_overwrites_existing_file(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("old content\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-write",
                    "match": {"path": str(target)},
                    "tools": ["Write"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "new content\n",
                },
            ),
        )
        assert target.read_text() == "new content\n"


class TestMultiEditGrant:
    """Grants for the MultiEdit tool apply a list of edits in sequence."""

    def test_multi_edit_applies_all_edits_in_order(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("alpha beta gamma delta\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-multi",
                    "match": {"path": str(target)},
                    "tools": ["MultiEdit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "MultiEdit",
                {
                    "file_path": str(target),
                    "edits": [
                        {"old_string": "alpha", "new_string": "A"},
                        {"old_string": "gamma", "new_string": "G"},
                    ],
                },
            ),
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert target.read_text() == "A beta G delta\n"

    def test_multi_edit_failure_on_missing_old_string_rolls_back(self, tmp_path):
        """If any single edit's old_string is missing, the whole MultiEdit
        must fail atomically — no partial state left on disk."""
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("alpha beta gamma\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-multi",
                    "match": {"path": str(target)},
                    "tools": ["MultiEdit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "MultiEdit",
                {
                    "file_path": str(target),
                    "edits": [
                        {"old_string": "alpha", "new_string": "A"},
                        {"old_string": "MISSING", "new_string": "X"},
                    ],
                },
            ),
        )
        # File is unchanged (rollback)
        assert target.read_text() == "alpha beta gamma\n"
        # Grant NOT consumed
        grants = _load_grants(grants_file)
        assert grants[0]["consumed"] is False
        # Hook still emits a deny with an error reason
        assert out is not None
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "not found" in reason.lower() or "failed" in reason.lower()


class TestToolFilter:
    """The `tools` list on a grant scopes which tool names it matches."""

    def test_edit_grant_does_not_match_write(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("original\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-edit-only",
                    "match": {"path": str(target)},
                    "tools": ["Edit"],  # Edit only!
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "new content\n",
                },
            ),
        )
        # Write was NOT granted, so hook exits silently
        assert out is None
        assert target.read_text() == "original\n"


class TestConsume:
    """One-shot grants (persistent=False) must be consumed after the first
    matching application."""

    def test_one_shot_grant_is_consumed(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("original\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-oneshot",
                    "match": {"path": str(target)},
                    "tools": ["Edit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "original",
                    "new_string": "modified",
                    "replace_all": False,
                },
            ),
        )
        # Grant is now consumed
        grants = _load_grants(grants_file)
        assert len(grants) == 1
        assert grants[0]["consumed"] is True
        assert "consumed_at" in grants[0]

    def test_consumed_grant_does_not_match_again(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("modified\n")
        # A previously-consumed grant
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-used",
                    "match": {"path": str(target)},
                    "tools": ["Edit"],
                    "persistent": False,
                    "consumed": True,
                    "consumed_at": "2026-04-08T00:00:00Z",
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        out = _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "modified",
                    "new_string": "changed again",
                    "replace_all": False,
                },
            ),
        )
        assert out is None
        # File unchanged
        assert target.read_text() == "modified\n"

    def test_persistent_grant_is_not_consumed(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("original\n")
        grants_file.write_text(
            json.dumps(
                {
                    "id": "grant-persistent",
                    "match": {"path": str(target)},
                    "tools": ["Edit"],
                    "persistent": True,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            )
            + "\n"
        )
        _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "original",
                    "new_string": "modified",
                    "replace_all": False,
                },
            ),
        )
        grants = _load_grants(grants_file)
        assert grants[0]["consumed"] is False
        assert "consumed_at" not in grants[0]

    def test_multiple_grants_only_first_match_consumed(self, tmp_path):
        grants_file = tmp_path / "grants.jsonl"
        target = tmp_path / "target.md"
        target.write_text("original\n")
        # Two matching grants; hook should consume only the first.
        lines = [
            json.dumps(
                {
                    "id": "grant-A",
                    "match": {"path": str(target)},
                    "tools": ["Edit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:00Z",
                }
            ),
            json.dumps(
                {
                    "id": "grant-B",
                    "match": {"path": str(target)},
                    "tools": ["Edit"],
                    "persistent": False,
                    "consumed": False,
                    "created_at": "2026-04-08T00:00:01Z",
                }
            ),
        ]
        grants_file.write_text("\n".join(lines) + "\n")
        _run_hook(
            tmp_path,
            grants_file,
            _pretooluse_payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "original",
                    "new_string": "modified",
                    "replace_all": False,
                },
            ),
        )
        grants = _load_grants(grants_file)
        # grant-A consumed, grant-B untouched
        assert grants[0]["id"] == "grant-A"
        assert grants[0]["consumed"] is True
        assert grants[1]["id"] == "grant-B"
        assert grants[1]["consumed"] is False
