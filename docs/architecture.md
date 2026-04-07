# Architecture

## Overview

`claude-worker` is a Python CLI that manages Claude Code subprocesses via
named FIFOs and the stream-json protocol. Each worker is a `claude -p` process
with bidirectional JSONL I/O, wrapped by a daemonized Python "manager" that
bridges the FIFO, tees the stdout log, and persists session metadata.

```
                    ┌──────────────────────────────────────────┐
                    │           Manager Process                 │
                    │         (daemonized via fork)              │
  claude-worker     │                                            │
  send NAME msg ──▶ │   ┌─────────┐       ┌──────────────┐      │
                    │   │ in FIFO │──▶──▶─│ claude -p     │      │
                    │   └─────────┘  thread│ --stream-json │      │
                    │                      │               │      │
                    │   ┌─────────┐  thread│               │      │
  claude-worker     │   │ log file│◀──◀──◀─│               │      │
  read NAME      ◀──│   └─────────┘       └──────────────┘      │
                    │                                            │
                    │   ┌───────────────┐                        │
                    │   │ session,pid,  │  state sidecar files   │
                    │   │ claude-pid,   │                        │
                    │   │ identity.md,  │                        │
                    │   │ missing-tags  │                        │
                    │   └───────────────┘                        │
                    └──────────────────────────────────────────┘
```

## Runtime directory

Each worker gets `/tmp/claude-workers/{UID}/{name}/`:

```
/tmp/claude-workers/1000/my-worker/
├── in                # named FIFO — accepts stream-json user messages
├── log               # regular file — all claude stdout (JSONL)
├── pid               # manager process PID
├── claude-pid        # claude subprocess PID (test-harness sidecar)
├── session           # claude session ID (written after init message)
├── identity.md       # PM identity markdown (PM workers only)
└── missing-tags.json # PM tag monitoring dedup log (PM workers only)
```

Persistent metadata (session ID, cwd, claude args, PM flag, agent) lives in
`/tmp/claude-workers/{UID}/.sessions.json`, keyed by worker name. Writes use
atomic sibling-file + `os.replace()` so a crash mid-save never truncates the
file.

### Why UID in the path?

Prevents collisions on shared machines. Multiple users can run workers
without interfering.

### Why FIFOs instead of Unix domain sockets?

FIFOs are simpler — any process can write with `echo '...' > in` or
`open(fifo, 'w')`. No client/server protocol needed. The tradeoff is that
FIFOs are one-way and need careful EOF handling (see below).

### Why no `out` FIFO?

Early design included an `out` FIFO, but it was dropped. FIFOs block the
writer if nobody is reading, which would stall claude's output. A regular
`log` file avoids this — claude always writes, and readers can seek/tail
at will. This also lets `read --since` scan back into historical content.

## Process model

### The fork/manager pattern

`claude-worker start` calls `os.fork()` and has the child detach via
`setsid()`, redirect stdio to `/dev/null`, and become the manager. The
parent CLI waits for the PID file to appear (synchronization) and
returns immediately (unless `--background` or a prompt-response wait
is in progress).

The manager's main job is:

1. Create the runtime directory and FIFO
2. Launch `claude -p --input-format stream-json --output-format stream-json
   --replay-user-messages --dangerously-skip-permissions [extra claude args]`
3. Start two daemon threads:
   - **stdout_to_log**: reads claude's stdout, writes to the `log` file,
     captures the session ID from the first `system/init` message
   - **fifo_to_stdin**: reads the `in` FIFO, forwards to claude's stdin
4. Forward an initial prompt (if provided) as the first user message
5. Wait for claude to exit, then clean up

### Test-only forkless mode

`run_manager` is a thin wrapper around `_run_manager_forkless(...,
install_signals=True)`. The test harness calls `_run_manager_forkless`
directly in a thread with `install_signals=False`, skipping the fork
and the SIGTERM/SIGINT handlers. Production behavior is identical;
tests get to inspect runtime state and cleanly tear down workers
without a forked process.

The test harness discovers the claude subprocess via a sidecar
`runtime/claude-pid` file that the manager writes after `subprocess.Popen`,
letting tests SIGTERM the stub directly to trigger shutdown without
signaling the Python test runner.

