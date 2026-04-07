"""Tests for _wait_for_queue_response marker UUID race (Imp-6).

Bug: _wait_for_queue_response scans the log from byte 0 looking for the
[queue:<id>] tag. If the same tag string is already in the log from a
prior cycle (or, more subtly, if a sub-millisecond collision causes two
queue IDs to collide), it returns 0 against the wrong message.

Fix: accept an after_uuid marker — same pattern as _wait_for_turn — and
only consider log entries appearing AFTER that marker when matching.
cmd_send captures _get_last_uuid(log_file) before writing the FIFO,
same as it already does for the non-queue wait-for-turn path.
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
