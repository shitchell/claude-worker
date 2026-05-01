"""Tests for positional-message shell-hazard detection (#092, D110).

Two failure classes share one root cause: the shell mangles or splits a
positional message body before argparse sees it. Class A is loud
(argparse rejects an unknown ``--flag`` token); Class B is silent
(argparse accepts a mangled body, our code joins it back, ships the
wrong content). The fix has three legs:

1. Heuristic refusal in ``cmd_send`` / ``cmd_broadcast`` — risky bodies
   exit 1 with a stderr error naming the trigger.
2. Argparse postscript via ``ShellAwareParser`` — the standard
   "unrecognized arguments:" message gets a stdin-recommendation tail.
3. Help-text / README documentation pointing at the heredoc pattern.

T1-T11 below cover (1) and (2). E2e verbatim-delivery via stdin lives
in ``tests/test_end_to_end.py`` (T12).
"""

from __future__ import annotations

import argparse
import io

import pytest


def _build_send_args(
    name: str | None,
    message: list[str],
    *,
    queue: bool = False,
) -> argparse.Namespace:
    """Build a minimal Namespace cmd_send accepts.

    Mirrors the shape of the namespace argparse produces for
    ``thread send`` so the validator can run end-to-end without a
    parser dependency.
    """
    return argparse.Namespace(
        name=name,
        message=message,
        queue=queue,
        dry_run=False,
        verbose=False,
        show_response=False,
        show_full_response=False,
        broadcast=False,
        alive=False,
        all_chats=False,
        chat=None,
        role=None,
        status=None,
        cwd_filter=None,
    )


# -- T1-T5, T9: validator unit tests (trigger detection) ----------------


class TestValidatePositionalMessage:
    """``_validate_positional_message`` returns the matched trigger
    name or None. Each trigger must fire on the right input and only
    on the right input."""

    def test_t1_backtick_token_returns_backtick(self):
        from claude_worker.cli import _validate_positional_message

        assert _validate_positional_message(["run", "`ls`", "now"]) == "backtick"

    def test_t2_dollar_paren_returns_shell_substitution(self):
        from claude_worker.cli import _validate_positional_message

        assert (
            _validate_positional_message(["echo", "$(date)"]) == "shell-substitution"
        )

    def test_t3_dollar_brace_returns_shell_substitution(self):
        from claude_worker.cli import _validate_positional_message

        assert (
            _validate_positional_message(["echo", "${HOME}"]) == "shell-substitution"
        )

    def test_t4_em_dash_with_three_tokens_returns_em_dash(self):
        from claude_worker.cli import _validate_positional_message

        assert (
            _validate_positional_message(["use", "—", "tool"]) == "em-dash"
        )

    def test_t5_double_asterisk_with_three_tokens_returns_double_asterisk(self):
        from claude_worker.cli import _validate_positional_message

        assert (
            _validate_positional_message(["fix", "**bold**", "thing"])
            == "double-asterisk"
        )

    def test_t9_em_dash_alone_in_one_token_is_safe(self):
        """A short single-token body with an em-dash is benign — only
        multi-token bodies (≥3) are treated as the markdown-paste
        signal."""
        from claude_worker.cli import _validate_positional_message

        assert _validate_positional_message(["Hello—friend"]) is None

    def test_em_dash_two_tokens_is_safe(self):
        """Two tokens isn't enough to trigger the markdown heuristic."""
        from claude_worker.cli import _validate_positional_message

        assert _validate_positional_message(["Hello", "—"]) is None

    def test_double_asterisk_two_tokens_is_safe(self):
        from claude_worker.cli import _validate_positional_message

        assert _validate_positional_message(["**bold**", "now"]) is None

    def test_double_dash_token_returns_double_dash_separator(self):
        from claude_worker.cli import _validate_positional_message

        assert (
            _validate_positional_message(["before", "--", "after"])
            == "double-dash-separator"
        )

    def test_option_like_token_returns_option_like_token(self):
        from claude_worker.cli import _validate_positional_message

        assert (
            _validate_positional_message(["use", "--port", "8080"])
            == "option-like-token"
        )

    def test_embedded_newline_returns_embedded_newline(self):
        from claude_worker.cli import _validate_positional_message

        assert (
            _validate_positional_message(["line1\nline2"]) == "embedded-newline"
        )

    def test_simple_safe_message_returns_none(self):
        from claude_worker.cli import _validate_positional_message

        assert _validate_positional_message(["hello", "world"]) is None

    def test_empty_message_returns_none(self):
        from claude_worker.cli import _validate_positional_message

        assert _validate_positional_message([]) is None

    def test_en_dash_with_three_tokens_returns_en_dash(self):
        from claude_worker.cli import _validate_positional_message

        assert (
            _validate_positional_message(["a", "–", "b", "c"]) == "en-dash"
        )


