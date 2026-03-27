# Architecture

## Overview

claude-worker is a Python CLI that manages Claude Code subprocesses via named FIFOs and the stream-json protocol. Each worker is a `claude -p` process with bidirectional JSONL communication.

```
                    ┌─────────────────────────────────────────┐
                    │           Manager Process                │
                    │          (daemonized via fork)            │
                    │                                          │
  claude-worker     │   ┌─────────┐       ┌──────────────┐    │
  send NAME msg ──▶ │   │ in FIFO │──▶──▶─│ claude -p     │    │
                    │   └─────────┘  thread│ --stream-json │    │
                    │                      │               │    │
                    │   ┌─────────┐  thread│               │    │
  claude-worker     │   │ log file│◀──◀──◀─│               │    │
  read NAME     ◀── │   └─────────┘       └──────────────┘    │
                    │                                          │
                    └─────────────────────────────────────────┘
```

## Runtime Directory

Each worker gets `/tmp/claude-workers/{UID}/{name}/`:

```
/tmp/claude-workers/1000/my-worker/
├── in       # named FIFO — accepts stream-json user messages
├── log      # regular file — all claude stdout (JSONL)
├── pid      # manager process PID
└── session  # claude session ID (written after init)
```

### Why UID in the path?

Prevents collisions on shared machines. Multiple users can run workers without interfering.

### Why FIFOs instead of Unix domain sockets?

FIFOs are simpler — any process can write with `echo '...' > in` or `open(fifo, 'w')`. No client/server protocol needed. The tradeoff is that FIFOs are one-way and need careful EOF handling (see below).

### Why no `out` FIFO?

Early design included an `out` FIFO, but it was dropped. FIFOs block the writer if nobody is reading, which would stall claude's output. A regular `log` file avoids this — claude always writes, and readers can seek/tail at will.

## Process Model

### The Manager

`claude-worker start` forks a background manager process. The manager:

1. Creates the runtime directory and FIFO
2. Spawns `claude -p --input-format stream-json --output-format stream-json`
3. Runs two threads:
   - **FIFO reader**: reads from `in` FIFO, forwards to claude's stdin
   - **Log writer**: reads claude's stdout, writes to `log` file, captures session ID
4. Waits for claude to exit, then cleans up

The parent process (the CLI) waits for the PID file to appear, optionally waits for the session ID (if an initial prompt was sent), prints the worker info, and exits.

### Why fork instead of running in foreground?

The `start` command should return immediately so it works naturally in scripts and from other Claude instances. The manager runs as a background daemon with `os.setsid()` to detach from the terminal.

### Why not just shell background (`&`)?

Forking with `setsid()` gives a clean daemon: no terminal dependency, proper PID tracking, and the parent can wait for initialization before returning.

## Stream-JSON Protocol

Claude Code's `-p` flag with `--input-format stream-json --output-format stream-json` enables bidirectional JSONL communication:

**Input** (written to stdin):
```json
{"type":"user","message":{"role":"user","content":"your message here"}}
```

**Output** (read from stdout): standard Claude Code JSONL — system messages, assistant messages, tool use/results, and result messages.

### Key lesson: no back-to-back user messages

The stream-json protocol does not support sending two user messages in sequence. If you need to combine a system prompt and an instruction, concatenate them into a single message. This is why `--prompt-file` and `--prompt` are joined with `\n\n`.

### Key lesson: `result` does NOT mean session ended

In `-p` stream-json mode, each turn emits a `result` message, but the process stays alive for the next user message. A `result` only means "session truly over" if the process has exited. This is a critical distinction:

| State | Meaning |
|-------|---------|
| `result` + process alive | Turn complete, ready for next message |
| `result` + process dead | Session ended, no more turns |
| `assistant` with `stop_reason=end_turn` | Turn complete (interactive mode) |
| `assistant` with `stop_reason=tool_use` | Mid-turn, tool execution in progress |
| `assistant` with `stop_reason=None` | Partial/streaming chunk |

### Key lesson: user messages are not echoed by default

Claude's stream-json output does NOT include the user messages you send. Without echoing, `wait-for-turn` and `--last-turn` cannot track turn boundaries because they can't see where one turn ends and the next begins.

The fix: add `--replay-user-messages` to the claude invocation. This makes claude echo user messages back in the output stream, so they appear in the log file naturally.

### Key lesson: init arrives after first user message

The `system/init` message (which contains the session ID) does not arrive until after the first user message is sent. If you start a worker without a prompt, there's no init until someone sends a message. The `start` command handles this by only waiting for the session file when an initial prompt is provided.

## FIFO Plumbing

### The EOF problem

Named FIFOs (mkfifo) have a critical behavior: when the last writer closes, the reader sees EOF. If `claude-worker send` writes a message and closes the FIFO, claude's stdin would see EOF and the process would exit.

### The dummy write fd trick

The FIFO reader thread opens the FIFO in a specific order:

```python
# 1. Open read end with O_NONBLOCK (returns immediately, even without a writer)
rd_fd = os.open(in_fifo, os.O_RDONLY | os.O_NONBLOCK)
# 2. Open write end (won't block because a reader exists)
wr_fd = os.open(in_fifo, os.O_WRONLY)
```

The write fd is never written to — it just keeps the FIFO "alive" so that external writers can come and go without triggering EOF. The read fd uses `select()` with a 1-second timeout to poll for data without busy-waiting.

## Status Detection

Worker status is determined by combining PID liveness with log analysis:

1. **dead**: PID file missing or process not running
2. **starting**: process alive but no log output yet
3. **working**: process alive, last significant log entry is a user message (or no turn boundary since last user message)
4. **waiting**: process alive, last significant log entry is a `result` or `assistant` with `stop_reason=end_turn`

### `wait-for-turn` behavior

`wait-for-turn` first scans the existing log to check if the turn already completed. If the most recent turn boundary appears after the most recent user message, it returns immediately. Otherwise, it tails the log file waiting for a new turn boundary.

This prevents a race condition: if you call `send` and then `wait-for-turn`, but the response arrives before `wait-for-turn` starts tailing, the scan catches it.

## Dependencies

- **claugs** (`claude_logs`): JSONL parsing, message type models, rendering/formatting. Used by the `read` command to parse and display log output. This is a locally-installed package (not on PyPI).
- **claude CLI**: the subprocess being wrapped. Must be on PATH.

## Environment

### ANTHROPIC_API_KEY

The manager explicitly **unsets** `ANTHROPIC_API_KEY` in the subprocess environment. This forces claude to use subscription-based auth rather than API billing. Without this, you'd burn API credits instead of using your Max subscription.

### Default flags

Workers are launched with `--dangerously-skip-permissions` by default since they're non-interactive subordinate processes. This can be overridden via extra claude args.
