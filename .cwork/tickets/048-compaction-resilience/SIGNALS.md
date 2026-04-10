# Signal Behavior Experiment — claude -p stream-json mode

## Test Setup

Workers started with `--no-permission-hook` to isolate signal behavior.
Claude process PID from `runtime/claude-pid`. Signals sent directly to
the claude (Node.js) process, NOT the manager.

## Results

| Signal | While Idle | While Mid-Turn |
|--------|-----------|----------------|
| SIGUSR1 | **No effect.** Claude stays alive, no log entries, status unchanged. | **No effect.** Claude continues generating output. Log entries continue normally. |
| SIGUSR2 | **KILLS the session.** Claude process dies immediately. Manager cleans up runtime dir. | (Not tested separately — assumed fatal like idle.) |
| SIGINT | (Not tested while idle.) | **KILLS the session.** Claude process dies immediately. Manager cleans up runtime dir. Fatal, not an interrupt. |

## Key Findings

1. **SIGUSR1 is ignored by the claude process.** It's caught (in the
   signal mask) but the handler does nothing visible. The manager's
   SIGUSR1 handler (handle_replace) only fires when SIGUSR1 is sent
   to the MANAGER PID, not the claude PID.

2. **SIGUSR2 kills the session.** Fatal — not usable as an interrupt
   mechanism.

3. **SIGINT kills the session in -p mode.** Unlike the interactive UI
   where Ctrl-C interrupts the turn and returns to the prompt, in -p
   stream-json mode SIGINT terminates the process entirely. **Not
   usable as a graceful interrupt.**

4. **There is NO signal that gracefully interrupts a turn in -p mode**
   without killing the session. The only mid-turn intervention
   mechanism is **PostToolUse exit 2** (blocking error shown to Claude).

## Implications for #048

- **PostToolUse exit 2 is the ONLY viable mid-turn mechanism.** No
  signal can interrupt a turn without killing the session.
- The "FIFO user message as soft interrupt" approach also won't work
  mid-turn — FIFO messages are queued until the turn ends.
- The context_mid_turn.py PostToolUse hook design is confirmed as the
  right approach.
- If we need to FORCE a turn to end (context critically high), the
  only option is killing the session (SIGINT/SIGUSR2) and restarting
  with --resume. This is the nuclear option — acceptable for >95%
  context but not for 80%.
