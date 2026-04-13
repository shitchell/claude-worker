"""Tests for the deterministic migration system (#061)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from unittest import mock

import pytest

from claude_worker.cli import (
    MIGRATIONS_DIR,
    Migration,
    _discover_migrations,
    _read_migration_version,
    _run_migration,
    _sync_bundled_migrations,
    _write_migration_version,
    cmd_migrate,
)


def test_discover_migrations_empty_dir(tmp_path: Path) -> None:
    """Empty migrations dir returns empty list."""
    d = tmp_path / "migrations"
    d.mkdir()
    assert _discover_migrations(d) == []


def test_discover_migrations_finds_scripts(tmp_path: Path) -> None:
    """NNN-*.sh files are discovered in sorted order with correct numbers."""
    d = tmp_path / "migrations"
    d.mkdir()
    for name in ("002-beta.sh", "001-alpha.sh", "003-gamma.sh"):
        script = d / name
        script.write_text("#!/usr/bin/env bash\nexit 0\n")
        script.chmod(0o755)

    result = _discover_migrations(d)
    assert len(result) == 3
    assert result[0].number == 1
    assert result[0].name == "001-alpha.sh"
    assert result[1].number == 2
    assert result[1].name == "002-beta.sh"
    assert result[2].number == 3
    assert result[2].name == "003-gamma.sh"


def test_discover_migrations_skips_non_sh(tmp_path: Path) -> None:
    """Non-.sh files and files without NNN- prefix are ignored."""
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "001-valid.sh").write_text("#!/usr/bin/env bash\n")
    (d / "notes.txt").write_text("not a migration\n")
    (d / "readme.md").write_text("# readme\n")
    (d / "no-number.sh").write_text("#!/usr/bin/env bash\n")
    (d / "__init__.py").write_text("")

    result = _discover_migrations(d)
    assert len(result) == 1
    assert result[0].name == "001-valid.sh"


def test_read_write_migration_version(tmp_path: Path) -> None:
    """Write version 3, read back returns 3."""
    project = str(tmp_path / "project")
    os.makedirs(os.path.join(project, ".cwork"), exist_ok=True)

    _write_migration_version(project, 3)
    assert _read_migration_version(project) == 3


def test_read_migration_version_missing(tmp_path: Path) -> None:
    """No version file returns 0."""
    project = str(tmp_path / "project")
    os.makedirs(project, exist_ok=True)
    assert _read_migration_version(project) == 0


def test_migrate_runs_pending(tmp_path: Path) -> None:
    """Both pending migrations run and version updates to 2."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    project = tmp_path / "project"
    (project / ".cwork").mkdir(parents=True)

    # Create two migration scripts that touch marker files
    s1 = migrations_dir / "001-first.sh"
    s1.write_text(
        '#!/usr/bin/env bash\nmkdir -p "$1/.cwork"\ntouch "$1/.cwork/migrated-001"\n'
    )
    s1.chmod(0o755)

    s2 = migrations_dir / "002-second.sh"
    s2.write_text(
        '#!/usr/bin/env bash\nmkdir -p "$1/.cwork"\ntouch "$1/.cwork/migrated-002"\n'
    )
    s2.chmod(0o755)

    args = argparse.Namespace(
        project=str(project),
        dry_run=False,
        list_migrations=False,
    )

    with mock.patch("claude_worker.cli.MIGRATIONS_DIR", migrations_dir):
        with mock.patch("claude_worker.cli._sync_bundled_migrations", return_value=0):
            cmd_migrate(args)

    assert (project / ".cwork" / "migrated-001").exists()
    assert (project / ".cwork" / "migrated-002").exists()
    assert _read_migration_version(str(project)) == 2


