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

from claude_worker.cli import (
    _load_read_marker,
    _save_read_marker,
)
from claude_worker.manager import get_runtime_dir


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
