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

## AREA 1: Mid-Turn System Message Injection

### The Gap

The Stop hook only fires BETWEEN turns. If an agent reads a large
file (e.g., a 50k-line log) and blows past 80% in a single turn,
the context threshold hook never fires. The agent continues working
until the turn ends, by which time it may be at 95%+ and one turn
away from compaction.

### PostToolUse: The Mid-Turn Hook

**PostToolUse fires after EACH tool call within a turn.** This is
confirmed by the hooks reference. It fires after Read, Bash, Edit,
Grep — every tool invocation. This gives us a per-tool-call
checkpoint.

### Mid-turn message delivery mechanism

Claude Code hooks have specific exit code semantics:
- **Exit 0**: stdout parsed as JSON (for hook-specific output)
- **Exit 2**: stderr fed to Claude as a **blocking error message**
- **Other**: non-blocking error, continues

**Exit 2 with stderr content gets shown to Claude DURING the turn.**
This is the injection mechanism. A PostToolUse hook can:
1. Check context window usage (via compute_context_window_usage)
2. If over threshold, exit 2 with stderr: "WARNING: context at 85%.
   Wrap up immediately."
3. Claude sees this as a tool-result error and can adjust behavior

### Design: context_check PostToolUse hook

```python
# context_mid_turn.py — PostToolUse hook
# After each tool call, check if context is over warning thresholds

def main():
    payload = json.load(sys.stdin)
    transcript = payload.get("transcript_path", "")
    
    cw = compute_context_window_usage(transcript)
    if cw is None:
        sys.exit(0)
    
    pct = cw.total / window_size
    
    if pct >= 0.85:
        print(
            "[system:context-critical] URGENT: Context at {pct}%. "
            "Stop what you're doing. Complete your current thought, "
            "then wrap up. Do NOT start new tool calls.",
            file=sys.stderr
        )
        sys.exit(2)  # blocking error — Claude sees this
    
    if pct >= 0.65:
        # Non-blocking warning (exit 0, but echo to stdout)
        print(json.dumps({
            "hookSpecificOutput": {
                "message": f"Context at {int(pct*100)}%. Delegate more."
            }
        }))
    
    sys.exit(0)
```

### Performance concern

compute_context_window_usage scans the ENTIRE log file forward.
For a 25MB log (like this TL session), that's expensive on every
tool call. Options:
- **Use transcript_path** (Claude Code's session log, not the
  worker log) — may be smaller
- **Cache**: read usage from the last assistant message only
  (reverse scan, O(1))
- **Sampling**: only check every Nth tool call (e.g., every 5th)
- **File size heuristic**: log file size > threshold → check usage

Recommendation: cache the last usage value and only re-scan when
log size has grown significantly since last check.

### Sentinel for one-shot warnings

Like the context threshold's wakeup-context-sent sentinel, each
warning level needs a sentinel to prevent spam:
- 50% warned: sentinel file
- 65% warned: sentinel file  
- 85% warned: sentinel file (this one exits 2 = blocking)

## AREA 2: Interrupt Mechanism

### Signal analysis

The claude process (Node.js) catches these signals:
```
SIGHUP, SIGINT, SIGQUIT, SIGTRAP, SIGABRT, SIGUSR1, SIGUSR2,
SIGTERM, SIGCHLD, and several others
```

**SIGUSR1 and SIGUSR2 are caught** — they have custom handlers.
What they do is unknown without Claude Code source access.

### Stream-json interrupt

The stream-json input format only supports user messages:
```json
{"type":"user","message":{"role":"user","content":"..."}}
```

There's no documented interrupt/cancel message type. Writing a
user message to the FIFO during a turn queues it for the next turn
— it doesn't interrupt the current one.

### SIGINT as interrupt

SIGINT (Ctrl-C) is how the interactive UI interrupts turns. The
node process catches it (SigCgt includes SIGINT). In `-p` mode,
SIGINT likely terminates the current API call and ends the turn.

**This is the interrupt mechanism.** The manager could:
1. Detect context > threshold via PostToolUse hook
2. Send SIGINT to the claude process
3. Claude aborts the current turn
4. Stop hook fires → wrap-up message injected

### Risk: SIGINT might kill the session

In `-p` mode, SIGINT might terminate the entire process, not just
the current turn. This needs testing before implementation.

### Alternative: FIFO user message as soft interrupt

Write a `[system:interrupt] Stop and wrap up now` message to the
FIFO. This doesn't interrupt the current turn, but the agent will
see it when the turn ends (or if it reads a tool result that
happens to be this message). Combined with the PostToolUse exit-2
warning, the agent gets both a mid-turn warning AND a queued
follow-up instruction.

## Identity Re-injection: No Truncation

Per PM directive: remove the 3000 char cap on identity re-injection.
If compaction stripped the full identity, inject the full identity.
Modern Claude Code handles large hook stdout output.

The identity.md files are: pm.md (723 lines), technical-lead.md
(295 lines), rhc identity.md (~80 lines). None are so large that
they'd cause issues with hook stdout.

## Interaction Analysis

All hooks interact through the per-worker settings.json:

```
SessionStart hooks:
  - session-uuid-env-injection.sh (existing, user-level)
  - compaction_detector.py (compact events only)
  - identity_reinjector.py (all events, full identity on compact)

PreToolUse hooks:
  - permission_grant.py (Edit/Write/MultiEdit)
  - cwd_guard.py (Edit/Write/MultiEdit)

PostToolUse hooks:
  - ticket_watcher.py (Edit/Write/MultiEdit)
  - commit_checker.py (Bash containing "git commit")
  - context_mid_turn.py (ALL tools — context check) [NEW]

Stop hooks:
  - context_threshold.py (50%/65%/80% thresholds)
```

The PostToolUse context check is the most impactful addition — it
fills the gap between per-turn Stop hooks and mid-turn awareness.
