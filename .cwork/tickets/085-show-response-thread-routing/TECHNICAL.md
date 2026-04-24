# TECHNICAL — #085 show-response-thread-routing

## Root cause: `active-thread` sidecar is a global singleton that races

The response tee (`_tee_assistant_to_thread` in `manager.py:168`)
reads `<runtime>/active-thread` to decide where to append the
worker's assistant response. The sidecar is written by
`check_thread_changes` (line 550-551) whenever ANY thread the
worker participates in receives a new message from another sender.

For a PM worker with multiple active threads (pair-human-pm,
pair-tl-pm, cwork-change notifications, ticket-watcher, etc.),
the active-thread sidecar flip-flops on every poll cycle:

```
T+0s  Human sends to PM via pair-human-pm
      → check_thread_changes sets active-thread = pair-human-pm
      → [system:new-message] injected to FIFO
T+0s  PM starts processing human's message
T+5s  TL sends a status update via pair-tl-pm
      → check_thread_changes sets active-thread = pair-tl-pm  ← OVERWRITTEN
T+8s  PM finishes processing, assistant outputs end_turn
      → _tee_assistant_to_thread reads active-thread = pair-tl-pm
      → Response tee'd to pair-tl-pm (WRONG — should be pair-human-pm)
```

Result: the response exists in the worker's SESSION LOG (ls shows
"last: <real reply>") but the THREAD the sender reads from
(pair-human-pm) has no assistant response. `--show-response` reads
from the sender's thread → finds nothing → silent empty output.

This isn't a rare edge case — for PM workers with multiple
consumers + ticket-watcher + cwork-monitor, the active-thread
flips on nearly every 5s poll cycle.

## Investigation summary

| Component | Observation |
|-----------|-------------|
| `_send_to_single_worker` | Writes to correct thread (pair-sender-worker) ✓ |
| `check_thread_changes` | Detects growth correctly ✓ |
| `_set_active_thread` | Called on EVERY new message from ANY thread ← BUG |
| `_tee_assistant_to_thread` | Reads global sidecar, tees to whatever thread was last set ← consequence of bug |
| `_show_worker_response` | Reads from sender's pair thread, finds nothing ← correct code, wrong thread has the data |
| `cmd_read` (post-D95) | Thread-based read, respects pair-thread routing ✓ |

## Fix options

### Option A: Parse the triggering thread from the notification content (RECOMMENDED)

In `_tee_assistant_to_thread`, instead of reading the `active-thread`
sidecar, walk the worker's LOG backward from the current position to
find the most recent `[system:new-message] Thread <id> from ...`
user message. Extract `<id>` and tee to that thread.

Advantages:
- Per-turn, not global — immune to concurrent thread activity
- No state file to race on
- Works correctly even if the 5s poll delivers multiple
  notifications in the same cycle

Disadvantages:
- Regex parsing of the notification content (fragile if the
  format changes, but the format is under our control)
- O(n) log walk per tee — bounded to the most recent few lines

Implementation: ~30 lines. New helper `_extract_thread_from_log(log_path)`
that reads the last few lines backward and returns the thread_id from
the most recent `[system:new-message]` line.

### Option B: Only update active-thread for "real" user messages

In `check_thread_changes`, skip the `_set_active_thread` call when
the new message content starts with `[system:` or when the sender is
a known system actor.

Advantages:
- Minimal code change (1 condition)

Disadvantages:
- Still races between multiple REAL user messages on different
  threads. If two humans send to the PM within the same 5s poll,
  the second one overwrites active-thread and the first response
  routes wrong.
- Thread messages don't start with `[system:` — those are FIFO-
  injected notifications. All thread messages are real user content.
  So this filter wouldn't actually help.

**REJECTED** — doesn't address the fundamental race.

### Option C: Store a per-turn thread binding in the sidecar

When `fifo_to_stdin_body` forwards a `[system:new-message]` to
claude's stdin, parse the thread_id from the notification and write
it to the sidecar. Since FIFO forwarding is sequential (one message
at a time), the sidecar reflects the LAST message forwarded to
claude, which is the one claude is currently processing.

