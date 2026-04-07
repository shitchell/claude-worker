"""Tests for missing-tag dedup log size cap (Imp-12).

The missing-tag log (.claude-workers/<UID>/<name>/missing-tags.json) was
keyed by UUID with no eviction. A PM worker with thousands of tagged
turns and a small fraction of missed tags would accumulate dedup entries
forever, and each read of the worker's log would rewrite the whole
dedup JSON file. Slow, unbounded disk footprint.

Fix: cap the dedup log at MISSING_TAG_LOG_MAX_ENTRIES entries. When the
cap would be exceeded, drop the OLDEST entries (FIFO) — if they're old
enough to fall off the cap, the operator has had ample time to see the
warnings for them.
"""

from __future__ import annotations

import json

import pytest


class TestMissingTagLogCap:
    """_handle_missing_tag_reports respects MISSING_TAG_LOG_MAX_ENTRIES."""

    def test_cap_constant_exists_and_is_reasonable(self):
        from claude_worker.cli import MISSING_TAG_LOG_MAX_ENTRIES

        # A reasonable cap: enough to hold a day or two of real misses,
        # small enough to not grow unbounded.
        assert isinstance(MISSING_TAG_LOG_MAX_ENTRIES, int)
        assert 100 <= MISSING_TAG_LOG_MAX_ENTRIES <= 10000

    def test_entries_beyond_cap_evict_oldest(self, fake_worker, monkeypatch):
        """Adding more than the cap's worth of entries should keep only
        the most recent ones."""
        from claude_worker import cli as cw_cli

        # Use a small cap for the test so we don't need to synthesize
        # thousands of entries
        monkeypatch.setattr(cw_cli, "MISSING_TAG_LOG_MAX_ENTRIES", 5)

        name = fake_worker([])  # empty log, just need the runtime dir

        # Record 10 misses; only the last 5 should survive
        reports = [
            {
                "uuid": f"uuid-{i:04d}-0000-0000-0000-000000000000",
                "chat_id": f"chat-{i}",
                "preview": f"miss #{i}",
            }
            for i in range(10)
        ]
        for report in reports:
            cw_cli._handle_missing_tag_reports(name, [report])

        log_path = cw_cli._missing_tag_log_path(name)
        data = json.loads(log_path.read_text())
        assert len(data) == 5

        # The last 5 UUIDs should be present; the first 5 evicted
        surviving_uuids = set(data.keys())
        for i in range(5):
            assert f"uuid-{i:04d}-0000-0000-0000-000000000000" not in surviving_uuids
        for i in range(5, 10):
            assert f"uuid-{i:04d}-0000-0000-0000-000000000000" in surviving_uuids

    def test_under_cap_keeps_everything(self, fake_worker, monkeypatch):
        """If we're under the cap, no eviction happens."""
        from claude_worker import cli as cw_cli

        monkeypatch.setattr(cw_cli, "MISSING_TAG_LOG_MAX_ENTRIES", 100)

        name = fake_worker([])

        reports = [
            {
                "uuid": f"uuid-{i:04d}-0000-0000-0000-000000000000",
                "chat_id": f"chat-{i}",
                "preview": f"miss #{i}",
            }
            for i in range(10)
        ]
        for report in reports:
            cw_cli._handle_missing_tag_reports(name, [report])

        log_path = cw_cli._missing_tag_log_path(name)
        data = json.loads(log_path.read_text())
        assert len(data) == 10