### Claude binary resolution

The manager looks up the claude binary via `_resolve_claude_bin()`, which
reads `CLAUDE_WORKER_CLAUDE_BIN` (defaulting to `"claude"` on PATH). Tests
point this at `tests/stub_claude.sh` to run the full pipeline without a
real claude install. Production deployments never need to set this env var.

### Signal handling

The manager installs SIGTERM and SIGINT handlers that:

1. Call `proc.terminate()` on the claude subprocess
2. `proc.wait(timeout=SIGTERM_WAIT_TIMEOUT_SECONDS)` — 10s grace period
3. On timeout, escalate to `proc.kill()` and wait again (best-effort)
4. In a `finally` block, `cleanup_runtime_dir(name)` — always runs
5. `sys.exit(0)`

`cleanup_runtime_dir` uses `shutil.rmtree(runtime, ignore_errors=True)` so
it's idempotent and handles subdirectories. Three callers race on it in
practice — the SIGTERM handler, the natural `proc.wait` exit at the bottom
of `_run_manager_forkless`, and `cmd_stop` after sending SIGTERM — and all
three are safe because of the idempotency.

### Manager thread panic handling

Both daemon threads run through a `_run_manager_thread` wrapper that catches
`Exception` from the thread body and routes it to `_manager_thread_panic`,
which (a) appends a `type: "manager_error"` sentinel line to the log so
operators reading the log see a clear signal, and (b) SIGTERMs the manager's
own PID so the worker transitions to `dead` in `ls`.

This is deliberate loud-failure design: a half-working manager (log pump
dead but process still alive) is worse than a dead one because operators
don't know to investigate. See the `feedback_coding_principles.md` state
awareness principle.

## Stream-json protocol