# -- T1-T5: cmd_send refusal integration --------------------------------


class TestCmdSendRefusalIntegration:
    """cmd_send must exit 1 with a stderr error naming the trigger and
    pointing at the stdin pattern when the validator fires."""

    def test_t1_cmd_send_refuses_backtick_body(self, fake_worker, capsys):
        from claude_worker.cli import cmd_send

        name = fake_worker([], alive=True)
        args = _build_send_args(name, ["run", "`ls`", "now"])
        with pytest.raises(SystemExit) as exc_info:
            cmd_send(args)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "matched: backtick" in err
        assert "cat <<'EOF' | claude-worker thread send <name>" in err
        assert "single-quoted EOF" in err

    def test_t2_cmd_send_refuses_dollar_paren_body(self, fake_worker, capsys):
        from claude_worker.cli import cmd_send

        name = fake_worker([], alive=True)
        args = _build_send_args(name, ["echo", "$(whoami)"])
        with pytest.raises(SystemExit) as exc_info:
            cmd_send(args)
        assert exc_info.value.code == 1
        assert "matched: shell-substitution" in capsys.readouterr().err

    def test_t3_cmd_send_refuses_dollar_brace_body(self, fake_worker, capsys):
        from claude_worker.cli import cmd_send

        name = fake_worker([], alive=True)
        args = _build_send_args(name, ["echo", "${HOME}"])
        with pytest.raises(SystemExit) as exc_info:
            cmd_send(args)
        assert exc_info.value.code == 1
        assert "matched: shell-substitution" in capsys.readouterr().err

    def test_t4_cmd_send_refuses_em_dash_multi_token(self, fake_worker, capsys):
        from claude_worker.cli import cmd_send

        name = fake_worker([], alive=True)
        args = _build_send_args(name, ["use", "the", "tool", "—", "now"])
        with pytest.raises(SystemExit) as exc_info:
            cmd_send(args)
        assert exc_info.value.code == 1
        assert "matched: em-dash" in capsys.readouterr().err

    def test_t5_cmd_send_refuses_double_asterisk_multi_token(
        self, fake_worker, capsys
    ):
        from claude_worker.cli import cmd_send

        name = fake_worker([], alive=True)
        args = _build_send_args(name, ["fix", "the", "**bold**", "bug"])
        with pytest.raises(SystemExit) as exc_info:
            cmd_send(args)
        assert exc_info.value.code == 1
        assert "matched: double-asterisk" in capsys.readouterr().err

    def test_cmd_broadcast_refuses_backtick_body(self, fake_worker, capsys):
        """The same validator runs for cmd_broadcast (G4: shared helper)."""
        from claude_worker.cli import cmd_broadcast

        fake_worker([], alive=True)
        args = _build_send_args(None, ["run", "`ls`", "now"])
        with pytest.raises(SystemExit) as exc_info:
            cmd_broadcast(args)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "matched: backtick" in err
        # broadcast suggestion text uses the broadcast subcommand
        assert "claude-worker broadcast" in err


# -- T6: argparse "unrecognized arguments" postscript -------------------


class TestArgparsePostscript:
    """Class A (loud) — argparse rejects an unknown ``--flag`` token in
    a positional message. ShellAwareParser appends a stdin-pattern
    postscript so the operator sees the canonical fix."""

    def test_t6_unrecognized_argument_emits_postscript(self, capsys):
        from claude_worker.cli import ShellAwareParser

        parser = ShellAwareParser(prog="claude-worker")
        parser.add_argument("name", nargs="?")
        parser.add_argument("message", nargs="*")

        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["target", "use", "--port", "8080"])
        # argparse exits 2 on parse errors; the postscript is additive.
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "unrecognized arguments" in err
        assert "stdin to bypass argparse" in err
        assert "cat <<'EOF' | claude-worker thread send <name>" in err

    def test_unrelated_error_does_not_get_postscript(self, capsys):
        """The postscript only fires for ``unrecognized arguments:``
        errors so other parser failures (missing required, type errors)
        keep their normal text."""
        from claude_worker.cli import ShellAwareParser

        parser = ShellAwareParser(prog="claude-worker")
        parser.add_argument("--port", type=int, required=True)

        with pytest.raises(SystemExit):
            parser.parse_args([])
        err = capsys.readouterr().err
        assert "stdin to bypass argparse" not in err
        assert "required" in err

    def test_subparsers_inherit_shell_aware_class(self):
        """``add_subparsers`` defaults ``parser_class`` to ``type(self)``
        so sub-parsers get ShellAwareParser without needing an explicit
        kwarg — verify that contract holds."""
        from claude_worker.cli import ShellAwareParser

        parser = ShellAwareParser(prog="claude-worker")
        sub = parser.add_subparsers()
        child = sub.add_parser("thread")
        assert isinstance(child, ShellAwareParser)


