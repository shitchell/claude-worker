"""Tests for claude-worker subagents subcommand (#083, D100).

Covers:
- ``_cwd_to_project_slug`` — algorithm (/ and . → -)
- ``_resolve_subagents_dir`` — stitches session + cwd → project path
- ``_summarize_subagent`` — defensive JSONL + meta parser
- ``cmd_subagents`` — text + json output, edge cases
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_worker import cli as cw_cli


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestCwdToSlug:
    def test_simple_path(self) -> None:
        assert cw_cli._cwd_to_project_slug("/home/guy") == "-home-guy"

    def test_nested_path(self) -> None:
        assert (
            cw_cli._cwd_to_project_slug(
                "/home/guy/git/github.com/shitchell/claude-worker"
            )
            == "-home-guy-git-github-com-shitchell-claude-worker"
        )

    def test_dot_becomes_dash(self) -> None:
        """Dots in path components (e.g., dev.azure.com) map to dashes."""
        assert (
            cw_cli._cwd_to_project_slug("/home/guy/git/dev.azure.com/proj")
            == "-home-guy-git-dev-azure-com-proj"
        )

    def test_worktree_double_dash(self) -> None:
        """`/.worktrees/` produces literal double-dash in the slug."""
        assert (
            cw_cli._cwd_to_project_slug("/home/proj/.worktrees/branch")
            == "-home-proj--worktrees-branch"
        )

    def test_empty_cwd(self) -> None:
        assert cw_cli._cwd_to_project_slug("") == ""

    def test_case_preserved(self) -> None:
        assert cw_cli._cwd_to_project_slug("/Home/Guy/Project") == "-Home-Guy-Project"


class TestResolveSubagentsDir:
    def test_missing_session_file_returns_none(
        self, fake_worker, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        name = fake_worker([])
        # Ensure no session file.
        from claude_worker import manager as cw_manager

        runtime = cw_manager.get_base_dir() / name
        (runtime / "session").unlink(missing_ok=True)

        dir_, sid, cwd = cw_cli._resolve_subagents_dir(name)
        assert dir_ is None
        assert sid is None

    def test_missing_cwd_returns_dir_none_but_keeps_session(
        self, fake_worker, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        name = fake_worker([])
        from claude_worker import manager as cw_manager

        runtime = cw_manager.get_base_dir() / name
        (runtime / "session").write_text("abc-123")
        # Don't write cwd to .sessions.json
        dir_, sid, cwd = cw_cli._resolve_subagents_dir(name)
        assert dir_ is None
        assert sid == "abc-123"

    def test_nonexistent_subagents_dir_returns_none_with_context(
        self,
        fake_worker,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Session + cwd present but no subagents dir on disk → None (but keep session/cwd)."""
        # Redirect HOME so the lookup doesn't hit real ~/.claude/projects.
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        name = fake_worker([])
        from claude_worker import manager as cw_manager

        runtime = cw_manager.get_base_dir() / name
        (runtime / "session").write_text("sess-1")
        cw_manager.save_worker(name, cwd="/tmp/project")

        dir_, sid, cwd = cw_cli._resolve_subagents_dir(name)
        assert dir_ is None
        assert sid == "sess-1"
        assert cwd == "/tmp/project"

    def test_existing_subagents_dir_resolves(
        self,
        fake_worker,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        name = fake_worker([])
        from claude_worker import manager as cw_manager

        runtime = cw_manager.get_base_dir() / name
        (runtime / "session").write_text("sess-2")
        cw_manager.save_worker(name, cwd="/tmp/proj")

        expected = (
            fake_home / ".claude" / "projects" / "-tmp-proj" / "sess-2" / "subagents"
        )
        expected.mkdir(parents=True)

        dir_, sid, cwd = cw_cli._resolve_subagents_dir(name)
        assert dir_ == expected
        assert sid == "sess-2"
        assert cwd == "/tmp/proj"


class TestSummarizeSubagent:
    def test_basic_summary(self, tmp_path: Path) -> None:
        meta = tmp_path / "agent-abc.meta.json"
        meta.write_text(
            json.dumps({"agentType": "Explore", "description": "investigate X"})
        )
        jsonl = tmp_path / "agent-abc.jsonl"
        start = _iso(datetime(2026, 4, 16, 8, 0, 0))
        mid = _iso(datetime(2026, 4, 16, 8, 1, 0))
        end = _iso(datetime(2026, 4, 16, 8, 2, 0))
        _write_jsonl(
            jsonl,
            [
                {"timestamp": start, "type": "user", "message": {"content": []}},
                {
                    "timestamp": mid,
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "id": "t1",
                                "input": {"command": "ls"},
                            }
                        ]
                    },
                },
                {
                    "timestamp": end,
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "id": "t2",
                                "input": {"command": "pytest"},
                            }
                        ]
                    },
                },
            ],
        )
        summary = cw_cli._summarize_subagent(meta, jsonl)
        assert summary["agent_id"] == "abc"
        assert summary["type"] == "Explore"
        assert summary["description"] == "investigate X"
        assert summary["tool_call_count"] == 2
        assert summary["last_action"] == "Bash(pytest)"
        assert summary["started_at"] == start
        assert summary["last_action_at"] == end

    def test_missing_meta(self, tmp_path: Path) -> None:
        meta = tmp_path / "agent-xyz.meta.json"  # absent
        jsonl = tmp_path / "agent-xyz.jsonl"
        _write_jsonl(
            jsonl,
            [{"timestamp": _iso(datetime(2026, 4, 16, 8, 0, 0)), "type": "user"}],
        )
        summary = cw_cli._summarize_subagent(meta, jsonl)
        assert summary["type"] == "unknown"
        assert summary["description"] == ""
        assert summary["agent_id"] == "xyz"

    def test_empty_jsonl(self, tmp_path: Path) -> None:
        meta = tmp_path / "agent-empty.meta.json"
        meta.write_text(json.dumps({"agentType": "general-purpose"}))
        jsonl = tmp_path / "agent-empty.jsonl"
        jsonl.write_text("")
        summary = cw_cli._summarize_subagent(meta, jsonl)
        assert summary["tool_call_count"] == 0
        assert summary["last_action"] is None
        assert summary["started_at"] is None


