# 048: Compaction Resilience — Research & Design

## Problem Analysis

Compaction is the biggest reliability threat to the continuity model.
When it fires:
1. Prior conversation content is compressed to a summary
2. Identity file instructions may be partially or fully lost
3. GVP awareness is lost (agent forgets guiding elements)
4. Ongoing work context is compressed (agent may not remember
   what ticket it's implementing or what approach was agreed)
5. The wrap-up and analyze-session steps never happen
6. No notification — the agent continues working without
   realizing its context has degraded

Evidence: this TL session had 5 compaction events. The PM saw
context drop from ~67% to 1% and raised an alarm.

## Prevention Mechanisms

### P1: Context budget warnings (Stop hook)

Add 50% and 65% warning thresholds to context_threshold.py:

```
50%: [system:context-warning] At 50% context. Delegate more,
     implement less. Each tool call output consumes context.
65%: [system:context-warning] At 65% context. Consider wrapping
     up current task and delegating remaining work. Context
     pressure increases error rate and compaction risk.
80%: [system:context-threshold] (existing) Begin wrap-up procedure.
```

These are early warnings before the 80% wrap-up trigger. They
encourage delegation which slows context growth.

LOE: ~15 lines — add two more threshold checks to the existing
context_threshold.py module.

### P2: Delegation enforcement for identity workers

Identity workers (PM, TL) should delegate more aggressively.
Options:

**a. Prose reinforcement (0 LOE):** Stronger language in identity
files: "Direct implementation is a last resort. Every function
you implement directly consumes 5-10x more context than the
delegation message that achieves the same result."

**b. PreToolUse warning on Edit/Write for TL (15 LOE):** A hook
that warns (not blocks) when TL identity uses Edit/Write directly:
"Warning: direct file modification consumes context. Consider
delegating to a worker." This is the lightest enforcement.

**c. Context-per-turn tracking (30 LOE):** Track context growth
per turn. If a single turn grew context by >5%, warn about
delegation ratio. Requires computing delta between turns.

Recommendation: start with (a), implement (b) if (a) is ignored.

### P3: Compaction-aware task sizing

Identity workers should size tasks to fit within a context budget:
- Simple tasks: do directly (~5% context cost)
- Medium tasks: delegate to worker (~1% context cost for the
  delegation message)
- Large tasks: must delegate, no exception

This is prose guidance — hard to enforce via hooks.

## Detection Mechanisms

### D1: SessionStart hook on compact events (SHIPPED)

compaction_detector.py already fires on compact events. Currently
echoes a [system:compaction-detected] message.

### D2: Identity re-injection on compact/clear

A SessionStart hook that, on compact/clear events, echoes the full
identity.md contents to stdout so the agent's behavioral guidance
is immediately available post-compaction. Also includes ticket count
and GVP summary.

Design: identity_reinjector.py module. On compact/clear events:
- Echo identity.md (first 3000 chars to avoid overwhelming)
- Echo ticket summary (todo/active/done counts)
- Echo GVP summary (decision count, key principles)

On startup/resume: echo only ticket summary + GVP summary (identity
is already in system prompt via --append-system-prompt-file).

LOE: ~50 lines.

### D3: Compaction logging

When compaction is detected:
- Append to .cwork/<identity>/LOG.md
- Fire claude-worker notify if configured
- Note that analyze-session and wrap-up were SKIPPED

LOE: ~15 lines added to compaction_detector.py.

### D4: Post-compaction quality gate

The [system:compaction-detected] message should instruct:
1. Re-read identity file
2. Re-read latest handoff
3. Verify current work by reading ticket files
4. Report to PM: "compaction occurred, re-bootstrapped, working on X"

This is already partially implemented in the current
compaction_detector.py message. Needs enhancement to be more
specific about what to re-read.

LOE: ~5 lines.

## Recovery Mechanisms

### R1: Handoff file as recovery anchor

The handoff file (.cwork/<identity>/handoffs/<timestamp>.md) is the
recovery point. After compaction, the agent reads the latest handoff
to reconstruct what it was working on. This already works for PM
replacement — compaction is just an unexpected replacement.

No code change needed — the instruction in the compaction message
already says "re-read latest handoff."

### R2: Session analysis of pre-compaction work

Can we retroactively analyze what happened before compaction?
The worker log still contains the full JSONL from before compaction.
analyze-session could produce a partial analysis of the pre-compaction
segment by reading the log up to the new system/init message.

LOE: ~20 lines — modify analyze-session to accept a line range.

## Recommended Implementation Order

### Immediate (~30 LOE)
1. **Context warnings at 50%/65%** — prevent compaction
2. **Enhance compaction_detector.py** — logging + better message

### Short-term (~65 LOE)
3. **Identity re-injection hook** — full identity on compact/clear
4. **Compaction logging** to LOG.md + notify

### Later (~30 LOE)
5. **TL Edit/Write warning** — delegation enforcement
6. **Pre-compaction segment analysis** — retroactive analysis

## Key Insight

The best defense against compaction is **not happening**. Prevention
(P1: warnings, P2: delegation enforcement) is more valuable than
detection (D1-D4) or recovery (R1-R2). An agent that delegates
aggressively and wraps up at 80% never hits compaction.

The second-best defense is **fast recovery**. Identity re-injection
(D2) + handoff reading (R1) gets the agent back to functional
within one turn after compaction.
