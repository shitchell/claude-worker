# TECHNICAL — #087 thread-auto-detection-consumer

## Root cause

`_resolve_read_thread_id` computes `pair-<sender>-<target>` where
sender comes from `_resolve_sender()`:

```
Priority:
1. CW_WORKER_NAME (worker → worker)
2. CLAUDE_SESSION_UUID (Claude Code interactive)
3. "human" (plain terminal)
```

When the human SENT a message from a plain terminal (sender = "human",
thread = `pair-gvp-human`), but later READS from inside Claude Code
(sender = CLAUDE_SESSION_UUID, thread = `pair-<UUID>-gvp`), the
pair-thread-ids don't match. The computed thread doesn't exist, so
`thread read` returns empty / only shows sent messages.

For PM workers there's an additional wrinkle: `_resolve_chat_id`
returns a chat-UUID if the target is PM and CLAUDECODE=1. This
produces `chat-<UUID>` which also doesn't match `pair-gvp-human`.

The fundamental issue: sender identity is ephemeral (session UUID
changes per session), but thread names are persistent (created on
first send and reused across sessions).

## Proposed fix: existence-based fallback in `_resolve_read_thread_id`

After computing the thread_id, check if it actually EXISTS in the
thread index. If not, try common fallback identities:

```python
def _resolve_read_thread_id(args):
    # ... existing --thread / --chat priority checks ...

    sender = _resolve_sender()
    primary = pair_thread_id(sender, args.name)

    # Existence check: if the primary thread exists, use it.
    # Otherwise try fallback identities.
    from claude_worker.thread_store import load_index
    index = load_index()
    if primary in index:
        return primary

    # Fallback: try "human" (interactive terminal identity)
    human_thread = pair_thread_id("human", args.name)
    if human_thread in index:
        return human_thread

    # Fallback: try CW_WORKER_NAME if different from sender
    worker_name = os.environ.get("CW_WORKER_NAME", "")
    if worker_name and worker_name != sender:
        worker_thread = pair_thread_id(worker_name, args.name)
        if worker_thread in index:
            return worker_thread

    # Nothing matched — return primary (will produce a clean
    # "no messages" output rather than silently picking wrong thread)
    return primary
```

Changes:
- Only `_resolve_read_thread_id` in `cli.py` (~15 lines added)
- `_resolve_sender()` unchanged
- Thread creation / send path unchanged (sender identity is still
  context-dependent when WRITING, which is correct — you want
  different sessions to have different pair threads for
  disambiguation)

## Why not change `_resolve_sender()` globally?

Tempting to always return "human" for interactive users, but that
breaks PM chat routing which relies on session-UUID-based separation
(each consumer gets its own chat-UUID thread for isolation). The fix
must be READ-only: try multiple identities, pick the one that
matches an existing thread.

## Why not search by participant list?

Scanning ALL threads for the target as participant would work but
is O(n) in thread count and could match multiple threads (a PM
might participate in pair-gvp-human, pair-gvp-tl, chat-abc, etc.).
The fallback-identity approach is O(1) lookups (index is a dict)
and deterministic.

## `--thread` interaction

Explicit `--thread` wins unconditionally (line 1985-1987 in current
code). The fallback only applies to the auto-detection path. No
change needed.

## Test plan

1. `test_resolves_to_existing_pair_thread` — sender="human",
   target="gvp", thread pair-gvp-human exists → returns it.
2. `test_falls_back_to_human_when_uuid_thread_missing` — sender=
   UUID (Claude Code env), no UUID-based thread exists, but
   pair-gvp-human does → returns pair-gvp-human.
3. `test_uuid_thread_preferred_when_exists` — both UUID-based
   and "human" threads exist → returns UUID-based (primary wins).
4. `test_explicit_thread_overrides_all` — `--thread pair-x-y`
   always wins regardless of existence.
5. `test_worker_to_worker_resolves_correctly` — CW_WORKER_NAME=
   "pm", target="tl", pair-pm-tl exists → returns it.
6. `test_no_thread_exists_returns_primary` — no matching threads
   → returns the primary pair-thread-id (clean error path).

## Risk assessment

1. **False match**: if pair-gvp-human exists but contains a
   DIFFERENT human's conversation, the fallback would show it.
   Acceptable: "human" is a singular identity in this system.
2. **Index load cost**: `load_index()` reads `~/.cwork/threads/
   index.json` — a small JSON file, already loaded frequently
   by other code paths. No measurable cost.
3. **PM chat-ID path**: the chat-ID path runs BEFORE the pair-
   thread fallback. If chat-ID returns a valid chat thread, the
   fallback never fires. No interference.

## LOE

- cli.py: ~20 lines (existence check + fallbacks)
- tests: ~100 lines
- Total: ~120 lines

## GVP alignment

- V1 (clarity-over-cleverness): the user types `thread read gvp`
  and sees their conversation, no `--thread` needed
- V2 (explicit-over-implicit): the fallback is visible in the
  resolution chain, not a hidden default

New decision D103.
