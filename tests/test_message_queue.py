"""Tests for the message queue system (guaranteed delivery via reply).

Covers queue file creation, queue draining, and the reply subcommand.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from claude_worker.manager import (
    drain_queue,
    enqueue_message,
    get_queue_dir,
)


class TestEnqueueMessage:
    """enqueue_message must create a well-formed queue file."""

    def test_creates_queue_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("claude_worker.manager.Path.home", lambda: tmp_path)
        path = enqueue_message("test-worker", "pm-sender", "hello from PM")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["sender"] == "pm-sender"
        assert data["content"] == "hello from PM"
        assert "timestamp" in data

    def test_multiple_messages_ordered(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("claude_worker.manager.Path.home", lambda: tmp_path)
        p1 = enqueue_message("test-worker", "a", "first")
        p2 = enqueue_message("test-worker", "b", "second")
        # Files should be ordered by name (epoch-ns)
        assert p1.name < p2.name


class TestDrainQueue:
    """drain_queue must inject messages into the FIFO and delete queue files."""

    def test_drains_messages_to_fifo(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("claude_worker.manager.Path.home", lambda: tmp_path)

        # Create a FIFO
        fifo_path = tmp_path / "in"
        os.mkfifo(fifo_path)

        # Enqueue a message
        enqueue_message("test-worker", "pm", "test message")
        queue_dir = get_queue_dir("test-worker")
        assert len(list(queue_dir.iterdir())) == 1

        # Open FIFO reader so drain can write
        rd_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
        try:
            drained = drain_queue("test-worker", fifo_path)
            assert drained == 1

            # Queue file should be deleted
            assert len(list(queue_dir.iterdir())) == 0

            # FIFO should contain the message
            data = os.read(rd_fd, 65536)
            msg = json.loads(data.decode().strip())
            assert msg["type"] == "user"
            assert "[system:queue-drain]" in msg["message"]["content"]
            assert "[reply-from:pm]" in msg["message"]["content"]
            assert "test message" in msg["message"]["content"]
        finally:
            os.close(rd_fd)

    def test_empty_queue_returns_zero(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("claude_worker.manager.Path.home", lambda: tmp_path)
        fifo_path = tmp_path / "in"
        os.mkfifo(fifo_path)
        assert drain_queue("nonexistent-worker", fifo_path) == 0

    def test_corrupt_file_skipped(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("claude_worker.manager.Path.home", lambda: tmp_path)
        queue_dir = get_queue_dir("test-worker")
        queue_dir.mkdir(parents=True)

        # Write a corrupt queue file
        (queue_dir / "corrupt.json").write_text("not json{{{")
        # Write a valid one
        enqueue_message("test-worker", "pm", "valid message")

        fifo_path = tmp_path / "in"
        os.mkfifo(fifo_path)
        rd_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
        try:
            drained = drain_queue("test-worker", fifo_path)
            assert drained == 1  # only the valid one
        finally:
            os.close(rd_fd)


class TestCmdReply:
    """cmd_reply must enqueue a message without needing a FIFO."""

    def test_reply_creates_queue_file(self, tmp_path: Path, monkeypatch):
        from claude_worker.cli import cmd_reply

        monkeypatch.setattr("claude_worker.manager.Path.home", lambda: tmp_path)
        # Patch _find_worker_by_ancestry to return None (not inside a worker)
        monkeypatch.setattr("claude_worker.cli._find_worker_by_ancestry", lambda: None)

        args = argparse.Namespace(
            name="target-worker",
            message=["hello", "from", "reply"],
            sender="test-sender",
        )
        cmd_reply(args)

        queue_dir = get_queue_dir("target-worker")
        files = list(queue_dir.iterdir())
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["sender"] == "test-sender"
        assert data["content"] == "hello from reply"
