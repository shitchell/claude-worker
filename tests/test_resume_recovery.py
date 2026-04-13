"""Tests for resume/recovery bugs (#068, #069, #070).

Bug #068: start --resume errors on dead workers whose runtime dir still exists.
Bug #069: sessions.json claude_args has legacy /tmp/ paths after migration.
Bug #070: resume should find latest archive when .sessions.json has no entry.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_start_args(**overrides) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for cmd_start."""
    defaults = dict(
        name="test-worker",
        cwd=None,
        prompt=None,
        prompt_file=None,
        agent=None,
        resume=False,
        background=True,
        foreground=False,
        show_response=False,
        show_full_response=False,
        pm=False,
        team_lead=False,
        identity=None,
        no_permission_hook=True,
        claude_args=[],
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestResumeDeadWorkerArchivesStaleDir:
    """#068: start --resume with a dead worker's stale runtime dir should
    archive it and proceed, not error with FileExistsError."""

    def test_resume_dead_worker_archives_stale_dir(self, fake_worker, capsys, tmp_path):
        from claude_worker.cli import cmd_start

        name = "test-worker"
        fake_worker([], name=name)

        # Write a dead PID (PID 1 is init — not our process; use a
        # definitely-dead PID by picking a very high number)
        base_dir = tmp_path / "workers"
        runtime = base_dir / name
        (runtime / "pid").write_text("999999999")
        (runtime / "session").write_text("abc12345-dead-session")

        # Write sessions.json so resume finds the session_id
        sessions = {name: {"session_id": "abc12345-dead-session", "claude_args": [], "cwd": "/tmp"}}
        (base_dir / ".sessions.json").write_text(json.dumps(sessions))

        # Use foreground mode to avoid fork/setsid issues in tests
        args = _make_start_args(name=name, resume=True, foreground=True, background=False)

        # Mock run_manager to no-op (we don't want to actually start claude)
        with patch("claude_worker.cli.run_manager"):
            with pytest.raises(SystemExit) as exc_info:
                cmd_start(args)
            # Foreground mode exits with 0 after run_manager
            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Archived stale runtime dir" in captured.err

        # Verify a new runtime dir was created (create_runtime_dir succeeded)
        assert runtime.exists()
        # Verify the archive exists
        archives = [
            d for d in base_dir.iterdir()
            if d.is_dir() and d.name.startswith(f"{name}.")
        ]
        assert len(archives) == 1

    def test_resume_alive_worker_errors(self, fake_worker, capsys, tmp_path):
        """#068: start --resume with an alive worker should error."""
        from claude_worker.cli import cmd_start

        name = "test-worker"
        fake_worker([], name=name, alive=True)

        # Write sessions.json
        base_dir = tmp_path / "workers"
        sessions = {name: {"session_id": "abc12345", "claude_args": [], "cwd": "/tmp"}}
        (base_dir / ".sessions.json").write_text(json.dumps(sessions))

        args = _make_start_args(name=name, resume=True)

        with pytest.raises(SystemExit) as exc_info:
            cmd_start(args)
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "still alive" in captured.err


class TestFixLegacyPaths:
    """#069: legacy /tmp/claude-workers/ paths in saved args should be fixed."""

    def test_fix_legacy_paths(self, fake_worker, tmp_path):
        from claude_worker.cli import _fix_legacy_paths_in_args

        name = "my-worker"
        fake_worker([], name=name)
        base_dir = tmp_path / "workers"

        args = [
            "--append-system-prompt-file",
            f"/tmp/claude-workers/1000/{name}/identity.md",
            "--settings",
            f"/tmp/claude-workers/1000/{name}/settings.json",
        ]
        fixed = _fix_legacy_paths_in_args(args, name)

        expected_base = str(base_dir / name) + "/"
        assert fixed[1] == f"{expected_base}identity.md"
        assert fixed[3] == f"{expected_base}settings.json"
        # Flags unchanged
        assert fixed[0] == "--append-system-prompt-file"
        assert fixed[2] == "--settings"

    def test_fix_legacy_paths_no_change(self, fake_worker, tmp_path):
        from claude_worker.cli import _fix_legacy_paths_in_args

        name = "my-worker"
        fake_worker([], name=name)
        base_dir = tmp_path / "workers"

        current_path = str(base_dir / name / "identity.md")
        args = ["--append-system-prompt-file", current_path]
        fixed = _fix_legacy_paths_in_args(args, name)

        assert fixed == args

    def test_fix_legacy_paths_different_uid(self, fake_worker, tmp_path):
        """Paths with any numeric UID should be fixed."""
        from claude_worker.cli import _fix_legacy_paths_in_args

        name = "my-worker"
        fake_worker([], name=name)
        base_dir = tmp_path / "workers"

        args = [f"/tmp/claude-workers/5555/{name}/identity.md"]
        fixed = _fix_legacy_paths_in_args(args, name)

        expected_base = str(base_dir / name) + "/"
        assert fixed[0] == f"{expected_base}identity.md"


class TestFindLatestArchive:
    """#070: _find_latest_archive should return the most recent archive."""

    def test_find_latest_archive(self, fake_worker, tmp_path):
        from claude_worker.cli import _find_latest_archive

        name = "worker"
        fake_worker([], name=name)
        base_dir = tmp_path / "workers"

        # Create two archive dirs
        (base_dir / f"{name}.20260410T000000").mkdir()
        (base_dir / f"{name}.20260411T000000").mkdir()

        result = _find_latest_archive(name)
        assert result is not None
        assert result.name == f"{name}.20260411T000000"

    def test_find_latest_archive_none(self, fake_worker, tmp_path):
        from claude_worker.cli import _find_latest_archive

        name = "worker"
        fake_worker([], name=name)

        result = _find_latest_archive(name)
        assert result is None

    def test_find_latest_archive_with_session_suffix(self, fake_worker, tmp_path):
        from claude_worker.cli import _find_latest_archive

        name = "worker"
        fake_worker([], name=name)
        base_dir = tmp_path / "workers"

        (base_dir / f"{name}.20260410T000000.abc123").mkdir()
        (base_dir / f"{name}.20260411T120000.def456").mkdir()
        (base_dir / f"{name}.20260409T000000").mkdir()

        result = _find_latest_archive(name)
        assert result is not None
        assert result.name == f"{name}.20260411T120000.def456"


class TestResumeFromArchiveSession:
    """#070: resume should recover session_id from archive when
    .sessions.json has no entry."""

    def test_resume_from_archive_session(self, fake_worker, capsys, tmp_path):
        from claude_worker.cli import cmd_start

        name = "test-worker"
        fake_worker([], name=name)
        base_dir = tmp_path / "workers"

        # Remove the runtime dir (simulates post-stop state)
        runtime = base_dir / name
        import shutil
        shutil.rmtree(runtime)

        # Create an archive with a session file
        archive = base_dir / f"{name}.20260411T000000.abc123"
        archive.mkdir()
        (archive / "session").write_text("abc12345-full-session-id")

        # No .sessions.json entry for this worker
        (base_dir / ".sessions.json").write_text(json.dumps({}))

        # Use foreground mode to avoid fork/setsid issues in tests
        args = _make_start_args(name=name, resume=True, foreground=True, background=False)

        with patch("claude_worker.cli.run_manager"):
            with pytest.raises(SystemExit) as exc_info:
                cmd_start(args)
            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Recovered session from archive" in captured.err

    def test_resume_no_archive_no_session_errors(self, fake_worker, capsys, tmp_path):
        """No .sessions.json entry and no archive → error."""
        from claude_worker.cli import cmd_start

        name = "test-worker"
        fake_worker([], name=name)
        base_dir = tmp_path / "workers"

        # Remove the runtime dir
        runtime = base_dir / name
        import shutil
        shutil.rmtree(runtime)

        # Empty .sessions.json
        (base_dir / ".sessions.json").write_text(json.dumps({}))

        args = _make_start_args(name=name, resume=True)

        with pytest.raises(SystemExit) as exc_info:
            cmd_start(args)
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "no saved session" in captured.err
