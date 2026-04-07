"""Smoke test for the running_worker fixture + stub-claude harness.

This is the canary — if it passes, the Tier A infrastructure is working
and the richer end-to-end tests can build on it.
"""

from __future__ import annotations

import json
import time

import pytest


class TestStubHarnessSmoke:
    """Verify the stub-claude harness actually spins up a manager with a
    stub subprocess, pumps logs, and shuts down cleanly."""

    def test_initial_message_produces_response(self, running_worker):
        """Spawn a worker with an initial prompt; verify the stub echoes
        back a response into the log file."""
        handle = running_worker(
            name="smoke-1",
            initial_message="hello from test",
            stub_session_id="deadbeef-0000-0000-0000-000000000001",
        )

        # Wait for the stub's response to appear in the log
        assert handle.wait_for_log(
            "stub response to: hello from test", timeout=5.0
        ), f"stub response never appeared in log: {handle.log_path.read_text() if handle.log_path.exists() else '(no log)'}"

        # Verify the init message carries the deterministic session_id
        lines = handle.log_path.read_text().splitlines()
        init_line = next(
            line for line in lines if '"type": "system"' in line and '"init"' in line
        )
        init_data = json.loads(init_line)
        assert init_data["session_id"] == "deadbeef-0000-0000-0000-000000000001"

        handle.stop()
        assert not handle.thread.is_alive()
