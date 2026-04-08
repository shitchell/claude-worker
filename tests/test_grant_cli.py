"""Tests for the `grant`, `grants`, and `revoke` CLI subcommands.

These tests exercise the grants-file management side of the permission
system (the CLI-facing half). The hook module that consumes the grants
is tested in test_permission_grant.py.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _build_grant_args(
    name: str = "test-worker",
    *,
    path: str | None = None,
    glob: str | None = None,
    tool_use_id: str | None = None,
    last: bool = False,
    tool: list[str] | None = None,
    persistent: bool = False,
    reason: str | None = None,
) -> argparse.Namespace:
    # argparse with action="append" yields None when --tool is never
    # passed; mirror that so cmd_grant's default-applying logic is
    # exercised faithfully.
    return argparse.Namespace(
        name=name,
        path=path,
        glob=glob,
        tool_use_id=tool_use_id,
        last=last,
        tool=tool,
        persistent=persistent,
        reason=reason,
    )


def _build_grants_args(name: str = "test-worker") -> argparse.Namespace:
    return argparse.Namespace(name=name)


def _build_revoke_args(
    name: str = "test-worker", grant_id: str | None = None, all_: bool = False
) -> argparse.Namespace:
    # argparse maps --all to attribute `all_` because `all` shadows builtin.
    ns = argparse.Namespace(name=name, grant_id=grant_id)
    setattr(ns, "all", all_)
    return ns


def _load_grants(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestCmdGrant:
    """Happy paths for writing new grants into grants.jsonl."""

    def test_path_grant_creates_jsonl_entry(self, fake_worker):
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        cw_cli.cmd_grant(
            _build_grant_args(name=name, path="/tmp/test/.claude/target.md")
        )
        grants_file = cw_cli.get_runtime_dir(name) / "grants.jsonl"
        grants = _load_grants(grants_file)
        assert len(grants) == 1
        g = grants[0]
        assert g["match"] == {"path": "/tmp/test/.claude/target.md"}
        assert g["persistent"] is False
        assert g["consumed"] is False
        assert g["tools"] == ["Edit", "Write", "MultiEdit"]
        assert "id" in g
        assert g["id"].startswith("grant-")
        assert "created_at" in g

    def test_glob_grant_stored_with_kind(self, fake_worker):
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        cw_cli.cmd_grant(
            _build_grant_args(
                name=name, glob="/home/foo/.claude/skills/**/*.md", persistent=True
            )
        )
        grants = _load_grants(cw_cli.get_runtime_dir(name) / "grants.jsonl")
        assert len(grants) == 1
        assert grants[0]["match"] == {"glob": "/home/foo/.claude/skills/**/*.md"}
        assert grants[0]["persistent"] is True

    def test_tool_use_id_grant(self, fake_worker):
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        cw_cli.cmd_grant(_build_grant_args(name=name, tool_use_id="toolu_01abc"))
        grants = _load_grants(cw_cli.get_runtime_dir(name) / "grants.jsonl")
        assert grants[0]["match"] == {"tool_use_id": "toolu_01abc"}

    def test_reason_stored(self, fake_worker):
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        cw_cli.cmd_grant(
            _build_grant_args(
                name=name, path="/tmp/x.md", reason="testing batch refactor"
            )
        )
        grants = _load_grants(cw_cli.get_runtime_dir(name) / "grants.jsonl")
        assert grants[0]["reason"] == "testing batch refactor"

    def test_tool_filter_restricts_tools(self, fake_worker):
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        cw_cli.cmd_grant(_build_grant_args(name=name, path="/tmp/x.md", tool=["Edit"]))
        grants = _load_grants(cw_cli.get_runtime_dir(name) / "grants.jsonl")
        assert grants[0]["tools"] == ["Edit"]

    def test_multiple_grants_append(self, fake_worker):
        """Consecutive grants append to the JSONL without clobbering."""
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        cw_cli.cmd_grant(_build_grant_args(name=name, path="/tmp/a.md"))
        cw_cli.cmd_grant(_build_grant_args(name=name, path="/tmp/b.md"))
        cw_cli.cmd_grant(
            _build_grant_args(name=name, glob="/tmp/c/**/*.py", persistent=True)
        )
        grants = _load_grants(cw_cli.get_runtime_dir(name) / "grants.jsonl")
        assert len(grants) == 3
        assert grants[0]["match"] == {"path": "/tmp/a.md"}
        assert grants[1]["match"] == {"path": "/tmp/b.md"}
        assert grants[2]["match"] == {"glob": "/tmp/c/**/*.py"}


class TestGrantLastFromLog:
    """`claude-worker grant NAME --last` scans the log backwards for a
    sensitive-file denial and grants the exact (tool, file_path,
    tool_use_id) of the most recent one. This is the ergonomic default
    that pairs with `send "retry"` without forcing the caller to read
    denial details manually."""

    def _denial_log(self, target_path: str, tool_use_id: str = "toolu_01last"):
        """Build a synthetic log with a tool_use → sensitive denial pair."""
        from conftest import make_system_init, make_result_message

        return [
            make_system_init("u-init"),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": "Edit",
                            "input": {
                                "file_path": target_path,
                                "old_string": "old",
                                "new_string": "new",
                                "replace_all": False,
                            },
                        }
                    ],
                    "stop_reason": None,
                    "model": "claude-opus-4-6",
                    "id": "msg_01",
                },
                "uuid": "u-tooluse",
                "session_id": "sess",
                "parent_tool_use_id": None,
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": (
                                f"Claude requested permissions to edit "
                                f"{target_path} which is a sensitive file."
                            ),
                            "is_error": True,
                        }
                    ],
                },
                "uuid": "u-toolresult",
                "session_id": "sess",
                "parent_tool_use_id": None,
            },
            make_result_message("u-result"),
        ]

    def test_last_finds_recent_denial_and_grants_it(self, fake_worker):
        from claude_worker import cli as cw_cli

        name = fake_worker(self._denial_log("/tmp/fake/.claude/target.md"))
        cw_cli.cmd_grant(_build_grant_args(name=name, last=True))

        grants = _load_grants(cw_cli.get_runtime_dir(name) / "grants.jsonl")
        assert len(grants) == 1
        g = grants[0]
        assert g["match"] == {"path": "/tmp/fake/.claude/target.md"}
        assert g["tools"] == ["Edit"]
        # The tool_use_id from the denial is captured for auditability
        assert g.get("source_tool_use_id") == "toolu_01last"

    def test_last_errors_when_no_denial_in_log(self, fake_worker, capsys):
        from claude_worker import cli as cw_cli
        from conftest import (
            make_system_init,
            make_assistant_message,
            make_result_message,
        )

        # Log with no denial at all
        name = fake_worker(
            [
                make_system_init("u-init"),
                make_assistant_message("hello world", "u-a"),
                make_result_message("u-r"),
            ]
        )
        # --last with no denial should exit nonzero and write nothing
        try:
            cw_cli.cmd_grant(_build_grant_args(name=name, last=True))
        except SystemExit as exc:
            assert exc.code != 0
        err = capsys.readouterr().err
        assert "no" in err.lower() and "denial" in err.lower()
        grants_file = cw_cli.get_runtime_dir(name) / "grants.jsonl"
        assert not grants_file.exists() or _load_grants(grants_file) == []

    def test_last_picks_newest_denial_when_multiple(self, fake_worker):
        from claude_worker import cli as cw_cli
        from conftest import (
            make_system_init,
            make_result_message,
        )

        entries = [make_system_init("u-init")]
        # Older denial
        entries.extend(
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_01older",
                                "name": "Edit",
                                "input": {
                                    "file_path": "/tmp/.claude/old.md",
                                    "old_string": "a",
                                    "new_string": "b",
                                    "replace_all": False,
                                },
                            }
                        ],
                        "stop_reason": None,
                        "model": "x",
                        "id": "msg_old",
                    },
                    "uuid": "u-old-tooluse",
                    "session_id": "s",
                    "parent_tool_use_id": None,
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01older",
                                "content": (
                                    "Claude requested permissions to edit "
                                    "/tmp/.claude/old.md which is a sensitive file."
                                ),
                                "is_error": True,
                            }
                        ],
                    },
                    "uuid": "u-old-result",
                    "session_id": "s",
                    "parent_tool_use_id": None,
                },
                make_result_message("u-turn1"),
            ]
        )
        # Newer denial
        entries.extend(
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_01newer",
                                "name": "Write",
                                "input": {
                                    "file_path": "/tmp/.claude/new.md",
                                    "content": "hi",
                                },
                            }
                        ],
                        "stop_reason": None,
                        "model": "x",
                        "id": "msg_new",
                    },
                    "uuid": "u-new-tooluse",
                    "session_id": "s",
                    "parent_tool_use_id": None,
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01newer",
                                "content": (
                                    "Claude requested permissions to edit "
                                    "/tmp/.claude/new.md which is a sensitive file."
                                ),
                                "is_error": True,
                            }
                        ],
                    },
                    "uuid": "u-new-result",
                    "session_id": "s",
                    "parent_tool_use_id": None,
                },
                make_result_message("u-turn2"),
            ]
        )

        name = fake_worker(entries)
        cw_cli.cmd_grant(_build_grant_args(name=name, last=True))

        grants = _load_grants(cw_cli.get_runtime_dir(name) / "grants.jsonl")
        assert len(grants) == 1
        assert grants[0]["match"] == {"path": "/tmp/.claude/new.md"}
        assert grants[0]["tools"] == ["Write"]
        assert grants[0]["source_tool_use_id"] == "toolu_01newer"


class TestCmdGrants:
    """`claude-worker grants NAME` lists active grants."""

    def test_lists_empty(self, fake_worker, capsys):
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        cw_cli.cmd_grants(_build_grants_args(name=name))
        out = capsys.readouterr().out
        # Should succeed with an empty-message, not crash
        assert "no" in out.lower() or "empty" in out.lower() or out.strip() == ""

    def test_lists_active_grants(self, fake_worker, capsys):
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        cw_cli.cmd_grant(_build_grant_args(name=name, path="/tmp/a.md"))
        cw_cli.cmd_grant(
            _build_grant_args(name=name, glob="/tmp/b/**/*.py", persistent=True)
        )
        cw_cli.cmd_grants(_build_grants_args(name=name))
        out = capsys.readouterr().out
        assert "/tmp/a.md" in out
        assert "/tmp/b/**/*.py" in out

    def test_lists_hides_consumed_grants_by_default(self, fake_worker, capsys):
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        runtime = cw_cli.get_runtime_dir(name)
        grants_file = runtime / "grants.jsonl"
        grants_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "id": "grant-active",
                            "match": {"path": "/tmp/active.md"},
                            "tools": ["Edit"],
                            "persistent": False,
                            "consumed": False,
                            "created_at": "2026-04-08T00:00:00Z",
                        }
                    ),
                    json.dumps(
                        {
                            "id": "grant-done",
                            "match": {"path": "/tmp/done.md"},
                            "tools": ["Edit"],
                            "persistent": False,
                            "consumed": True,
                            "consumed_at": "2026-04-08T00:00:01Z",
                            "created_at": "2026-04-08T00:00:00Z",
                        }
                    ),
                ]
            )
            + "\n"
        )
        cw_cli.cmd_grants(_build_grants_args(name=name))
        out = capsys.readouterr().out
        assert "/tmp/active.md" in out
        assert "/tmp/done.md" not in out


class TestCmdRevoke:
    """`claude-worker revoke NAME [GRANT_ID | --all]`."""

    def test_revoke_by_id(self, fake_worker):
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        cw_cli.cmd_grant(_build_grant_args(name=name, path="/tmp/a.md"))
        cw_cli.cmd_grant(_build_grant_args(name=name, path="/tmp/b.md"))
        grants_file = cw_cli.get_runtime_dir(name) / "grants.jsonl"
        grants = _load_grants(grants_file)
        target_id = grants[0]["id"]

        cw_cli.cmd_revoke(_build_revoke_args(name=name, grant_id=target_id))

        after = _load_grants(grants_file)
        # Only the non-revoked grant remains
        assert len(after) == 1
        assert after[0]["match"] == {"path": "/tmp/b.md"}

    def test_revoke_all(self, fake_worker):
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        cw_cli.cmd_grant(_build_grant_args(name=name, path="/tmp/a.md"))
        cw_cli.cmd_grant(_build_grant_args(name=name, path="/tmp/b.md"))
        cw_cli.cmd_revoke(_build_revoke_args(name=name, all_=True))

        grants_file = cw_cli.get_runtime_dir(name) / "grants.jsonl"
        after = _load_grants(grants_file)
        assert after == []

    def test_revoke_nonexistent_id_errors(self, fake_worker, capsys):
        from claude_worker import cli as cw_cli

        name = fake_worker([])
        cw_cli.cmd_grant(_build_grant_args(name=name, path="/tmp/a.md"))
        try:
            cw_cli.cmd_revoke(
                _build_revoke_args(name=name, grant_id="grant-nonexistent")
            )
        except SystemExit as exc:
            assert exc.code != 0
        err = capsys.readouterr().err
        assert "not found" in err.lower() or "no such" in err.lower()
