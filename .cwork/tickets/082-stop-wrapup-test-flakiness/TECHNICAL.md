# TECHNICAL — #082 stop-wrapup-test-flakiness

## Root cause: race in `_wait_for_turn` between reverse walk and tail loop

Reproduced with 40-50% rate via repeated runs. Added debug
instrumentation to the stub-claude to capture its stdin reads. On
failure the stub log shows:

```
<t0>    stub started
<t0>    read: {"type": "user", "content": "hello"}
<t0>    responding
<t0>    done responding
<t0+0.05> read: {"type": "user", "content": "[system:stop-requested] …"}
<t0+0.05> responding
<t0+0.05> done responding
```

Both messages arrive. Both get responses. The stub is fine. The
test still times out at 30s. So the stub's output is being written
to the worker log, but `_wait_for_turn` isn't seeing it.

### The race

`_wait_for_turn(name, after_uuid=marker_uuid)` runs two phases:

1. **Reverse walk** via `_iter_log_reverse`: find the most recent
   `result`/`assistant(end_turn)` newer than `marker_uuid`.
2. **Tail loop**: if no turn-end found, open log, `f.seek(0, 2)`,
   and poll for new lines.

The race is between phases:

| time | event |
|------|-------|
| t0   | `cmd_stop` writes wrap-up to FIFO → returns |
| t0+δ | `_wait_for_turn` opens log for reverse walk |
| t0+δ | Reverse walk: hits marker_uuid, no turn-end found → `turn_end_after_last_user = None` |
| t0+2δ | `stdout_to_log_body` writes assistant + result to log (stub already finished) |
| t0+3δ | `_wait_for_turn` enters tail loop: `open(log_file)` then `f.seek(0, 2)` |
| t0+3δ | But the assistant + result are ALREADY in the log — seek-to-end skips them |
| t0+3δ | Tail loop polls forever; no new data arrives |
| t0+30s | pytest-timeout fires |

When the reverse walk happens to run AFTER the log writer — which
is the usual case — the reverse walk finds the turn-end and returns
0. When the reverse walk happens to run BEFORE the writer — 40-50%
of runs — the handoff to the tail loop races with the writer and
loses.

### Why this is specific to `cmd_stop`

`cmd_send` has the same two-phase structure but uses a longer write
path (thread-store append → manager thread monitor → FIFO inject),
which gives the stub-claude more time to respond before the reverse
walk starts. `cmd_stop` writes directly to the FIFO and immediately
calls `_wait_for_turn`, making the race window much tighter.

## Fix (two complementary mitigations)

Empirically a forward-scan alone still left 2/10 runs failing —
the tail loop's `seek(0, 2) + readline()` pattern can still miss
data that lands between the scan and the first readline. So the
fix combines two pieces:

### 1. Grace window after FIFO write in `cmd_stop`

`FIFO_HANDOFF_GRACE_SECONDS = 0.2` — sleep between the FIFO write
and `_wait_for_turn` so the fifo-pump + stub + log-writer have
time to append the turn-end before the scan phase begins. This
alone took the repro rate to 10/10 in isolation.

### 2. Forward scan fallback in `_wait_for_turn`

After the reverse walk finds no turn-end, forward-scan the log
from `after_uuid` before falling through to the tail-poll loop.
Defense in depth: if the grace window isn't enough (heavily loaded
system, large log, etc.), the forward scan still catches any data
that landed during the scan phase.

The forward scan is O(log-size-after-marker), bounded to a single
turn of output for realistic callers.

Together: 10/10 on the gated flake check (10 consecutive runs
of the formerly-flaky live-worker test).

### Implementation

New helper `_forward_scan_for_turn_end(log_file, after_uuid, chat_tag)`
that walks the log forward from beginning, skips up to and including
`after_uuid`, then scans remaining entries for a turn-end. Returns
the turn-end dict (or None).

`_wait_for_turn` calls it after the reverse walk if
`turn_end_after_last_user is None`. Before entering the tail poll.

### Tests

Adding an explicit regression test is hard because the race is
timing-dependent. Instead:

1. **Unit test** `_forward_scan_for_turn_end` directly with a
   synthetic log: verify it finds a turn-end that appears after
   `after_uuid`, skipping earlier entries correctly.

2. **Repeated-run test** — a pytest that runs the live-worker test
   10x and asserts it passes every time. Flaky → fail. Stable →
   pass. Run only when `CW_FLAKE_CHECK=1` is set so it doesn't
   slow down normal test runs.

3. **Confirm via empirical repro loop** after the fix: same
   10-run sweep that used to show 40-50% failure should show 0%.

## LOE

- cli.py: ~40 lines (one new helper + wire-in)
- tests: ~80 lines (one unit test + one gated repro loop)
- stub_claude.py: remove the temporary STUB_DEBUG_LOG instrumentation
  before commit

Total: ~120 lines. Well under budget.

## Risks

1. **Forward scan cost on large logs**: bounded to 1-2 turns worth
   of data post-marker (since _wait_for_turn is called with a
   fresh marker). Not a concern for realistic workers.
2. **Still-streaming turn**: if the reverse walk missed a turn-end
   because only the assistant chunk has landed (no result yet),
   the forward scan will also miss it — tail loop will catch the
   result when it lands. No regression.
3. **Chat-tag filter interaction**: the forward scan needs to
   respect `chat_tag` the same way the reverse walk and tail do.
   Mirror the existing check.

## GVP alignment

- G3 (test-first-with-real-conditions): the fix is informed by
  real repro, not speculation.
- P4 (test-real-conditions-not-monkey-patched-away): the race is
  a real production bug — tests exposing it are doing their job;
  fix the bug, don't silence the test.

New decision `D101` records the race + fix.
