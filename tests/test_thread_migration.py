"""Tests for Phase 3 of the thread primitive migration (D88).

Covers:
  - thread ID helpers (pair_thread_id, chat_thread_id)
  - ensure_thread create/no-op/extend semantics
  - cmd_send writes to threads rather than FIFOs
  - cmd_read reads from threads, --log falls back to the log
  - active-thread sidecar + response tee
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from claude_worker import cli as cw_cli
from claude_worker import manager as cw_manager
from claude_worker.manager import (
    _get_active_thread,
    _set_active_thread,
    _tee_assistant_to_thread,
)
from claude_worker.thread_store import (
    append_message,
    chat_thread_id,
    create_thread,
    ensure_thread,
    load_index,
    pair_thread_id,
    read_messages,
)


# -- Thread ID helpers -----------------------------------------------------


def test_pair_thread_id_symmetric():
    """pair_thread_id is order-independent."""
    assert pair_thread_id("alice", "bob") == pair_thread_id("bob", "alice")


def test_pair_thread_id_format():
    """Returned ID is pair-<a>-<b> in sorted order."""
    assert pair_thread_id("bob", "alice") == "pair-alice-bob"
    assert pair_thread_id("pm", "tl") == "pair-pm-tl"


def test_pair_thread_id_missing_sender():
    """Empty / None sender falls back to '?' without crashing."""
    assert pair_thread_id("", "bob") == "pair-?-bob"


def test_chat_thread_id_format():
    """chat_thread_id returns chat-<id>."""
    assert chat_thread_id("abc") == "chat-abc"
    assert chat_thread_id("xyz-123") == "chat-xyz-123"


# -- ensure_thread ---------------------------------------------------------


def test_ensure_thread_creates_new(tmp_path: Path):
    """First call creates the thread file + index entry."""
    cwd = str(tmp_path)
    tid = ensure_thread(cwd, "pair-a-b", participants=["a", "b"])
    assert tid == "pair-a-b"
    assert (tmp_path / ".cwork" / "threads" / "pair-a-b.jsonl").exists()
    index = load_index(cwd)
    assert index["pair-a-b"]["participants"] == ["a", "b"]


def test_ensure_thread_existing_no_op(tmp_path: Path):
    """Second call with same participants leaves the index untouched."""
    cwd = str(tmp_path)
    ensure_thread(cwd, "pair-a-b", participants=["a", "b"])
    index_before = load_index(cwd)

    ensure_thread(cwd, "pair-a-b", participants=["a", "b"])
    index_after = load_index(cwd)

    assert index_before == index_after


def test_ensure_thread_adds_new_participant(tmp_path: Path):
    """Calling with a new participant extends the list in place."""
    cwd = str(tmp_path)
    ensure_thread(cwd, "chat-xyz", participants=["pm", "tl"])

    ensure_thread(cwd, "chat-xyz", participants=["pm", "tl", "rhc"])
    participants = load_index(cwd)["chat-xyz"]["participants"]
    assert participants == ["pm", "tl", "rhc"]


def test_ensure_thread_preserves_order(tmp_path: Path):
    """New participants are appended; existing order is preserved."""
    cwd = str(tmp_path)
    ensure_thread(cwd, "pair-x-y", participants=["x", "y"])

    ensure_thread(cwd, "pair-x-y", participants=["z", "y"])
    participants = load_index(cwd)["pair-x-y"]["participants"]
    assert participants == ["x", "y", "z"]


# -- cmd_send migration ----------------------------------------------------


def _make_send_args(
    name: str,
    message_text: str,
    **overrides,
) -> argparse.Namespace:
    """Build an argparse.Namespace matching the send subparser defaults."""
    defaults: dict = dict(
        name=name,
        message=[message_text],
        queue=False,
        dry_run=False,
        verbose=False,
        show_response=False,
        show_full_response=False,
        broadcast=False,
        chat=None,
        all_chats=False,
        alive=False,
        role=None,
        status=None,
        cwd_filter=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_send_writes_to_thread_not_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """cmd_send appends to the thread store without writing to the FIFO."""
    base_dir = tmp_path / "workers"
    base_dir.mkdir()
    monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)
    monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base_dir)

    # Runtime dir + a placeholder log so resolve_worker succeeds
    runtime = base_dir / "tl"
    runtime.mkdir(parents=True)
    (runtime / "log").write_text("")

    # Saved session entry with a cwd under tmp_path — threads live there
    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    sessions_path = base_dir / ".sessions.json"
    sessions_path.write_text(
        json.dumps({"tl": {"cwd": str(project_cwd), "identity": "worker"}})
    )

    # Bypass the status gate (no pid → "dead") by using --queue
    monkeypatch.setenv("CW_WORKER_NAME", "pm")

    # Track FIFO writes: raise if anyone opens the 'in' FIFO for writing
    in_fifo_path = runtime / "in"
    real_open = open

    def _no_fifo_writes(path, mode="r", *a, **kw):
        if str(path) == str(in_fifo_path) and "w" in mode:
            raise AssertionError(f"FIFO write should not happen: {path}")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", _no_fifo_writes)

    args = _make_send_args("tl", "hello from pm", queue=True)
    # Neutralize the post-send wait (it'd hang on a missing process)
    monkeypatch.setattr(cw_cli, "_wait_for_queue_response", lambda *a, **kw: 0)
    monkeypatch.setattr(cw_cli, "_wait_for_turn", lambda *a, **kw: 0)

    rc = cw_cli._send_to_single_worker("tl", "hello from pm", args)
    assert rc == 0

    # The thread should exist and contain our message
    thread_id = pair_thread_id("pm", "tl")
    messages = read_messages(str(project_cwd), thread_id)
    assert len(messages) == 1
    assert messages[0]["sender"] == "pm"
    assert "hello from pm" in messages[0]["content"]


def test_send_uses_pair_thread_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Sender from CW_WORKER_NAME + recipient name produce pair-<a>-<b>."""
    base_dir = tmp_path / "workers"
    base_dir.mkdir()
    monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)
    monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base_dir)

    runtime = base_dir / "tl"
    runtime.mkdir(parents=True)
    (runtime / "log").write_text("")

    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    sessions_path = base_dir / ".sessions.json"
    sessions_path.write_text(
        json.dumps({"tl": {"cwd": str(project_cwd), "identity": "worker"}})
    )

    monkeypatch.setenv("CW_WORKER_NAME", "pm")
    monkeypatch.setattr(cw_cli, "_wait_for_queue_response", lambda *a, **kw: 0)
    monkeypatch.setattr(cw_cli, "_wait_for_turn", lambda *a, **kw: 0)

    args = _make_send_args("tl", "hi", queue=True)
    rc = cw_cli._send_to_single_worker("tl", "hi", args)
    assert rc == 0

    # Verify the resulting thread ID
    index = load_index(str(project_cwd))
    assert "pair-pm-tl" in index
    assert sorted(index["pair-pm-tl"]["participants"]) == ["pm", "tl"]


