# TECHNICAL — #077 non-blocking-repl

## Library choice: prompt_toolkit (confirmed)

`prompt_toolkit` (already installed globally at 3.0.52) over the
alternatives:

- **vs textual**: textual is full-screen TUI framework; overkill
  for a REPL. prompt_toolkit's `Application` + `patch_stdout` +
  input buffer primitives are built for exactly this use case.
- **vs urwid**: older, lower-level, no built-in asyncio integration.
- **vs custom termios/curses**: high risk of edge cases with
  redirected stdout, subagent output, terminal resize.

prompt_toolkit's `Application` with a `BufferControl` (output log
above) and a `TextArea` (input pinned at bottom) is the canonical
pattern — it composes with asyncio out of the box.

### New dependency

Add `prompt_toolkit>=3.0` to `pyproject.toml`. Very common — already
a transitive dep of many tools in the user's environment.

## Mode selection: `--tui` flag (new mode, default stays turn-based)

**Default REPL remains turn-based** (today's behavior). The new
TUI mode is opt-in via `claude-worker repl --tui NAME`.

Rationale:
1. TUI requires a TTY. Piped stdin / non-TTY environments need the
   turn-based fallback.
2. Turn-based REPL composes well with shell pipelines; TUI
   hijacks the terminal.
3. Low-risk incremental change — no behavior change for existing
   users.
4. Matches the ticket's design-question #4 ("keep simple REPL as
   fallback for dumb terminals"): yes, because non-TTY needs it.

If PM prefers TUI as the default when on a TTY (falling back to
turn-based otherwise), the dispatch in `cmd_repl` just flips
condition order. Trivial to change post-implementation.

**OPEN QUESTION for PM**: default-TUI-on-TTY vs opt-in
`--tui`? Proceeding with opt-in; say the word if you want
default-on-TTY.

## Streaming model: turn-complete (not token-by-token)

Worker messages stream to log on turn completion (one
`assistant` JSONL line per message). Token-by-token would require
parsing intra-turn streaming chunks and maintaining per-stream
reconstruction state — doable but significant added complexity.

Current `read --follow` and the existing `--continuous` REPL show
turn-complete. TUI keeps that model for symmetry.

Future: token streaming is a separate ticket if the human wants
finer-grained display.

## Multi-sender visual distinction

Each log/thread message includes a `sender` field (for thread
messages) or a role (`user`/`assistant`) (for log messages).

Rendering:
- **Assistant** (worker output): no prefix, plain text, formatted
  via existing `claude_logs.ANSIFormatter`.
- **User (me)** (what I typed and submitted): `> ` prefix, cyan.
- **Other senders** (inbound from another worker/human via thread):
  `[sender] ` prefix, yellow.
- **System notifications** (`[system:new-message]`, etc.): gray,
  italic if supported.

Colors applied via prompt_toolkit's `FormattedText` primitives.

## Architecture

```
┌────────────────────────────────────────────────┐
│ Output buffer (scrollable, read-only)          │
│                                                │
│ [alice] hi                                     │
│ > what's the status?                           │
│ (assistant output from worker...)              │
│                                                │
│ ──────────────────────────────────────────────│
│ > |                           (input field)    │
└────────────────────────────────────────────────┘
```

Components (prompt_toolkit):
- `Window(BufferControl(output_buffer))` — top, flex height
- `Window(height=1)` — separator
- `Window(BufferControl(input_buffer))` — bottom, 1 line

Async tasks:
1. **Log tailer** — async generator reading the worker's log file,
   yielding new `assistant`/`user`/`system` entries. Appends to
   `output_buffer`. Same polling pattern as `_watch_thread`.
2. **Thread tailer** — same for the worker's pair thread (catches
   inbound messages from other senders).
3. **Input handler** — on Enter, read input_buffer, append to
   output_buffer with `> ` prefix, submit via `_send_to_single_worker`,
   clear input_buffer.

All three run concurrently. prompt_toolkit's event loop handles
redraw automatically on buffer mutation.

## Exit conditions

- Ctrl-D on empty input → graceful exit (app.exit())
- `/exit` or `/quit` typed → graceful exit
- Ctrl-C → same two-strikes pattern as current REPL
- Worker dies → print notice, exit

## Tests

TUI testing is notoriously painful. Strategy:

1. **Unit tests for extracted helpers** — color/format functions,
   prefix logic, exit-condition matching. No prompt_toolkit import
   required. Easy to hit with pytest.
2. **Smoke test the TUI wiring** — use `prompt_toolkit.input.create_pipe_input`
   and `DummyOutput` to construct a minimal Application, feed a
   keystroke, assert it reaches the input buffer, simulate a message
   arrival, assert it appears in output buffer. ~50 lines.
3. **No full-screen behavioral test** — that requires a pty harness.
   Out of scope for this ticket; covered by manual smoke.

## Scope estimate

- `_repl_tui()` + async tailers: ~180 lines
- `--tui` flag + dispatch: ~15 lines
- Extracted helpers for testability: ~40 lines
- Tests: ~100 lines (mostly unit)
- README update: ~30 lines
- pyproject.toml: 1 line

Total: ~365 lines — within the 500-line budget.

## Risks

- **TUI + scratch subagent output**: if a subagent inside claude
  writes to stdout while the TUI has the terminal, we'd get
  garbled output. Mitigation: prompt_toolkit's `patch_stdout` context
  manager routes prints through the output buffer. Confirm in impl.
- **Terminal resize**: prompt_toolkit handles SIGWINCH natively, but
  the output-buffer scroll position should pin to bottom on resize.
- **Test flakiness in TUI smoke test**: async + pipe input can be
  flaky. Keep the smoke test minimal (single keystroke, single
  message arrival) and use `asyncio.wait_for` with tight timeouts.
- **prompt_toolkit version pin**: 3.0 API is stable, but different
  minor versions have moved some imports. Pin `>=3.0,<4`.

## GVP alignment

- V1 (clarity-over-cleverness): the REPL being frozen while agent
  works is surprising; a pinned input field is more obvious.
- G3 (testable-at-every-layer): extracted helpers + smoke test hit
  both layers. Full TUI testing is manual — flag in the docs.
- New decision `D96` records the library choice + opt-in mode
  rationale.
