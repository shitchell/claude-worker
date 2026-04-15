"""Shared pytest fixtures for claude-worker tests."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest


STUB_CLAUDE_SCRIPT = Path(__file__).resolve().parent / "stub_claude.sh"


@pytest.fixture(autouse=True, scope="session")
def _clear_worker_env():
    """Prevent worker env vars from leaking into test processes (D92).

    When the test suite runs inside a worker (e.g., PM running pytest),
    CW_WORKER_NAME/CW_IDENTITY/CW_PARENT_WORKER are set. These change
    sender resolution in _resolve_sender(), producing non-deterministic
    thread pair names. Clear them for the entire suite and restore on
    teardown.
    """
    env_vars = ("CW_WORKER_NAME", "CW_IDENTITY", "CW_PARENT_WORKER")
    saved = {k: os.environ.pop(k) for k in env_vars if k in os.environ}
    yield
    os.environ.update(saved)


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
    # Patch legacy base dir to an empty temp dir so fallback lookups
    # don't hit real /tmp/claude-workers/ during tests.
    legacy_dir = tmp_path / "legacy-workers"
    legacy_dir.mkdir()
    monkeypatch.setattr(cw_manager, "_legacy_base_dir", lambda: legacy_dir)
    monkeypatch.setattr(cw_cli, "_legacy_base_dir", lambda: legacy_dir)

    def _factory(
        entries: list[dict[str, Any]],
        name: str = "test-worker",
        pm: bool = False,
        alive: bool = False,
    ) -> str:
        runtime = base_dir / name
        runtime.mkdir(parents=True, exist_ok=True)
        _write_jsonl(runtime / "log", entries)
        if alive:
            # Write a pid file pointing at the test process itself, so
            # helpers that check PID liveness (_wait_for_turn, etc.) see
            # the fake worker as alive.
            (runtime / "pid").write_text(str(os.getpid()))
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


@pytest.fixture
def running_worker(tmp_path: Path, monkeypatch):
    """Factory fixture: start a real manager + stub-claude subprocess.

    Unlike ``fake_worker`` (which writes a canned log and monkey-patches
    get_base_dir), this spawns the actual ``_run_manager_forkless`` logic
    in a thread with a stub-claude binary, so the full pipeline is
    exercised: FIFO plumbing, log-writer thread, session capture,
    initial prompt forwarding, cleanup-on-exit.

    Usage::

        handle = running_worker(initial_message="hello")
        # handle.name is the worker name, handle.runtime_dir the dir,
        # handle.log_path the live JSONL log.
        #
        # Send messages via `claude-worker send` equivalents or by
        # writing to handle.runtime_dir / "in" directly.
        #
        # handle.stop() triggers clean shutdown and joins the thread.

    Yields a ``RunningWorkerHandle``. The fixture cleans up the thread
    and runtime dir on test teardown automatically, so explicit
    handle.stop() is optional but recommended for tests that care
    about deterministic shutdown ordering.
    """
    from claude_worker import cli as cw_cli
    from claude_worker import manager as cw_manager

    base_dir = tmp_path / "workers"
    base_dir.mkdir()
    monkeypatch.setattr(cw_manager, "get_base_dir", lambda: base_dir)
    monkeypatch.setattr(cw_cli, "get_base_dir", lambda: base_dir)
    legacy_dir = tmp_path / "legacy-workers"
    legacy_dir.mkdir()
    monkeypatch.setattr(cw_manager, "_legacy_base_dir", lambda: legacy_dir)
    monkeypatch.setattr(cw_cli, "_legacy_base_dir", lambda: legacy_dir)

    # Point manager at the stub-claude script
    monkeypatch.setenv("CLAUDE_WORKER_CLAUDE_BIN", str(STUB_CLAUDE_SCRIPT))

    spawned: list[RunningWorkerHandle] = []

    def _factory(
        name: str = "test-worker",
        initial_message: str | None = None,
        cwd: str | None = None,
        stub_session_id: str | None = None,
        stub_script: dict | None = None,
        stub_delay_ms: int | None = None,
        ready_timeout: float = 5.0,
    ) -> "RunningWorkerHandle":
        # Create runtime dir + FIFO (normally done by cmd_start)
        runtime_dir = cw_manager.create_runtime_dir(name)
        # Record the worker's cwd so helpers that resolve the thread
        # location (thread_store, cmd_send) find the same directory the
        # manager is watching. Normally cmd_start writes this before
        # forking the manager; the test fixture mirrors that step.
        cw_manager.save_worker(name, cwd=cwd or str(tmp_path))

        # Per-call stub configuration lives on the manager's env, which
        # is copied from os.environ — so monkeypatch.setenv is sufficient.
        if stub_session_id is not None:
            monkeypatch.setenv("CLAUDE_STUB_SESSION_ID", stub_session_id)
        if stub_script is not None:
            script_path = tmp_path / f"{name}-stub-script.json"
            script_path.write_text(json.dumps(stub_script))
            monkeypatch.setenv("CLAUDE_STUB_SCRIPT", str(script_path))
        if stub_delay_ms is not None:
            monkeypatch.setenv("CLAUDE_STUB_DELAY_MS", str(stub_delay_ms))

        thread = threading.Thread(
            target=cw_manager._run_manager_forkless,
            kwargs=dict(
                name=name,
                cwd=cwd or str(tmp_path),
                claude_args=[],
                initial_message=initial_message,
                install_signals=False,
            ),
            daemon=True,
        )
        thread.start()

        # Wait for the pid file to appear (manager init complete)
        pid_file = runtime_dir / "pid"
        deadline = time.monotonic() + ready_timeout
        while not pid_file.exists():
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"running_worker({name}) did not create pid file within "
                    f"{ready_timeout}s"
                )
            time.sleep(0.02)

        handle = RunningWorkerHandle(
            name=name,
            runtime_dir=runtime_dir,
            log_path=runtime_dir / "log",
            thread=thread,
        )
        spawned.append(handle)
        return handle

    yield _factory

    # Teardown: make sure every spawned worker is stopped. `running_worker`
    # uses SIGTERM via cmd_stop's pattern — write an empty message? no,
    # just close stdin. The stub exits on EOF, so closing the FIFO's last
    # writer causes the chain: FIFO eof → nothing (dummy writer keeps it
    # open) — we need a different approach.
    #
    # Simpler: close the FIFO's dummy writer by writing a sentinel that
    # the stub interprets as "exit". The stub iterates stdin line by line
    # and exits on EOF, so sending a "type":"exit" control message won't
    # work unless the stub handles it. Cleanest approach: use os.kill on
    # the claude subprocess via the pid we have — but we don't have the
    # claude pid, only the manager thread.
    #
    # Pragmatic solution: the manager owns a subprocess.Popen; we expose
    # it by having the factory hold a reference and stop() kills it.
    # Implemented via handle.stop() below.
    for handle in spawned:
        handle.stop()


class RunningWorkerHandle:
    """Handle returned by the ``running_worker`` fixture.

    Holds references needed by tests: the worker name, its runtime
    directory, the live log path, and the manager thread. ``stop()``
    shuts down the worker by closing the FIFO writers so the stub
    sees EOF and exits, then joins the manager thread.
    """

    def __init__(
        self,
        name: str,
        runtime_dir: Path,
        log_path: Path,
        thread: threading.Thread,
    ) -> None:
        self.name = name
        self.runtime_dir = runtime_dir
        self.log_path = log_path
        self.thread = thread
        self._stopped = False

    def wait_for_log(self, match: str, timeout: float = 5.0) -> bool:
        """Poll the log file until ``match`` appears, or timeout.

        Returns True on success, False on timeout. Useful for tests that
        need to synchronize with the manager's log-writer thread.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.log_path.exists():
                try:
                    if match in self.log_path.read_text():
                        return True
                except OSError:
                    pass
            time.sleep(0.02)
        return False

    def stop(self, timeout: float = 5.0) -> None:
        """Shut down the worker: SIGTERM the stub-claude subprocess so
        the manager thread's proc.wait() returns, then join the thread.

        Reads the PID from runtime_dir/claude-pid (written by
        _run_manager_forkless specifically for this test-harness use
        case). Signal handlers are NOT installed in test mode
        (install_signals=False), so we cannot just SIGTERM the manager
        PID (that would kill the test runner) — we signal the stub
        child directly instead.

        Idempotent — safe to call multiple times or on an already-dead
        worker.
        """
        import signal as _signal

        if self._stopped:
            return
        self._stopped = True

        claude_pid_file = self.runtime_dir / "claude-pid"
        if claude_pid_file.exists():
            try:
                claude_pid = int(claude_pid_file.read_text().strip())
                os.kill(claude_pid, _signal.SIGTERM)
            except (ValueError, OSError, ProcessLookupError):
                pass

        self.thread.join(timeout=timeout)


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
    text: str,
    uuid: str,
    session_id: str = "sess",
    stop_reason: str | None = None,
) -> dict[str, Any]:
    """Build an assistant text message entry."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
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
