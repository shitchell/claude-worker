# TECHNICAL — #076 interactive-session-messaging

## Scope (PM-approved, tight)

Two narrow changes to let interactive (non-worker) sessions participate
in the messaging system now that threads are centralized (D93):

1. **Soften `_send_to_single_worker` target validation** — if the target
   has no runtime dir (not a worker), fall back to thread-only delivery
   when the target is a known participant in an existing thread.
2. **Add `claude-worker thread watch <thread-id>`** — blocking tail of
   a thread JSONL, printing new messages as they arrive. Lets
   interactive sessions observe replies without polling.

Out of scope for this ticket: ephemeral registration, notification
hooks, REPL integration. Those are separate tickets if needed.

## Approach

### 1. `_send_to_single_worker` — thread-only fallback

Current flow (`cli.py:1509-1651`):

```
if not runtime.exists():
    print("worker not found"); return 1
...status gate, FIFO-less thread write, _wait_for_turn...
```

New flow:

```
runtime_exists = runtime.exists()
if not runtime_exists:
    if target in known thread participants:
        # thread-only send: skip status gate, skip wait_for_turn
    else:
        error + hint (suggest thread watch / check name)
...
write to thread (unchanged)
if not runtime_exists:
    return 0  # no FIFO to notify, no turn to wait for
else:
    ...existing wait logic
```

**"Known participant" check**: scan `thread_store.load_index()` for
any thread whose `participants` list contains the target name. O(n)
in thread count; acceptable at current scale. Prevents typos from
silently creating dead threads.

**Sender for the fallback path**: same `_resolve_sender()` logic
already used; for an interactive Claude Code session inside a
claude-worker-aware environment, the `CLAUDE_SESSION_UUID` /
`CW_WORKER_NAME` ancestry walk already returns "human" or the
session ID. No new identity resolution needed.

### 2. `thread watch` subcommand

```
claude-worker thread watch <thread-id> [--since MSG_ID] [--timeout SECONDS]
```

Implementation (~40 lines):

- Open the thread JSONL; seek to end (or resolve `--since` position).
- Loop: `select.select([], [], [], poll_interval)` with file `mtime`
  check + re-read from last offset. Print new messages as they arrive
  via `_format_thread_message`-equivalent.
- Ctrl-C exits with 0; timeout exits with 2.
- Use the same JSONL-line parser pattern as `read_messages`.

Constants added to `cli.py` top:

```python
THREAD_WATCH_POLL_INTERVAL_SECONDS: float = 0.5
```

No new module needed — logic fits in `cmd_thread` under the existing
`elif action == "watch":` branch plus a small helper
`_watch_thread(thread_id, since_id, timeout)`.

### Tests (per G3)

Leverage the autouse `_isolate_global_threads` fixture (from D93).

New test file `tests/test_interactive_messaging.py`:

1. `test_send_to_non_worker_thread_participant_succeeds` — create
   thread with `["worker-a", "rhc"]`, invoke `_send_to_single_worker`
   with target `"rhc"`, assert message appended, no worker dir
   required, `rc == 0`.
2. `test_send_to_unknown_target_still_errors` — target neither worker
   nor thread participant; assert rc != 0 and error contains "not found".
3. `test_thread_watch_tails_new_messages` — spawn `thread watch` in a
   subprocess, append a message via `append_message`, read subprocess
   stdout within timeout, assert message present.
4. `test_thread_watch_exits_on_timeout` — watch with 1s timeout, no
   new messages, assert rc == 2 after ~1s.
5. `test_thread_watch_since_id` — prefill thread with 3 messages,
   watch with `--since <msg-2-id>`, assert only messages after msg-2
   appear.

All tests use the fixture; no real `~/.cwork/threads/` writes.

## Risk assessment

- **Thread-index scan cost**: O(n) participants per send for
  non-worker targets. At current scale (< 100 threads) this is
  sub-ms. If it becomes hot, cache the participant → threads map.
- **Duplicate send hiding typos**: a typo like `"hman"` instead of
  `"human"` would still error (not in any participant list).
  Covered by test 2.
- **Race in `thread watch`**: between `mtime` check and file read,
  a new message could arrive. Re-read from last offset handles this
  — mtime is only a wake signal, offset tracking is authoritative.
- **Subprocess test flakiness**: `thread watch` test uses subprocess
  + timeout; follows the same pattern as other subprocess tests in
  the suite.
- **Does not touch FIFO or manager**: no stream-json risk, no
  compaction risk, no `_wait_for_turn` changes.

## LOE estimate

- `_send_to_single_worker` soften: ~20 lines
- `thread watch` subcommand: ~45 lines (parser + helper + handler branch)
- Tests: ~120 lines
- **Total diff: ~185 lines** — under the 300-line ceiling the PM
  flagged.

## GVP alignment

- G2 (loud-over-silent-failure): eliminates the silent
  "worker not found" dead-end for legit thread replies.
- V2 (explicit-over-implicit): interactive participants are now
  first-class thread targets, not a special case.
- D93 (centralized thread storage) is the prerequisite; this
  ticket consumes that work.

New decision `D94` will record the softened-validation rule plus
the `thread watch` primitive. Refs: `claude_worker/cli.py` (both
edits) and `tests/test_interactive_messaging.py`.

## Test plan

1. Unit tests above (5 cases).
2. `pytest tests/ --timeout=30` full suite must stay at 461+ pass.
3. Manual smoke: in two terminals —
   - Terminal A (interactive): `claude-worker thread watch pair-human-<w>`
   - Terminal B: `claude-worker send <w> "hello"`, worker replies
   - Assert reply appears in Terminal A within ~1s of worker write.

## What gets delegated, what stays inline

Per PM's lesson from #074: implement inline (no Task tool) because
scope is small. TL will edit `claude_worker/cli.py`,
`tests/test_interactive_messaging.py` directly. Tests run locally
with `--timeout=30`.
