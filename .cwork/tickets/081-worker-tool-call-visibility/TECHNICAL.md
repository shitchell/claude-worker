# TECHNICAL — #081 worker-tool-call-visibility (Phase A)

## PM-approved scope

Phase A only: `ls` gains current-tool-call + duration display.
Phase B (subagents subcommand) split to ticket #083 (filed).

## Design

### Detection

Walk the worker's log backwards (via existing `_iter_log_reverse`)
looking for the most recent assistant message with `tool_use`
content blocks. For each tool_use id found:

1. Scan forward from that point to see if a matching
   `tool_result` appears in a subsequent user message's content.
2. If at least one tool_use has no matching result → the worker
   is "currently in a tool call".

Duration: `now() - assistant_message.timestamp`.

This is point-in-time — same invariants as the existing status
detection. No active polling.

### Display

`ls` gains a new line under `last:` showing `tool:`:

```
  my-worker
    pid: 1234  status: working  idle: -  cwd: ~/proj
    session: abc123...
    last: I'll run the tests now...
    tool: Bash(pytest tests/)  (12s)
```

When no tool is open, the line is omitted (not `tool: (idle)` —
we already have `status: waiting` for that).

### Tool-name formatting

Extracted helper `_format_tool_call(tool_use_block)`:

| Tool | Display |
|------|---------|
| Bash | `Bash({cmd truncated to TOOL_PREVIEW_LENGTH})` |
| Edit/Write/Read/MultiEdit | `Edit({basename(file_path)})` |
| Task/Agent | `Task({description first 40 chars})` |
| Grep/Glob | `Grep({pattern truncated})` |
| (other) | bare tool name |

Duration formatting reuses the existing `_format_duration` helper.

### JSON output

`ls --format json` gains a `current_tool` field per worker:

```json
{
  "name": "...",
  "status": "working",
  "current_tool": {
    "name": "Bash",
    "display": "Bash(pytest tests/)",
    "duration_seconds": 12.3,
    "tool_use_id": "toolu_..."
  },
  ...
}
```

`current_tool: null` when none open. Never omitted (explicit null
keeps JSON shape stable for scripts).

### Implementation

New helpers in `cli.py`:

```python
def _find_current_tool_call(log_path: Path, now: float | None = None) -> dict | None:
    """Walk log backwards, return the currently-open tool_use dict or None.

    Returns {tool_name, input, started_at, tool_use_id, duration_seconds}.
    """

def _format_tool_call(tool_use: dict) -> str:
    """Render a tool_use block for ls display."""
```

`_format_worker_line` and the JSON serializer gain a call to
`_find_current_tool_call(log_path)` and format the result.

### Performance

Walking log backward is O(n) bytes until the first assistant
message. For a multi-MB log this could be ~100ms. Bound:
only scan the last `TOOL_CALL_SCAN_WINDOW_BYTES` bytes (e.g.,
last 256KB). If no matching assistant message is found in that
window, return None — effectively saying "couldn't tell".

## Tests

`tests/test_tool_call_visibility.py`:

1. `test_no_assistant_messages_returns_none` — empty log → None.
2. `test_assistant_without_tool_use_returns_none` — only text
   blocks → None.
3. `test_open_tool_use_detected` — assistant emits tool_use,
   no tool_result yet → returns the tool info.
4. `test_resolved_tool_use_returns_none` — assistant emits
   tool_use, subsequent user has tool_result → None.
5. `test_multiple_tool_uses_one_open` — assistant emits 2
   tool_use blocks, user returns 1 tool_result → the unresolved
   one is returned.
6. `test_format_bash_truncated` — `_format_tool_call` truncates
   long Bash commands.
7. `test_format_edit_basename` — Edit shows file basename only.
8. `test_format_task_description` — Task shows description.
9. `test_format_duration_displayed` — `(12s)` / `(2m 15s)` /
   `(1h)` formatting.
10. `test_ls_json_includes_current_tool` — running `cmd_list` in
    JSON mode includes the `current_tool` field (or `null`).
11. `test_ls_text_shows_tool_line` — running `cmd_list` in text
    mode includes the `tool:` line when a tool is open.

## Risks

1. **Inline tool_result race** — claude can emit multiple
   assistant messages with tool_use before any user tool_result
   arrives (multi-tool turn). Our walk-back scheme finds the
   MOST RECENT assistant, which may not include the open
   tool_use. Mitigation: if the most recent assistant has only
   resolved tool_uses, walk further back until we hit the
   genuinely open one, capped by scan window.
2. **Large logs** — capped at 256KB scan window.
3. **Clock skew** — duration uses `log-mtime-in-same-turn` vs
   `now()`. Acceptable imprecision for display.

## LOE

- cli.py: ~120 lines (helpers + ls format extension)
- tests: ~200 lines
- README: ~15 lines (`ls` section update)

Total: ~335 lines. Well under 500.

## GVP alignment

- G2 (loud-over-silent-failure): exposes mid-turn tool state
  so `ls` tells you "Bash(sleep 300)" not just "working".
- V1 (clarity-over-cleverness): one line answers "what is the
  worker actually doing right now?"

New decision `D98` records the mechanism and the 256KB scan cap.
