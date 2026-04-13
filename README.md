# claude-worker

Launch and communicate with Claude Code subprocess workers via named FIFOs and
stream-json.

## Why

Claude Code's Task/Agent system has a max nesting depth of 2. If a Claude
launches a Task, that Task cannot launch its own sub-Tasks. By spawning Claude
as a subprocess via `claude -p --input-format stream-json --output-format
stream-json`, we break through this limitation and get arbitrary-depth nesting.
This wrapper standardizes the worker lifecycle so both humans and other Claude
instances can manage workers the same way.

Beyond the nesting escape hatch, `claude-worker` adds:

- **Multi-turn conversations** with background workers that survive between
  commands
- **PM mode** — a single worker coordinates multiple consumers (orchestrators,
  other Claude sessions) with chat-tag routing so responses don't cross-talk
- **Queue correlation** — `send --queue` embeds a per-call correlation ID and
  waits for the specific tagged response, so concurrent senders don't pick up
  each other's replies
- **A SessionStart hook** (`install-hook`) that injects `CLAUDE_SESSION_UUID`
  into every Claude Bash tool call, enabling automatic chat routing

## Install

```bash
pip install -e .
```

Requires Python 3.11+, the `claude` CLI on PATH, and `claugs` (the
`claude_logs` Python package) for log parsing.

## Quick start

```bash
# Start a worker with an initial prompt — blocks until claude responds,
# then prints status
claude-worker start --name researcher --prompt "You are a research assistant"

# Send a follow-up message — also blocks by default
claude-worker send researcher "summarize the architecture of this repo"

# Print the response that just arrived
claude-worker read researcher --last-turn

# Send + show response in one step (no separate read)
claude-worker send researcher "now focus on the database layer" --show-response

# List all workers
claude-worker list         # or: claude-worker ls

# Or chat interactively (turn-by-turn human REPL)
claude-worker repl researcher

# Stop a worker and clean up its runtime directory
claude-worker stop researcher
```

## Worker lifecycle and status

Each running worker has one of four statuses, visible in `list`:

| Status     | Meaning                                                        |
|------------|----------------------------------------------------------------|
| `starting` | Manager forked, claude subprocess launching, no log output yet |
| `working`  | Claude is actively processing a user message                   |
| `waiting`  | Turn complete, ready to accept the next input                  |
| `dead`     | PID not alive or manager process never started                 |

`send` consults status before writing:

- `starting`: waits up to 30s for it to clear
- `working`: rejects with a hint to use `--queue`
- `waiting`: proceeds normally
- `dead`: rejects with a hint to use `start --resume --name <name>`

## Commands

### `start`

```
claude-worker start [--name NAME] [--cwd DIR] [--prompt PROMPT]
                    [--prompt-file FILE] [--agent NAME] [--resume]
                    [--background] [--foreground]
                    [--show-response | --show-full-response]
                    [--identity NAME | --pm | --team-lead]
                    [--no-permission-hook] [-- CLAUDE_ARGS...]
```

Start a new worker. Forks a background manager process that handles the claude
subprocess lifecycle.