# -- T7: stdin path bypasses validation ---------------------------------


class TestStdinBypassesValidator:
    """T7: the same risky content piped via stdin must be delivered
    verbatim. The validator only inspects the positional ``args.message``
    list, so an empty list with stdin data is the canonical safe path."""

    def test_t7_stdin_with_risky_content_is_delivered_verbatim(
        self, fake_worker, monkeypatch
    ):
        from claude_worker import cli as cw_cli

        name = fake_worker([], alive=True)

        # The validator only inspects args.message tokens, not stdin.
        # cmd_send falls through to _send_to_single_worker; intercept
        # it to capture the content cmd_send chose.
        captured: dict = {}

        def fake_send_to_single_worker(target, content, args):
            captured["target"] = target
            captured["content"] = content
            return 0

        monkeypatch.setattr(
            cw_cli, "_send_to_single_worker", fake_send_to_single_worker
        )
        monkeypatch.setattr(cw_cli, "_print_worker_status", lambda *_a, **_k: None)

        risky_payload = (
            "Run `ls` and tell me **why** —\n"
            "multi-line markdown survives intact.\n"
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(risky_payload))

        args = _build_send_args(name, [])
        with pytest.raises(SystemExit) as exc_info:
            cw_cli.cmd_send(args)
        assert exc_info.value.code == 0
        assert captured["target"] == name
        # Verbatim — no whitespace collapse, no shell expansion.
        assert captured["content"] == risky_payload


# -- T8: simple positional bodies still work (no regression) ------------


class TestNoRegressionOnSimplePositional:
    """T8: short benign positional messages must still be delivered.
    The validator's job is to refuse the risky shapes only; it must
    not generate false positives on plain prose."""

    def test_t8_simple_positional_message_passes_validator(
        self, fake_worker, monkeypatch
    ):
        from claude_worker import cli as cw_cli

        name = fake_worker([], alive=True)

        captured: dict = {}

        def fake_send_to_single_worker(target, content, args):
            captured["content"] = content
            return 0

        monkeypatch.setattr(
            cw_cli, "_send_to_single_worker", fake_send_to_single_worker
        )
        monkeypatch.setattr(cw_cli, "_print_worker_status", lambda *_a, **_k: None)

        args = _build_send_args(name, ["hello", "world"])
        with pytest.raises(SystemExit) as exc_info:
            cw_cli.cmd_send(args)
        assert exc_info.value.code == 0
        # Joined back the same way pre-D110 cmd_send did.
        assert captured["content"] == "hello world"


# -- T10/T11: existing _reparse_send_flags behavior is preserved --------


class TestReparseSendFlagsRegression:
    """T10/T11 belt-and-braces — the validator runs after
    ``_reparse_send_flags`` (D110), so trailing recognized flags are
    still extracted before the risk check sees the body."""

    def test_t10_only_queue_flag_extracts_to_queue_true(self):
        from claude_worker.cli import _reparse_send_flags

        args = _build_send_args(name="w", message=["--queue"])
        args = _reparse_send_flags(args)
        assert args.message == []
        assert args.queue is True

    def test_t11_trailing_queue_after_message_extracts_cleanly(self):
        from claude_worker.cli import _reparse_send_flags

        args = _build_send_args(name="w", message=["hello", "world", "--queue"])
        args = _reparse_send_flags(args)
        assert args.message == ["hello", "world"]
        assert args.queue is True

    def test_validator_runs_after_reparse_so_trailing_queue_is_safe(
        self, fake_worker, monkeypatch
    ):
        """Belt-and-braces: ``send NAME "hello world" --queue`` should
        deliver successfully — the trailing flag is consumed before the
        validator sees the body."""
        from claude_worker import cli as cw_cli

        name = fake_worker([], alive=True)
        captured: dict = {}

        def fake_send_to_single_worker(target, content, args):
            captured["content"] = content
            return 0

        # --queue path goes through _wait_for_queue_response after a
        # short FIFO write. Stub the whole single-worker path so this
        # test stays at the validator boundary.
        monkeypatch.setattr(
            cw_cli, "_send_to_single_worker", fake_send_to_single_worker
        )
        monkeypatch.setattr(cw_cli, "_print_worker_status", lambda *_a, **_k: None)

        args = _build_send_args(name, ["hello", "world", "--queue"])
        with pytest.raises(SystemExit) as exc_info:
            cw_cli.cmd_send(args)
        assert exc_info.value.code == 0
        assert captured["content"] == "hello world"
