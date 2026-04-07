"""Tests for `start --resume` error handling (Imp-4).

Bug: running `claude-worker start --resume` without `--name` would
silently generate a random worker name (via generate_name()) and then
fail with `Error: no saved session for worker 'worker-7e3a'` — pointing
at a name the user never provided. Confusing failure mode.

Fix: require --name when --resume is passed, with a clear error message.
"""

from __future__ import annotations

import argparse

import pytest


class TestStartResumeRequiresName:
    """start --resume without --name must error clearly, not silently
    generate a random name."""

    def test_resume_without_name_errors(self, fake_worker, capsys):
        """No random name, no 'missing session' message — a direct error."""
        from claude_worker.cli import cmd_start

        # Use fake_worker (with its monkey-patched base dir) so the cmd_start
        # code path doesn't touch the real /tmp/claude-workers.
        fake_worker([], name="unused-fixture-anchor")

        args = argparse.Namespace(
            name=None,
            cwd=None,
            prompt_file=None,
            prompt=None,
            agent=None,
            resume=True,
            background=True,
            show_response=False,
            show_full_response=False,
            pm=False,
            claude_args=[],
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_start(args)
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        # Error must clearly reference the --name requirement, not a
        # randomly-generated worker identifier
        assert "--name" in captured.err
        assert "--resume" in captured.err
        # Should NOT mention a random worker-XXXX name
        assert "worker-" not in captured.err