- `--name`, `-n` — worker name. Auto-generated as `worker-XXXX` if omitted.
- `--cwd` — working directory for claude.
- `--prompt` — string to send as the first user message.
- `--prompt-file` — file whose contents become the first user message.
  Combined with `--prompt` into a single message (the stream-json protocol
  doesn't allow back-to-back user messages).
- `--agent` — claude agent profile to use for the session.
- `--resume` — resume a previously-stopped worker by name. Requires `--name`
  to be set explicitly; without it the command errors rather than inventing
  a random name.
- `--background` — return immediately without waiting for the first response.
  Prints a hint with the pre-send marker UUID so a later `wait-for-turn
  --after-uuid X` can target this specific turn without racing against a
  stale `result` message.
- `--foreground` — run in the foreground without daemonizing. For
  `systemd Type=simple` or when you want the manager's lifecycle tied
  to the calling shell.
- `--show-response` — after the first turn completes, print the assistant's
  response (equivalent to `read --last-turn --exclude-user`).
- `--show-full-response` — after the first turn completes, print everything
  since the start (equivalent to `read --since <pre-start-marker>
  --exclude-user`).
- `--identity NAME` — launch with a custom identity from
  `~/.cwork/identities/<name>/identity.md`. See [Identity system](#identity-system).
- `--pm` — shorthand for `--identity pm`. Loads the bundled PM
  identity, enables chat-tag auto-routing, and tags `[PM]` in `ls` output.
  See [PM mode](#pm-mode-multi-consumer-workers).
- `--team-lead` — shorthand for `--identity technical-lead`. Loads the TL
  identity for code review and delegation. Tags `[TL]` in `ls` output.
- `--no-permission-hook` — disable the PreToolUse permission-grant hook
  (useful for tests or debugging).
- Extra args after `--` are passed through to `claude` (e.g.
  `claude-worker start --name fast -- --model haiku`).

### `send`

```
claude-worker send [--queue] [--broadcast] [--dry-run] [--verbose]
                   [--show-response | --show-full-response]
                   [--chat ID | --all-chats]
                   [--role pm|tl|worker] [--status STATUS]
                   [--alive] [--cwd PATH]
                   [NAME] [MESSAGE...]
```

Send a user message to a worker. Blocks until the worker responds. Message
can be positional args or piped via stdin:

```bash
echo "analyze this code" | claude-worker send myworker
```

- `--queue` — bypass the status gate; embed a `[queue:<epoch-ms>]` correlation
  tag in the message and wait for the specific tagged response. Use this when
  multiple senders might be producing responses concurrently, or when you
  need to send to a worker that's still processing a previous turn.
- `--broadcast` — send the message to all workers matching the filter flags.
  Worker name is omitted when broadcasting.
- `--role`, `--status`, `--alive`, `--cwd` — filter targets for broadcast
  (same filters as `list`).
- `--dry-run` — print the JSON envelope that would be sent without writing
  to the FIFO. Zero side effects.
- `--verbose` — print the JSON envelope to stderr before sending.
- `--show-response` — after the turn completes, print only the assistant's
  response.
- `--show-full-response` — after the turn completes, print everything new
  since the send. Mutually exclusive with `--show-response`.
- `--chat ID` — prepend a `[chat:<id>]` tag to the message. PM workers only;
  non-PM targets get a stderr warning and the message passes through unchanged.
- `--all-chats` — bypass automatic chat tagging (no-op for non-PM workers).

**Chat routing**: when running inside Claude Code against a PM worker,
`send` auto-prepends `[chat:$CLAUDE_SESSION_UUID]` if the hook is installed
and `CLAUDECODE=1`. See [PM mode](#pm-mode-multi-consumer-workers).

### `read`

```
claude-worker read [--follow] [--since ID_OR_TIMESTAMP] [--until UUID]
                   [--new] [--mark] [--last-turn] [--exclude-user] [-n N]
                   [--count | --summary] [--verbose]
                   [--color | --no-color] [--chat ID | --all-chats]
                   NAME
```

Read worker output, parsed and formatted via `claude_logs`. User-input
messages are **shown by default** — pass `--exclude-user` to hide them.

- `--follow`, `-f` — tail the log in real time.
- `--since UUID_OR_TIMESTAMP` — show messages after this UUID (case-insensitive
  prefix match, e.g. `abc12345`) or ISO timestamp. When nothing matches, prints
  a warning with the target and total scanned count. When the UUID matches but
  no messages follow, prints `No new messages since [abc12345]: <content>`
  with the marker's content so the user recognizes the reference.
- `--until UUID` — stop at the given UUID (exclusive). Combine with `--since`
  for a precise window: `read --since abc --until def`.
- `--new` — show only messages after the last `--mark`. Per-consumer via
  `CLAUDE_SESSION_UUID` or `--chat`. Mutually exclusive with `--since`.
- `--mark` — after displaying, save the last-seen UUID as a read marker.
  Use with `--new` to track "unread" messages per consumer.
- `--last-turn` — show the most recent conversational exchange. Walks backwards
  from the end of the log until at least one user-input AND one assistant
  message have been seen, then shows everything from the earlier of the two
  to the end. Degrades gracefully if only one type is present.
- `--exclude-user` — hide user-input messages from the display. The
  `--last-turn` window is still computed using user messages; they're hidden
  from output only. `--show-response` / `--show-full-response` force this
  flag since the orchestrator just sent the message and doesn't need it
  echoed.
- `-n N` — show only the last N displayable messages.
- `--count` — print the message count instead of content.
- `--summary` — print a one-line-per-message summary: `[uuid-short] ROLE:
  first ~80 chars`.
- `--context` — print the current context window usage as a one-liner
  (e.g. `77% (776k/1M)`) and exit. Bypasses all other read flags.
  Scriptable signal for "how full is this worker?" — see also
  `claude-worker tokens NAME` for the full stats view.
- `--verbose`, `-v` — include tool calls, tool results, and thinking blocks.
- `--color` / `--no-color` — force ANSI or plain output. Defaults to markdown
  when running inside Claude Code (`CLAUDECODE=1`), ANSI in human terminals.
- `--chat ID` — filter to messages containing the given chat tag. Auto-detected
  from `$CLAUDE_SESSION_UUID` for PM workers.
- `--all-chats` — show all chats regardless of env auto-detection.

Each output line is prefixed with `[HH:MM:SS uuid-short]`. At the bottom,
a hint suggests the follow-up command, preserving any `--exclude-user` the
caller used so re-running produces the same view.

### `wait-for-turn`

```
claude-worker wait-for-turn [--timeout SECONDS] [--after-uuid UUID]
                            [--settle SECONDS] NAME
```

Block until claude finishes its current turn.

- `--timeout SECONDS` — total time budget before returning 2 (timeout).
- `--after-uuid UUID` — ignore log entries up to and including this UUID.
  Pass the last log UUID captured before sending, so wait-for-turn doesn't
  match the prior turn's `result` message before the new input reaches
  claude.
- `--settle SECONDS` — after detecting a turn boundary, wait this long and
  confirm no new messages appeared before returning. Default 3s. Prevents
  false positives when the worker briefly idles between internal subagent
  dispatches. Set to 0 to disable. The settle window counts against
  `--timeout`.

Exit codes:

- `0` — turn complete, worker is ready for more input
- `1` — worker process died
- `2` — timeout

### `list` / `ls`

```
claude-worker list [--role pm|tl|worker] [--status working|waiting|dead|starting]
                   [--alive] [--cwd PATH] [--format text|json]
```

List all workers with optional filters. Filters are composable (AND logic).

- `--role` — filter by identity type (pm, tl, or plain worker).
- `--status` — filter by current status.
- `--alive` — shorthand for excluding dead workers.
- `--cwd PATH` — filter by working directory (prefix match).
- `--format json` — one JSON object per line for scriptable output.

Output format per worker:

```
  my-worker [PM]
    pid: 1234  status: waiting  idle: 12s  cwd: ~/projects/foo
    session: abc123...
    last: first ~80 chars of the most recent assistant message...
    context: 77% (776k/1M)
```

- `[PM]` / `[TL]` appears next to identity workers.
- `idle: <duration>` appears for workers in `waiting` or `dead` state.
- `last:` shows a preview of the most recent assistant text for quick
  "what's the worker doing?" glance.
- `context:` shows the current context window usage as a percentage
  and absolute count (e.g. `77% (776k/1M)`). Silent for workers that
  haven't produced a first turn yet. Backed by ``claugs``.

### `stop`

```
claude-worker stop [--force] [--no-wrap-up] [--wrap-up-timeout SECONDS] NAME
```

Stop a worker. Default behavior is two-phase: send a wrap-up message,
wait for the turn to complete (up to 15 minutes), then SIGTERM.

- `--force` — send SIGKILL instead of SIGTERM, skip wrap-up.
- `--no-wrap-up` — skip the wrap-up message, go straight to SIGTERM.
- `--wrap-up-timeout SECONDS` — maximum time to wait for wrap-up.
  Must be >= the configured minimum (default 900s). Values below the
  minimum are clamped with a stderr warning.

The manager archives the runtime directory before cleanup, preserving
logs and metadata for later review. Archives older than 30 days are
pruned automatically on the next `ls` invocation.

### `tokens`

```
claude-worker tokens NAME
```

Print token usage for a worker — both the current context window
footprint and cumulative session totals. Backed by ``claugs`` (the
``claude_logs`` package) token-stats API.

Example:

```
$ claude-worker tokens cw-dev
Worker: cw-dev
Session: 86c9ce5a-8223-4164-a794-48a3b89a4901

Context window:        80% (797k/1M)
  input:                          1
  cache_creation:             1,017
  cache_read:               796,789
  output:                        47
  source_line:                2,361

Session totals (deduped by message.id):
  input_tokens:               5,967
  output_tokens:             25,130
  cache_creation:         4,425,023
  cache_read:           377,360,475
  total_tokens:         381,816,595
  unique_api_calls:             850
  messages_considered:        1,345
```

Two views in one command:

- **Context window**: the current in-flight input footprint from the
  most recent assistant turn's usage block. Matches the "X/1M tokens"
  percentage Claude Code's UI shows. Computed as `input +
  cache_creation_input + cache_read_input` (output is reported for
  reference but not summed into the total). Excludes sub-agent (Task)
  calls, which have their own private context.
- **Session totals**: cumulative tokens across every API call in the
  session, deduped by `message.id` so streaming chunks don't
  double-count.

Context window size is auto-detected from the model string in the
worker's `system/init` message: models with `[1m]` suffix are 1M,
others default to 200K.

See also: `claude-worker read NAME --context` for a scriptable
one-line version, `claude-worker ls` for a per-worker context line.

### `repl`

```
claude-worker repl [--chat ID] [--verbose] [--continuous] NAME
```

Interactive turn-by-turn chat with a running worker. Built for humans
sitting at a terminal — not for orchestrators (use `send` for those).

- `--verbose`, `-v` — show tool calls, thinking, and metadata.
- `--continuous`, `-c` — continuous output mode: messages flow like
  `tail -f`, press Enter to type. No prompt shown by default.

The loop:

1. On entry, prints the worker's last conversational turn (if any) so
   you have context for what just happened.
2. Waits for the worker to be idle (`status == waiting`, using the same
   passive `STATUS_IDLE_THRESHOLD_SECONDS` check that `ls` uses).
3. Flushes any keystrokes you typed during the working phase, then
   shows a `you> ` prompt.
4. Sends your message and live-streams the worker's response as it
   arrives in the log file.
5. Loops back to step 2.

Exit:
- `Ctrl-D` on an empty prompt
- `Ctrl-C` twice in a row
- Type `/exit` or `/quit`

The worker stays alive after you exit the REPL — you can re-attach
later or use `send`/`read` against the same worker.

**PM workers**: the REPL auto-derives a stable chat ID from
`repl-<pid>-<tty>` so multi-consumer routing works without you having
to set `CLAUDE_SESSION_UUID` manually. Override with `--chat ID` if you
want a specific chat identity (e.g., to resume an existing PM
conversation across REPL sessions).

```bash
# Attach to an existing worker
claude-worker repl researcher

# PM worker with a specific chat identity
claude-worker repl pm-myproject --chat dev-debugging-session
```

### `install-hook`

```
claude-worker install-hook [--user | --project] [--yes] [--force]
```

Install a SessionStart hook that sets `CLAUDE_SESSION_UUID` in the environment
of every subsequent Claude Code Bash tool call. Required for PM mode's
auto-routing.

- `--user` — install into `~/.claude/settings.json` (default).
- `--project` — install into `./.claude/settings.json`.
- `--yes`, `-y` — skip the confirmation prompt.
- `--force` — add a duplicate entry even if the hook is already installed
  (idempotency-busting; usually unnecessary).

The hook script is written to `~/.claude/hooks/session-uuid-env-injection.sh`
(not tied to claude-worker — it's useful independently). Settings.json is
written atomically via a sibling `.tmp` file, so a crash mid-install cannot
corrupt the user's Claude Code config.

After installation, verify with:

```bash
claude -p 'env | grep CLAUDE_SESSION_UUID'
```

### `replaceme`

```
claude-worker replaceme [--skip-validation]
```

Replace the current worker with a fresh instance. Auto-detects which
worker is calling by walking the process ancestry and matching against
`claude-pid` files. Used by PM and TL workers to self-replace when
approaching context limits.

The command validates wrap-up completion (turn is idle, handoff file
exists for PM/TL workers), then forks a detached replacer process that:

1. Sends SIGUSR1 to the old manager (archives runtime dir instead of
   deleting it)
2. Waits for the old manager to exit
3. Creates a new runtime dir with the same name
4. Starts a fresh manager with `--resume` pointing at the old session

- `--skip-validation` — skip wrap-up checks (for stuck workers or
  human-supervised replacements).

### `notify`

```
claude-worker notify [--worker NAME] MESSAGE
```

Send a notification to the human via the configured channel. Reads
`~/.cwork/config.yaml`:

```yaml
notifications:
  enabled: true
  command: "curl -s -d '${MESSAGE}' https://ntfy.sh/my-topic"
```

The `command` is a shell template — `${MESSAGE}` is replaced with the
notification text. Any shell command that accepts a message works
(ntfy.sh, Slack webhooks, desktop notifications, etc.).

Rate-limited to one notification per 60 seconds per worker. Best-effort:
failures are logged to stderr, never crash the worker.

### `reply`

```
claude-worker reply [--sender NAME] NAME [MESSAGE...]
```

Send a reply to a worker's persistent message queue (no FIFO needed —
the manager drains the queue periodically). Message reads stdin if
omitted.

- `--sender` — sender identity. Auto-detected from PID ancestry if
  running inside a worker.

### `projects`

```
claude-worker projects
```

List registered projects with active workers and ticket counts. Projects
are registered automatically when an identity worker (`--identity`,
`--pm`, `--team-lead`) is started in a directory. Registry lives at
`~/.cwork/projects/registry.yaml`.

### `stats`

```
claude-worker stats
```

Print summary statistics from session analyses (cost, tokens, per
identity/project). Reads the `~/.cwork/analyses/summary.csv` populated
by the analyze-session skill during wrap-up.

### `grant`

```
claude-worker grant [--path PATH | --glob PATTERN | --tool-use-id ID | --last]
                    [--tool TOOL] [--persistent] [--reason TEXT] NAME
```

Pre-authorize a sensitive-file Edit/Write/MultiEdit call for a worker.
Claude Code's sensitive-file gate blocks edits to certain paths (e.g.
`.claude/**`) even with `--dangerously-skip-permissions`. This command
lets you authorize specific edits from outside the worker.

- `--path` — grant for an exact file path.
- `--glob` — grant for any file matching an fnmatch pattern.
- `--tool-use-id` — grant for an exact tool_use_id from the log.
- `--last` — grant the most recent sensitive-file denial in the worker's log.
- `--tool` — restrict to specific tools (Edit, Write, MultiEdit). Repeatable.
- `--persistent` — keep the grant active after first use (default: one-shot).
- `--reason` — optional audit note.

### `grants`

```
claude-worker grants NAME
```

List active permission grants for a worker.

### `revoke`

```
claude-worker revoke [--all] NAME [GRANT_ID]
```

Revoke a permission grant by ID, or `--all` to remove every grant.

## Identity system

Identity workers are started with `--identity NAME` (or the shorthands
`--pm` and `--team-lead`). The identity system provides:

- **Behavioral contract** — an `identity.md` file loaded via
  `--append-system-prompt-file` that defines the worker's role.
- **Wrap-up procedure** — a `wrap-up.md` file referenced during
  two-phase shutdown.
- **Skeleton scaffolding** — a directory tree copied into the project
  on first start (e.g., `.cwork/roles/pm/` for PM workers).
- **Custom hooks** — `hooks/hooks.json` merged into the per-worker
  settings for identity-specific hook wiring.
- **GVP elements** — `gvp/` directory with identity-specific goals,
  values, and principles.

Identity directories live at `~/.cwork/identities/<name>/`. Built-in
identities (`pm`, `technical-lead`) use bundled files from
`claude_worker/identities/` as defaults, with user-level overrides
taking precedence.

## PM mode (multi-consumer workers)

A PM (**Project Manager**) worker is a single claude instance that coordinates
multiple consumers — other orchestrators, other Claude sessions, humans —
routing each consumer's messages independently and maintaining per-consumer
conversation state.

### Setup

```bash
# 1. One-time: install the hook so CLAUDE_SESSION_UUID gets set
claude-worker install-hook --user --yes

# 2. Start a PM worker in the project directory
claude-worker start --pm --name pm-myproject --cwd /path/to/project
```

The PM is launched with the bundled PM identity loaded via
`--append-system-prompt-file`. On startup, it scans its own conversation
history for any prior `[chat:*]` tags, reads `MEMORY.md` and `PROJECT.md`
for project context, and creates a `.cwork/roles/pm/` state directory.

### Sending as a consumer

From inside any Claude Code session (so `CLAUDECODE=1` and
`CLAUDE_SESSION_UUID` are set by the hook):

```bash
claude-worker send pm-myproject "plan the auth refactor"
```

`send` detects the PM target and auto-prepends `[chat:$CLAUDE_SESSION_UUID]`
to the message. The PM responds with the same tag so subsequent `read`
calls can filter to this consumer's conversation only.

### Reading your own chat

```bash
claude-worker read pm-myproject
```

On a PM worker with `CLAUDE_SESSION_UUID` set, `read` automatically filters
to messages containing the caller's chat tag — you see only your own
conversation, not other consumers'.

Override with:

- `--chat <other-uuid>` to inspect another consumer's conversation
- `--all-chats` to see everything

### What the PM does on your behalf

- Isolates context: answers about consumer A's work don't leak details from
  consumer B's work.
- Detects conflicts: if two consumers want to modify the same resource, the
  PM surfaces the conflict in both responses.
- Logs everything: `.cwork/roles/pm/LOG.md` has a chronological audit trail;
  `.cwork/roles/pm/chats/<uuid>.md` has per-consumer histories.

### Missing-tag monitoring

For PM workers, `read` also verifies that every assistant response to a
tagged user message includes the matching `[chat:<id>]` tag. Misses are
logged (deduped by UUID) to `runtime/missing-tags.json` and surfaced as
stderr warnings. Capped at 1000 entries to avoid unbounded growth.

## Runtime directory layout

Each worker has a runtime directory at `~/.cwork/workers/<name>/`:

```
~/.cwork/workers/my-worker/
├── in              # named FIFO, accepts stream-json user messages
├── log             # all claude stdout, newline-delimited JSONL
├── pid             # manager process PID
├── claude-pid      # claude subprocess PID (used by test harness)
├── session         # claude session ID (written after init)
├── settings.json   # per-worker hooks (permission, context, CWD guard, ticket watcher)
├── identity.md     # PM/TL identity (identity workers only)
├── grants.jsonl    # pre-authorized edits (permission-grant feature)
└── missing-tags.json  # PM tag monitoring dedup log (PM workers only)
```

Worker metadata (session ID, cwd, claude args, PM/TL flags) is persisted to
`~/.cwork/workers/.sessions.json`. Writes are atomic so a crash
mid-save cannot truncate the file and break `--resume`.

Workers started under the legacy path (`/tmp/claude-workers/<UID>/`) are
found via backwards-compatible fallback.

When a worker is stopped, its runtime directory is archived to a
timestamped path (e.g., `my-worker.20260409T010000.abc123/`) before
cleanup. Archives older than 30 days are pruned automatically.

## Examples

```bash
# Queue multiple messages through a busy worker
claude-worker send worker1 "task 1" --queue &
claude-worker send worker1 "task 2" --queue &
wait  # each send blocks until its tagged response arrives

# Read a precise range
claude-worker read researcher --since abc12345 --until def67890

# Quick counts and summaries
claude-worker read researcher --count
claude-worker read researcher --summary -n 10

# PM worker with a specific agent
claude-worker start --pm --name pm-backend --cwd ~/projects/backend \
  --agent backend-pm
```

## Architecture

See `docs/architecture.md` for the internal design: fork/manager model, FIFO
plumbing, stream-json protocol notes, and the PM/chat routing pipeline.