class TestCmdSubagents:
    def _setup(
        self,
        fake_worker,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        session_id: str = "sess-a",
        cwd: str = "/proj",
        with_subagents: bool = True,
    ):
        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        name = fake_worker([])
        from claude_worker import manager as cw_manager

        runtime = cw_manager.get_base_dir() / name
        (runtime / "session").write_text(session_id)
        cw_manager.save_worker(name, cwd=cwd)

        subagents_dir: Path | None = None
        if with_subagents:
            slug = cw_cli._cwd_to_project_slug(cwd)
            subagents_dir = (
                fake_home / ".claude" / "projects" / slug / session_id / "subagents"
            )
            subagents_dir.mkdir(parents=True)
        return name, subagents_dir

    def test_error_when_no_session(
        self,
        fake_worker,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        name = fake_worker([])
        from claude_worker import manager as cw_manager

        runtime = cw_manager.get_base_dir() / name
        (runtime / "session").unlink(missing_ok=True)

        args = argparse.Namespace(name=name, format="text", limit=None)
        with pytest.raises(SystemExit) as exc:
            cw_cli.cmd_subagents(args)
        assert exc.value.code == 1
        assert "no session" in capsys.readouterr().err

    def test_no_subagents_dir_clean_zero(
        self,
        fake_worker,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        name, _ = self._setup(fake_worker, monkeypatch, tmp_path, with_subagents=False)
        args = argparse.Namespace(name=name, format="text", limit=None)
        cw_cli.cmd_subagents(args)
        out = capsys.readouterr().out
        assert "subagents: 0" in out
        assert "no subagents directory" in out

    def test_text_output_with_subagents(
        self,
        fake_worker,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        name, subagents_dir = self._setup(fake_worker, monkeypatch, tmp_path)
        assert subagents_dir is not None
        meta = subagents_dir / "agent-aaa.meta.json"
        meta.write_text(
            json.dumps({"agentType": "Explore", "description": "test agent"})
        )
        jsonl = subagents_dir / "agent-aaa.jsonl"
        _write_jsonl(
            jsonl,
            [
                {
                    "timestamp": _iso(datetime(2026, 4, 16, 8, 0, 0)),
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "id": "t",
                                "input": {"command": "echo hi"},
                            }
                        ]
                    },
                }
            ],
        )
        args = argparse.Namespace(name=name, format="text", limit=None)
        cw_cli.cmd_subagents(args)
        out = capsys.readouterr().out
        assert "agent-aaa  Explore" in out
        assert 'description: "test agent"' in out
        assert "1 tool call" in out
        assert "last: Bash(echo hi)" in out

    def test_json_output(
        self,
        fake_worker,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        name, subagents_dir = self._setup(fake_worker, monkeypatch, tmp_path)
        assert subagents_dir is not None
        (subagents_dir / "agent-b.meta.json").write_text(
            json.dumps({"agentType": "general-purpose"})
        )
        _write_jsonl(
            subagents_dir / "agent-b.jsonl",
            [{"timestamp": _iso(datetime(2026, 4, 16, 8, 0, 0)), "type": "user"}],
        )
        args = argparse.Namespace(name=name, format="json", limit=None)
        cw_cli.cmd_subagents(args)
        out = capsys.readouterr().out
        envelope = json.loads(out)
        assert envelope["worker"] == name
        assert envelope["session"] == "sess-a"
        assert len(envelope["subagents"]) == 1
        assert envelope["subagents"][0]["type"] == "general-purpose"

    def test_limit_caps_output(
        self,
        fake_worker,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        name, subagents_dir = self._setup(fake_worker, monkeypatch, tmp_path)
        assert subagents_dir is not None
        base = datetime(2026, 4, 16, 8, 0, 0)
        for i in range(5):
            (subagents_dir / f"agent-{i}.meta.json").write_text(
                json.dumps({"agentType": "Explore"})
            )
            _write_jsonl(
                subagents_dir / f"agent-{i}.jsonl",
                [{"timestamp": _iso(base + timedelta(minutes=i)), "type": "user"}],
            )
        args = argparse.Namespace(name=name, format="json", limit=2)
        cw_cli.cmd_subagents(args)
        envelope = json.loads(capsys.readouterr().out)
        assert len(envelope["subagents"]) == 2
        # Should be the two most-recently-active (agents 4 and 3).
        ids = [s["agent_id"] for s in envelope["subagents"]]
        assert ids == ["4", "3"]
