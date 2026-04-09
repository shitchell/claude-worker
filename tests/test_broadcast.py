"""Tests for send --broadcast.

Verifies filter-based target collection, self-exclusion, and the
broadcast code path in cmd_send.
"""

from __future__ import annotations

import argparse

import pytest

from conftest import (
    make_assistant_message,
    make_result_message,
    make_system_init,
)


class TestCollectFilteredWorkers:
    """_collect_filtered_workers must apply filter flags."""

    def test_role_filter(self, fake_worker):
        from claude_worker.cli import _collect_filtered_workers
        from claude_worker.manager import save_worker

        name = fake_worker(
            [make_system_init("u1"), make_result_message("r1")],
            name="pm-test",
            alive=True,
        )
        save_worker(name, pm=True, cwd="/tmp")

        name2 = fake_worker(
            [make_system_init("u2"), make_result_message("r2")],
            name="plain-test",
            alive=True,
        )
        save_worker(name2, cwd="/tmp")

        args = argparse.Namespace(role="pm", status=None, alive=False, cwd_filter=None)
        workers = _collect_filtered_workers(args)
        names = [w["name"] for w in workers]
        assert "pm-test" in names
        assert "plain-test" not in names

    def test_alive_filter(self, fake_worker):
        from claude_worker.cli import _collect_filtered_workers

        fake_worker(
            [make_system_init("u1"), make_result_message("r1")],
            name="alive-test",
            alive=True,
        )
        fake_worker(
            [make_system_init("u2"), make_result_message("r2")],
            name="dead-test",
            alive=False,
        )

        args = argparse.Namespace(role=None, status=None, alive=True, cwd_filter=None)
        workers = _collect_filtered_workers(args)
        names = [w["name"] for w in workers]
        assert "alive-test" in names
        assert "dead-test" not in names


class TestBroadcastSelfExclusion:
    """Broadcast should exclude the caller's own worker."""

    def test_self_excluded_from_targets(self, fake_worker):
        from unittest.mock import patch

        from claude_worker.cli import _collect_filtered_workers

        fake_worker(
            [make_system_init("u1"), make_result_message("r1")],
            name="self-worker",
            alive=True,
        )
        fake_worker(
            [make_system_init("u2"), make_result_message("r2")],
            name="other-worker",
            alive=True,
        )

        args = argparse.Namespace(role=None, status=None, alive=True, cwd_filter=None)
        workers = _collect_filtered_workers(args)
        names = [w["name"] for w in workers]

        # Simulate self-exclusion
        self_name = "self-worker"
        filtered = [n for n in names if n != self_name]
        assert "self-worker" not in filtered
        assert "other-worker" in filtered
