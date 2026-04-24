"""Tests for per-session read markers (--new / --mark).

Verifies marker save/load, --new filtering, and consumer isolation.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from conftest import (
    make_assistant_message,
    make_result_message,
    make_system_init,
    make_user_message,
)

from claude_worker import cli as cw_cli
from claude_worker.cli import (
    _load_read_marker,
    _read_marker_path,
    _save_read_marker,
    cmd_read,
)
from claude_worker.manager import get_runtime_dir
from claude_worker.thread_store import (
    append_message,
    ensure_thread,
    pair_thread_id,
)


def _make_read_args(name: str, **overrides) -> argparse.Namespace:
    """Build a fully-defaulted Namespace for cmd_read; matches #089 test needs."""
    defaults: dict = dict(
        name=name,
        follow=False,
        since=None,
        until=None,
        new=False,
        mark=False,
        last_turn=False,
        exclude_user=False,
        n=None,
        count=False,
        summary=False,
        context=False,
        verbose=False,
        color=False,
        no_color=True,
        chat=None,
        all_chats=False,
        log=False,
        thread=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestReadMarkerSaveLoad:
    """Marker save/load round-trips correctly."""

    def test_save_and_load(self, fake_worker):
        name = fake_worker(
            [make_system_init("u1"), make_result_message("r1")],
        )
        runtime = get_runtime_dir(name)
        args = argparse.Namespace(chat=None)

        _save_read_marker(runtime, args, "abc123")
        loaded = _load_read_marker(runtime, args)
        assert loaded == "abc123"

    def test_load_missing_returns_none(self, fake_worker):
        name = fake_worker(
            [make_system_init("u1"), make_result_message("r1")],
        )
        runtime = get_runtime_dir(name)
        args = argparse.Namespace(chat=None)

        loaded = _load_read_marker(runtime, args)
        assert loaded is None

    def test_different_consumers_isolated(self, fake_worker):
        name = fake_worker(
            [make_system_init("u1"), make_result_message("r1")],
        )
        runtime = get_runtime_dir(name)

        args_a = argparse.Namespace(chat="consumer-a")
        args_b = argparse.Namespace(chat="consumer-b")

        _save_read_marker(runtime, args_a, "uuid-a")
        _save_read_marker(runtime, args_b, "uuid-b")

        assert _load_read_marker(runtime, args_a) == "uuid-a"
        assert _load_read_marker(runtime, args_b) == "uuid-b"


class TestMarkThroughCmdRead:
    """Regression coverage for #089 — ``cmd_read`` with ``--mark``
    must save the marker without crashing, regardless of which read
    path (thread-first or log-fallback) produced the messages.
    """

    def test_mark_saves_marker_via_log_path(self, fake_worker, capsys):
        """Log-path (``--log``) with ``--mark`` writes the marker file.

        Pre-fix this raised ``NameError: name 'runtime' is not defined``
        inside ``_render_read_output`` because ``runtime`` was never
        threaded into its scope.
        """
        last_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        entries = [
            make_system_init("s1-0000-0000-0000-000000000000"),
            make_user_message("hello", "uu-0000-0000-0000-000000000000"),
            make_assistant_message("world", last_uuid),
            make_result_message("rr-0000-0000-0000-000000000000"),
        ]
        name = fake_worker(entries, name="mark-log-test")
        runtime = get_runtime_dir(name)

        # --log forces the log path (bypasses the thread-first shortcut)
        args = _make_read_args(name, log=True, mark=True)
        cmd_read(args)

        marker_path = _read_marker_path(runtime, "cli")
        assert marker_path.exists(), "marker file was not written"
        assert marker_path.read_text().strip() == last_uuid

    def test_mark_saves_marker_via_thread_path(
        self, fake_worker, tmp_path, monkeypatch, capsys
    ):
        """Thread-path (default post-D88) with ``--mark`` writes the marker.

        Pre-fix the thread path returned without calling
        ``_save_read_marker`` at all, so ``--mark`` was a silent no-op
        on the default read path.
        """
        # fake_worker patches get_base_dir for the thread-store lookup
        entries = [make_system_init("s1-0000-0000-0000-000000000000")]
        name = fake_worker(entries, name="mark-thread-test")
        runtime = get_runtime_dir(name)

        tid = pair_thread_id("pm", name)
        ensure_thread(tid, participants=["pm", name])
        append_message(tid, sender="pm", content="hi")
        last_msg = append_message(tid, sender=name, content="hey")
        expected_last_id = str(last_msg["id"])

        monkeypatch.setenv("CW_WORKER_NAME", "pm")
        args = _make_read_args(name, mark=True)
        cmd_read(args)

        marker_path = _read_marker_path(runtime, "cli")
        assert marker_path.exists(), "marker file was not written"
        assert marker_path.read_text().strip() == expected_last_id

    def test_new_mark_round_trip_via_log_path(self, fake_worker, capsys):
        """``--new --mark`` then ``--new`` shows only messages after
        the last marker — verifies the full save-then-filter loop on
        the log path.
        """
        first_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        entries = [
            make_system_init("s1-0000-0000-0000-000000000000"),
            make_user_message("q1", "uu11-0000-0000-0000-000000000001"),
            make_assistant_message("a1", first_uuid),
            make_result_message("rr-0000-0000-0000-000000000000"),
        ]
        name = fake_worker(entries, name="new-mark-log")

        args1 = _make_read_args(name, log=True, new=True, mark=True)
        cmd_read(args1)
        capsys.readouterr()  # drain

        # Append a second exchange
        import json

        second_uuid = "bbbbbbbb-5555-6666-7777-888888888888"
        new_entries = [
            make_user_message("q2", "uu22-0000-0000-0000-000000000002"),
            make_assistant_message("a2", second_uuid),
            make_result_message("rr2-0000-0000-0000-000000000001"),
        ]
        runtime = get_runtime_dir(name)
        with (runtime / "log").open("a") as f:
            for entry in new_entries:
                f.write(json.dumps(entry) + "\n")

        args2 = _make_read_args(name, log=True, new=True, mark=True)
        cmd_read(args2)
        out = capsys.readouterr().out
        assert "a2" in out
        assert "q2" in out
        assert "a1" not in out
        assert "q1" not in out
