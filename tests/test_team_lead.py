"""Tests for --team-lead flag on start.

Verifies mutual exclusivity with --pm, metadata persistence, and [TL]
tag in ls output.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from claude_worker.cli import (
    TL_IDENTITY_RESOURCE,
    TL_INTERNALIZE_MESSAGE,
    _format_worker_line,
    _load_bundled_resource,
)
from claude_worker.manager import get_saved_worker, get_sessions_file


class TestTeamLeadMutualExclusivity:
    """--team-lead and --pm must be mutually exclusive."""

    def test_pm_and_team_lead_are_mutually_exclusive(self):
        """Argparse should reject --pm and --team-lead together."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "claude_worker",
                "start",
                "--pm",
                "--team-lead",
                "--name",
                "x",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "not allowed" in result.stderr


class TestTeamLeadMetadata:
    """--team-lead should save team_lead=True in worker metadata."""

    def test_metadata_saved(self, fake_worker):
        """Save team_lead metadata via save_worker (same path cmd_start uses)."""
        from claude_worker.manager import save_worker

        from tests.conftest import make_system_init

        name = fake_worker(
            [make_system_init("u1")],
            name="tl-meta",
        )
        # Simulate what cmd_start does for --team-lead
        save_worker(name, team_lead=True, cwd="/tmp")

        saved = get_saved_worker(name)
        assert saved is not None
        assert saved.get("team_lead") is True


class TestTeamLeadLsTag:
    """A --team-lead worker should show [TL] in ls output."""

    def test_tl_tag_in_format_worker_line(self, fake_worker):
        from claude_worker.manager import save_worker

        from tests.conftest import make_system_init, make_result_message

        name = fake_worker(
            [make_system_init("u1"), make_result_message("u2")],
            name="tl-ls",
            alive=True,
        )
        save_worker(name, team_lead=True, cwd="/tmp")

        line = _format_worker_line(name)
        assert line is not None
        assert "[TL]" in line
        assert "[PM]" not in line

    def test_pm_tag_not_tl(self, fake_worker):
        """A PM worker should show [PM], not [TL]."""
        from claude_worker.manager import save_worker

        from tests.conftest import make_system_init, make_result_message

        name = fake_worker(
            [make_system_init("u1"), make_result_message("u2")],
            name="pm-ls",
            alive=True,
        )
        save_worker(name, pm=True, cwd="/tmp")

        line = _format_worker_line(name)
        assert line is not None
        assert "[PM]" in line
        assert "[TL]" not in line

    def test_plain_worker_no_tag(self, fake_worker):
        """A plain worker should show neither [PM] nor [TL]."""
        from claude_worker.manager import save_worker

        from tests.conftest import make_system_init, make_result_message

        name = fake_worker(
            [make_system_init("u1"), make_result_message("u2")],
            name="plain-ls",
            alive=True,
        )
        save_worker(name, cwd="/tmp")

        line = _format_worker_line(name)
        assert line is not None
        assert "[PM]" not in line
        assert "[TL]" not in line


class TestTeamLeadIdentityResource:
    """The TL identity file should be loadable from the package."""

    def test_identity_file_exists(self):
        content = _load_bundled_resource("identities", TL_IDENTITY_RESOURCE)
        assert "Technical Lead" in content
        assert len(content) > 100
