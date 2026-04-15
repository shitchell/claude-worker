# 066: Identity Version Tracking — Technical Approach

## Summary

Hash the source identity content at copy time, store in
`runtime/identity.hash`. Manager poll loop compares periodically;
emits `[system:identity-drift]` notification when source diverges.

## Source resolution

Identity files are loaded from one of two places (matching
`cmd_start`'s existing resolution at `cli.py:1270-1287`):

1. User-installed: `~/.cwork/identities/<name>/identity.md`
2. Bundled fallback: `claude_worker/identities/{pm,technical-lead}.md`

The hash must reflect the SAME source the runtime copy was made from.
We can't just hash the runtime file (which by definition matches its
own copy). We hash whatever was written, AT THE MOMENT IT WAS WRITTEN.

## Implementation

### 1. Hash helper in `manager.py`

```python
IDENTITY_HASH_FILE: str = "identity.hash"


def hash_identity_content(content: str) -> str:
    """Return a short stable hash of identity content."""
    import hashlib
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def write_identity_hash(runtime: Path, content: str) -> None:
    """Write the source content hash to runtime/identity.hash."""
    try:
        (runtime / IDENTITY_HASH_FILE).write_text(
            hash_identity_content(content) + "\n"
        )
    except OSError:
        pass


def read_identity_hash(runtime: Path) -> str | None:
    """Read the stored identity hash. Returns None if missing."""
    p = runtime / IDENTITY_HASH_FILE
    if not p.exists():
        return None
    try:
        return p.read_text().strip() or None
    except OSError:
        return None
```

### 2. Hash at copy time

`cmd_start` writes `identity.md` at `cli.py:1288`. Right after,
call `write_identity_hash(runtime, identity_content)`.

`cmd_replaceme` writes `identity.md` at `cli.py:3311`. Same
treatment — write the hash right after.

### 3. Source resolver for drift check

The drift check needs to re-read the SOURCE (not the runtime copy).
Add a helper in `manager.py` that mirrors `cmd_start`'s resolution:

```python
def _read_source_identity(identity: str) -> str | None:
    """Read the current source identity content. Returns None if not found.

    Mirrors cmd_start's resolution: user-installed first, bundled fallback
    for pm/technical-lead.
    """
    if not identity or identity == "worker":
        return None
    user_path = Path.home() / ".cwork" / "identities" / identity / "identity.md"
    if user_path.exists():
        try:
            return user_path.read_text()
        except OSError:
            pass
    # Bundled fallback (only pm and technical-lead)
    bundled = {"pm": "pm.md", "technical-lead": "technical-lead.md"}
    resource = bundled.get(identity)
    if resource:
        try:
            from importlib.resources import files
            return (
                files("claude_worker") / "identities" / resource
            ).read_text()
        except Exception:
            pass
    return None
```

### 4. Drift check in poll loop

Mirror the existing `check_periodic_tasks` pattern. Add:

```python
IDENTITY_DRIFT_CHECK_INTERVAL_SECONDS: float = 30.0


def check_identity_drift(
    identity: str,
    runtime: Path,
    in_fifo: Path,
    notified: bool,
) -> bool:
    """Compare runtime hash to source hash. Notify on drift.

    Returns the new "notified" flag — True if a drift notification
    has been sent for the current divergence (resets when source
    matches again).

    Best-effort: never raises.
    """
    try:
        stored = read_identity_hash(runtime)
        if stored is None:
            return notified  # no baseline — nothing to compare
        source_content = _read_source_identity(identity)
        if source_content is None:
            return notified  # source unavailable — can't check
        current = hash_identity_content(source_content)
        if current == stored:
            # Match: clear notified flag for next divergence cycle
            return False
        if notified:
            # Already notified about this divergence; don't spam
            return True
        # Drift detected — inject notification
        msg = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": (
                        f"[system:identity-drift] Source identity '{identity}' "
                        f"has changed since this worker started "
                        f"(stored hash: {stored}, source hash: {current}). "
                        f"Consider replaceme to pick up the new identity."
                    ),
                },
            }
        )
        try:
            wr = os.open(str(in_fifo), os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(wr, (msg + "\n").encode())
            finally:
                os.close(wr)
        except OSError:
            return notified  # FIFO write failed; try again next cycle
        return True
    except Exception:
        return notified
```

Wire into the poll loop in `_run_manager_forkless` (alongside
`check_periodic_tasks`):

```python
        identity_drift_notified: bool = False
        last_identity_drift_check = time.monotonic()
        # ... in the poll loop ...
        if (
            identity != "worker"
            and now - last_identity_drift_check >= IDENTITY_DRIFT_CHECK_INTERVAL_SECONDS
        ):
            last_identity_drift_check = now
            identity_drift_notified = check_identity_drift(
                identity, runtime, in_fifo, identity_drift_notified
            )
```

## Notification semantics

- **Lightweight per P12**: Single `[system:identity-drift]` line,
  no full diff. Worker reads source on demand if it cares.
- **Once per divergence**: The `notified` flag prevents repeated
  notifications for the same drift. If the source matches again
  (rare but possible — someone reverts), the flag clears so the
  next divergence triggers a new notification.
- **Worker takes action**: The notification suggests `replaceme`.
  The actual update is the worker's decision, not automatic.

## Tests

`tests/test_identity_drift.py`:

- `test_hash_deterministic`: same content → same hash
- `test_hash_changes_on_content_change`: different content → different hash
- `test_write_read_identity_hash_roundtrip`
- `test_read_identity_hash_missing_returns_none`
- `test_check_no_baseline_no_notification`: missing hash file → no notify
- `test_check_no_source_no_notification`: source unavailable → no notify
- `test_check_match_clears_notified_flag`: source matches → flag cleared
- `test_check_drift_emits_notification`: source differs → FIFO receives msg
- `test_check_drift_dedupes`: notified flag prevents repeat notifications
- `test_check_drift_resets_on_match`: match → notify → flag clears

For FIFO testing, follow the pattern from `test_thread_notifications.py`:
mock `os.open`/`os.write`/`os.close` on a sentinel fd to capture writes.

## Risks

- **Tight loop with read errors**: if source becomes unavailable
  permanently (network mount disconnected), check returns early
  without spamming. Safe.
- **Filesystem race**: source could be mid-edit when we hash it.
  Hash function is robust to partial reads (just produces a
  different hash). Next cycle catches the stable state.
- **Bundled identity edits**: bundled files only change on package
  update. Editable install (`pip install -e .`) means source = repo
  file → users editing `claude_worker/identities/pm.md` trigger
  drift on every save. Acceptable — matches the user's intent.

## LOE

Small. ~80 lines of code in manager.py, ~150 lines of tests.

## GVP

D91 (or next available number — verify when recording). Maps to
G2 (loud-over-silent-failure), V2 (explicit-over-implicit),
project:P12 (lightweight notifications).
