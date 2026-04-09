"""Tests for the _wait_for_turn scan-phase race and the after_uuid marker.

The race: cmd_send writes to the FIFO then calls _wait_for_turn, which scans
the existing log. If the scan runs BEFORE the new user message has been
forwarded from FIFO to claude, it sees the PRIOR turn's `result` message
and returns 0 immediately — handing a stale "turn done" to the caller.

Fix: `_wait_for_turn(after_uuid=marker)` skips entries up to and including
the marker, so a caller that captured the last UUID before writing can
avoid the stale match. cmd_send already does this (cli.py marker_uuid).
The `wait-for-turn` CLI subcommand also exposes --after-uuid for external
callers who need the same race protection.
"""

from __future__ import annotations

import argparse

import pytest

from conftest import (
    make_assistant_message,
    make_result_message,
    make_system_init,
    make_user_message,
)


def _build_wait_args(
    name: str,
    *,
    timeout: float | None = 0.5,
    settle: float = 0.0,
    after_uuid: str | None = None,
) -> argparse.Namespace:
    """Build an argparse.Namespace matching the wait-for-turn subparser."""
    return argparse.Namespace(
        name=name,
        timeout=timeout,
        settle=settle,
        after_uuid=after_uuid,
    )


# -- Low-level _wait_for_turn behavior ----------------------------------------


class TestWaitForTurnAfterUuid:
    """_wait_for_turn should respect after_uuid to avoid stale turn matches."""

    def test_without_after_uuid_finds_prior_result(self, fake_worker):
        """Baseline: scanning a log with a completed turn finds the result
        and returns 0. This is the normal path — and also the race path
        when the caller just wrote a FIFO message and the log hasn't
        caught up yet."""
        from claude_worker.cli import _wait_for_turn

        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("a1", "a1xx-0001-0002-0003-000400050006"),
                make_result_message("r1xx-0001-0002-0003-000400050006"),
            ],
            alive=True,
        )
        rc = _wait_for_turn(name, timeout=0.5)
        assert rc == 0  # finds the existing result

    def test_with_after_uuid_skips_prior_result(self, fake_worker):
        """With after_uuid pointing at the last message, the scan should
        skip the prior result and fall through to tail-waiting. With a
        short timeout and no new entries, it should return 2 (timeout)."""
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

    def test_with_after_uuid_finds_new_result(self, fake_worker):
        """With after_uuid pointing at a mid-log entry, a later result
        should be found during the scan phase."""
        from claude_worker.cli import _wait_for_turn

        # Marker points at assistant of turn 1; result of turn 2 should
        # be found after the marker.
        marker_uuid = "a1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("a1", marker_uuid),
                make_result_message("r1xx-0001-0002-0003-000400050006"),
                make_user_message("q2", "u2xx-0001-0002-0003-000400050006"),
                make_assistant_message("a2", "a2xx-0001-0002-0003-000400050006"),
                make_result_message("r2xx-0001-0002-0003-000400050006"),
            ],
            alive=True,
        )
        rc = _wait_for_turn(name, timeout=0.5, after_uuid=marker_uuid)
        assert rc == 0  # finds r2


# -- CLI surface: wait-for-turn --after-uuid must be exposed -------------------


class TestWaitForTurnCliSurface:
    """The wait-for-turn subcommand must accept --after-uuid and pass it
    through to _wait_for_turn so external callers get the same race
    protection that cmd_send uses internally."""

    def test_cmd_wait_for_turn_passes_after_uuid_through(
        self, fake_worker, monkeypatch
    ):
        """cmd_wait_for_turn must pass args.after_uuid to _wait_for_turn
        so the CLI actually reaches the race-protecting code path."""
        from claude_worker import cli as cw_cli

        last_uuid = "r1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("a1", "a1xx-0001-0002-0003-000400050006"),
                make_result_message(last_uuid),
            ],
            alive=True,
        )

        captured: dict = {}

        def _spy_wait_for_turn(
            name, timeout=None, after_uuid=None, settle=0.0, chat_tag=None
        ):
            captured["name"] = name
            captured["timeout"] = timeout
            captured["after_uuid"] = after_uuid
            captured["settle"] = settle
            captured["chat_tag"] = chat_tag
            return 0

        monkeypatch.setattr(cw_cli, "_wait_for_turn", _spy_wait_for_turn)

        args = _build_wait_args(name, timeout=1.0, after_uuid=last_uuid)
        with pytest.raises(SystemExit) as exc_info:
            cw_cli.cmd_wait_for_turn(args)
        assert exc_info.value.code == 0
        assert captured["after_uuid"] == last_uuid
