"""Tests for the session-uuid-env-injection.sh hook script.

Imp-3: the regex matches session_id anywhere in the payload and only
accepts lowercase hex. If the SessionStart payload schema ever embeds
a nested session_id (e.g., a replay or parent reference), the hook
extracts the wrong one. Uppercase UUIDs (RFC 4122 allows both) would
be silently rejected.

Also wants: a writability guard on CLAUDE_ENV_FILE so a malicious env
setter can't redirect the append to an arbitrary location.

These tests shell out to the actual bash script with canned stdin.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


HOOK_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "claude_worker"
    / "hooks"
    / "session-uuid-env-injection.sh"
)


def _run_hook(stdin_payload: str, env_file: Path) -> subprocess.CompletedProcess:
    """Run the hook script with the given stdin payload and CLAUDE_ENV_FILE."""
    env = os.environ.copy()
    env["CLAUDE_ENV_FILE"] = str(env_file)
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=stdin_payload,
        capture_output=True,
        text=True,
        env=env,
    )


class TestBasicExtraction:
    """The happy path: well-formed SessionStart payload → export line written."""

    def test_well_formed_payload(self, tmp_path):
        env_file = tmp_path / "env"
        env_file.write_text("")

        payload = (
            '{"session_id":"f94d9f0d-2e8d-44b5-920d-79bfea135ede",'
            '"hook_event_name":"SessionStart","cwd":"/tmp","transcript_path":"/tmp/t.jsonl",'
            '"permission_mode":"default"}'
        )
        result = _run_hook(payload, env_file)
        assert result.returncode == 0
        assert (
            "export CLAUDE_SESSION_UUID=f94d9f0d-2e8d-44b5-920d-79bfea135ede"
            in env_file.read_text()
        )

    def test_payload_with_spaces_after_colons(self, tmp_path):
        """Pretty-printed JSON (with spaces) should still work."""
        env_file = tmp_path / "env"
        env_file.write_text("")

        payload = (
            '{\n  "session_id": "f94d9f0d-2e8d-44b5-920d-79bfea135ede",\n'
            '  "hook_event_name": "SessionStart"\n}'
        )
        result = _run_hook(payload, env_file)
        assert result.returncode == 0
        assert (
            "export CLAUDE_SESSION_UUID=f94d9f0d-2e8d-44b5-920d-79bfea135ede"
            in env_file.read_text()
        )


class TestUppercaseUuid:
    """Imp-3: uppercase hex UUIDs (RFC 4122 allows them) must be accepted."""

    def test_uppercase_uuid_accepted(self, tmp_path):
        env_file = tmp_path / "env"
        env_file.write_text("")

        payload = '{"session_id":"F94D9F0D-2E8D-44B5-920D-79BFEA135EDE"}'
        result = _run_hook(payload, env_file)
        assert result.returncode == 0
        assert (
            "export CLAUDE_SESSION_UUID=F94D9F0D-2E8D-44B5-920D-79BFEA135EDE"
            in env_file.read_text()
        )

    def test_mixed_case_uuid_accepted(self, tmp_path):
        env_file = tmp_path / "env"
        env_file.write_text("")

        payload = '{"session_id":"F94d9F0d-2E8d-44b5-920D-79bfea135EDE"}'
        result = _run_hook(payload, env_file)
        assert result.returncode == 0
        assert "F94d9F0d-2E8d-44b5-920D-79bfea135EDE" in env_file.read_text()


class TestMalformedInput:
    """Malformed or missing inputs should fail closed (exit 0, nothing written)."""

    def test_no_session_id_exits_cleanly(self, tmp_path):
        env_file = tmp_path / "env"
        env_file.write_text("")

        payload = '{"hook_event_name":"SessionStart"}'
        result = _run_hook(payload, env_file)
        assert result.returncode == 0
        assert env_file.read_text() == ""

    def test_malformed_uuid_shape_rejected(self, tmp_path):
        env_file = tmp_path / "env"
        env_file.write_text("")

        # session_id with wrong shape (not 8-4-4-4-12)
        payload = '{"session_id":"not-a-uuid"}'
        result = _run_hook(payload, env_file)
        assert result.returncode == 0
        assert env_file.read_text() == ""

    def test_empty_stdin(self, tmp_path):
        env_file = tmp_path / "env"
        env_file.write_text("")

        result = _run_hook("", env_file)
        assert result.returncode == 0
        assert env_file.read_text() == ""

    def test_missing_env_file_var_exits_cleanly(self, tmp_path):
        """Without CLAUDE_ENV_FILE, nothing to do — exit without writing."""
        env = os.environ.copy()
        env.pop("CLAUDE_ENV_FILE", None)
        result = subprocess.run(
            ["bash", str(HOOK_SCRIPT)],
            input='{"session_id":"f94d9f0d-2e8d-44b5-920d-79bfea135ede"}',
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
