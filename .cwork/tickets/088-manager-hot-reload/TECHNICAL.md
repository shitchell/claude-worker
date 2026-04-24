# TECHNICAL — #088 manager-hot-reload (Option 2: version stamp + warning)

## Assessment: Option 2 is correct for this scope

Options 1 (self-restart) and 4 (thin shim) are architecturally
cleaner but carry significant implementation risk (child PID
preservation, FIFO state transfer, process supervisor lifecycle).
Option 3 (importlib.reload) is fragile in production. Option 2
gives immediate visibility with ~100 lines of low-risk code.
No compelling case for a different option.

## (a) Version source: dual-stamp (package version + git hash)

Package `__version__` alone misses the common case where code
changes but version isn't bumped (every commit during development).
Git hash alone fails in non-git installs.

**Stamp both**:

```python
def _compute_version_stamp() -> dict:
    import claude_worker
    stamp = {"version": claude_worker.__version__}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(claude_worker.__file__).parent,
        )
        if result.returncode == 0:
            stamp["git_hash"] = result.stdout.strip()
    except Exception:
        pass
    stamp["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return stamp
```

At check time, compare EITHER field. If `__version__` differs →
mismatch. If git hash differs → mismatch. If git unavailable on
both sides → fall back to `__version__` only.

## (b) Runtime version file: `runtime/version.json`

Written at manager startup via `_atomic_write_text` (project
convention for crash-safe persistent state). Contents:

```json
{"version": "0.1.16", "git_hash": "1c3af59", "started_at": "2026-04-24T12:00:00Z"}
```

Written AFTER `pid_file.write_text(str(os.getpid()))` at line
~1364, same init block.

## (c) Detection cadence: 30s poll, piggybacked on CWORK_MONITOR

New constant `VERSION_CHECK_INTERVAL_SECONDS: float = 30.0`.
Add to the main-loop periodic checks alongside cwork/thread/
identity-drift/ephemeral.

Check implementation:

```python
def _check_version_drift(running_stamp: dict) -> dict | None:
    """Return the current stamp if mismatched, else None."""
    current = _compute_version_stamp()
    if current.get("version") != running_stamp.get("version"):
        return current
    if (current.get("git_hash") and running_stamp.get("git_hash")
        and current["git_hash"] != running_stamp["git_hash"]):
        return current
    return None
```

One-shot dedup: `version_drift_notified: bool = False` flag, same
pattern as `identity_drift_notified`. Set True on first detection,
never re-checks until the stamp changes again (can't happen without
manager restart).

## (d) Warning surface: FIFO notification + log line

On mismatch, inject:

```
[system:manager-outdated] Manager code is outdated (running <hash>,
installed <hash>). Use `claude-worker replaceme` or `stop + start`
to pick up new code.
```

Same injection pattern as `[system:identity-drift]` and
`[system:new-message]` — write to the FIFO as a user message. The
worker sees it as a system notification in its conversation context.

Additionally, best-effort append a raw JSONL line to the log (so
`read --log` captures it even if the FIFO write fails).

## (e) Replaceme interaction

No special handling needed. `replaceme` already:
1. Archives the old runtime dir (preserving `runtime/version.json`)
2. Starts a fresh manager (which writes a new `runtime/version.json`)
3. The fresh manager's stamp reflects the NEW code

The [system:manager-outdated] notification itself tells the worker
"use replaceme". If the worker calls replaceme, the notification
was the prompt. No post-replacement logging needed — the
replaced-manager pattern is already documented in the handoff file.

## (f) Test plan

1. `test_compute_version_stamp_has_version` — `_compute_version_stamp()`
   returns dict with at least `version` key matching `claude_worker.__version__`.

2. `test_compute_version_stamp_has_git_hash` — when running in a
   git repo, stamp includes `git_hash` key. (Skip if no git.)

3. `test_check_version_drift_matching_returns_none` — same stamp
   → no mismatch.

4. `test_check_version_drift_version_changed` — different
   `version` field → returns current stamp.

5. `test_check_version_drift_hash_changed` — same version,
   different `git_hash` → returns current stamp.

6. `test_manager_writes_version_on_startup` — use `running_worker`
   fixture, verify `runtime/version.json` exists and contains
   valid JSON with `version` key.

7. `test_drift_notification_fires_once` — monkeypatch
   `_compute_version_stamp` to return a changed stamp, run the
   check twice, verify notification fires exactly once (dedup).

## LOE

- manager.py: ~60 lines (stamp, check, notification, constants)
- tests: ~100 lines
- Total: ~160 lines

## GVP alignment

- G2 (loud-over-silent-failure): the 5-day silent drift is the
  motivating incident; this makes it impossible to miss
- P10 (survive-context-disruption): code updates are a form of
  context change; the version stamp tracks it

New decision D105.
