# 036: Technical Design — REPL Continuous Output

## Architecture: Option B (continuous flow with on-demand input)

### State machine

```
                    ┌────────────────────┐
                    │     FLOWING        │
                    │  (tail -f mode)    │
                    │  output streams    │
                    │  stdin monitored   │
                    └────────┬───────────┘
                             │ keypress detected
                             ▼
                    ┌────────────────────┐
                    │    INPUTTING       │
                    │  output buffered   │
                    │  readline active   │
                    │  prompt visible    │
                    └────────┬───────────┘
                             │ Enter (submit) or Esc (cancel)
                             ▼
                    ┌────────────────────┐
                    │  flush buffered    │
                    │  messages, send    │
                    │  user input        │
                    └────────┬───────────┘
                             │
                             ▼ back to FLOWING
```

### Implementation: raw terminal mode + threads

**No prompt_toolkit dependency.** While prompt_toolkit would work, it's
a heavy dependency (~1MB) for what is fundamentally:
1. A background thread tailing the log and printing
2. A foreground thread in raw terminal mode detecting keypresses
3. A brief switch to cooked mode for readline input

The raw-mode approach is ~80 lines and uses only stdlib (termios, tty,
select, threading).

### Design

**FLOWING state:**
- Background thread: tail log file, render new messages via claugs,
  print to stdout
- Foreground: `select([sys.stdin], [], [], 0.1)` in a loop — checks
  for keypress without blocking output thread
- On keypress: transition to INPUTTING

**INPUTTING state:**
- Stop the output thread (set an event)
- Restore cooked terminal mode
- Show prompt: `you> ` via `input()` (gets readline for free)
- On submit: send message, restart output thread, back to FLOWING
- On Ctrl-D/Esc/empty: cancel, restart output thread, back to FLOWING

**Buffering during input:**
- The output thread pauses (stop event) but the log file keeps growing
- On transition back to FLOWING, seek to the position where we paused
  and catch up — no messages lost

**Terminal cleanup:**
- `try/finally` restores terminal to cooked mode on any exit
- Handles SIGINT, EOFError, exceptions

### LOE

- ~100 lines for the new REPL loop (replacing the current ~80 lines)
- Terminal mode management: ~15 lines
- Total: ~120 lines, net ~40 lines added

### Test plan

Limited — the continuous REPL is inherently interactive. Testable parts:
- State transitions (mocked stdin/stdout)
- Message buffering across state changes

The existing REPL tests exercise the send/receive pipeline which is
preserved. The UX change is in the input/output interleaving.

### Risk

Terminal state corruption if the process crashes in raw mode. Mitigated
by wrapping the entire REPL in a try/finally that restores termios.
