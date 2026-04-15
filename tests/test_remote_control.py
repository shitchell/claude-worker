"""Tests for CCR remote control (#067).

Covers the ``_enable_remote_control`` helper which sends a
``control_request`` message on claude's stdin and polls the log for
the matching ``control_response`` containing session/connect URLs.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_worker import manager as cw_manager


# -- Helpers --------------------------------------------------------------


def _make_proc_mock() -> tuple[MagicMock, list[bytes]]:
    """Build a MagicMock subprocess.Popen-like object whose ``stdin.write``
    appends into a capture list. Returns (proc, writes)."""
    writes: list[bytes] = []
    proc = MagicMock()
    proc.stdin.write.side_effect = lambda b: writes.append(b)
    proc.stdin.flush.return_value = None
    return proc, writes


def _write_response_after_delay(
    log_file: Path,
    request_id: str,
    delay: float = 0.3,
    session_url: str = "https://claude.ai/sess/abc",
    connect_url: str = "wss://api.anthropic.com/ws/abc",
    environment_id: str = "env-xyz",
) -> None:
    """Append a matching control_response to the log after ``delay``."""
    time.sleep(delay)
    response = {
        "type": "control_response",
        "request_id": request_id,
        "response": {
            "subtype": "remote_control",
            "session_url": session_url,
            "connect_url": connect_url,
            "environment_id": environment_id,
        },
    }
    with open(log_file, "a") as f:
        f.write(json.dumps(response) + "\n")


# -- Tests ----------------------------------------------------------------


class TestEnableRemoteControl:
    def test_sends_request(self, tmp_path, monkeypatch):
        """The helper must write a control_request with the right shape
        to claude's stdin."""
        log_path = tmp_path / "log"
        log_path.touch()
        proc, writes = _make_proc_mock()

        # Patch uuid.uuid4 to a known value for deterministic request_id
        fake_uuid = MagicMock()
        fake_uuid.hex = "testid1234567890abcdef"
        monkeypatch.setattr(cw_manager.uuid, "uuid4", lambda: fake_uuid)

        expected_request_id = "rc-testid123456"

        # Pre-write the matching response so the helper returns quickly
        response = {
            "type": "control_response",
            "request_id": expected_request_id,
            "response": {
                "subtype": "remote_control",
                "session_url": "https://claude.ai/sess/abc",
                "connect_url": "wss://api.anthropic.com/ws/abc",
                "environment_id": "env-xyz",
            },
        }
        log_path.write_text(json.dumps(response) + "\n")

        cw_manager._enable_remote_control(proc, log_path)

        # Exactly one stdin write: the control_request
        assert len(writes) == 1
        payload = json.loads(writes[0].decode().rstrip("\n"))
        assert payload["type"] == "control_request"
        assert payload["request_id"] == expected_request_id
        assert payload["request"] == {
            "subtype": "remote_control",
            "enabled": True,
        }
        proc.stdin.flush.assert_called()

    def test_prints_urls(self, tmp_path, monkeypatch, capsys):
        """A pre-existing matching control_response in the log must be
        parsed and its URLs printed to stderr."""
        log_path = tmp_path / "log"
        proc, _ = _make_proc_mock()

        fake_uuid = MagicMock()
        fake_uuid.hex = "abcdef1234567890abcdef"
        monkeypatch.setattr(cw_manager.uuid, "uuid4", lambda: fake_uuid)
        request_id = "rc-abcdef123456"

        response = {
            "type": "control_response",
            "request_id": request_id,
            "response": {
                "subtype": "remote_control",
                "session_url": "https://claude.ai/sess/xyz",
                "connect_url": "wss://api.anthropic.com/ws/xyz",
                "environment_id": "env-42",
            },
        }
        log_path.write_text(json.dumps(response) + "\n")

        cw_manager._enable_remote_control(proc, log_path)

        err = capsys.readouterr().err
        assert "[remote-control] Enabled" in err
        assert "https://claude.ai/sess/xyz" in err
        assert "wss://api.anthropic.com/ws/xyz" in err
        assert "env-42" in err

    def test_timeout(self, tmp_path, monkeypatch, capsys):
        """If no control_response ever arrives, the helper must time
        out and print a warning — but not raise."""
        log_path = tmp_path / "log"
        log_path.touch()
        proc, _ = _make_proc_mock()

        # Make timeout fire almost immediately
        monkeypatch.setattr(cw_manager, "REMOTE_CONTROL_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(cw_manager, "REMOTE_CONTROL_POLL_INTERVAL", 0.05)

        start = time.monotonic()
        cw_manager._enable_remote_control(proc, log_path)
        elapsed = time.monotonic() - start

        # Should return within a second (timeout was 0.3s)
        assert elapsed < 1.5
        err = capsys.readouterr().err
        assert "[remote-control] Timed out" in err
        assert "continues without remote control" in err

    def test_stdin_error(self, tmp_path, capsys):
        """A BrokenPipeError from stdin.write must be handled
        gracefully (warning printed, function returns)."""
        log_path = tmp_path / "log"
        log_path.touch()
        proc = MagicMock()

        def boom(_data):
            raise BrokenPipeError("pipe closed")

        proc.stdin.write.side_effect = boom

        # Must not raise
        cw_manager._enable_remote_control(proc, log_path)

        err = capsys.readouterr().err
        assert "[remote-control] Failed to send control_request" in err
        # Flush should not have been called since write raised first
        proc.stdin.flush.assert_not_called()

    def test_ignores_non_matching_response(
        self, tmp_path, monkeypatch, capsys
    ):
        """A control_response with a different request_id in the log
        must be ignored — the helper must keep polling and eventually
        time out."""
        log_path = tmp_path / "log"
        proc, _ = _make_proc_mock()

        fake_uuid = MagicMock()
        fake_uuid.hex = "mineisthis1234567890ab"
        monkeypatch.setattr(cw_manager.uuid, "uuid4", lambda: fake_uuid)

        # Seed a response with a DIFFERENT request_id
        other_response = {
            "type": "control_response",
            "request_id": "rc-someoneelses",
            "response": {
                "subtype": "remote_control",
                "session_url": "https://claude.ai/sess/other",
                "connect_url": "wss://api.anthropic.com/ws/other",
                "environment_id": "env-other",
            },
        }
        log_path.write_text(json.dumps(other_response) + "\n")

        monkeypatch.setattr(cw_manager, "REMOTE_CONTROL_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(cw_manager, "REMOTE_CONTROL_POLL_INTERVAL", 0.05)

        cw_manager._enable_remote_control(proc, log_path)

        err = capsys.readouterr().err
        # Did NOT print the other session's URL
        assert "https://claude.ai/sess/other" not in err
        # Did NOT print the "Enabled" banner
        assert "[remote-control] Enabled" not in err
        # Did time out
        assert "[remote-control] Timed out" in err

    def test_async_response_arrives_during_poll(self, tmp_path, monkeypatch, capsys):
        """A matching control_response written to the log mid-poll is
        detected on the next poll cycle."""
        log_path = tmp_path / "log"
        log_path.touch()
        proc, _ = _make_proc_mock()

        fake_uuid = MagicMock()
        fake_uuid.hex = "asyncid12345678901234a"
        monkeypatch.setattr(cw_manager.uuid, "uuid4", lambda: fake_uuid)
        request_id = "rc-asyncid12345"

        monkeypatch.setattr(cw_manager, "REMOTE_CONTROL_TIMEOUT_SECONDS", 3.0)
        monkeypatch.setattr(cw_manager, "REMOTE_CONTROL_POLL_INTERVAL", 0.1)

        # Kick off a writer thread that appends the response after a delay
        writer = threading.Thread(
            target=_write_response_after_delay,
            args=(log_path, request_id, 0.3),
        )
        writer.start()

        start = time.monotonic()
        cw_manager._enable_remote_control(proc, log_path)
        elapsed = time.monotonic() - start
        writer.join()

        # Returned well before the timeout
        assert elapsed < 2.5
        err = capsys.readouterr().err
        assert "[remote-control] Enabled" in err
        assert "https://claude.ai/sess/abc" in err