Advantages:
- Accurate per-message binding
- No log parsing in the tee

Disadvantages:
- Still races: fifo_to_stdin_body can forward multiple
  notifications back-to-back before claude responds. The last
  one wins, which may not be the one claude responds to first
  (claude processes messages in order, so actually the LAST
  forwarded IS what claude sees on its next readline).
- Adds parsing to the FIFO hot path

**VIABLE but Option A is simpler and more robust.**

### Option D: Drop the global sidecar entirely; use the log

Combine A's log-parsing approach with removing `_set_active_thread`
/ `_get_active_thread` / the sidecar file. The tee always derives
the target thread from the log. `check_thread_changes` still
notifies the worker (that's correct) but no longer maintains a
shared-state sidecar.

**RECOMMENDED — this is Option A + cleanup.**

## Proposed fix (Option D)

### Changes to `manager.py`

1. **New helper**: `_resolve_tee_thread(log_path: Path) -> str | None`
   - Read last ~20 lines of the log (small bounded read)
   - Walk backward for a user message containing
     `[system:new-message] Thread <thread_id> from`
   - Extract and return `<thread_id>` via regex
   - Return None if no match (worker is processing a non-thread
     message, e.g., the initial prompt)

2. **Modify `_tee_assistant_to_thread`**:
   - Replace `_get_active_thread(runtime)` call with
     `_resolve_tee_thread(log_path)`, passing the log path
   - Requires `log_path` as a new parameter

3. **Modify `stdout_to_log_body`**:
   - Pass `log_path` to `_tee_assistant_to_thread` (it already
     has it as `log` variable in scope)

4. **Remove sidecar writes from `check_thread_changes`**:
   - Delete the `_set_active_thread(runtime, thread_id)` call
     at line 550-551
   - Remove the `runtime` parameter from `check_thread_changes`
     signature (it was only used for the sidecar)
   - Update all callers of `check_thread_changes`

5. **Keep `_set_active_thread` / `_get_active_thread`** for now
   (don't delete the functions — other code may reference them
   for backward compat), but stop calling them. Remove in a
   follow-up ticket.

### Changes to `cli.py`

- `_show_worker_response` is correct as-is (reads from the
  sender's pair thread). No change needed — once the tee routes
  correctly, the response will appear in the right thread.

### Tests

1. **Unit test `_resolve_tee_thread`**: synthetic log lines,
   verify correct thread_id extraction, verify None for logs
   without notifications.

2. **Regression test**: use `running_worker` fixture, send a
   message, verify the response appears in the SAME thread the
   message was sent on (not some other thread).

3. **Multi-thread race test**: send messages on two different
   threads in quick succession, verify each response lands on
   its correct thread.

## Risk assessment

1. **Regex fragility**: the notification format
   `[system:new-message] Thread <id> from <sender>:` is under
   our control (manager.py:533-536). Pin the regex to the exact
   format; add a constant for the prefix.

2. **Log read cost**: reading the last ~20 lines for each
   end_turn is O(1) bounded — the log's tail is already in the
   OS page cache since `stdout_to_log_body` just wrote to it.
   Negligible.

3. **Non-thread messages**: if the worker is processing a message
   that DIDN'T come from a thread notification (e.g., the initial
   prompt, a FIFO direct-write from cmd_stop), `_resolve_tee_thread`
   returns None and no tee happens. This is correct — those
   messages don't have a reply-to thread.

4. **Backward compat**: removing the `runtime` param from
   `check_thread_changes` is a signature change. Callers pass it
   as a kwarg today (line 1470 in manager.py). Clean migration.

## LOE

- manager.py: ~50 lines (new helper + modify tee + remove sidecar write)
- tests: ~120 lines
- Total: ~170 lines

## GVP alignment

- G2 (loud-over-silent-failure): eliminates silent response loss
- V2 (explicit-over-implicit): tee target derived from the
  triggering message, not global state

New decision D102.
