"""Tests for skeleton scaffolding (_scaffold_from_skeleton)."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_worker.cli import _scaffold_from_skeleton


class TestScaffoldFromSkeleton:
    """_scaffold_from_skeleton must copy structure without overwriting."""

    def test_creates_directories(self, tmp_path: Path):
        """Skeleton directories are created in the target."""
        skeleton = tmp_path / "skeleton"
        (skeleton / "handoffs").mkdir(parents=True)
        (skeleton / "notes").mkdir(parents=True)

        target = tmp_path / "target"
        _scaffold_from_skeleton(skeleton, target)

        assert (target / "handoffs").is_dir()
        assert (target / "notes").is_dir()

    def test_copies_files(self, tmp_path: Path):
        """Skeleton files are copied to the target."""
        skeleton = tmp_path / "skeleton"
        skeleton.mkdir()
        (skeleton / "LOG.md").write_text("# Log")

        target = tmp_path / "target"
        _scaffold_from_skeleton(skeleton, target)

        assert (target / "LOG.md").exists()
        assert (target / "LOG.md").read_text() == "# Log"

    def test_does_not_overwrite_existing(self, tmp_path: Path):
        """Existing files in the target are not overwritten."""
        skeleton = tmp_path / "skeleton"
        skeleton.mkdir()
        (skeleton / "LOG.md").write_text("skeleton content")

        target = tmp_path / "target"
        target.mkdir()
        (target / "LOG.md").write_text("existing content")

        _scaffold_from_skeleton(skeleton, target)

        assert (target / "LOG.md").read_text() == "existing content"

    def test_missing_skeleton_is_noop(self, tmp_path: Path):
        """Non-existent skeleton dir → no error, no target created."""
        skeleton = tmp_path / "nonexistent"
        target = tmp_path / "target"

        _scaffold_from_skeleton(skeleton, target)

        assert not target.exists()

    def test_nested_structure(self, tmp_path: Path):
        """Deeply nested skeleton structure is preserved."""
        skeleton = tmp_path / "skeleton"
        (skeleton / "a" / "b" / "c").mkdir(parents=True)
        (skeleton / "a" / "b" / "c" / "deep.txt").write_text("deep")

        target = tmp_path / "target"
        _scaffold_from_skeleton(skeleton, target)

        assert (target / "a" / "b" / "c" / "deep.txt").read_text() == "deep"
