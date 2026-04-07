"""Tests for _iter_log_reverse (Imp-5).

The helper reads a JSONL log file backwards in chunks and yields parsed
entries from newest to oldest. This lets the "last X" helpers (last uuid,
last assistant preview, worker status, wait-for-turn scan) avoid reading
the entire log for a single data point, turning O(n) per-call into
O(chunk_size) amortized for long-running workers.

Correctness is critical: the chunk-boundary buffer handling is the kind
of code that's easy to get subtly wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


class TestIterLogReverseBasic:
    """Yield all entries in reverse order, handling standard cases."""

    def test_empty_file_yields_nothing(self, tmp_path):
        from claude_worker.cli import _iter_log_reverse

        log = tmp_path / "log"
        log.write_text("")
        assert list(_iter_log_reverse(log)) == []

    def test_nonexistent_file_yields_nothing(self, tmp_path):
        from claude_worker.cli import _iter_log_reverse

        assert list(_iter_log_reverse(tmp_path / "nope")) == []

    def test_single_entry(self, tmp_path):
        from claude_worker.cli import _iter_log_reverse

        log = tmp_path / "log"
        log.write_text('{"a":1}\n')
        assert list(_iter_log_reverse(log)) == [{"a": 1}]

    def test_multiple_entries_reverse_order(self, tmp_path):
        from claude_worker.cli import _iter_log_reverse

        log = tmp_path / "log"
        log.write_text('{"a":1}\n{"b":2}\n{"c":3}\n')
        result = list(_iter_log_reverse(log))
        assert result == [{"c": 3}, {"b": 2}, {"a": 1}]

    def test_no_trailing_newline(self, tmp_path):
        """A file without a trailing newline (last line is final bytes)."""
        from claude_worker.cli import _iter_log_reverse

        log = tmp_path / "log"
        log.write_text('{"a":1}\n{"b":2}')
        result = list(_iter_log_reverse(log))
        assert result == [{"b": 2}, {"a": 1}]


class TestIterLogReverseChunkBoundaries:
    """The buffer handling must correctly glue lines split across chunks."""

    def test_chunk_boundary_splits_line(self, tmp_path):
        """With a tiny chunk_size that splits every line mid-way."""
        from claude_worker.cli import _iter_log_reverse

        log = tmp_path / "log"
        log.write_text('{"a":1}\n{"b":2}\n{"c":3}\n')
        # chunk_size=4 will split every line across at least one boundary
        result = list(_iter_log_reverse(log, chunk_size=4))
        assert result == [{"c": 3}, {"b": 2}, {"a": 1}]

    def test_chunk_size_one(self, tmp_path):
        """Pathological chunk_size=1 should still yield correct order."""
        from claude_worker.cli import _iter_log_reverse

        log = tmp_path / "log"
        log.write_text('{"a":1}\n{"b":2}\n')
        result = list(_iter_log_reverse(log, chunk_size=1))
        assert result == [{"b": 2}, {"a": 1}]

    def test_many_entries(self, tmp_path):
        """Stress test with 200 entries and a moderate chunk size."""
        from claude_worker.cli import _iter_log_reverse

        entries = [{"n": i, "text": f"entry number {i}"} for i in range(200)]
        log = tmp_path / "log"
        log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        result = list(_iter_log_reverse(log, chunk_size=64))
        assert len(result) == 200
        assert result[0] == {"n": 199, "text": "entry number 199"}
        assert result[-1] == {"n": 0, "text": "entry number 0"}
        # Full reverse order
        for i, entry in enumerate(result):
            assert entry["n"] == 199 - i


class TestIterLogReverseMalformedLines:
    """Invalid JSONL lines should be silently skipped, not raised."""

    def test_invalid_json_line_skipped(self, tmp_path):
        from claude_worker.cli import _iter_log_reverse

        log = tmp_path / "log"
        log.write_text('{"a":1}\nnot json\n{"b":2}\n')
        result = list(_iter_log_reverse(log))
        assert result == [{"b": 2}, {"a": 1}]

    def test_empty_lines_skipped(self, tmp_path):
        from claude_worker.cli import _iter_log_reverse

        log = tmp_path / "log"
        log.write_text('{"a":1}\n\n{"b":2}\n')
        result = list(_iter_log_reverse(log))
        assert result == [{"b": 2}, {"a": 1}]


class TestIterLogReverseIsLazy:
    """The iterator should stop reading once the caller stops iterating."""

    def test_stops_after_first_yield(self, tmp_path):
        """Only reads enough chunks to deliver the first yield."""
        from claude_worker.cli import _iter_log_reverse

        log = tmp_path / "log"
        # 1000 entries
        log.write_text("\n".join(json.dumps({"n": i}) for i in range(1000)) + "\n")

        # Take just the first (newest) entry
        it = _iter_log_reverse(log, chunk_size=32)
        first = next(it)
        assert first == {"n": 999}
        it.close()  # explicitly release the underlying file
