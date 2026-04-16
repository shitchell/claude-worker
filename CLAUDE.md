# CLAUDE.md

Project-level instructions for Claude agents working on `claude-worker`.
Read this before touching code. Not a duplicate of the README — this is
the principles + gotchas + mental model that don't fit in user-facing docs.

## Coding principles

1. **DRY** — no duplicated logic. If you're writing similar code twice,
   it should have been extracted the first time.
2. **Clarity over cleverness** — obvious code, descriptive names,
   comments explain *why*, not *what*.
3. **Explicit over implicit** — visible dependencies in function
   signatures, no hidden state, no side-channel parameters on
   argparse Namespaces unless explicitly documented (we already have
   one of these: `args.chat_id` — don't add more).
4. **Named constants** — all timeouts, thresholds, buffer sizes,
   poll intervals, display limits are `UPPER_SNAKE_CASE` module-level
   constants. The existing ones are all at the top of `cli.py` and
   `manager.py`. Adding new ones? Put them there.
5. **One contiguous block** — a feature lives in one place; a change
   to a feature lives in one commit. If a feature touches 5+ files,
   the architecture is probably wrong.
6. **Strict typing** — `from __future__ import annotations` in every
   module, type hints on all function signatures, `X | None` style
   (not `Optional[X]`).
7. **State awareness** — know what state you're in, and don't silently
   wander into bad states. Half-working is worse than dead. See the
   manager thread panic handling for the canonical example.
8. **Proactive reusability** — extract helpers with one caller if a
   second caller is plausible; parameterize with defaults.

## Critical gotchas — read these before touching the related code

### FIFO / EOF

Named FIFOs have a killer behavior: when the last writer closes, the
reader sees EOF. Our fix is the **dummy-write-fd trick** in
`fifo_to_stdin_body`:

```python
rd_fd = os.open(in_fifo, os.O_RDONLY | os.O_NONBLOCK)
wr_fd = os.open(in_fifo, os.O_WRONLY)  # never written to, keeps FIFO alive
```

If you touch this code, preserve both opens. Closing `wr_fd` causes
every `claude-worker thread send` to kill claude's stdin on disconnect.

### No back-to-back user messages in stream-json

Claude's `-p` stream-json protocol does not allow sending two user
messages in sequence without an assistant response between them. If
you need to concatenate a system prompt and an instruction, join
them into a **single** user message string. This is why `--prompt-file`
and `--prompt` are joined with `\n\n` in `cmd_start`.

### `result` does not mean "session ended"

In `-p` stream-json mode, each turn emits a `result` message but the
claude process stays alive waiting for the next user message. A
`result` only means "session truly over" if the process has exited.

| State                                    | Meaning                    |
|------------------------------------------|----------------------------|
| `result` + process alive                 | Turn done, ready for input |
| `result` + process dead                  | Session over               |
| `assistant` with `stop_reason=end_turn`  | Turn done                  |
| `assistant` with `stop_reason=tool_use`  | Mid-turn, tool executing   |
| `assistant` with `stop_reason=None`      | Streaming chunk            |

Any code that reasons about "is the turn done?" must check both the
message type AND process liveness. See `get_worker_status` for the
canonical implementation.

### Status detection state machine

`get_worker_status` walks the log backwards via `_iter_log_reverse`
and combines the result with PID liveness. The state machine:

```
no pid file         → dead
pid not alive       → dead
no log yet + alive  → starting
most recent is:
  result + log mtime ≥ STATUS_IDLE_THRESHOLD → waiting
  result + log mtime < STATUS_IDLE_THRESHOLD → working (debounce)
  assistant stop_reason=end_turn (mtime check as above)
  assistant stop_reason=None           → keep walking back
  user (no trailing turn-end)          → working
  nothing meaningful (only system/init)→ waiting (idle worker)
```

The "nothing meaningful → waiting" case is important. A worker started
with `--background` and no prompt is literally idle, not working.
Previously (pre-Round 3) it fell through to `working` and stayed there
forever — a real bug worth remembering.

The `STATUS_IDLE_THRESHOLD_SECONDS` debounce is the *display* threshold
shared by `ls`, the REPL idle check, and the status lines printed after
`send`/`start`. It prevents false-idle readings when a worker has just
finished a turn but a subagent dispatch could be coming any moment. The
check is passive (log mtime), not active (no blocking sleep), so it
remains a point-in-time read suitable for the hot path.

`_wait_for_turn` (the active waiter used by the `wait-for-turn` CLI)
still uses the separate `--settle` window for active debounce. Don't
confuse the two: `STATUS_IDLE_THRESHOLD_SECONDS` is the display
threshold; `--settle` is the active wait threshold.

### User messages are filtered by type AND subtype

claugs' `should_show_message` checks type visibility first
(`is_visible("user")`), then subtype. Our `show_only` filter in
`cmd_read` must include BOTH the type `"user"` AND the subtype
`"user-input"`, because claugs runs the type gate first. If you only
put `"user-input"` in `show_only`, all user messages get filtered
before the subtype check runs. This bug hid user messages from every
`read` command in pre-Round 1 code.

### Atomic writes for persistent state

Three files contain persistent state that must survive crashes intact:

- `~/.claude/settings.json` (install-hook writes it; the user's
  sacred Claude Code config)
- `~/.cwork/workers/.sessions.json` (resume metadata)
- `~/.cwork/workers/<name>/missing-tags.json` (PM dedup log)

All three use `manager._atomic_write_text(path, content)`, which
writes to a sibling `.tmp` file and `os.replace()`s it into place.
POSIX rename is atomic, so either the new content is fully in place
or the old content remains. **Never** write these files with
`path.write_text()` directly.

### Race: `wait-for-turn` scan vs. FIFO write

After writing a message to the FIFO, there's a window where
`wait-for-turn`'s log scan can find the PRIOR turn's `result` message
before the new input reaches claude. Fix: capture the last UUID
*before* the FIFO write and pass it as `after_uuid`. `cmd_send` does
this internally; CLI users use `send --background` which prints the
marker hint, then `wait-for-turn --after-uuid X`.

`_wait_for_queue_response` has the same protection via the same
marker pattern.

### PM monitoring side-effect

`cmd_read` is usually a read-only operation, but for **PM workers**
it has a write side effect: scanning the log for missing chat tags
and updating `runtime/missing-tags.json`. This is deliberate — the
dedup log is where observation meets persistence — but be aware when
reasoning about read idempotence.

### Test harness quirks

Tests run `_run_manager_forkless(..., install_signals=False)` in a
thread, not a forked process. This has two implications:

1. SIGTERM to the test runner is NOT forwarded to the manager's
   claude subprocess. Tests read `runtime/claude-pid` and SIGTERM
   the stub-claude directly.
2. `install_signals=False` skips the `signal.signal()` calls, so
   the test runner's own signal handling is preserved.

Don't remove the `install_signals` parameter or change its default
without updating every end-to-end test.

### Token stats live in claugs, not here

`claude-worker tokens` / `ls` / `read --context` / the REPL banner all
call through to `claude_logs.compute_context_window_usage()` and
`compute_token_stats()` from the `claugs` PyPI package. The discovery
(which usage fields exist, how to dedupe streaming chunks) lives there.
Don't reimplement token accounting in claude-worker — if a field is
missing or wrong, fix it in claugs first and bump the version floor
in `pyproject.toml`. Current floor: `claugs>=0.6.8`.

The worker's own `~/.cwork/workers/<name>/log` contains the same
`usage` blocks Claude Code writes to its session log, so we pass the
runtime log directly to `compute_context_window_usage` without
deriving Claude Code project slugs or cross-referencing paths.

Context window size detection (`_detect_context_window_size`) reads
the `model` field from the `system/init` message: anything ending in
`[1m]` is 1M tokens, everything else defaults to 200K. Fallback when
the init is missing is 1M — optimistic default means under-reported
percentage (safer than over-reporting and making the user think they
have more headroom than they do).

## Directory map

```
claude_worker/
├── __init__.py              # version string
├── __main__.py              # `python -m claude_worker` entry
├── cli.py                   # all subcommand handlers + helpers
├── manager.py               # daemon process + fork wrapper
├── commit_checker.py        # PostToolUse hook: check commits for tests + GVP
├── compaction_detector.py   # SessionStart hook: detect + log compaction events
├── context_threshold.py     # Stop hook: context window check after each turn
├── cwd_guard.py             # PreToolUse hook: deny writes outside worker CWD
├── identity_reinjector.py   # SessionStart hook: re-inject identity on compact/resume
├── permission_grant.py      # PreToolUse hook: apply pre-authorized edits
├── project_registry.py      # ~/.cwork/projects/registry.yaml management
├── ticket_lifecycle.py      # ticket directory structural validation
├── ticket_watcher.py        # PostToolUse hook: notify PM/TL of ticket changes
├── token_tracking.py        # session analysis CSV + stats reader
├── hooks/
│   └── session-uuid-env-injection.sh  # pure-bash SessionStart hook
├── identities/
│   ├── pm.md                # PM worker behavioral contract
│   ├── pm-wrapup.md         # PM wrap-up procedure
│   ├── technical-lead.md    # TL worker behavioral contract
│   └── tl-wrapup.md         # TL wrap-up procedure
├── references/
│   └── ai-driven-development.md  # AI dev guide (bundled)
└── skills/
    └── analyze-session.md   # session analysis skill (bundled)

tests/
├── conftest.py              # fake_worker, running_worker, helpers
├── stub_claude.py           # stub claude binary (canonical + scripted modes)
├── stub_claude.sh           # wrapper accepting claude CLI flags
└── test_*.py                # ~310 tests covering all subcommands
```

## Before merging

- All tests green: `pytest tests/`
- `black` formatted (pre-commit hook enforces this)
- `from __future__ import annotations` at the top of new Python files
- New magic numbers extracted to named constants
- New persistent-state writes use `_atomic_write_text`
- New log-reading code uses `_iter_log_reverse` if it only needs the tail

## Push when done

Push to origin as the final step of any bugfix/feature round. Don't
leave commits sitting locally. See the feedback-workers-push-their-work
memory in the orchestrator's memory store for the full rationale.
