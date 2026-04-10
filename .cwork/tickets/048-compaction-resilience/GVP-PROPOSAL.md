# GVP Alignment Check + Proposed Updates for #048

## Existing Elements Covering #048 Design

| #048 Component | Existing Element | Coverage |
|----------------|-----------------|----------|
| Identity re-injection on compaction | G2 (loud-over-silent) | Partial — G2 says failures must be observable, but doesn't address recovery |
| Mid-turn context warnings | G2, P2 (named constants) | Partial — constants exist but no principle about mid-turn intervention |
| Compaction logging + notification | G2, V3 (atomic persistence) | Good — logging + notification follows G2 |
| Post-compaction quality gate | G3 (test-first) | Weak — G3 is about tests, not about post-disruption verification |
| Delegation enforcement | V4 (delegation-beats-self-discovery) | Good — V4 already says prefer delegation |
| Context budget warnings | P2 (named constants) | Good — thresholds are named constants |

## Gaps Identified

### Gap 1: No principle about context resilience

No guiding element addresses "what to do when context is disrupted."
G2 (loud failure) covers detection but not recovery. The compaction
scenario — context silently compressed, agent continues with degraded
understanding — isn't covered.

### Gap 2: No principle about mid-turn intervention

All existing enforcement is between-turn (Stop hooks, status gates).
The #048 design introduces mid-turn intervention (PostToolUse exit 2).
No guiding element establishes when mid-turn intervention is
appropriate vs. when to wait for the next turn boundary.

### Gap 3: No value about context budget awareness

V4 (delegation) says delegation is preferred, but doesn't connect it
to context budget management. The insight that "direct implementation
consumes 5-10x more context than delegation" isn't captured.

## Proposed New Elements

```yaml
principles:
  - id: P10
    name: survive-context-disruption
    statement: >
      When context is disrupted (compaction, clear, crash), the
      recovery path is: (1) re-read identity guidance, (2) re-read
      latest handoff, (3) verify current work against files, (4)
      report status. Identity re-injection hooks provide the
      behavioral guidance; handoff files provide the work context.
      The agent must never continue working after disruption without
      explicitly verifying its understanding.
    tags: [context, observability]
    maps_to: [project:G2]

  - id: P11
    name: mid-turn-intervention-via-posttooluse
    statement: >
      PostToolUse hooks may intervene mid-turn by exiting with code
      2 and writing an urgent message to stderr. This is the ONLY
      mechanism for mid-turn agent communication — no signal
      gracefully interrupts a turn in -p mode. Use sparingly: only
      for critical thresholds (context >85%) where waiting for
      turn end risks compaction. Non-critical warnings use Stop
      hooks (between turns).
    tags: [context, architecture]
    maps_to: [project:G2, project:V3]

values:
  - id: V6
    name: context-budget-awareness
    statement: >
      Context is a finite, non-renewable resource within a session.
      Every tool call output, every file read, every inline code
      change consumes context. Delegation (sending work to a sub-
      worker) costs ~1% of context; direct implementation costs
      5-10%. Identity workers (PM, TL) must delegate aggressively
      to preserve context for coordination, which is their primary
      value. Context budget warnings at 50%, 65%, and 80% enforce
      this awareness.
    tags: [context, delegation]
    maps_to: []

decisions:
  - id: D53
    name: compaction-resilience-design
    decision: >
      Compaction resilience uses a layered approach: prevention
      (context budget warnings at 50%/65%/80% via Stop hook,
      delegation enforcement for identity workers), detection
      (SessionStart hook on compact events, compaction_detector.py),
      recovery (identity re-injection via identity_reinjector.py
      on compact/clear events, handoff file as recovery anchor).
      Mid-turn intervention uses PostToolUse exit 2 (the only
      viable mechanism — signals kill the session in -p mode).
      No identity text truncation on re-injection.
    origin: >
      Ticket #048 (2026-04-10). Signal experiment confirmed:
      SIGUSR1 no effect on claude process, SIGUSR2/SIGINT kill
      the session. PostToolUse exit 2 is the only mid-turn path.
    tags: [context, architecture, observability]
    maps_to: [project:P10, project:P11, project:V6]
    refs: []
```

## Proposed Modifications to Existing Elements

### V4 (delegation-beats-self-discovery): add context rationale

Current statement focuses on "fresh context is faster AND more
accurate." Proposed addition to the statement:

> ...and consumes dramatically less of the delegating worker's
> context budget. A delegation message costs ~1% of context; the
> equivalent direct implementation costs 5-10%.

### G2 (loud-over-silent-failure): extend to disruption

Current statement covers "subsystem fails" but not "context is
disrupted." Proposed addition:

> ...Context disruption (compaction, clear) must also produce an
> observable signal and trigger a recovery procedure.
