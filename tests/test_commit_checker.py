"""Tests for commit checker PostToolUse hook."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
import subprocess

import pytest

from claude_worker.commit_checker import _check_commit


class TestCheckCommit:
    """_check_commit must detect missing tests and GVP updates."""

    def test_warns_on_python_without_tests(self):
        """Python source changed but no test files → G3 warning."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "claude_worker/cli.py\n"

        with patch(
            "claude_worker.commit_checker.subprocess.run", return_value=mock_result
        ):
            warnings = _check_commit()

        assert any("G3 WARNING" in w for w in warnings)

    def test_warns_on_python_without_gvp(self):
        """Python source changed but no .gvp/ update → GVP warning."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "claude_worker/cli.py\n"

        with patch(
            "claude_worker.commit_checker.subprocess.run", return_value=mock_result
        ):
            warnings = _check_commit()

        assert any("GVP WARNING" in w for w in warnings)

    def test_no_warning_with_tests_and_gvp(self):
        """Both test files and GVP updated → no warnings."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            "claude_worker/cli.py\ntests/test_new.py\n.gvp/library/project.yaml\n"
        )

        with patch(
            "claude_worker.commit_checker.subprocess.run", return_value=mock_result
        ):
            warnings = _check_commit()

        assert len(warnings) == 0

    def test_no_warning_for_docs_only(self):
        """Docs-only commit (no .py files) → no warnings."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "README.md\nCLAUDE.md\n"

        with patch(
            "claude_worker.commit_checker.subprocess.run", return_value=mock_result
        ):
            warnings = _check_commit()

        assert len(warnings) == 0
