"""Tests for wait-for-turn edge case: --after-uuid is the most recent entry.

Bug report (#030): wait-for-turn with --after-uuid X returns exit 0
immediately when X is the most recent log entry. Investigation shows
this should actually timeout (rc=2) because the reverse scan correctly
breaks at the marker without finding a turn boundary.
"""

from __future__ import annotations

import pytest

from conftest import (
    make_assistant_message,
    make_result_message,
    make_system_init,
    make_user_message,
)


class TestAfterUuidMostRecent:
    """When --after-uuid points to the most recent entry, wait-for-turn
    should poll for new turns and timeout if none arrive."""

    def test_after_uuid_is_last_result_times_out(self, fake_worker):
        """After-uuid = last result → no newer turn → timeout (rc=2)."""
        from claude_worker.cli import _wait_for_turn

        last_uuid = "r1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("a1", "a1xx-0001-0002-0003-000400050006"),
                make_result_message(last_uuid),
            ],
            alive=True,
        )
        rc = _wait_for_turn(name, timeout=0.5, after_uuid=last_uuid)
        assert rc == 2  # timeout — nothing after the marker

    def test_after_uuid_is_last_assistant_times_out(self, fake_worker):
        """After-uuid = last assistant → no newer turn → timeout."""
        from claude_worker.cli import _wait_for_turn

        last_uuid = "a1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("a1", last_uuid),
                make_result_message("r1xx-0001-0002-0003-000400050006"),
            ],
            alive=True,
        )
        # Note: after_uuid matches a1, so result r1 IS after the marker
        # and should be found. This is the CORRECT behavior — the turn
        # after the marker is complete.
        rc = _wait_for_turn(name, timeout=0.5, after_uuid=last_uuid)
        assert rc == 0  # finds r1 after the marker

    def test_dead_worker_returns_1_not_0(self, fake_worker):
        """If the worker is dead, should return 1, not 0."""
        from claude_worker.cli import _wait_for_turn

        last_uuid = "r1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("a1", "a1xx-0001-0002-0003-000400050006"),
                make_result_message(last_uuid),
            ],
            alive=False,  # dead worker
        )
        rc = _wait_for_turn(name, timeout=0.5, after_uuid=last_uuid)
        assert rc == 1  # dead
