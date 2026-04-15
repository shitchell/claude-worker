"""Tests for identity drift detection (#066).

Source identity files (``~/.cwork/identities/<name>/identity.md`` or the
bundled fallback) are hashed at copy time. The manager's poll loop
re-hashes the source periodically and injects a
``[system:identity-drift]`` notification when the current source
diverges from the stored hash. The ``notified`` flag dedupes per
divergence so one edit produces one notification, not a flood.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_worker.manager import (
    check_identity_drift,
    hash_identity_content,
    read_identity_hash,
    write_identity_hash,
)


# -- Helpers ---------------------------------------------------------------


def _install_fd_capture(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Monkeypatch os.open/write/close to capture FIFO writes.

    Mirrors the pattern in ``tests/test_thread_notifications.py``:
    os.open returns a sentinel fd, os.write on that fd appends to the
    returned list, os.close on it is a no-op. Real calls on any other
    fd pass through unchanged.
    """
    writes: list[bytes] = []
    real_write = os.write
    real_close = os.close
    fake_fd = 999_998

    def mock_open(path, flags, *args, **kwargs):
        return fake_fd

    def mock_write(fd, data):
        if fd == fake_fd:
            writes.append(data)
            return len(data)
        return real_write(fd, data)

    def mock_close(fd):
        if fd == fake_fd:
            return
        return real_close(fd)

    monkeypatch.setattr(os, "open", mock_open)
    monkeypatch.setattr(os, "write", mock_write)
    monkeypatch.setattr(os, "close", mock_close)
    return writes


def _install_user_identity(
    monkeypatch: pytest.MonkeyPatch, home: Path, name: str, content: str
) -> Path:
    """Point ``Path.home()`` at ``home`` and install a user identity file."""
    monkeypatch.setattr(Path, "home", lambda: home)
    identity_dir = home / ".cwork" / "identities" / name
    identity_dir.mkdir(parents=True, exist_ok=True)
    path = identity_dir / "identity.md"
    path.write_text(content)
    return path


# -- hash_identity_content -------------------------------------------------


def test_hash_deterministic():
    """Same content → same hash."""
    a = hash_identity_content("hello world")
    b = hash_identity_content("hello world")
    assert a == b
    # 16 hex chars
    assert len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)


def test_hash_changes_on_content_change():
    """Different content → different hash."""
    a = hash_identity_content("identity v1")
    b = hash_identity_content("identity v2")
    assert a != b


# -- write_identity_hash / read_identity_hash ------------------------------


def test_write_read_identity_hash_roundtrip(tmp_path: Path):
    """Written hash can be read back and matches hash_identity_content."""
    content = "some identity text"
    write_identity_hash(tmp_path, content)
    stored = read_identity_hash(tmp_path)
    assert stored == hash_identity_content(content)


def test_read_identity_hash_missing_returns_none(tmp_path: Path):
    """Missing hash file → None."""
    assert read_identity_hash(tmp_path) is None


def test_read_identity_hash_empty_file_returns_none(tmp_path: Path):
    """Empty (whitespace-only) hash file → None."""
    (tmp_path / "identity.hash").write_text("   \n")
    assert read_identity_hash(tmp_path) is None


# -- check_identity_drift --------------------------------------------------


def test_check_no_baseline_no_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No stored hash → no notification, notified flag preserved."""
    writes = _install_fd_capture(monkeypatch)
    # Install a source so the test would drift if it got that far
    home = tmp_path / "home"
    _install_user_identity(monkeypatch, home, "pm", "pm v1")
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    result = check_identity_drift("pm", runtime, tmp_path / "in", notified=False)
    assert result is False
    assert writes == []


def test_check_no_source_no_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Source unavailable → no notification, notified flag preserved."""
    writes = _install_fd_capture(monkeypatch)
    # Point HOME at an empty dir (no user identity) and use an identity
    # that has no bundled fallback.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    write_identity_hash(runtime, "baseline content")

    result = check_identity_drift(
        "custom-identity", runtime, tmp_path / "in", notified=False
    )
    assert result is False
    assert writes == []


def test_check_match_clears_notified_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When source matches stored hash, notified flag is cleared to False."""
    writes = _install_fd_capture(monkeypatch)
    home = tmp_path / "home"
    _install_user_identity(monkeypatch, home, "pm", "pm stable")

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    write_identity_hash(runtime, "pm stable")

    # Even if previously notified, a match clears the flag
    result = check_identity_drift("pm", runtime, tmp_path / "in", notified=True)
    assert result is False
    assert writes == []


def test_check_drift_emits_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Source diverges from stored hash → one FIFO write with drift payload."""
    writes = _install_fd_capture(monkeypatch)
    home = tmp_path / "home"
    _install_user_identity(monkeypatch, home, "pm", "pm NEW content")

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    write_identity_hash(runtime, "pm OLD content")

    result = check_identity_drift("pm", runtime, tmp_path / "in", notified=False)
    assert result is True
    assert len(writes) == 1

    payload = writes[0].decode()
    assert payload.endswith("\n")
    envelope = json.loads(payload.strip())
    assert envelope["type"] == "user"
    content = envelope["message"]["content"]
    assert "[system:identity-drift]" in content
    assert "'pm'" in content
    assert "replaceme" in content
    # Both hashes appear in the message for debuggability
    assert hash_identity_content("pm OLD content") in content
    assert hash_identity_content("pm NEW content") in content


def test_check_drift_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Once notified for a divergence, repeated checks do not re-notify."""
    writes = _install_fd_capture(monkeypatch)
    home = tmp_path / "home"
    _install_user_identity(monkeypatch, home, "pm", "pm v2")

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    write_identity_hash(runtime, "pm v1")

    # First pass: notify
    notified = check_identity_drift("pm", runtime, tmp_path / "in", notified=False)
    assert notified is True
    assert len(writes) == 1

    # Second pass (same divergence): stays True, no new write
    notified = check_identity_drift("pm", runtime, tmp_path / "in", notified=notified)
    assert notified is True
    assert len(writes) == 1

    # Third pass: still no spam
    notified = check_identity_drift("pm", runtime, tmp_path / "in", notified=notified)
    assert notified is True
    assert len(writes) == 1


def test_check_drift_resets_on_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Drift → notify → source reverts → flag clears → new divergence re-notifies."""
    writes = _install_fd_capture(monkeypatch)
    home = tmp_path / "home"
    src = _install_user_identity(monkeypatch, home, "pm", "pm v2")

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    write_identity_hash(runtime, "pm v1")

    # 1. Drift → notify
    notified = check_identity_drift("pm", runtime, tmp_path / "in", notified=False)
    assert notified is True
    assert len(writes) == 1

    # 2. User reverts source to match stored hash
    src.write_text("pm v1")
    notified = check_identity_drift("pm", runtime, tmp_path / "in", notified=notified)
    assert notified is False  # flag cleared
    assert len(writes) == 1  # no new write

    # 3. New divergence → re-notify
    src.write_text("pm v3")
    notified = check_identity_drift("pm", runtime, tmp_path / "in", notified=notified)
    assert notified is True
    assert len(writes) == 2  # fresh notification


def test_check_worker_identity_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Plain 'worker' identity has no source to drift against → no notify."""
    writes = _install_fd_capture(monkeypatch)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    # Even with a stored hash somehow, a 'worker' identity has no source
    write_identity_hash(runtime, "stub")

    result = check_identity_drift(
        "worker", runtime, tmp_path / "in", notified=False
    )
    assert result is False
    assert writes == []
