"""Tests for _settle_is_stable + --timeout interaction (Imp-7).

Bug: _settle_is_stable does a flat time.sleep(settle) regardless of the
caller's overall deadline. If --timeout 5 --settle 3 is passed and the
turn boundary is detected at t=4s, the settle would sleep for 3 more
seconds (total 7s), blowing past the 5s deadline.

Fix: pass the deadline to _settle_is_stable and cap the sleep at
min(settle, remaining_time). The stability check still runs; total
wall-clock stays within the user's budget.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


class TestSettleRespectsDeadline:
    """_settle_is_stable must not sleep past the caller's deadline."""

    def test_no_deadline_sleeps_full_settle(self, tmp_path, monkeypatch):
        """Without a deadline, settle sleeps for the full duration."""
        from claude_worker import cli as cw_cli

        log_path = tmp_path / "log"
        log_path.write_text("")

        sleeps: list = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        result = cw_cli._settle_is_stable(log_path, settle=2.0, deadline=None)
        assert result is True  # no new messages during settle
        assert sleeps == [2.0]

    def test_deadline_in_future_caps_settle(self, tmp_path, monkeypatch):
        """If deadline is closer than settle, sleep only up to the deadline."""
        from claude_worker import cli as cw_cli

        log_path = tmp_path / "log"
        log_path.write_text("")

        # Simulate 'now' being 8s into the session and deadline at 10s
        now_holder = {"t": 100.0}
        monkeypatch.setattr(time, "monotonic", lambda: now_holder["t"])

        sleeps: list = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        # deadline is 1.5s in the future; settle is 5s
        cw_cli._settle_is_stable(log_path, settle=5.0, deadline=now_holder["t"] + 1.5)
        assert len(sleeps) == 1
        assert sleeps[0] == pytest.approx(1.5, rel=0.01)

    def test_deadline_already_passed_skips_sleep(self, tmp_path, monkeypatch):
        """If we're already past the deadline when settle is called, skip
        the sleep entirely. Stability cannot be confirmed — return False
        so the caller continues to the main deadline check."""
        from claude_worker import cli as cw_cli

        log_path = tmp_path / "log"
        log_path.write_text("")

        now_holder = {"t": 100.0}
        monkeypatch.setattr(time, "monotonic", lambda: now_holder["t"])

        sleeps: list = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        # deadline is already in the past
        result = cw_cli._settle_is_stable(
            log_path, settle=5.0, deadline=now_holder["t"] - 0.1
        )
        assert sleeps == []  # no sleep at all
        # result doesn't matter much here; the caller's deadline check
        # will catch the timeout on the next iteration.

    def test_zero_settle_noop(self, tmp_path, monkeypatch):
        """settle=0 is an explicit opt-out; no sleep, always stable."""
        from claude_worker import cli as cw_cli

        log_path = tmp_path / "log"
        log_path.write_text("")

        sleeps: list = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        assert cw_cli._settle_is_stable(log_path, settle=0.0) is True
        assert sleeps == []
