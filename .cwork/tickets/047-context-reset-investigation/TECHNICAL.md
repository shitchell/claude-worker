# 047: Context Reset Investigation

## Finding: Compaction DID occur. Multiple times.

The TL session log at /tmp/claude-workers/1000/cw-lead/log contains
**5 system/init messages** with the same session_id but different uuids.
Each init represents a compaction event — Claude Code truncates the
conversation history and restarts the context with a fresh init message.

After compaction, the context window resets to a small number (just the
compacted summary + system prompt). This is why `claude-worker ls`
reported ~1% — it was reading the usage from the most recent assistant
message AFTER compaction, which has a tiny footprint.

## Root cause chain

1. Session runs to ~67% context
2. Compaction fires (automatic or manual)
3. New system/init written to log with same session_id, new uuid
4. Next assistant message has low usage (context was compacted)
5. `compute_context_window_usage` reads this low usage as current state
6. `claude-worker ls` reports 1%
7. Context grows back as the session continues working

## Why the TL didn't notice

Compaction is transparent to the agent — the conversation continues
without interruption. The agent's internal context was compressed but
it retained continuity via Claude Code's compaction summary. From the
agent's perspective, nothing happened.

## Evidence

```
5 system/init messages in log
285 lines containing "compact" (from session_id matching)
Same session_id: 1ac90ea7-db69-4f1f-9703-4012dc03db65
Different uuids per init: 97dd24b0, ff44acff, 5b694fa7, 824a4f9e, 52260be7
```

## Impact

The 1% reading was **correct** — it accurately reflected the post-
compaction context window. The PM's alarm was based on the assumption
that the TL should be at ~67%, but compaction legitimately reduced it.

## Recommendations

1. **No code fix needed** — the reporting is correct
2. **Consider**: should `ls` show "compacted" or "recently compacted"
   when the context drops significantly between polls? This would
   prevent alarm.
3. **The context threshold notification (#016)** fires at 80% — if
   compaction resets to 1%, the threshold won't fire again until
   context grows back to 80%. This is correct behavior (the sentinel
   file prevents re-fire anyway, and a fresh init means new session
   state).
