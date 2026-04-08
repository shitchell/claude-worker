#!/usr/bin/env python3
"""Stub claude binary for claude-worker tests.

Emits canned JSONL matching real claude's stream-json format, without
requiring a real claude install. Drives the end-to-end tests that need
a live subprocess — `send → manager → claude → log` — that unit tests
with monkey-patched fixtures can't reach.

## Protocol

Real claude (`claude -p --input-format stream-json --output-format
stream-json`) reads one user message per stdin line as:

    {"type":"user","message":{"role":"user","content":"..."}}

And emits a stream including at minimum:
- system/init (once at startup, carries session_id)
- assistant messages (with text content blocks)
- result messages (turn boundary)

The stub reproduces enough of this for manager.py's log-pump and
session-capture logic to work.

## Invocation

The manager launches the stub with the same flags it would use for real
claude. The stub ignores unknown flags and just speaks the stream-json
protocol on stdin/stdout.

## Modes

### Canonical mode (default)

On startup, emits a system/init message carrying a deterministic
session_id (from CLAUDE_STUB_SESSION_ID env var, else a random UUID).

For each user message on stdin, emits:
- assistant message echoing the user content (text block)
- result message with stop_reason=end_turn

Exits cleanly on EOF.

### Scripted mode

Set CLAUDE_STUB_SCRIPT to a JSON file with this schema::

    {
      "session_id": "optional-deterministic-uuid",
      "on_user": [
        {
          "match": "optional literal substring of user content",
          "emit": [
            {"type":"assistant","text":"response text"},
            {"type":"result"}
          ]
        }
      ],
      "default_emit": [
        {"type":"assistant","text":"default response"},
        {"type":"result"}
      ]
    }

For each user message, the stub checks `on_user` rules in order and
emits the first matching rule's messages. If nothing matches, emits
`default_emit` (or the canonical echo if `default_emit` is absent).

### Delay

Set CLAUDE_STUB_DELAY_MS to add a sleep (in ms) before each emitted
message, simulating a slow worker. Useful for timing-sensitive tests.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid


def _emit(msg: dict) -> None:
    """Write a JSONL line to stdout and flush immediately."""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _session_id() -> str:
    """Resolve the session_id from env or generate a fresh random one."""
    env_sid = os.environ.get("CLAUDE_STUB_SESSION_ID", "").strip()
    if env_sid:
        return env_sid
    return str(uuid.uuid4())


def _delay() -> None:
    """Optional per-message delay from env."""
    delay_ms = os.environ.get("CLAUDE_STUB_DELAY_MS", "").strip()
    if not delay_ms:
        return
    try:
        time.sleep(int(delay_ms) / 1000.0)
    except ValueError:
        pass


def _load_script() -> dict | None:
    """Load the scripted-response config if CLAUDE_STUB_SCRIPT is set."""
    path = os.environ.get("CLAUDE_STUB_SCRIPT", "").strip()
    if not path:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _build_assistant_message(session_id: str, text: str) -> dict:
    """Build an assistant message matching claude's schema.

    Includes a realistic ``usage`` block so tests exercising token-stats
    helpers (``compute_context_window_usage``, ``compute_token_stats``,
    and claude-worker's ls/tokens/read--context wire-ins) see non-zero
    data. Token values are overridable via env vars:

    - CLAUDE_STUB_INPUT_TOKENS
    - CLAUDE_STUB_OUTPUT_TOKENS
    - CLAUDE_STUB_CACHE_CREATION_TOKENS
    - CLAUDE_STUB_CACHE_READ_TOKENS

    Defaults are small (1 / len(text) / 100 / 1000) so the stub's
    totals stay readable but non-trivial.
    """
    input_tokens = int(os.environ.get("CLAUDE_STUB_INPUT_TOKENS", "1"))
    output_tokens = int(
        os.environ.get("CLAUDE_STUB_OUTPUT_TOKENS", str(max(len(text), 1)))
    )
    cache_creation = int(os.environ.get("CLAUDE_STUB_CACHE_CREATION_TOKENS", "100"))
    cache_read = int(os.environ.get("CLAUDE_STUB_CACHE_READ_TOKENS", "1000"))
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "stub-claude",
            "id": f"msg_{uuid.uuid4().hex[:10]}",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
        "session_id": session_id,
        "uuid": str(uuid.uuid4()),
        "parent_tool_use_id": None,
    }


def _build_result_message(session_id: str) -> dict:
    """Build a result (turn-end) message matching claude's schema."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": session_id,
        "stop_reason": "end_turn",
        "num_turns": 1,
        "uuid": str(uuid.uuid4()),
    }


def _emit_init(session_id: str) -> None:
    """Emit the system/init message that carries the session_id. The
    manager parses this to populate runtime/session and .sessions.json."""
    _emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "cwd": os.getcwd(),
            "tools": [],
            "mcp_servers": [],
            "model": "stub-claude",
            "permissionMode": "bypassPermissions",
            "uuid": str(uuid.uuid4()),
        }
    )


def _emit_for_script_rule(rule: dict, session_id: str, user_content: str) -> None:
    """Emit messages for one scripted response rule."""
    for item in rule.get("emit", []):
        _delay()
        msg_type = item.get("type")
        if msg_type == "assistant":
            _emit(_build_assistant_message(session_id, item.get("text", "")))
        elif msg_type == "result":
            _emit(_build_result_message(session_id))


def _respond_canonical(session_id: str, user_content: str) -> None:
    """Canonical response: echo user content in assistant + result."""
    _delay()
    _emit(_build_assistant_message(session_id, f"stub response to: {user_content}"))
    _delay()
    _emit(_build_result_message(session_id))


def _respond_scripted(script: dict, session_id: str, user_content: str) -> None:
    """Scripted response: find a matching rule and emit its messages."""
    for rule in script.get("on_user", []):
        match = rule.get("match", "")
        if match and match in user_content:
            _emit_for_script_rule(rule, session_id, user_content)
            return
    # No rule matched — try default_emit, or fall back to canonical echo
    default = script.get("default_emit")
    if default is not None:
        _emit_for_script_rule({"emit": default}, session_id, user_content)
    else:
        _respond_canonical(session_id, user_content)


def main() -> int:
    session_id = _session_id()
    # Allow the script file to override session_id
    script = _load_script()
    if script and script.get("session_id"):
        session_id = script["session_id"]

    _emit_init(session_id)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") != "user":
            continue
        content = payload.get("message", {}).get("content", "")
        if not isinstance(content, str):
            content = str(content)

        if script is not None:
            _respond_scripted(script, session_id, content)
        else:
            _respond_canonical(session_id, content)

    return 0


if __name__ == "__main__":
    sys.exit(main())