Claude Code's `-p` flag with `--input-format stream-json --output-format
stream-json` enables bidirectional JSONL communication:

**Input** (written to stdin):
```json
{"type":"user","message":{"role":"user","content":"your message here"}}
```

**Output** (read from stdout): standard Claude Code JSONL — system messages,
assistant messages, tool use/results, and result messages.

### Key lesson: no back-to-back user messages

The stream-json protocol does not support sending two user messages in
sequence. If you need to combine a system prompt and an instruction,
concatenate them into a single message. This is why `--prompt-file` and
`--prompt` are joined with `\n\n`.

### Key lesson: `result` does NOT mean session ended

In `-p` stream-json mode, each turn emits a `result` message, but the
process stays alive for the next user message. A `result` only means
"session truly over" if the process has exited. This is a critical
distinction:

| State                                       | Meaning                         |
|---------------------------------------------|---------------------------------|
| `result` + process alive                    | Turn complete, ready for input  |
| `result` + process dead                     | Session ended, no more turns    |
| `assistant` with `stop_reason=end_turn`     | Turn complete (interactive)     |
| `assistant` with `stop_reason=tool_use`     | Mid-turn, tool executing        |
| `assistant` with `stop_reason=None`         | Partial/streaming chunk         |

### Key lesson: user messages are not echoed by default

Claude's stream-json output does NOT include the user messages you send.
Without echoing, `wait-for-turn` and `--last-turn` cannot track turn
boundaries because they can't see where one turn ends and the next begins.

The fix: add `--replay-user-messages` to the claude invocation. This makes
claude echo user messages back in the output stream with `isReplay: true`,
so they appear in the log file naturally.

### Key lesson: init arrives after first user message

The `system/init` message (which contains the session ID) does not arrive
until after the first user message is sent. If you start a worker without
a prompt, there's no init until someone sends a message. The `start`
command handles this by only waiting for the session file when an initial
prompt is provided.

## FIFO plumbing

### The EOF problem

Named FIFOs (mkfifo) have a critical behavior: when the last writer closes,
the reader sees EOF. If `claude-worker send` writes a message and closes
the FIFO, claude's stdin would see EOF and the process would exit.

### The dummy-write-fd trick

The FIFO reader thread opens the FIFO in a specific order:

```python
# 1. Open read end with O_NONBLOCK (returns immediately, even without a writer)
rd_fd = os.open(in_fifo, os.O_RDONLY | os.O_NONBLOCK)
# 2. Open write end (won't block because a reader exists)
wr_fd = os.open(in_fifo, os.O_WRONLY)
```

The write fd is never written to — it just keeps the FIFO "alive" so
that external writers can come and go without triggering EOF. The read
fd uses `select()` with a 1-second timeout to poll for data without
busy-waiting.

### Partial reads

`os.read(rd_fd, 65536)` may return fewer bytes than a full message if
the writer hasn't flushed yet or the message is larger than the pipe
buffer. The stream-json protocol is newline-delimited, but the code
forwards raw bytes — it doesn't buffer until a newline. In practice,
`send` writes small JSON objects in a single `write()` + `flush()`, and
writes under `PIPE_BUF` (typically 4096 bytes) are atomic per POSIX. A
very large single message theoretically could split, but no real-world
usage has triggered this.

## Status detection

`get_worker_status(runtime)` walks the log file backwards via
`_iter_log_reverse` (O(1) amortized per call, not O(log_size)) to find
the most recent user/assistant/result entry, then combines that with
PID liveness:

1. No PID file → `dead`
2. PID not alive → `dead`
3. Log doesn't exist yet → `starting` (if alive) or `dead`
4. Most recent entry:
   - `result` → `waiting`
   - `assistant` with `stop_reason=end_turn` → `waiting`
   - `assistant` mid-stream (stop_reason=None) → keep walking back
   - `user` with no trailing turn-end → `working`
   - Nothing meaningful (only system/init, hooks) → `waiting` (idle)

The "only system/init → waiting" case is important: a worker started with
`--background` and no prompt is literally idle, not working.

### `wait-for-turn` behavior

`wait-for-turn` first walks the log backwards to check if the turn already
completed. If a turn-end boundary appears after the most recent user message
(or after the `--after-uuid` marker, if set), it returns immediately.
Otherwise, it seeks to EOF and tails the log waiting for a new turn
boundary.

The `--after-uuid` marker exists because `cmd_send` has a race: after
writing to the FIFO, the scan could find the PRIOR turn's `result`
before the new input reaches claude. Capturing the last UUID before
writing, then passing it as `after_uuid`, skips past the stale state.
The `wait-for-turn` CLI exposes this as `--after-uuid`, and
`send --background` prints a ready-made `wait-for-turn --after-uuid X`
hint with the pre-send marker so orchestrators don't race.

### Settle / debounce

`--settle SECONDS` (default 3) instructs `wait-for-turn` to wait the
settle duration after detecting a turn boundary and re-check that no
new messages appeared before returning. Prevents false positives when
the worker briefly idles between internal subagent dispatches. The
settle window counts against `--timeout`, so `--timeout 5 --settle 3`
never blows past the 5-second budget.

## Reverse log iteration

`_iter_log_reverse(path, chunk_size=8192)` reads JSONL files backwards
in chunks and yields parsed entries newest-to-oldest. Used by:

- `_get_last_uuid` — send-time marker, needs only the last line
- `_get_last_assistant_preview` — `ls` preview, needs the last
  assistant-with-text
- `get_worker_status` — needs the last user/assistant/result
- `_wait_for_turn` initial scan — needs turn state since marker
- `_read_static` fast path for `--last-turn` / `-n N` when there's
  no full-log state to track (no `--since`/`--until`, not a PM worker)

All five previously scanned the log forward from byte 0 on every call
— O(log_size) per call, painful for long-running PM workers. The
reverse iterator is lazy: stopping after the first yield only reads
enough chunks to deliver that yield.

## Chat routing (PM mode)

PM workers are launched with `--append-system-prompt-file
<runtime>/identity.md` pointing at the bundled `claude_worker/identities/pm.md`.
The identity tells the PM:

- Every incoming message may carry a `[chat:<uuid>]` tag
- The final assistant message of each turn MUST echo the tag back
- Per-consumer conversation state lives in
  `.claude-worker-pm/chats/<uuid>.md` in the PM's working directory
- A chronological audit log lives in `.claude-worker-pm/LOG.md`
- On startup, scan the conversation history for prior `[chat:*]` tags
  to recover consumer state

### Auto-detection

`_resolve_chat_id(worker_name, explicit_chat, all_chats)` implements a
4-tier priority:

1. `all_chats=True` → None (explicit opt-out of filtering/tagging)
2. `explicit_chat` set → if target is PM, use it; if non-PM, warn and
   return None (pass-through)
3. Env-based auto-detection → `CLAUDECODE == "1"` AND `CLAUDE_SESSION_UUID`
   set, PM workers only
4. Otherwise → None

`cmd_send` calls `_resolve_chat_id` and prepends `[chat:<id>] ` to the
message body if a chat ID is effective. `cmd_read` calls it and filters
the scanned messages to those containing the chat tag.

### SessionStart hook

`claude-worker install-hook` writes `~/.claude/hooks/session-uuid-env-injection.sh`
and adds a SessionStart hook entry to `~/.claude/settings.json` (or
`./.claude/settings.json` with `--project`). The hook reads the SessionStart
JSON payload from stdin, extracts `session_id` with a pure-bash regex
(no jq dependency), and appends `export CLAUDE_SESSION_UUID=<id>` to
`$CLAUDE_ENV_FILE` — which Claude Code sources before every Bash tool
call.

Result: inside a Claude Code session, every Bash tool invocation sees
`CLAUDE_SESSION_UUID` set to the session's UUID. `claude-worker send`
(also `read`) picks that up and auto-tags against PM workers.

The install is atomic (sibling `.tmp` + `os.replace()`) so a crash
mid-install cannot corrupt `~/.claude/settings.json` — which is the
user's sacred Claude Code config.

### Missing-tag monitoring

`cmd_read` against a PM worker walks the log tracking `(user chat tag,
last assistant of turn)` pairs and reports a miss when the final
assistant message doesn't contain the user's chat tag. Misses are
recorded to `runtime/missing-tags.json`, deduped by assistant UUID,
and surfaced as stderr warnings only on the first observation.

The dedup log is capped at `MISSING_TAG_LOG_MAX_ENTRIES` (1000) with
FIFO eviction to prevent unbounded growth on long-running PM workers.
Writes are atomic.

## Queue correlation

`send --queue` is for multi-orchestrator scenarios where several senders
might be producing responses to the same worker concurrently. The queue
path:

1. Generate a correlation ID: `str(int(time.time() * 1000))` (epoch ms)
2. Append `[Please include [queue:<id>] literally in your response...]`
   to the message body
3. Capture `marker_uuid = _get_last_uuid(log_file)` before writing the FIFO
4. Write the message to the FIFO
5. `_wait_for_queue_response(name, queue_id, after_uuid=marker_uuid)`
   walks the log from the marker forward, then tails, looking for a
   line containing the tag string.

The `after_uuid` marker protects against sub-millisecond collisions
and stale matches the same way `_wait_for_turn`'s marker protects the
normal send path.

## Dependencies

- **`claugs`** (`claude_logs`): JSONL parsing, message type models,
  rendering/formatting. Used by `cmd_read` to parse and display log
  output. Installed from the local claude-stream repo (not on PyPI).
- **`claude` CLI**: the subprocess being wrapped. Must be on PATH.
  Tests override via `CLAUDE_WORKER_CLAUDE_BIN`.

## Environment variables

| Variable                       | Purpose                                          |
|--------------------------------|--------------------------------------------------|
| `CLAUDE_WORKER_CLAUDE_BIN`     | Override claude binary path (test injection)    |
| `CLAUDECODE`                   | Set by Claude Code; enables PM auto-routing when `=1` |
| `CLAUDE_SESSION_UUID`          | Set by the install-hook; used for PM auto-routing |
| `ANTHROPIC_API_KEY`            | **Explicitly unset** by the manager so claude uses subscription auth, not API billing |

## Default claude flags

Workers are launched with:

```
claude -p \
  --input-format stream-json \
  --output-format stream-json \
  --replay-user-messages \
  --dangerously-skip-permissions \
  [extra args from --agent / positional CLAUDE_ARGS]
```

`--dangerously-skip-permissions` is set because workers are non-interactive
subordinate processes. Can be overridden via extra claude args.
