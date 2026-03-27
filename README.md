# claude-worker

Launch and communicate with Claude Code subprocess workers via named FIFOs and stream-json.

## Why

Claude Code's Task/Agent system has a max nesting depth of 2. If a Claude launches a Task, that Task cannot launch its own sub-Tasks. By spawning Claude as a subprocess via `claude -p --input-format stream-json --output-format stream-json`, we break through this limitation and get 3+ depth nesting. This wrapper standardizes the lifecycle so both humans and other Claude instances can manage workers the same way.

## Install

```bash
pip install -e .
```

Requires `claugs` (the `claude_logs` Python package) and the `claude` CLI.

## Quick Start

```bash
# Start a worker with an initial prompt
claude-worker start --name researcher --prompt "You are a research assistant"

# Send a message
claude-worker send researcher "summarize the architecture of this repo"

# Wait for claude to finish responding
claude-worker wait-for-turn researcher

# Read the response
claude-worker read researcher --last-turn

# Multi-turn conversation
claude-worker send researcher "now focus on the database layer"
claude-worker wait-for-turn researcher
claude-worker read researcher --last-turn

# List all workers
claude-worker list

# Stop a worker
claude-worker stop researcher
```

## Commands

### `start`

```
claude-worker start [--name NAME] [--cwd DIR] [--prompt-file PATH] [--prompt MSG] [-- CLAUDE_ARGS...]
```

Start a new claude worker process. Forks a background manager that handles the subprocess lifecycle.

- `--name` — worker name (auto-generated like `worker-a3f8` if omitted)
- `--cwd` — working directory for the claude process
- `--prompt-file` — file whose contents become the initial prompt
- `--prompt` — string to send as initial prompt
- If both `--prompt-file` and `--prompt` are given, they are concatenated into a single message (back-to-back user messages are not supported by the stream-json protocol)
- Extra args after `--` are passed through to `claude` (e.g. `-- --model sonnet`)

### `send`

```
claude-worker send NAME [MESSAGE...]
```

Send a user message to a worker. Message can be positional args or piped via stdin:

```bash
echo "analyze this code" | claude-worker send myworker
```

### `read`

```
claude-worker read NAME [--follow] [--since ID_OR_TIMESTAMP] [--last-turn]
```

Read worker output, parsed and formatted via `claude_logs`.

- `--follow` / `-f` — tail the log in real-time
- `--since` — show messages after a UUID or ISO timestamp
- `--last-turn` — show only the most recent turn's output

Every output line includes a timestamp and message UUID prefix.

### `wait-for-turn`

```
claude-worker wait-for-turn NAME [--timeout SECONDS]
```

Block until claude finishes its current turn. Prints the triggering message (JSON) to stdout.

Exit codes:
- `0` — turn complete, worker is ready for more input
- `1` — worker process died
- `2` — timeout

### `list`

```
claude-worker list
```

List all workers with name, PID, status, and session ID.

Statuses: `starting`, `working`, `waiting`, `dead`.

### `stop`

```
claude-worker stop NAME [--force]
```

Stop a worker. Sends SIGTERM by default, SIGKILL with `--force`. Cleans up the runtime directory.

## Usage from Another Claude

A Claude instance can spawn and manage workers:

```bash
# Start a specialized sub-agent
claude-worker start --name sub1 \
  --cwd /path/to/repo \
  --prompt "You are a code reviewer. Review all Python files for security issues."

# Wait for it to finish
claude-worker wait-for-turn sub1

# Read the result
claude-worker read sub1 --last-turn

# Continue the conversation
claude-worker send sub1 "Now check the JavaScript files too"
claude-worker wait-for-turn sub1
claude-worker read sub1 --last-turn

# Clean up
claude-worker stop sub1
```