def test_send_dry_run_does_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """--dry-run prints what would be appended but does not touch the thread."""
    base_dir = tmp_path / "workers"
    base_dir.mkdir()
    monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)
    monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base_dir)

    runtime = base_dir / "tl"
    runtime.mkdir(parents=True)
    (runtime / "log").write_text("")

    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    sessions_path = base_dir / ".sessions.json"
    sessions_path.write_text(
        json.dumps({"tl": {"cwd": str(project_cwd), "identity": "worker"}})
    )

    monkeypatch.setenv("CW_WORKER_NAME", "pm")
    args = _make_send_args("tl", "dry", dry_run=True)
    rc = cw_cli._send_to_single_worker("tl", "dry", args)
    assert rc == 0

    threads_dir = project_cwd / ".cwork" / "threads"
    assert not threads_dir.exists() or not any(threads_dir.glob("*.jsonl"))


# -- cmd_read migration ----------------------------------------------------


def _make_read_args(name: str, **overrides) -> argparse.Namespace:
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
        no_color=False,
        chat=None,
        all_chats=False,
        log=False,
        thread=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_read_reads_from_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """cmd_read prints thread messages for both sides of the conversation."""
    base_dir = tmp_path / "workers"
    base_dir.mkdir()
    monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)
    monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base_dir)

    runtime = base_dir / "tl"
    runtime.mkdir(parents=True)
    (runtime / "log").write_text("")

    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    sessions_path = base_dir / ".sessions.json"
    sessions_path.write_text(
        json.dumps({"tl": {"cwd": str(project_cwd), "identity": "worker"}})
    )

    # Pre-populate the pair thread with two messages
    tid = pair_thread_id("pm", "tl")
    ensure_thread(str(project_cwd), tid, participants=["pm", "tl"])
    append_message(str(project_cwd), tid, sender="pm", content="question?")
    append_message(str(project_cwd), tid, sender="tl", content="answer.")

    monkeypatch.setenv("CW_WORKER_NAME", "pm")
    args = _make_read_args("tl")
    cw_cli.cmd_read(args)

    out = capsys.readouterr().out
    assert "question?" in out
    assert "answer." in out
    assert "pm" in out
    assert "tl" in out


