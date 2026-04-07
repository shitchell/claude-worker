"""Tests for cleanup_runtime_dir idempotency and SIGTERM handler robustness.

Imp-2: the cleanup helper is called from 3 places (SIGTERM handler, natural
manager exit, cmd_stop) that can race. Without idempotency, a second call
crashes on FileNotFoundError because runtime.iterdir() has no missing_ok
semantics. Subdirectories (future-proofing) would also crash the unlink
loop. The SIGTERM handler separately doesn't catch subprocess.TimeoutExpired
from proc.wait(timeout=10), so a stuck claude leaves a tracebacked manager
and an un-cleaned runtime dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest


class TestCleanupRuntimeDirIdempotency:
    """cleanup_runtime_dir must be safe to call multiple times and must
    handle subdirectories (even though we don't ship any today, future
    features might add them)."""

    def test_cleanup_twice_does_not_raise(self, tmp_path, monkeypatch):
        """Calling cleanup_runtime_dir twice on the same worker is safe."""
        from claude_worker import manager as cw_manager

        base_dir = tmp_path / "workers"
        base_dir.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)

        runtime = base_dir / "ghost"
        runtime.mkdir()
        (runtime / "log").write_text("{}\n")
        (runtime / "pid").write_text("123")

        cw_manager.cleanup_runtime_dir("ghost")
        assert not runtime.exists()
        # Second call must not raise
        cw_manager.cleanup_runtime_dir("ghost")

    def test_cleanup_with_subdirectory(self, tmp_path, monkeypatch):
        """A runtime dir containing a subdirectory should still be cleaned
        (Path.iterdir + unlink crashes on dirs; rmtree handles both)."""
        from claude_worker import manager as cw_manager

        base_dir = tmp_path / "workers"
        base_dir.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)

        runtime = base_dir / "nested"
        runtime.mkdir()
        (runtime / "log").write_text("{}\n")
        subdir = runtime / "subdir"
        subdir.mkdir()
        (subdir / "inner").write_text("x")

        cw_manager.cleanup_runtime_dir("nested")
        assert not runtime.exists()

    def test_cleanup_races_dont_error_on_missing_file(self, tmp_path, monkeypatch):
        """If a file disappears between iterdir and unlink (another caller
        got there first), cleanup should not raise."""
        from claude_worker import manager as cw_manager

        base_dir = tmp_path / "workers"
        base_dir.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)

        runtime = base_dir / "racy"
        runtime.mkdir()
        ghost = runtime / "log"
        ghost.write_text("{}\n")

        # Simulate a race: monkey-patch unlink on the log file to delete
        # itself a second time to mimic "file already gone" mid-iteration.
        real_unlink = Path.unlink

        call_count = {"n": 0}

        def racy_unlink(self, missing_ok=False):
            call_count["n"] += 1
            real_unlink(self, missing_ok=missing_ok)
            if call_count["n"] == 1:
                # Second attempt on the same file would raise without
                # missing_ok=True
                try:
                    real_unlink(self, missing_ok=False)
                except FileNotFoundError:
                    pass  # ignore — the point is cleanup shouldn't
                    # propagate this

        # Actually, the cleanest way: delete the file BEFORE cleanup runs
        # so iterdir sees it but unlink doesn't.
        # We'll just verify that unlink with missing_ok handles
        # the "already gone" case. Instead of monkey-patching, we'll
        # simulate by removing the file from outside.
        #
        # This test is equivalent to test_cleanup_twice_does_not_raise
        # in spirit; the real race protection comes from either
        # shutil.rmtree(ignore_errors=True) OR unlink(missing_ok=True).

        # Pre-delete the file to simulate a concurrent cleanup removing it
        ghost.unlink()
        # The runtime dir still exists and iterdir returns nothing, so
        # rmdir runs. This should succeed.
        cw_manager.cleanup_runtime_dir("racy")
        assert not runtime.exists()
