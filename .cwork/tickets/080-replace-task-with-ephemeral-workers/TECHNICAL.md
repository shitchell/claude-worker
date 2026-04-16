# TECHNICAL — #080 replace-task-with-ephemeral-workers

## PM-confirmed decisions

1. **Inactivity detection**: log mtime (reliable; `working` status
   persists through tool calls per OBS26).
2. **Cleanup**: graceful — wrap-up message → SIGTERM after short
   timeout (default 30s).
3. **Parent tracking**: reuse `CW_PARENT_WORKER` (D92).
4. **Task tool**: identity guidance only (no hook deny).
5. **Idle default**: 300s (5 min). Configurable via
   `--ephemeral-idle-timeout SECONDS`.
6. **Manager check cadence**: 30s (piggyback on the existing
   `CWORK_MONITOR_INTERVAL_SECONDS` cycle).
7. **Termination log line**:
   `[system:ephemeral-timeout] Worker X idle N minutes, terminating.`

## Implementation plan

### Constants (cli.py)

```python
EPHEMERAL_IDLE_TIMEOUT_SECONDS: int = 300
EPHEMERAL_WRAPUP_TIMEOUT_SECONDS: int = 30
```

### CLI surface — `cmd_start`

Two new flags:
- `--ephemeral` (bool) — marks the worker as short-lived. Sets
  `CW_EPHEMERAL=true` in the worker's env. Survives resume.
- `--ephemeral-idle-timeout SECONDS` (int) — override the default
  300s inactivity window. Only meaningful with `--ephemeral`.

### Persistence

Session metadata (`.sessions.json`) gains two optional keys:
- `ephemeral: bool`
- `ephemeral_idle_timeout: int`

Runtime also writes a plain-text sentinel `runtime/ephemeral`
whose content is the idle-timeout in seconds. The manager reads
this once at startup — avoids re-parsing sessions.json per poll.

### Env wiring (manager.py)

```python
env["CW_EPHEMERAL"] = "true" if ephemeral else "false"
```

(Conditional on `--ephemeral` being set.)

### Ephemeral inactivity check (manager.py)

New helper:

```python
def _ephemeral_should_reap(log_path, idle_timeout, now=None):
    """Return True if the log file's mtime is older than idle_timeout."""
```

Extracted for unit testing — pure function, no side effects.

Main-loop integration: alongside the existing cwork/thread
monitors, add a periodic check gated on the ephemeral sentinel.
On trigger:

1. Compute idle minutes.
2. Append `[system:ephemeral-timeout] Worker <name> idle <m>
   minutes, terminating.` to the log.
3. Write a graceful wrap-up message to the FIFO (same pattern
   as `cmd_stop`'s `_send_wrap_up_message`).
4. Wait up to `EPHEMERAL_WRAPUP_TIMEOUT_SECONDS` for the worker
   to idle-after-wrapup (poll via `get_worker_status`).
5. SIGTERM the claude subprocess; the normal cleanup path handles
   the rest.

The manager loop then exits its poll cycle because the claude
process is gone.

### Identity guidance (pm.md, technical-lead.md)

Add a short section to each:

> **Ephemeral delegation**: use `claude-worker start --ephemeral` to
> spawn short-lived workers for long-running implementation work.
> Do **not** use the Task tool for multi-minute tasks — the Task
> tool blocks your message queue until it returns, so you become
> mute to the PM/other workers mid-task. Ephemeral workers auto-
> terminate after 5 minutes of inactivity, so cleanup is free.

### Tests

`tests/test_ephemeral_worker.py`:

1. `test_should_reap_fresh_worker` — mtime just now, idle timeout
   300s → False.
2. `test_should_reap_idle_worker` — mtime 10m ago, timeout 300s
   → True.
3. `test_should_reap_missing_log` — no log file → True (treat
   as idle-since-forever; shouldn't happen with live workers but
   defensive).
4. `test_start_writes_ephemeral_sentinel` — `cmd_start --ephemeral`
   writes `runtime/ephemeral` with the timeout as text.
5. `test_session_metadata_includes_ephemeral` — `.sessions.json`
   entry after `--ephemeral` has the two fields.
6. `test_env_var_set_on_ephemeral` — `run_manager` subprocess env
   has `CW_EPHEMERAL=true` only when ephemeral.

End-to-end lifecycle test via the stub-claude fixture: start an
ephemeral worker with a 2s idle timeout, let it idle, assert the
manager reaps it.

### README

New subsection under `start`:

```
--ephemeral
    Mark the worker as short-lived. The manager reaps the worker
    after --ephemeral-idle-timeout seconds of log inactivity
    (default 300). Use this instead of the Task tool for
    long-running delegation — the delegating worker stays
    responsive because it only runs `claude-worker start`
    (non-blocking), not a blocking tool call.
--ephemeral-idle-timeout SECONDS
    Override the 300s default.
```

## LOE

Per PM's 600-line ceiling:
- cli.py: ~40 lines (flags + sentinel write)
- manager.py: ~60 lines (reaper + helper)
- identities: ~30 lines (pm.md + technical-lead.md)
- tests: ~120 lines
- README: ~25 lines

Total: ~275 lines. Well under budget.

## Risks

1. **Wrap-up timeout tight at 30s**: worker may not finish wrap-up
   in 30s. Acceptable — the whole point is fast cleanup. If this
   bites in practice, `--ephemeral-wrapup-timeout` flag is easy to
   add later.
2. **Ephemeral survives resume?**: yes — sessions.json persists the
   flag. Design choice per PM.
3. **Race between manager reap and worker naturally exiting**:
   the reap path does a graceful FIFO wrap-up; if the worker has
   already exited, `_send_wrap_up_message` fails silently and we
   proceed to SIGTERM (no-op on dead pid). Safe.
4. **Tests touching real ~/.cwork**: reuse existing `fake_worker`
   / `running_worker` fixtures + monkeypatched `get_base_dir`.

## GVP alignment

- G2 (loud-over-silent-failure): termination emits a visible log
  line.
- V4 (delegation-beats-self-discovery): the whole point —
  unblocks delegation for long tasks.
- V6 (context-budget-awareness): ephemeral workers cost context
  only for their own work.

New decision `D97` records the mechanism.