def test_read_log_flag_falls_back_to_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """--log uses the raw log path even when a thread exists."""
    base_dir = tmp_path / "workers"
    base_dir.mkdir()
    monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)
    monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base_dir)

    runtime = base_dir / "tl"
    runtime.mkdir(parents=True)
    # Log contains a distinctive marker; thread is empty
    log_entry = {
        "type": "user",
        "message": {"role": "user", "content": "RAW_LOG_MARKER"},
        "uuid": "11111111-2222-3333-4444-555555555555",
        "timestamp": "2026-04-07T00:00:00.000Z",
    }
    (runtime / "log").write_text(json.dumps(log_entry) + "\n")

    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    sessions_path = base_dir / ".sessions.json"
    sessions_path.write_text(
        json.dumps({"tl": {"cwd": str(project_cwd), "identity": "worker"}})
    )

    args = _make_read_args("tl", log=True)
    cw_cli.cmd_read(args)

    out = capsys.readouterr().out
    assert "RAW_LOG_MARKER" in out


def test_read_explicit_thread_missing_prints_no_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """Nonexistent thread via --thread prints a 'No messages' hint."""
    base_dir = tmp_path / "workers"
    base_dir.mkdir()
    monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)
    monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base_dir)

    runtime = base_dir / "tl"
    runtime.mkdir(parents=True)
    (runtime / "log").write_text("")

    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    sessions_path = base_dir / ".sessions.json"
    sessions_path.write_text(
        json.dumps({"tl": {"cwd": str(project_cwd), "identity": "worker"}})
    )

    monkeypatch.setenv("CW_WORKER_NAME", "pm")
    # Explicit --thread override makes the thread-read errors visible
    # (instead of silently falling back to the log).
    args = _make_read_args("tl", thread="pair-pm-tl")
    first, last = cw_cli.cmd_read(args)

    err = capsys.readouterr().err
    assert first is None and last is None
    assert "No messages" in err


def test_read_missing_thread_falls_back_to_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """Auto-detected pair thread missing -> silently use the session log."""
    base_dir = tmp_path / "workers"
    base_dir.mkdir()
    monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)
    monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base_dir)

    runtime = base_dir / "tl"
    runtime.mkdir(parents=True)
    log_entry = {
        "type": "user",
        "message": {"role": "user", "content": "LOG_FALLBACK_MARKER"},
        "uuid": "22222222-2222-3333-4444-555555555555",
        "timestamp": "2026-04-07T00:00:00.000Z",
    }
    (runtime / "log").write_text(json.dumps(log_entry) + "\n")

    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    sessions_path = base_dir / ".sessions.json"
    sessions_path.write_text(
        json.dumps({"tl": {"cwd": str(project_cwd), "identity": "worker"}})
    )

    monkeypatch.setenv("CW_WORKER_NAME", "pm")
    args = _make_read_args("tl")
    cw_cli.cmd_read(args)

    out = capsys.readouterr().out
    assert "LOG_FALLBACK_MARKER" in out


# -- cmd_reply migration ---------------------------------------------------


def test_reply_appends_to_pair_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """cmd_reply writes to pair-<sender>-<recipient> and does not exit."""
    base_dir = tmp_path / "workers"
    base_dir.mkdir()
    monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)
    monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base_dir)

    project_cwd = tmp_path / "project"
    project_cwd.mkdir()
    sessions_path = base_dir / ".sessions.json"
    sessions_path.write_text(
        json.dumps({"pm": {"cwd": str(project_cwd), "identity": "pm"}})
    )

    monkeypatch.setenv("CW_WORKER_NAME", "tl")
    # Disable ancestry walk so sender resolution falls through to CW_WORKER_NAME
    monkeypatch.setattr(cw_cli, "_find_worker_by_ancestry", lambda: None)

    args = argparse.Namespace(
        name="pm",
        message=["the answer is 42"],
        sender=None,
    )
    cw_cli.cmd_reply(args)

    tid = pair_thread_id("tl", "pm")
    messages = read_messages(str(project_cwd), tid)
    assert len(messages) == 1
    assert messages[0]["sender"] == "tl"
    assert "the answer is 42" in messages[0]["content"]
    assert "reply" in (messages[0].get("tags") or [])


# -- Active-thread sidecar -------------------------------------------------


def test_set_get_active_thread(tmp_path: Path):
    """Write active-thread, read it back."""
    _set_active_thread(tmp_path, "pair-pm-tl")
    assert _get_active_thread(tmp_path) == "pair-pm-tl"


