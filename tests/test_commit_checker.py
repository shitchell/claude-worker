"""Tests for commit checker PostToolUse hook."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
import subprocess

import pytest

from claude_worker.commit_checker import (
    _check_commit,
    _log_commit,
    CAIRN_VALIDATE_TIMEOUT_SECONDS,
    COMMIT_LOG_NAME,
)


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


# -- Helpers for cairn validate tests --


def _mock_git_diff(changed_files: list[str]) -> MagicMock:
    """Create a mock subprocess result for git diff."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "\n".join(changed_files)
    return result


def _mock_cairn_validate(
    returncode: int, stderr: str = "", stdout: str = ""
) -> MagicMock:
    """Create a mock subprocess result for cairn validate."""
    result = MagicMock()
    result.returncode = returncode
    result.stderr = stderr
    result.stdout = stdout
    return result


def _make_side_effect(
    changed_files: list[str],
    cairn_rc: int = 0,
    cairn_stderr: str = "",
    cairn_stdout: str = "",
    cairn_raise: Exception | None = None,
):
    """Return a side_effect function that dispatches on the command."""

    def side_effect(cmd, **kwargs):
        if cmd[0] == "git":
            return _mock_git_diff(changed_files)
        if cmd[0] == "cairn":
            if cairn_raise:
                raise cairn_raise
            return _mock_cairn_validate(cairn_rc, cairn_stderr, cairn_stdout)
        return MagicMock(returncode=0, stdout="", stderr="")

    return side_effect


class TestCairnValidate:
    """Check 3: cairn validate runs when GVP library files change."""

    def test_cairn_validate_runs_on_gvp_change(self):
        """When cairn validate fails after GVP change, a CAIRN WARNING is emitted."""
        side_effect = _make_side_effect(
            changed_files=[
                "claude_worker/cli.py",
                "tests/test_cli.py",
                ".gvp/library/project.yaml",
            ],
            cairn_rc=1,
            cairn_stderr="error: missing ref for D42",
        )

        with patch(
            "claude_worker.commit_checker.subprocess.run", side_effect=side_effect
        ):
            warnings = _check_commit()

        cairn_warnings = [w for w in warnings if "CAIRN WARNING" in w]
        assert len(cairn_warnings) == 1
        assert "cairn validate" in cairn_warnings[0]
        assert "missing ref for D42" in cairn_warnings[0]

    def test_cairn_validate_passes_silently(self):
        """When cairn validate succeeds, no CAIRN WARNING is produced."""
        side_effect = _make_side_effect(
            changed_files=[
                "claude_worker/cli.py",
                "tests/test_cli.py",
                ".gvp/library/project.yaml",
            ],
            cairn_rc=0,
        )

        with patch(
            "claude_worker.commit_checker.subprocess.run", side_effect=side_effect
        ):
            warnings = _check_commit()

        assert not any("CAIRN WARNING" in w for w in warnings)

    def test_cairn_not_installed_skips_silently(self):
        """When cairn is not installed (FileNotFoundError), no warning is produced."""
        side_effect = _make_side_effect(
            changed_files=[
                "claude_worker/cli.py",
                "tests/test_cli.py",
                ".gvp/library/project.yaml",
            ],
            cairn_raise=FileNotFoundError("cairn"),
        )

        with patch(
            "claude_worker.commit_checker.subprocess.run", side_effect=side_effect
        ):
            warnings = _check_commit()

        assert not any("CAIRN" in w for w in warnings)

    def test_cairn_validate_timeout(self):
        """When cairn validate times out, a timeout warning is produced."""
        side_effect = _make_side_effect(
            changed_files=[
                "claude_worker/cli.py",
                "tests/test_cli.py",
                ".gvp/library/project.yaml",
            ],
            cairn_raise=subprocess.TimeoutExpired(
                cmd=["cairn", "validate"],
                timeout=CAIRN_VALIDATE_TIMEOUT_SECONDS,
            ),
        )

        with patch(
            "claude_worker.commit_checker.subprocess.run", side_effect=side_effect
        ):
            warnings = _check_commit()

        cairn_warnings = [w for w in warnings if "CAIRN WARNING" in w]
        assert len(cairn_warnings) == 1
        assert "timed out" in cairn_warnings[0]

    def test_no_gvp_files_no_cairn_check(self):
        """When no .gvp/library/ files are changed, cairn validate is NOT called."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "claude_worker/cli.py\ntests/test_cli.py\n"

        with patch(
            "claude_worker.commit_checker.subprocess.run", return_value=mock_result
        ) as mock_run:
            _check_commit()

        # subprocess.run should be called exactly once (for git diff),
        # never for cairn validate
        assert mock_run.call_count == 1
        assert mock_run.call_args[0][0][0] == "git"


class TestCommitLog:
    """Tests for _log_commit writing to .cwork/commits.log."""

    def test_log_commit_appends(self, tmp_path, monkeypatch):
        """_log_commit appends a line to .cwork/commits.log."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".cwork").mkdir()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc1234 Fix the thing"

        with patch(
            "claude_worker.commit_checker.subprocess.run", return_value=mock_result
        ):
            _log_commit()

        log_file = tmp_path / ".cwork" / COMMIT_LOG_NAME
        assert log_file.exists()
        content = log_file.read_text()
        assert "abc1234 Fix the thing" in content
        assert "|" in content  # timestamp | hash subject

    def test_log_commit_no_cwork_dir(self, tmp_path, monkeypatch):
        """_log_commit silently skips when .cwork/ doesn't exist."""
        monkeypatch.chdir(tmp_path)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc1234 Fix the thing"

        with patch(
            "claude_worker.commit_checker.subprocess.run", return_value=mock_result
        ):
            _log_commit()  # should not crash

        assert not (tmp_path / ".cwork" / COMMIT_LOG_NAME).exists()

    def test_log_commit_git_failure(self, tmp_path, monkeypatch):
        """_log_commit silently skips on git failure."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".cwork").mkdir()

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch(
            "claude_worker.commit_checker.subprocess.run", return_value=mock_result
        ):
            _log_commit()

        assert not (tmp_path / ".cwork" / COMMIT_LOG_NAME).exists()
