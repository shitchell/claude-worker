# TECHNICAL — #084 ephemeral-completion-notify

## Hook point in the manager

`_run_manager_forkless` has one exit path (line ~1570):

```python
proc.wait()
log_thread.join(timeout=LOG_THREAD_JOIN_TIMEOUT_SECONDS)
cleanup_runtime_dir(name, reason="exit")
```

The notification goes BETWEEN `log_thread.join` (log is complete)
and `cleanup_runtime_dir` (log still exists on disk):

```python
proc.wait()
log_thread.join(timeout=LOG_THREAD_JOIN_TIMEOUT_SECONDS)
_notify_parent_on_exit(name, log_path, ephemeral_reaped)  # NEW
cleanup_runtime_dir(name, reason="exit")
```

Both clean-exit and idle-reap reach this same code path:
- **Clean exit**: claude process exits naturally → `proc.poll()`
  sees it, loop breaks, falls through to proc.wait/cleanup.
- **Idle reap**: `_reap_ephemeral_worker` sends wrap-up + SIGTERM →
  proc dies → loop breaks → same path.

A boolean flag `ephemeral_reaped` is set True inside the reap
block (line ~1533) and False otherwise. This distinguishes the
two cases for the notification message.

## `[worker-status]` message format

```
[worker-status] <name> completed (<reason>).
Last message: "<preview>"
```

Where:
- `<reason>` = "reaped after <N>m idle" | "clean exit"
- `<preview>` = last ~160 chars of the most recent assistant text
  block from the log (truncated with "...")

## Parent thread resolution

1. Read `CW_PARENT_WORKER` from `os.environ` (set by the manager
   at worker startup per D92, line ~1264).
2. If empty/unset → no notification (worker was not spawned by
   another worker; e.g., human-started PM).
3. Compute `pair_thread_id(name, parent_name)` — the pair thread
   between child and parent.
4. `ensure_thread()` (creates if missing, no-op if exists).
5. `append_message(thread_id, sender=name, content=message)`.

The thread monitor on the parent's manager will pick up the new
message within 5s and inject a `[system:new-message]` notification
— exactly the same path all thread sends use.

## Edge cases

### Parent is dead
`ensure_thread` + `append_message` always succeed (they write to
the global thread store, not the parent's FIFO). The message
persists in the thread. If the parent restarts (via --resume or
replaceme), it'll see the notification on its next thread-monitor
poll. No special handling needed.

### `CW_PARENT_WORKER` is unset
Skip the notification entirely. Non-ephemeral workers started by
humans don't have parents. Gated on `parent_name` being non-empty.

### Log is empty (worker never produced output)
`_last_assistant_preview_from_log` returns "" → preview line is
omitted from the message. The notification still fires with the
exit reason.

## Implementation

### New helper in `manager.py`

```python
def _notify_parent_on_exit(
    name: str,
    log_path: Path,
    reaped: bool,
    idle_seconds: float | None = None,
) -> None:
```

~35 lines. Reads CW_PARENT_WORKER, builds the message, writes to
the pair thread. Uses a local log-tail for the preview (not
`_get_last_assistant_preview` from cli.py — that depends on
`_iter_log_reverse` which is also in cli.py; instead, read the
last ~4KB of the log, parse backward for the first assistant
message with text content).

### Flag in the main loop

```python
ephemeral_reaped = False  # before the while loop
# ... inside the reap block:
ephemeral_reaped = True
```

### Wiring

```python
proc.wait()
log_thread.join(...)
_notify_parent_on_exit(name, log_path, ephemeral_reaped)
cleanup_runtime_dir(name, reason="exit")
```

## Test plan

1. `test_notify_parent_on_clean_exit` — set CW_PARENT_WORKER=pm,
   write a synthetic log with an assistant message, call
   `_notify_parent_on_exit(name, log, reaped=False)`. Assert: pair
   thread pair-<child>-pm has a message containing "[worker-status]"
   + "clean exit" + the assistant preview text.

2. `test_notify_parent_on_reap` — same but `reaped=True,
   idle_seconds=300`. Assert: message contains "reaped after 5m
   idle".

3. `test_no_notification_when_parent_unset` — CW_PARENT_WORKER
   unset. Assert: no thread created, no messages.

4. `test_notification_with_empty_log` — log exists but no assistant
   messages. Assert: notification fires but preview is omitted.

5. `test_lifecycle_ephemeral_reap_sends_notification` — end-to-end
   using `running_worker` fixture with ephemeral sentinel + short
   idle timeout (same pattern as test_ephemeral_worker.py). After
   manager exits, read the pair thread and verify the
   [worker-status] message landed.

## LOE

- manager.py: ~50 lines (helper + flag + wiring)
- tests: ~120 lines
- Total: ~170 lines

## GVP alignment

- G2 (loud-over-silent-failure): parent learns about child
  completion without polling
- P12 (consistent-lightweight-notifications-with-read-on-demand):
  the [worker-status] message is a short notification; parent reads
  full details on demand via the pair thread

New decision D104.