def test_get_active_thread_missing_returns_none(tmp_path: Path):
    """No file -> None."""
    assert _get_active_thread(tmp_path) is None


def test_get_active_thread_empty_returns_none(tmp_path: Path):
    """Empty file -> None."""
    (tmp_path / "active-thread").write_text("")
    assert _get_active_thread(tmp_path) is None


# -- Response tee ---------------------------------------------------------


def _assistant_jsonl(
    text: str,
    stop_reason: str | None = "end_turn",
) -> str:
    """Build a one-line JSONL assistant message matching claude's format."""
    message: dict = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "model": "claude-opus-4-6",
        "id": "msg_test",
    }
    envelope = {
        "type": "assistant",
        "message": message,
        "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    }
    return json.dumps(envelope) + "\n"


def test_tee_end_turn_appends_to_active_thread(tmp_path: Path):
    """A stop_reason=end_turn assistant message is appended to the thread."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    # Set active thread + create the thread file
    create_thread(str(cwd), participants=["worker1"], thread_id="pair-pm-worker1")
    _set_active_thread(runtime, "pair-pm-worker1")

    line = _assistant_jsonl("hello world", stop_reason="end_turn")
    teed = _tee_assistant_to_thread(line, runtime, str(cwd), "worker1")
    assert teed is True

    messages = read_messages(str(cwd), "pair-pm-worker1")
    assert len(messages) == 1
    assert messages[0]["sender"] == "worker1"
    assert messages[0]["content"] == "hello world"
    assert "assistant" in messages[0]["tags"]


def test_tee_skips_partial_chunks(tmp_path: Path):
    """Mid-turn chunks (stop_reason=None) are not teed."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    create_thread(str(cwd), participants=["w"], thread_id="pair-pm-w")
    _set_active_thread(runtime, "pair-pm-w")

    line = _assistant_jsonl("streaming partial", stop_reason=None)
    assert _tee_assistant_to_thread(line, runtime, str(cwd), "w") is False

    messages = read_messages(str(cwd), "pair-pm-w")
    assert messages == []


def test_tee_skips_tool_use_turns(tmp_path: Path):
    """Tool-use-only assistant turns (no text block) are not teed."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    create_thread(str(cwd), participants=["w"], thread_id="pair-pm-w")
    _set_active_thread(runtime, "pair-pm-w")

    envelope = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
            ],
            "stop_reason": "tool_use",
            "model": "claude-opus-4-6",
            "id": "msg_test",
        },
        "uuid": "uuid-tool-use",
    }
    line = json.dumps(envelope) + "\n"
    assert _tee_assistant_to_thread(line, runtime, str(cwd), "w") is False


def test_tee_no_active_thread_is_noop(tmp_path: Path):
    """Without an active thread set, tee is a no-op."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    line = _assistant_jsonl("nowhere to go")
    assert _tee_assistant_to_thread(line, runtime, str(cwd), "w") is False


def test_tee_skips_non_assistant_messages(tmp_path: Path):
    """User / system / result messages are not teed."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    create_thread(str(cwd), participants=["w"], thread_id="pair-pm-w")
    _set_active_thread(runtime, "pair-pm-w")

    user_line = (
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "hi"},
                "uuid": "u1",
            }
        )
        + "\n"
    )
    assert _tee_assistant_to_thread(user_line, runtime, str(cwd), "w") is False


def test_tee_handles_malformed_line(tmp_path: Path):
    """A non-JSON line returns False (doesn't raise)."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    _set_active_thread(runtime, "pair-pm-w")
    assert _tee_assistant_to_thread("not json at all", runtime, str(cwd), "w") is False


def test_tee_concatenates_multiple_text_blocks(tmp_path: Path):
    """An assistant message with several text blocks concatenates them."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    create_thread(str(cwd), participants=["w"], thread_id="pair-pm-w")
    _set_active_thread(runtime, "pair-pm-w")

    envelope = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "first half"},
                {"type": "tool_use", "name": "Bash", "input": {}},
                {"type": "text", "text": "second half"},
            ],
            "stop_reason": "end_turn",
            "model": "claude-opus-4-6",
            "id": "msg_multi",
        },
        "uuid": "uuid-multi",
    }
    line = json.dumps(envelope) + "\n"
    assert _tee_assistant_to_thread(line, runtime, str(cwd), "w") is True

    messages = read_messages(str(cwd), "pair-pm-w")
    assert len(messages) == 1
    assert "first half" in messages[0]["content"]
    assert "second half" in messages[0]["content"]
