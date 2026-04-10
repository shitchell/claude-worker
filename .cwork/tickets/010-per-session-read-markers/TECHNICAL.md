# 010: Technical Design — Per-Session Read Markers

## Approach

Two new flags on `read`:
- `--mark` — after displaying, save the last-seen UUID for this consumer
- `--new` — show only entries after the last mark (shorthand for
  `--since <last-mark>`)

Consumer identity: `CLAUDE_SESSION_UUID` (from SessionStart hook) or
`--chat` value. Falls back to a generic "cli" marker if neither is set.

## Storage

```
<runtime>/read-markers/<consumer-hash>.txt
```

One file per consumer, containing a single UUID string (the last message
displayed). Consumer hash = md5 of the session UUID for filesystem safety.

## Composition

- `--new` is sugar for `--since <last-saved-uuid>`. If no mark exists,
  shows everything (same as omitting `--since`).
- `--mark` runs after display, records the UUID of the last rendered
  message.
- `--new --mark` = show new + update marker (the common case).
- `--new` + `--since` conflict: error ("--new and --since are mutually
  exclusive").
- `--chat` composes naturally: marker is per chat-id, not per session.

## LOE

- ~30 lines: save/load marker, --mark/--new flag handling in cmd_read
- ~15 lines: argparse + validation
- ~20 lines: tests
- Total: ~65 lines
