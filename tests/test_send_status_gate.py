"""Tests for cmd_send's status gate (Imp-11 coverage).

cmd_send consults worker status before writing to the FIFO:
- `dead` → error with a start --resume hint
- `working` → error with a --queue hint
- `starting` → wait for it to transition
- `waiting` → proceed

This was implemented in Feature 1 but only manually tested. These tests
exercise each branch at the argparse-boundary level.
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


def _build_send_args(
    name: str,
    message: list[str],
    *,
    queue: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        name=name,
        message=message,
        queue=queue,
        show_response=False,
        show_full_response=False,
        chat=None,
        all_chats=False,
    )


class TestSendStatusGate:
    """cmd_send must refuse to send to a busy/dead worker (without --queue)."""

    def test_send_to_dead_worker_errors(self, fake_worker, capsys):
        """No pid file → worker is dead → error mentions --resume."""
        from claude_worker.cli import cmd_send

        name = fake_worker(
            [make_system_init("sys-uuid-0001-0002-000300040005")],
            alive=False,  # no pid file → dead
        )
        args = _build_send_args(name, ["hello"])
        with pytest.raises(SystemExit) as exc_info:
            cmd_send(args)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "dead" in captured.err
        assert "--resume" in captured.err

    def test_send_to_working_worker_errors(self, fake_worker, capsys):
        """Worker actively processing → error mentions --queue."""
        from claude_worker.cli import cmd_send

        name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("in-flight", "u1xx-0001-0002-0003-000400050006"),
                # No assistant/result yet → status = working
            ],
            alive=True,
        )
        args = _build_send_args(name, ["hello"])
        with pytest.raises(SystemExit) as exc_info:
            cmd_send(args)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "busy" in captured.err
        assert "--queue" in captured.err

    def test_queue_flag_bypasses_status_gate(self, fake_worker, monkeypatch):
        """--queue should bypass the busy check and proceed to write."""
        from claude_worker import cli as cw_cli

        name = cw_cli.get_base_dir() / "test-worker"
        # Re-use fake_worker fixture
        worker_name = fake_worker(
            [
                make_system_init("sys-uuid-0001-0002-000300040005"),
                make_user_message("in-flight", "u1xx-0001-0002-0003-000400050006"),
            ],
            alive=True,
        )
        # Create the FIFO for the write
        import os as _os

        fifo_path = cw_cli.get_runtime_dir(worker_name) / "in"
        _os.mkfifo(fifo_path)

        # Intercept the queue wait so we don't actually tail the log
        captured_args: dict = {}

        def fake_wait_for_queue_response(name, queue_id, **kwargs):
            captured_args["name"] = name
            captured_args["queue_id"] = queue_id
            return (0, "echo")

        monkeypatch.setattr(
            cw_cli, "_wait_for_queue_response", fake_wait_for_queue_response
        )

        args = _build_send_args(worker_name, ["hello"], queue=True)

        # Spawn a reader in a thread so the FIFO write doesn't block
        # forever on an empty FIFO with no consumer
        import threading

        def drain_fifo():
            with open(fifo_path, "r") as f:
                f.read()

        reader = threading.Thread(target=drain_fifo, daemon=True)
        reader.start()

        with pytest.raises(SystemExit) as exc_info:
            cw_cli.cmd_send(args)
        assert exc_info.value.code == 0  # success — gate was bypassed
        assert "queue_id" in captured_args
