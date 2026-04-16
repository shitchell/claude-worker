# 074: TECHNICAL — Centralize thread storage

## Approach

Move thread storage from per-project `<cwd>/.cwork/threads/` to global
`~/.cwork/threads/`. Clean-jump migration per P11 — no dual-state.

### Core change: `_threads_dir()` becomes global

Replace `_threads_dir(cwd)` → `_threads_dir()` returning
`~/.cwork/threads/`. The `cwd` parameter is removed from ALL public
functions in `thread_store.py` (8 functions). `_index_path()` follows
the same change.

### Functions to change in thread_store.py

| Function | Current sig | New sig |
|----------|-------------|---------|
| `_threads_dir(cwd)` | → Path(cwd)/.cwork/threads | → Path.home()/.cwork/threads |
| `_index_path(cwd)` | → _threads_dir(cwd)/index.json | → _threads_dir()/index.json |
| `load_index(cwd)` | reads cwd index | `load_index()` |
| `_save_index(cwd, index)` | writes cwd index | `_save_index(index)` |
| `create_thread(cwd, ...)` | creates in cwd | `create_thread(...)` |
| `append_message(cwd, ...)` | appends in cwd | `append_message(...)` |
| `read_messages(cwd, ...)` | reads from cwd | `read_messages(...)` |
| `list_threads(cwd, ...)` | lists from cwd | `list_threads(...)` |
| `close_thread(cwd, ...)` | closes in cwd | `close_thread(...)` |
| `get_thread_participants(cwd, ...)` | reads cwd index | `get_thread_participants(...)` |
| `ensure_thread(cwd, ...)` | ensures in cwd | `ensure_thread(...)` |

### Callers to update

**cli.py** (5 call sites):
1. `_send_to_single_worker` (line 1526-1632): remove `target_cwd`
   resolution (lines 1598-1599), pass no cwd to ensure_thread /
   append_message. Also remove `cwd` from dry-run JSON output.
2. `_resolve_read_thread_id` (line 1797): no cwd changes needed
   (doesn't call thread_store with cwd).
3. `_read_thread_messages` (line 1843): remove `target_cwd`
   resolution (lines 1860-1869), pass no cwd to read_messages.
   The fallback-to-log logic stays but the CWD lookup is removed.
4. `cmd_reply` (line 3430): remove `target_cwd` resolution
   (lines 3458-3459), pass no cwd to ensure_thread / append_message.
5. `cmd_thread` (line 5079): remove `cwd = os.getcwd()` (line 5089),
   pass no cwd to all thread_store calls.

**manager.py** (5 call sites):
1. `_tee_assistant_to_thread` (line 84): remove `cwd` param,
   call append_message without cwd.
2. `snapshot_threads` (line 314): change from
   `Path(cwd)/.cwork/threads` to `Path.home()/.cwork/threads`.
   Remove `cwd` param.
3. `_read_new_messages_since_size` (line 337): same path change,
   remove `cwd` param.
4. `check_thread_changes` (line 367): remove `cwd` param, update
   internal calls.
5. Manager poll loop (line 1317, 1360): remove `resolved_cwd` arg
   from `snapshot_threads` and `check_thread_changes` calls.

**Also update** `_tee_assistant_to_thread` signature at its call
site (manager.py:1281) — remove `resolved_cwd` arg.

### cwork directory monitor

`snapshot_cwork_dir(cwd)` watches `<cwd>/.cwork/` which currently
includes `<cwd>/.cwork/threads/`. After centralization, thread changes
won't appear in the per-project .cwork/ monitor. This is fine — the
dedicated `check_thread_changes` / `snapshot_threads` handles thread
notifications already. The cwork monitor continues watching tickets,
roles, etc. which remain per-project.

### Migration

On startup, if `<cwd>/.cwork/threads/` exists and `~/.cwork/threads/`
does not (or is empty), move the files:
- Copy all `.jsonl` files and `index.json` from per-project to global
- Merge indexes if global already has entries (multi-project scenario)
- After successful migration, remove per-project `threads/` dir

Place migration in a helper function `_migrate_threads_to_global(cwd)`
called from `_run_manager_forkless` at startup, before the first
`snapshot_threads()` call. Best-effort: log a warning on failure,
don't crash the manager.

Also add a migration function callable from CLI:
`claude-worker migrate-threads [--cwd DIR]` for manual migration of
projects that haven't been started yet.

### Thread identity — no change needed

Thread IDs (`pair-<a>-<b>`, `chat-<id>`) are already globally unique
because worker names are globally unique (enforced by
`~/.cwork/workers/`). No project context needed in the key.

## Test plan

1. All existing thread tests adapted to use global path (via
   monkeypatch on `_threads_dir` or `Path.home`)
2. New migration test: per-project threads → global
3. New test: two workers in different CWDs communicate via global
   threads
4. Full suite passes (both bare + CW_WORKER_NAME=worker env per D92)

## Risks

- Tests that create threads at `tmp_path/.cwork/threads/` need to
  monkeypatch `_threads_dir()` to return `tmp_path/threads/` instead.
  The `fake_worker` and `running_worker` fixtures need updating.
- The `.cwork/threads/` directory monitor (`snapshot_cwork_dir`) will
  no longer see thread changes in the per-project scan. The test
  `test_cwork_monitor.py` may need adjustment if it asserts on
  thread files appearing in the .cwork/ diff.

## LOE

Medium — ~15 function signature changes, ~5 CWD resolution blocks
removed from cli.py, manager.py path updates, migration helper,
~15 test files need import/call adjustments. Implementation: ~2500
tokens. Tests: ~1500 tokens.

## GVP alignment

- V5: one-contiguous-block (threads no longer scattered per-project)
- P5: fix-class-of-bug-not-instance (CWD-scoping is the class)
- P11: clean-jump migration (no live state, bounded surface)
