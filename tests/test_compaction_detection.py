"""Tests for compaction detection: compact_boundary counting
and the distinction between system/init and actual compactions."""

from __future__ import annotations

import json
from pathlib import Path

from claude_worker.cli import _count_compactions


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    """Write a list of dicts as JSONL to the given path."""
    with path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


class TestCountCompactions:
    """_count_compactions must only count compact_boundary, never init."""

    def test_count_compactions_empty_log(self, tmp_path: Path) -> None:
        """Empty log file returns empty list."""
        log = tmp_path / "log"
        log.write_text("")
        assert _count_compactions(log) == []

    def test_count_compactions_no_boundaries(self, tmp_path: Path) -> None:
        """Log with system/init messages but no compact_boundary returns empty.

        This is the critical test proving init != compaction.
        """
        log = tmp_path / "log"
        entries = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "test-sess",
                "model": "claude-opus-4-6[1m]",
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello"}]},
            },
            {"type": "result", "result": "success"},
            {
                "type": "system",
                "subtype": "init",
                "session_id": "test-sess",
                "model": "claude-opus-4-6[1m]",
            },
        ]
        _write_jsonl(log, entries)
        assert _count_compactions(log) == []

    def test_count_compactions_with_boundaries(self, tmp_path: Path) -> None:
        """Log with compact_boundary messages returns correct records."""
        log = tmp_path / "log"
        entries = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "test-sess",
                "model": "claude-opus-4-6[1m]",
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compactMetadata": {"trigger": "auto", "preTokens": 500000},
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compactMetadata": {"trigger": "manual", "preTokens": 800000},
            },
        ]
        _write_jsonl(log, entries)

        result = _count_compactions(log)
        assert len(result) == 2

        assert result[0]["line"] == 2
        assert result[0]["trigger"] == "auto"
        assert result[0]["pre_tokens"] == 500000

        assert result[1]["line"] == 3
        assert result[1]["trigger"] == "manual"
        assert result[1]["pre_tokens"] == 800000

    def test_count_compactions_mixed(self, tmp_path: Path) -> None:
        """Log with both system/init AND compact_boundary only counts boundaries."""
        log = tmp_path / "log"
        entries = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "s1",
                "model": "claude-opus-4-6[1m]",
            },
            {"type": "assistant", "message": {"content": []}},
            {"type": "result", "result": "success"},
            {
                "type": "system",
                "subtype": "init",
                "session_id": "s1",
                "model": "claude-opus-4-6[1m]",
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compactMetadata": {"trigger": "auto", "preTokens": 600000},
            },
            {
                "type": "system",
                "subtype": "init",
                "session_id": "s1",
                "model": "claude-opus-4-6[1m]",
            },
            {"type": "assistant", "message": {"content": []}},
            {"type": "result", "result": "success"},
            {
                "type": "system",
                "subtype": "init",
                "session_id": "s1",
                "model": "claude-opus-4-6[1m]",
            },
        ]
        _write_jsonl(log, entries)

        result = _count_compactions(log)
        assert len(result) == 1
        assert result[0]["trigger"] == "auto"
        assert result[0]["pre_tokens"] == 600000

    def test_init_is_not_compaction(self, tmp_path: Path) -> None:
        """10 system/init messages (simulating -p mode) produce 0 compactions.

        Regression test for the misidentification bug where system/init
        messages were incorrectly counted as compaction events.
        """
        log = tmp_path / "log"
        entries = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": f"sess-{i}",
                "model": "claude-opus-4-6[1m]",
            }
            for i in range(10)
        ]
        _write_jsonl(log, entries)

        result = _count_compactions(log)
        assert result == [], (
            f"Expected 0 compactions from init messages, got {len(result)}. "
            "system/init fires every turn in -p mode and is NOT a compaction."
        )

    def test_count_compactions_missing_file(self, tmp_path: Path) -> None:
        """Non-existent log file returns empty list without error."""
        log = tmp_path / "nonexistent-log"
        assert _count_compactions(log) == []

    def test_count_compactions_missing_metadata(self, tmp_path: Path) -> None:
        """compact_boundary without compactMetadata uses defaults."""
        log = tmp_path / "log"
        entries = [
            {"type": "system", "subtype": "compact_boundary"},
        ]
        _write_jsonl(log, entries)

        result = _count_compactions(log)
        assert len(result) == 1
        assert result[0]["trigger"] == "unknown"
        assert result[0]["pre_tokens"] == 0
