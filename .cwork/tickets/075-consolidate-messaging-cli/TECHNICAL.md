# TECHNICAL — #075 consolidate-messaging-cli

## PM-approved scope (Phase A only)

CLI-surface rename. Internal `cmd_*` functions keep their names
(cosmetic rename is explicitly out of scope).

### Removed top-level subcommands

- `send` → moves under `thread send`
- `read` → moves under `thread read`
- `wait-for-turn` → moves under `thread wait`
- `reply` → removed entirely (redundant with `thread send` + D93 pair routing)

### New top-level subcommand

- `broadcast <msg>` — extracted from `send --broadcast`. Filter flags
  (`--role`, `--status`, `--alive`, `--cwd`) move with it.

### Extended thread subcommands

- `thread send` gains full flag set: `--queue`, `--chat`/`--all-chats`,
  `--show-response`/`--show-full-response`, `--dry-run`, `--verbose`.
- `thread read` gains full flag set: `--follow`, `--since`, `--until`,
  `--new`, `--mark`, `--last-turn`, `--exclude-user`, `-n`, `--count`,
  `--summary`, `--context`, `--verbose`, `--color`/`--no-color`,
  `--chat`/`--all-chats`, `--thread`, `--log`.
- `thread wait <name-or-thread-id>` — dual semantics:
  - arg starts with `pair-` or `chat-` → wait for next message on
    that thread
  - else → wait for worker's turn boundary (current `wait-for-turn`)
  - flags `--timeout`, `--after-uuid`, `--settle`, `--chat` preserved.

## Implementation plan

1. Extract `cmd_broadcast` from `cmd_send`. `cmd_send` becomes single
   target only — the broadcast branch (~30 lines) moves verbatim.
2. Move `p_send`, `p_read`, `p_wait` argparse blocks to sit under
   `thread_sub` instead of `sub`. Dispatch via `set_defaults(func=...)`
   on each new subparser so `main()` calls `cmd_send`, `cmd_read`,
   `cmd_wait_for_turn` directly.
3. Delete `p_reply` and `cmd_reply`; drop its handler entry.
4. Add top-level `p_broadcast` parser with filter flags.
5. Update `handlers` dict: remove `send/read/wait-for-turn/reply`;
   add `broadcast`.
6. Update `cmd_thread` to route `send/read/wait` actions to the
   internal handlers (for the case where users dispatch via
   `thread_action` path rather than `set_defaults(func=...)`). In
   practice `set_defaults(func=...)` handles it; cmd_thread only
   handles the thread-native actions (`create`, `list`, `close`,
   `watch`).
7. `thread wait` arg detection: look at prefix of positional arg.
8. Update `ticket_watcher.py:191` — subprocess call changes from
   `["claude-worker", "send", target, "--queue", msg]` to
   `["claude-worker", "thread", "send", target, "--queue", msg]`.
9. Update all tests that invoke the CLI via subprocess or argparse
   strings. Tests that call `cmd_send(args)` / `cmd_read(args)`
   directly continue to work — the internal functions are unchanged.
10. Update README.md, CLAUDE.md, pm.md, technical-lead.md with new
    command forms. All 22 doc references rewritten.

## GVP alignment

- V1 (clarity-over-cleverness): one obvious way to message.
- P13 (clean-break-over-backwards-compat): no aliases, no
  deprecation warnings. Human directive.
- D89 (FIFO internal-only in user-facing text): this extends D89
  from FIFO to thread-vs-legacy-send.

New decision `D95` records the consolidation.

## LOE estimate

- CLI refactor in `cli.py`: ~250 lines diff (parser moves + extraction
  of `cmd_broadcast` + handler dict cleanup)
- `ticket_watcher.py`: 1-line change
- Tests: ~100 lines of updates (subprocess strings, argparse namespace
  attribute sets)
- Docs: ~100 lines rewrite across 4 files

Target total: 400-500 lines. If tests explode past that, stop and
regroup.

## Risks

- **Repl internal call sites**: `cmd_read` is called internally at
  2 sites (the REPL and the `--show-response` path). Those use
  namespace objects directly — no CLI parsing — so they're unaffected
  by the parser move.
- **`ticket_watcher.py` subprocess**: must be updated atomically with
  the CLI change, or ticket-watcher notifications break between the
  two commits.
- **Broadcast filter defaults**: when extracted, the broadcast parser
  must set the same argparse defaults currently relied on by
  `_collect_filtered_workers` (e.g., `alive=False`, `role=None`).
- **Flag-reparsing helper** (`_reparse_send_flags`) remains relevant
  for `thread send` because it handles the trailing-flag-absorbed-by-
  message-nargs pattern. Reuse as-is; the `_SEND_BOOL_FLAGS` /
  `_SEND_VALUE_FLAGS` dicts stay valid since the flag set didn't
  shrink (just moved parsers).
