"""Tests for the non-blocking TUI REPL (#077, D96).

Covers the extracted helpers (``_tui_classify_line``, ``_tui_format_prefix``)
with no prompt_toolkit dependency, plus a minimal smoke test that
constructs the Application wiring with pipe input/DummyOutput to
verify the layout loads and an Enter keystroke exits cleanly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from claude_worker import cli as cw_cli


class TestTuiClassifyLine:
    """_tui_classify_line partitions log entries into display kinds."""

    def test_assistant_by_type(self) -> None:
        data = {"type": "assistant"}
        msg = SimpleNamespace(role="assistant", content="hi")
        assert cw_cli._tui_classify_line(data, msg) == "assistant"

    def test_assistant_by_role(self) -> None:
        data = {"type": "other"}
        msg = SimpleNamespace(role="assistant")
        assert cw_cli._tui_classify_line(data, msg) == "assistant"

    def test_user_input_plain_content(self) -> None:
        data = {"type": "user"}
        msg = SimpleNamespace(role="user", content="hello worker")
        assert cw_cli._tui_classify_line(data, msg) == "user-input"

    def test_system_notification(self) -> None:
        data = {"type": "user"}
        msg = SimpleNamespace(role="user", content="[system:new-message] ping")
        assert cw_cli._tui_classify_line(data, msg) == "system"

    def test_inbound_sender_tag(self) -> None:
        data = {"type": "user"}
        msg = SimpleNamespace(role="user", content="[rhc] hey there")
        assert cw_cli._tui_classify_line(data, msg) == "inbound"

    def test_unknown_type_skipped(self) -> None:
        data = {"type": "result"}
        msg = SimpleNamespace(role="", content="")
        assert cw_cli._tui_classify_line(data, msg) == "skip"

    def test_missing_attrs_tolerated(self) -> None:
        """parse_message results may be missing role/content attrs."""
        data = {"type": "user"}
        msg = object()  # bare object, no role/content
        # Should classify as user-input (empty content passes the checks)
        assert cw_cli._tui_classify_line(data, msg) == "user-input"


class TestTuiFormatPrefix:
    """_tui_format_prefix produces the right display prefix per kind."""

    def test_assistant_no_prefix(self) -> None:
        assert cw_cli._tui_format_prefix("assistant") == ""

    def test_user_input_arrow(self) -> None:
        assert cw_cli._tui_format_prefix("user-input") == "> "

    def test_inbound_with_sender(self) -> None:
        assert cw_cli._tui_format_prefix("inbound", sender="rhc") == "[rhc] "

    def test_inbound_without_sender(self) -> None:
        assert cw_cli._tui_format_prefix("inbound") == "[inbound] "

    def test_system_dot(self) -> None:
        assert cw_cli._tui_format_prefix("system") == "· "

    def test_unknown_no_prefix(self) -> None:
        assert cw_cli._tui_format_prefix("anything-else") == ""


class TestTuiSmoke:
    """Minimal Application smoke test — verifies wiring without a real TTY."""

    def test_tui_application_exits_on_ctrl_d(self, tmp_path, monkeypatch):
        """Construct the Application and submit Ctrl-D; assert clean exit."""
        # Build an empty log file so _repl_tui has something to tail.
        log_file = tmp_path / "log"
        log_file.write_text("")

        # Stub _send_to_single_worker so Enter (if pressed) doesn't try
        # to reach a real worker. Not triggered in this test, but safer.
        monkeypatch.setattr(cw_cli, "_send_to_single_worker", lambda *a, **kw: 0)

        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        with create_pipe_input() as pipe_input:
            pipe_input.send_text("\x04")  # Ctrl-D
            with create_app_session(input=pipe_input, output=DummyOutput()):
                # Run with a tight deadline — if the Ctrl-D binding doesn't
                # trip, the test should fail via the outer pytest timeout.
                cw_cli._repl_tui(
                    name="fake-worker",
                    log_file=log_file,
                    chat_id=None,
                    verbose=False,
                )
        # No exception = clean exit.
