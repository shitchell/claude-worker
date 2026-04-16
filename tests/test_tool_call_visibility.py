"""Tests for ls current-tool-call visibility (#081, D98).

Covers:
- ``_format_tool_call`` — per-tool pretty rendering
- ``_format_tool_call_duration`` — short duration formatting
- ``_find_current_tool_call`` — walks log, matches tool_use/tool_result
- ``cmd_list`` — text and JSON output include the new field correctly
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pytest

from claude_worker import cli as cw_cli


def _write_log(path: Path, entries: list[dict]) -> None:
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _assistant(blocks: list[dict], timestamp: str = "2026-04-16T08:00:00Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": blocks},
    }


def _user_result(tool_use_id: str, timestamp: str = "2026-04-16T08:00:01Z") -> dict:
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "ok",
                }
            ],
        },
    }


class TestFormatToolCall:
    def test_bash_command(self) -> None:
        block = {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}
        assert cw_cli._format_tool_call(block) == "Bash(ls -la)"

    def test_bash_truncates_long_command(self) -> None:
        cmd = "a" * 200
        block = {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
        result = cw_cli._format_tool_call(block)
        assert result.startswith("Bash(")
        assert "…" in result
        assert len(result) < 100  # well within the preview length

    def test_edit_shows_basename(self) -> None:
        block = {
            "type": "tool_use",
            "name": "Edit",
            "input": {"file_path": "/home/guy/proj/src/main.py"},
        }
        assert cw_cli._format_tool_call(block) == "Edit(main.py)"

    def test_write_basename(self) -> None:
        block = {
            "type": "tool_use",
            "name": "Write",
            "input": {"file_path": "/a/b/c.txt"},
        }
        assert cw_cli._format_tool_call(block) == "Write(c.txt)"

    def test_task_description(self) -> None:
        block = {
            "type": "tool_use",
            "name": "Task",
            "input": {"description": "Explore codebase"},
        }
        assert cw_cli._format_tool_call(block) == "Task(Explore codebase)"

    def test_grep_pattern(self) -> None:
        block = {"type": "tool_use", "name": "Grep", "input": {"pattern": "def foo"}}
        assert cw_cli._format_tool_call(block) == "Grep(def foo)"

    def test_unknown_tool_bare_name(self) -> None:
        block = {"type": "tool_use", "name": "WeirdTool", "input": {"x": 1}}
        assert cw_cli._format_tool_call(block) == "WeirdTool"

    def test_missing_input_gives_bare_name(self) -> None:
        block = {"type": "tool_use", "name": "Bash"}
        assert cw_cli._format_tool_call(block) == "Bash"


class TestFormatDuration:
    def test_seconds(self) -> None:
        assert cw_cli._format_tool_call_duration(12) == "(12s)"

    def test_minutes_seconds(self) -> None:
        assert cw_cli._format_tool_call_duration(135) == "(2m 15s)"

    def test_hours_minutes(self) -> None:
        assert cw_cli._format_tool_call_duration(3780) == "(1h 3m)"

    def test_zero(self) -> None:
        assert cw_cli._format_tool_call_duration(0.0) == "(0s)"


class TestFindCurrentToolCall:
    def test_empty_log_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        log.write_text("")
        assert cw_cli._find_current_tool_call(log) is None

    def test_missing_log_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "nope"
        assert cw_cli._find_current_tool_call(log) is None

    def test_assistant_without_tool_use_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        _write_log(
            log,
            [_assistant([{"type": "text", "text": "hi"}])],
        )
        assert cw_cli._find_current_tool_call(log) is None

    def test_open_tool_use_detected(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        _write_log(
            log,
            [
                _assistant(
                    [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Bash",
                            "input": {"command": "sleep 30"},
                        }
                    ]
                )
            ],
        )
        result = cw_cli._find_current_tool_call(log, now=1e12)
        assert result is not None
        assert result["tool_use_id"] == "toolu_1"
        assert result["name"] == "Bash"
        assert "Bash(sleep 30)" == result["display"]
        assert result["duration_seconds"] > 0

    def test_resolved_tool_use_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        _write_log(
            log,
            [
                _assistant(
                    [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ]
                ),
                _user_result("toolu_1"),
            ],
        )
        assert cw_cli._find_current_tool_call(log) is None

    def test_multiple_tool_uses_one_open(self, tmp_path: Path) -> None:
        log = tmp_path / "log"
        _write_log(
            log,
            [
                _assistant(
                    [
                        {
                            "type": "tool_use",
                            "id": "toolu_a",
                            "name": "Bash",
                            "input": {"command": "echo 1"},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_b",
                            "name": "Bash",
                            "input": {"command": "echo 2"},
                        },
                    ]
                ),
                _user_result("toolu_a"),
                # toolu_b has no result yet
            ],
        )
        result = cw_cli._find_current_tool_call(log)
        assert result is not None
        assert result["tool_use_id"] == "toolu_b"

    def test_picks_most_recent_open_call(self, tmp_path: Path) -> None:
        """When multiple assistant messages have open tool_uses, pick the newest."""
        log = tmp_path / "log"
        _write_log(
            log,
            [
                _assistant(
                    [
                        {
                            "type": "tool_use",
                            "id": "old",
                            "name": "Bash",
                            "input": {"command": "slow"},
                        }
                    ],
                    timestamp="2026-04-16T08:00:00Z",
                ),
                _assistant(
                    [
                        {
                            "type": "tool_use",
                            "id": "new",
                            "name": "Edit",
                            "input": {"file_path": "/x/y.py"},
                        }
                    ],
                    timestamp="2026-04-16T08:05:00Z",
                ),
            ],
        )
        result = cw_cli._find_current_tool_call(log)
        assert result is not None
        assert result["tool_use_id"] == "new"


class TestLsIntegration:
    """cmd_list wires the helper into both text and JSON output."""

    def test_json_includes_current_tool_key(
        self, fake_worker, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = fake_worker(
            [
                _assistant(
                    [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Bash",
                            "input": {"command": "pytest"},
                        }
                    ]
                )
            ],
            alive=True,
        )
        args = argparse.Namespace(
            format="json",
            role=None,
            status=None,
            alive=False,
            cwd_filter=None,
        )
        cw_cli.cmd_list(args)
        out = capsys.readouterr().out.strip().splitlines()
        assert out, "expected at least one worker in output"
        # Find the entry for our fake worker.
        entries = [json.loads(line) for line in out]
        match = next((e for e in entries if e["name"] == name), None)
        assert match is not None
        assert "current_tool" in match
        assert match["current_tool"] is not None
        assert match["current_tool"]["name"] == "Bash"
        assert "Bash(pytest)" == match["current_tool"]["display"]

    def test_json_null_when_no_tool_open(
        self, fake_worker, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = fake_worker(
            [_assistant([{"type": "text", "text": "just talking"}])],
            alive=True,
        )
        args = argparse.Namespace(
            format="json",
            role=None,
            status=None,
            alive=False,
            cwd_filter=None,
        )
        cw_cli.cmd_list(args)
        out = capsys.readouterr().out.strip().splitlines()
        entries = [json.loads(line) for line in out]
        match = next((e for e in entries if e["name"] == name), None)
        assert match is not None
        assert match["current_tool"] is None

    def test_text_shows_tool_line(
        self, fake_worker, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = fake_worker(
            [
                _assistant(
                    [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Bash",
                            "input": {"command": "sleep 30"},
                        }
                    ]
                )
            ],
            alive=True,
        )
        args = argparse.Namespace(
            format=None,
            role=None,
            status=None,
            alive=False,
            cwd_filter=None,
        )
        cw_cli.cmd_list(args)
        out = capsys.readouterr().out
        assert "tool: Bash(sleep 30)" in out

    def test_text_omits_tool_line_when_none(
        self, fake_worker, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = fake_worker(
            [_assistant([{"type": "text", "text": "just talking"}])],
            alive=True,
        )
        args = argparse.Namespace(
            format=None,
            role=None,
            status=None,
            alive=False,
            cwd_filter=None,
        )
        cw_cli.cmd_list(args)
        out = capsys.readouterr().out
        assert "tool:" not in out
