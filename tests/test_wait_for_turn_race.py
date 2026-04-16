"""Tests for the #082/D101 race fix in _wait_for_turn.

Covers:
- ``_forward_scan_for_turn_end`` — standalone helper tests
- Gated flake-check: run the live-worker wrap-up test 10 times,
  fail if any iteration fails. Gated on CW_FLAKE_CHECK env so
  normal runs aren't slow.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_worker import cli as cw_cli


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestForwardScanForTurnEnd:
    def test_missing_log_returns_none(self, tmp_path: Path) -> None:
        assert (
            cw_cli._forward_scan_for_turn_end(tmp_path / "nope", after_uuid=None)
            is None
        )

    def test_no_marker_returns_first_turn_end(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        _write_jsonl(
            log,
            [
                {"type": "user", "uuid": "u1"},
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {"role": "assistant", "stop_reason": "end_turn"},
                },
                {"type": "result", "uuid": "r1"},
            ],
        )
        result = cw_cli._forward_scan_for_turn_end(log, after_uuid=None)
        assert result is not None
        assert result["uuid"] == "a1"  # first end-turn assistant

    def test_skips_up_to_marker(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        _write_jsonl(
            log,
            [
                {"type": "user", "uuid": "u1"},
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {"stop_reason": "end_turn"},
                },
                {"type": "result", "uuid": "marker"},
                {"type": "user", "uuid": "u2"},
                {
                    "type": "assistant",
                    "uuid": "a2",
                    "message": {"stop_reason": "end_turn"},
                },
                {"type": "result", "uuid": "r2"},
            ],
        )
        result = cw_cli._forward_scan_for_turn_end(log, after_uuid="marker")
        assert result is not None
        assert result["uuid"] == "a2"

    def test_no_turn_after_marker_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        _write_jsonl(
            log,
            [
                {"type": "user", "uuid": "u1"},
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {"stop_reason": "end_turn"},
                },
                {"type": "result", "uuid": "marker"},
                {"type": "user", "uuid": "u2"},
                # No end_turn / result after u2 yet
            ],
        )
        assert cw_cli._forward_scan_for_turn_end(log, after_uuid="marker") is None

    def test_prefix_uuid_match(self, tmp_path: Path) -> None:
        """Marker lookup honors _uuid_matches (prefix match like other helpers)."""
        log = tmp_path / "log"
        _write_jsonl(
            log,
            [
                {"type": "result", "uuid": "abcd1234-full-uuid-here"},
                {
                    "type": "assistant",
                    "uuid": "xyz",
                    "message": {"stop_reason": "end_turn"},
                },
            ],
        )
        result = cw_cli._forward_scan_for_turn_end(log, after_uuid="abcd1234")
        assert result is not None
        assert result["uuid"] == "xyz"

    def test_chat_tag_filter_skips_unmatched_turn(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        _write_jsonl(
            log,
            [
                {"type": "user", "uuid": "u1"},
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "untagged reply"}],
                    },
                },
                {"type": "result", "uuid": "r1"},
                {"type": "user", "uuid": "u2"},
                {
                    "type": "assistant",
                    "uuid": "a2",
                    "message": {
                        "role": "assistant",
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "[chat:me] tagged reply"}],
                    },
                },
                {"type": "result", "uuid": "r2"},
            ],
        )
        result = cw_cli._forward_scan_for_turn_end(log, after_uuid=None, chat_tag="me")
        assert result is not None
        assert result["uuid"] == "a2"


# ---------------------------------------------------------------------------
# Gated flake check: run the live-worker wrap-up test 10 times. Fails if
# any iteration regresses to the #082 timing bug. Gated on CW_FLAKE_CHECK
# so normal pytest runs don't waste ~60s on the sweep.

FLAKE_CHECK_ENV = "CW_FLAKE_CHECK"


@pytest.mark.skipif(
    os.environ.get(FLAKE_CHECK_ENV) != "1",
    reason=f"Set {FLAKE_CHECK_ENV}=1 to enable the 10x stability sweep",
)
def test_stop_wrapup_is_stable_across_10_runs() -> None:
    """Regression sweep for #082: 10 consecutive runs must all pass."""
    target = "tests/test_stop_wrapup.py::TestStopWrapup::test_live_worker_gets_wrapup_before_sigterm"
    failures = 0
    for _ in range(10):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", target, "--timeout=30", "-q"],
            capture_output=True,
            text=True,
            timeout=40,
        )
        if result.returncode != 0:
            failures += 1
    assert failures == 0, f"{failures}/10 runs failed — race still present"
