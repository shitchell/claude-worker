"""Tests for .cwork/ directory monitoring."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_worker.manager import (
    diff_cwork_snapshots,
    snapshot_cwork_dir,
)


class TestSnapshotCworkDir:
    """snapshot_cwork_dir must capture file mtimes and sizes."""

    def test_returns_empty_for_missing_dir(self, tmp_path: Path):
        assert snapshot_cwork_dir(str(tmp_path)) == {}

    def test_captures_files(self, tmp_path: Path):
        cwork = tmp_path / ".cwork" / "tickets"
        cwork.mkdir(parents=True)
        (cwork / "INDEX.md").write_text("# Tickets")
        snap = snapshot_cwork_dir(str(tmp_path))
        assert ".cwork/tickets/INDEX.md" in snap
        mtime, size = snap[".cwork/tickets/INDEX.md"]
        assert size == len("# Tickets")


class TestDiffSnapshots:
    """diff_cwork_snapshots must detect added and modified files."""

    def test_no_changes(self):
        snap = {"a": (1.0, 10), "b": (2.0, 20)}
        assert diff_cwork_snapshots(snap, snap) == []

    def test_new_file_detected(self):
        old = {"a": (1.0, 10)}
        new = {"a": (1.0, 10), "b": (2.0, 20)}
        changed = diff_cwork_snapshots(old, new)
        assert changed == ["b"]

    def test_modified_file_detected(self):
        old = {"a": (1.0, 10)}
        new = {"a": (2.0, 15)}  # different mtime and size
        changed = diff_cwork_snapshots(old, new)
        assert changed == ["a"]

    def test_empty_old_returns_all_new(self):
        new = {"a": (1.0, 10), "b": (2.0, 20)}
        # Empty old = first scan, should return all files
        changed = diff_cwork_snapshots({}, new)
        assert sorted(changed) == ["a", "b"]
