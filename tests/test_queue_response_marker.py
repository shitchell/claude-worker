"""Tests for queue correlation: _wait_for_queue_response marker race and
_generate_queue_id collision resistance.

The marker race (D2): _wait_for_queue_response scans the log for the
[queue:<id>] tag. The after_uuid marker skips past stale matches.

Queue ID collision (D12): _generate_queue_id uses random hex instead
of epoch-ms to eliminate sub-millisecond collision risk.
"""

from __future__ import annotations

import pytest

from conftest import (
    make_assistant_message,
    make_result_message,
    make_user_message,
)


class TestQueueResponseMarker:
    """_wait_for_queue_response must skip past after_uuid before matching."""

    def test_matches_tag_without_marker(self, fake_worker):
        """Baseline: without marker, the scan finds the tag anywhere."""
        from claude_worker.cli import _wait_for_queue_response

        name = fake_worker(
            [
                make_user_message("hi", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message(
                    "response [queue:123] here",
                    "a1xx-0001-0002-0003-000400050006",
                ),
                make_result_message("r1xx-0001-0002-0003-000400050006"),
            ],
            alive=True,
        )
        rc = _wait_for_queue_response(name, "123", timeout=0.5)
        assert rc == 0

    def test_skips_stale_tag_with_marker(self, fake_worker):
        """With a marker pointing past an existing stale tag, the scan
        should skip it and tail-wait (returning 2 on timeout)."""
        from claude_worker.cli import _wait_for_queue_response

        last_uuid = "r1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_user_message("hi", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message(
                    "stale response [queue:123]",
                    "a1xx-0001-0002-0003-000400050006",
                ),
                make_result_message(last_uuid),
            ],
            alive=True,
        )
        # Same queue ID, but marker points at the end of the log — the
        # stale [queue:123] from before the marker must be ignored.
        rc = _wait_for_queue_response(name, "123", timeout=0.5, after_uuid=last_uuid)
        assert rc == 2  # timeout — no new tag after the marker

    def test_finds_fresh_tag_after_marker(self, fake_worker):
        """A tag appearing after the marker must still be found."""
        from claude_worker.cli import _wait_for_queue_response

        marker = "a1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("stale", marker),
                make_user_message("q2", "u2xx-0001-0002-0003-000400050006"),
                make_assistant_message(
                    "fresh [queue:999]", "a2xx-0001-0002-0003-000400050006"
                ),
            ],
            alive=True,
        )
        rc = _wait_for_queue_response(name, "999", timeout=0.5, after_uuid=marker)
        assert rc == 0


class TestQueueIdCollisionResistance:
    """_generate_queue_id must produce collision-resistant IDs (D12)."""

    def test_ids_are_unique(self):
        """Two consecutive calls must produce different IDs."""
        from claude_worker.cli import _generate_queue_id

        id1 = _generate_queue_id()
        id2 = _generate_queue_id()
        assert id1 != id2

    def test_id_is_hex(self):
        """Queue IDs should be hex strings (not epoch-ms integers)."""
        from claude_worker.cli import _generate_queue_id

        qid = _generate_queue_id()
        # Should be valid hex and not look like an epoch timestamp
        int(qid, 16)  # raises ValueError if not hex
        assert len(qid) == 8  # token_hex(4) = 8 hex chars
