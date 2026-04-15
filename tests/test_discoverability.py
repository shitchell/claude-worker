"""Tests for tool discoverability commands (#071, D86).

Covers the four standard discoverability commands that every custom CLI
utility should expose (main:P10):

- `version` / `--version`  → print semver
- `changelog [--since V]`  → print CHANGELOG.md (optionally filtered)
- `docs`                   → print path to README.md
- `skill`                  → print path to installed skill
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from claude_worker import __version__
from claude_worker.cli import (
    cmd_changelog,
    cmd_docs,
    cmd_skill,
    cmd_version,
)


# -- version ---------------------------------------------------------


def test_version_prints_semver(capsys):
    cmd_version(argparse.Namespace())
    out = capsys.readouterr().out
    assert out.strip() == __version__


# -- changelog -------------------------------------------------------


SAMPLE_CHANGELOG = """# Changelog

## 0.1.16 (2026-04-15)

### Added
- Discoverability commands.

## 0.1.15 (2026-04-10)

### Fixed
- Resume edge case.

## 0.1.14 (2026-04-01)

### Added
- Initial feature.
"""


def _make_changelog_cwd(tmp_path: Path, monkeypatch) -> Path:
    """Create a CHANGELOG.md in tmp_path and chdir there."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE_CHANGELOG)
    monkeypatch.chdir(tmp_path)
    return path


def test_changelog_prints_content(tmp_path, monkeypatch, capsys):
    _make_changelog_cwd(tmp_path, monkeypatch)
    cmd_changelog(argparse.Namespace(since=None))
    out = capsys.readouterr().out
    assert "## 0.1.16" in out
    assert "## 0.1.15" in out
    assert "## 0.1.14" in out
    # Full content should be reproduced verbatim.
    assert out == SAMPLE_CHANGELOG


def test_changelog_since_filters(tmp_path, monkeypatch, capsys):
    _make_changelog_cwd(tmp_path, monkeypatch)
    cmd_changelog(argparse.Namespace(since="0.1.15"))
    out = capsys.readouterr().out
    assert "## 0.1.16" in out
    # Everything at or older than --since is excluded.
    assert "## 0.1.15" not in out
    assert "## 0.1.14" not in out
    # The header is retained.
    assert out.startswith("# Changelog")


def test_changelog_missing_errors(tmp_path, monkeypatch, capsys):
    # No CHANGELOG.md in cwd. Force the fallback lookup to miss too by
    # pointing Path.cwd() at an empty dir and pre-checking the fallback
    # candidate doesn't exist via monkeypatching `_find_project_file`.
    monkeypatch.chdir(tmp_path)

    from claude_worker import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_find_project_file", lambda _name: None)

    with pytest.raises(SystemExit) as excinfo:
        cmd_changelog(argparse.Namespace(since=None))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "No CHANGELOG.md found" in err


# -- docs ------------------------------------------------------------


def test_docs_prints_path(tmp_path, monkeypatch, capsys):
    readme = tmp_path / "README.md"
    readme.write_text("# Claude Worker\n")
    monkeypatch.chdir(tmp_path)

    cmd_docs(argparse.Namespace())
    out = capsys.readouterr().out.strip()
    assert out == str(readme)


def test_docs_missing_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    from claude_worker import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_find_project_file", lambda _name: None)

    with pytest.raises(SystemExit) as excinfo:
        cmd_docs(argparse.Namespace())
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "README.md not found" in err


# -- skill -----------------------------------------------------------


def test_skill_prints_path_when_installed(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path
    skill_dir = fake_home / ".claude" / "skills" / "claude-worker"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# claude-worker skill\n")

    from claude_worker import cli as cli_mod

    monkeypatch.setattr(cli_mod, "SKILL_INSTALL_PATH", skill_file)

    cmd_skill(argparse.Namespace())
    out = capsys.readouterr().out.strip()
    assert out == str(skill_file)


def test_skill_errors_when_missing(tmp_path, monkeypatch, capsys):
    missing = tmp_path / ".claude" / "skills" / "claude-worker" / "SKILL.md"

    from claude_worker import cli as cli_mod

    monkeypatch.setattr(cli_mod, "SKILL_INSTALL_PATH", missing)

    with pytest.raises(SystemExit) as excinfo:
        cmd_skill(argparse.Namespace())
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Skill not installed" in err
