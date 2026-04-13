"""Tests for archive metadata.json preservation.

Ticket #060: when a worker is archived, the archive directory should include
a metadata.json with audit trail information (worker name, reason, timestamp,
session ID, identity, successor).
"""

from __future__ import annotations

import json
import os

import pytest


class TestArchiveCreatesMetadata:
    """archive_runtime_dir writes metadata.json into the archive."""

    def test_archive_creates_metadata_json(self, tmp_path, monkeypatch):
        """Archiving a runtime dir creates metadata.json in the archive."""
        from claude_worker import manager as cw_manager

        base = tmp_path / "workers"
        base.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base)

        runtime = base / "test-worker"
        runtime.mkdir()
        (runtime / "session").write_text("abc12345-session-id")
        (runtime / "log").write_text("")
        os.mkfifo(runtime / "in")

        archive_path = cw_manager.archive_runtime_dir("test-worker", reason="stop")
        assert archive_path is not None
        assert (archive_path / "metadata.json").exists()

    def test_archive_metadata_has_all_fields(self, tmp_path, monkeypatch):
        """metadata.json contains all required audit trail fields."""
        from claude_worker import manager as cw_manager

        base = tmp_path / "workers"
        base.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base)

        runtime = base / "audit-worker"
        runtime.mkdir()
        (runtime / "session").write_text("deadbeef-1234")
        (runtime / "log").write_text("")

        archive_path = cw_manager.archive_runtime_dir(
            "audit-worker", reason="stop", successor="new-worker"
        )
        assert archive_path is not None

        metadata = json.loads((archive_path / "metadata.json").read_text())
        expected_keys = {
            "worker_name",
            "archive_reason",
            "archive_timestamp",
            "session_id",
            "identity",
            "successor",
        }
        assert set(metadata.keys()) == expected_keys

    def test_archive_reason_stop(self, tmp_path, monkeypatch):
        """reason='stop' is recorded in metadata."""
        from claude_worker import manager as cw_manager

        base = tmp_path / "workers"
        base.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base)

        runtime = base / "stop-worker"
        runtime.mkdir()
        (runtime / "session").write_text("sess1234")

        archive_path = cw_manager.archive_runtime_dir(
            "stop-worker", reason="stop"
        )
        metadata = json.loads((archive_path / "metadata.json").read_text())
        assert metadata["archive_reason"] == "stop"
        assert metadata["worker_name"] == "stop-worker"

    def test_archive_reason_replaceme_with_successor(self, tmp_path, monkeypatch):
        """reason='replaceme' and successor are recorded in metadata."""
        from claude_worker import manager as cw_manager

        base = tmp_path / "workers"
        base.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base)

        runtime = base / "old-worker"
        runtime.mkdir()
        (runtime / "session").write_text("repl5678")

        archive_path = cw_manager.archive_runtime_dir(
            "old-worker", reason="replaceme", successor="new-worker"
        )
        metadata = json.loads((archive_path / "metadata.json").read_text())
        assert metadata["archive_reason"] == "replaceme"
        assert metadata["successor"] == "new-worker"

    def test_archive_metadata_missing_session(self, tmp_path, monkeypatch):
        """Archive with no session file gracefully produces empty session_id."""
        from claude_worker import manager as cw_manager

        base = tmp_path / "workers"
        base.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base)

        runtime = base / "no-session"
        runtime.mkdir()
        # No session file created

        archive_path = cw_manager.archive_runtime_dir(
            "no-session", reason="stop"
        )
        assert archive_path is not None

        metadata = json.loads((archive_path / "metadata.json").read_text())
        assert metadata["session_id"] == ""

    def test_archive_metadata_reads_identity(self, tmp_path, monkeypatch):
        """Identity is read from .sessions.json when available."""
        from claude_worker import manager as cw_manager

        base = tmp_path / "workers"
        base.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base)

        # Write a .sessions.json with identity info
        sessions = {"id-worker": {"identity": "pm", "session": "xyz"}}
        (base / ".sessions.json").write_text(json.dumps(sessions))

        runtime = base / "id-worker"
        runtime.mkdir()
        (runtime / "session").write_text("xyz12345")

        archive_path = cw_manager.archive_runtime_dir(
            "id-worker", reason="stop"
        )
        metadata = json.loads((archive_path / "metadata.json").read_text())
        assert metadata["identity"] == "pm"

    def test_archive_metadata_identity_missing_sessions_file(
        self, tmp_path, monkeypatch
    ):
        """Identity is empty string when .sessions.json doesn't exist."""
        from claude_worker import manager as cw_manager

        base = tmp_path / "workers"
        base.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base)

        runtime = base / "orphan"
        runtime.mkdir()
        (runtime / "session").write_text("orph1234")

        archive_path = cw_manager.archive_runtime_dir(
            "orphan", reason="exit"
        )
        metadata = json.loads((archive_path / "metadata.json").read_text())
        assert metadata["identity"] == ""

    def test_archive_default_reason_is_unknown(self, tmp_path, monkeypatch):
        """Default reason is 'unknown' when not specified."""
        from claude_worker import manager as cw_manager

        base = tmp_path / "workers"
        base.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base)

        runtime = base / "default-reason"
        runtime.mkdir()

        archive_path = cw_manager.archive_runtime_dir("default-reason")
        metadata = json.loads((archive_path / "metadata.json").read_text())
        assert metadata["archive_reason"] == "unknown"


class TestCleanupPassesReason:
    """cleanup_runtime_dir forwards reason to archive_runtime_dir."""

    def test_cleanup_passes_reason_exit(self, tmp_path, monkeypatch):
        """cleanup_runtime_dir with reason='exit' produces metadata with that reason."""
        from claude_worker import manager as cw_manager

        base = tmp_path / "workers"
        base.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base)
        # Stub legacy base to avoid touching /tmp
        monkeypatch.setattr(cw_manager, "_legacy_base_dir", lambda: tmp_path / "legacy")

        runtime = base / "exit-worker"
        runtime.mkdir()
        (runtime / "session").write_text("exit1234")
        (runtime / "log").write_text("")

        cw_manager.cleanup_runtime_dir("exit-worker", reason="exit")

        # The runtime dir is gone (rmtree after archive)
        assert not runtime.exists()

        # Find the archive dir
        archives = [d for d in base.iterdir() if d.is_dir() and "exit-worker." in d.name]
        assert len(archives) == 1

        metadata = json.loads((archives[0] / "metadata.json").read_text())
        assert metadata["archive_reason"] == "exit"
        assert metadata["worker_name"] == "exit-worker"

    def test_cleanup_default_reason_is_stop(self, tmp_path, monkeypatch):
        """cleanup_runtime_dir default reason is 'stop'."""
        from claude_worker import manager as cw_manager

        base = tmp_path / "workers"
        base.mkdir()
        monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base)
        monkeypatch.setattr(cw_manager, "_legacy_base_dir", lambda: tmp_path / "legacy")

        runtime = base / "stop-default"
        runtime.mkdir()
        (runtime / "session").write_text("stop1234")

        cw_manager.cleanup_runtime_dir("stop-default")

        archives = [d for d in base.iterdir() if d.is_dir() and "stop-default." in d.name]
        assert len(archives) == 1

        metadata = json.loads((archives[0] / "metadata.json").read_text())
        assert metadata["archive_reason"] == "stop"
