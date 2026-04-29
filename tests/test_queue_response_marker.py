"""Tests for queue correlation: _wait_for_queue_response marker race,
graceful fallback on timeout, and _generate_queue_id collision resistance.

The marker race (D2): _wait_for_queue_response scans the log for the
[queue:<id>] tag. The after_uuid marker skips past stale matches.

The graceful fallback (D109): when the literal tag never echoes, fall
back to checking for any post-marker assistant turn-end. If one exists,
the message was delivered — exit 0 with a "treating as success" note.
The new return contract is a (rc, reason) tuple where reason is one of
"echo", "turn-end-fallback", "stuck", "died", "transport".

Queue ID collision (D12): _generate_queue_id uses random hex instead
of epoch-ms to eliminate sub-millisecond collision risk.
"""

from __future__ import annotations

import argparse

import pytest

from conftest import (
    make_assistant_message,
    make_result_message,
    make_user_message,
)


class TestQueueResponseMarker:
    """_wait_for_queue_response must skip past after_uuid before matching
    and return the new (rc, reason) tuple contract."""

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
        rc, reason = _wait_for_queue_response(name, "123", timeout=0.5)
        assert (rc, reason) == (0, "echo")


class TestQueueResponseFallback:
    """Cases T1-T7 from TECHNICAL.md / D109 — graceful fallback contract."""

    def test_t1_echo_after_marker(self, fake_worker):
        """T1: recipient echoes [queue:<id>] strictly after the marker."""
        from claude_worker.cli import _wait_for_queue_response

        marker = "a1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("stale", marker),
                make_user_message("q2", "u2xx-0001-0002-0003-000400050006"),
                make_assistant_message(
                    "fresh [queue:999]",
                    "a2xx-0001-0002-0003-000400050006",
                ),
            ],
            alive=True,
        )
        rc, reason = _wait_for_queue_response(
            name, "999", timeout=0.5, after_uuid=marker
        )
        assert (rc, reason) == (0, "echo")

    def test_t2_turn_end_after_marker_no_echo(self, fake_worker):
        """T2: recipient produces a turn-end after the marker but never
        echoes the queue tag — fallback says success."""
        from claude_worker.cli import _wait_for_queue_response

        marker = "a1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("stream-chunk", marker),
                make_user_message("q2", "u2xx-0001-0002-0003-000400050006"),
                make_assistant_message(
                    "real reply, no tag echo",
                    "a2xx-0001-0002-0003-000400050006",
                    stop_reason="end_turn",
                ),
            ],
            alive=True,
        )
        rc, reason = _wait_for_queue_response(
            name, "999", timeout=0.5, after_uuid=marker
        )
        assert (rc, reason) == (0, "turn-end-fallback")

    def test_t3_never_produces_turn_end(self, fake_worker):
        """T3: recipient is stuck — no turn-end, no echo. Returns (1, "stuck")."""
        from claude_worker.cli import _wait_for_queue_response

        marker = "a1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("mid-turn chunk", marker),
            ],
            alive=True,
        )
        rc, reason = _wait_for_queue_response(
            name, "999", timeout=0.5, after_uuid=marker
        )
        assert (rc, reason) == (1, "stuck")

    def test_t4_worker_died(self, fake_worker):
        """T4: worker process died after delivery. Even if a turn-end was
        produced, the death takes precedence so operators see the failure."""
        from claude_worker.cli import _wait_for_queue_response

        marker = "a1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("stream-chunk", marker),
                make_assistant_message(
                    "last gasp",
                    "a2xx-0001-0002-0003-000400050006",
                    stop_reason="end_turn",
                ),
            ],
            alive=False,
        )
        rc, reason = _wait_for_queue_response(
            name, "999", timeout=0.5, after_uuid=marker
        )
        assert (rc, reason) == (1, "died")

    def test_t5_transport_failure_returns_2(self, fake_worker, monkeypatch):
        """T5: ``append_message`` raises (transport failure). cmd_send
        must surface this as exit 2, distinct from the new "stuck" exit 1."""
        from claude_worker import cli, thread_store

        marker = "a1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_user_message("hi", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message("ok", marker),
            ],
            alive=True,
        )

        def _boom(*_args, **_kwargs):
            raise OSError("simulated transport failure")

        monkeypatch.setattr(thread_store, "append_message", _boom)

        args = argparse.Namespace(
            name=name,
            message=["hello"],
            queue=True,
            show_response=False,
            show_full_response=False,
            chat=None,
            all_chats=False,
            dry_run=False,
            verbose=False,
            broadcast=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_send(args)
        assert exc_info.value.code == 2

    def test_t6_stale_tag_no_fresh_echo_no_fresh_turn_end(self, fake_worker):
        """T6: regression guard for D2 — stale [queue:<id>] from a prior
        cycle, marker points past it, and nothing meaningful follows.
        Returns (1, "stuck") — neither the stale tag nor a missing
        turn-end can fake delivery success."""
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
        rc, reason = _wait_for_queue_response(
            name, "123", timeout=0.5, after_uuid=last_uuid
        )
        assert (rc, reason) == (1, "stuck")

    def test_t7_stale_tag_before_marker_fresh_turn_end_after(self, fake_worker):
        """T7: combined-defense — stale [queue:<id>] echo precedes the
        marker (so the tail loop must skip it via after_uuid), and a
        fresh assistant turn-end follows the marker but without echo.
        Fallback gives (0, "turn-end-fallback")."""
        from claude_worker.cli import _wait_for_queue_response

        marker = "a1xx-0001-0002-0003-000400050006"
        name = fake_worker(
            [
                make_user_message("q1", "u1xx-0001-0002-0003-000400050006"),
                make_assistant_message(
                    "stale response [queue:STALE]",
                    "a0xx-0001-0002-0003-000400050006",
                ),
                make_assistant_message("marker chunk", marker),
                make_user_message("q2", "u2xx-0001-0002-0003-000400050006"),
                make_assistant_message(
                    "fresh reply, no echo",
                    "a2xx-0001-0002-0003-000400050006",
                    stop_reason="end_turn",
                ),
            ],
            alive=True,
        )
        rc, reason = _wait_for_queue_response(
            name, "STALE", timeout=0.5, after_uuid=marker
        )
        assert (rc, reason) == (0, "turn-end-fallback")


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
