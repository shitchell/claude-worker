"""Shared pytest fixtures for claude-worker tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write a list of dicts as JSONL to the given path."""
    with path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


@pytest.fixture
def fake_worker(tmp_path: Path, monkeypatch):
    """Factory fixture: create a fake worker runtime dir with a synthetic log.

    Creates ``<tmp_path>/workers/<name>/log`` from the given JSONL entries
    and monkey-patches claude_worker.manager.get_base_dir to point at
    ``<tmp_path>/workers`` so production code (cmd_read, _worker_is_pm, etc.)
    resolves the fake worker as if it were real.

    Returns the worker name. Invoke production commands via cmd_read(args)
    with args.name set to this name.

    Usage::

        name = fake_worker([entry1, entry2, ...])
        # Then build args and call cmd_read(args)
    """
    base_dir = tmp_path / "workers"
    base_dir.mkdir()

    from claude_worker import cli as cw_cli
    from claude_worker import manager as cw_manager

    monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)
    monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base_dir)
    # get_runtime_dir / get_saved_worker / get_sessions_file all derive from
    # get_base_dir, so patching the two visible symbols is sufficient.

    def _factory(
        entries: list[dict[str, Any]],
        name: str = "test-worker",
        pm: bool = False,
    ) -> str:
        runtime = base_dir / name
        runtime.mkdir(parents=True, exist_ok=True)
        _write_jsonl(runtime / "log", entries)
        if pm:
            # Write minimal .sessions.json so _worker_is_pm sees the flag
            sessions_path = base_dir / ".sessions.json"
            sessions_data = {}
            if sessions_path.exists():
                sessions_data = json.loads(sessions_path.read_text())
            sessions_data[name] = {"pm": True}
            sessions_path.write_text(json.dumps(sessions_data))
        return name

    return _factory


@pytest.fixture
def synthetic_log(tmp_path: Path):
    """Factory fixture: write a synthetic claude JSONL log and return its path.

    Lower-level than ``fake_worker`` — just writes a log file, no runtime dir
    or monkey-patching. Use this when you want to drive ``_read_static``
    directly with a custom config, not the production ``cmd_read`` pipeline.
    """

    def _factory(entries: list[dict[str, Any]], name: str = "log") -> Path:
        path = tmp_path / name
        _write_jsonl(path, entries)
        return path

    return _factory


def make_user_message(text: str, uuid: str, session_id: str = "sess") -> dict[str, Any]:
    """Build a replayed user message entry matching claude-worker's log format."""
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": uuid,
        "session_id": session_id,
        "parent_tool_use_id": None,
        "timestamp": "2026-04-07T00:00:00.000Z",
        "isReplay": True,
    }


def make_assistant_message(
    text: str, uuid: str, session_id: str = "sess"
) -> dict[str, Any]:
    """Build an assistant text message entry."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": None,
            "model": "claude-opus-4-6",
            "id": f"msg_{uuid[:8]}",
        },
        "uuid": uuid,
        "session_id": session_id,
        "parent_tool_use_id": None,
    }


def make_result_message(uuid: str, session_id: str = "sess") -> dict[str, Any]:
    """Build a turn-end result message entry."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "uuid": uuid,
        "session_id": session_id,
        "stop_reason": "end_turn",
        "num_turns": 1,
    }


def make_system_init(uuid: str, session_id: str = "sess") -> dict[str, Any]:
    """Build a system init message."""
    return {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "uuid": uuid,
        "cwd": "/tmp",
        "model": "claude-opus-4-6",
        "tools": [],
        "mcp_servers": [],
    }