def test_migrate_skips_applied(tmp_path: Path) -> None:
    """With version at 1, only migration 002 runs."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    project = tmp_path / "project"
    (project / ".cwork").mkdir(parents=True)
    _write_migration_version(str(project), 1)

    s1 = migrations_dir / "001-first.sh"
    s1.write_text(
        '#!/usr/bin/env bash\nmkdir -p "$1/.cwork"\ntouch "$1/.cwork/migrated-001"\n'
    )
    s1.chmod(0o755)

    s2 = migrations_dir / "002-second.sh"
    s2.write_text(
        '#!/usr/bin/env bash\nmkdir -p "$1/.cwork"\ntouch "$1/.cwork/migrated-002"\n'
    )
    s2.chmod(0o755)

    args = argparse.Namespace(
        project=str(project),
        dry_run=False,
        list_migrations=False,
    )

    with mock.patch("claude_worker.cli.MIGRATIONS_DIR", migrations_dir):
        with mock.patch("claude_worker.cli._sync_bundled_migrations", return_value=0):
            cmd_migrate(args)

    # 001 was already applied — its marker should NOT exist
    assert not (project / ".cwork" / "migrated-001").exists()
    # 002 should have run
    assert (project / ".cwork" / "migrated-002").exists()
    assert _read_migration_version(str(project)) == 2


def test_migrate_dry_run(tmp_path: Path) -> None:
    """Dry run doesn't execute scripts or update version."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    project = tmp_path / "project"
    (project / ".cwork").mkdir(parents=True)

    s1 = migrations_dir / "001-first.sh"
    s1.write_text(
        '#!/usr/bin/env bash\nmkdir -p "$1/.cwork"\ntouch "$1/.cwork/migrated-001"\n'
    )
    s1.chmod(0o755)

    args = argparse.Namespace(
        project=str(project),
        dry_run=True,
        list_migrations=False,
    )

    with mock.patch("claude_worker.cli.MIGRATIONS_DIR", migrations_dir):
        with mock.patch("claude_worker.cli._sync_bundled_migrations", return_value=0):
            cmd_migrate(args)

    assert not (project / ".cwork" / "migrated-001").exists()
    assert _read_migration_version(str(project)) == 0


def test_migrate_stops_on_failure(tmp_path: Path) -> None:
    """First migration succeeds, second fails. Version stays at 1, third doesn't run."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    project = tmp_path / "project"
    (project / ".cwork").mkdir(parents=True)

    s1 = migrations_dir / "001-ok.sh"
    s1.write_text(
        '#!/usr/bin/env bash\nmkdir -p "$1/.cwork"\ntouch "$1/.cwork/migrated-001"\n'
    )
    s1.chmod(0o755)

    s2 = migrations_dir / "002-fail.sh"
    s2.write_text("#!/usr/bin/env bash\nexit 1\n")
    s2.chmod(0o755)

    s3 = migrations_dir / "003-never.sh"
    s3.write_text(
        '#!/usr/bin/env bash\nmkdir -p "$1/.cwork"\ntouch "$1/.cwork/migrated-003"\n'
    )
    s3.chmod(0o755)

    args = argparse.Namespace(
        project=str(project),
        dry_run=False,
        list_migrations=False,
    )

    with mock.patch("claude_worker.cli.MIGRATIONS_DIR", migrations_dir):
        with mock.patch("claude_worker.cli._sync_bundled_migrations", return_value=0):
            cmd_migrate(args)

    assert (project / ".cwork" / "migrated-001").exists()
    assert not (project / ".cwork" / "migrated-003").exists()
    assert _read_migration_version(str(project)) == 1


def test_sync_bundled_migrations(tmp_path: Path) -> None:
    """Sync copies bundled 001 script to target dir."""
    target = tmp_path / "migrations"
    # Don't create it — _sync_bundled_migrations should create it
    synced = _sync_bundled_migrations(target)
    assert synced >= 1
    assert (target / "001-roles-directory-rename.sh").exists()
    # Verify it's executable
    assert os.access(target / "001-roles-directory-rename.sh", os.X_OK)
