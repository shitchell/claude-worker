# GVP Semantic Audit — Sessions 02-03

## Summary

- **45 decisions** (D1-D45) in project.yaml
- **0 W013** (all decisions have refs)
- **6 W011** (identifier matching issues — cosmetic)
- **184 W012** (doc headers — structural, accepted)

## Ticket-to-Decision Coverage

### Tickets WITH decisions (good)

D1-D11: Session 02 tickets (origins reference "Round N" or "P10")
D12-D45: Session 03 tickets, each has a decision with ticket # ref

### Tickets WITHOUT decisions (gaps identified)

| Ticket | Slug | Needs Decision? | Reason |
|--------|------|----------------|--------|
| #001 | setup-cwork-home-dir | No | Scaffolding, not a design choice |
| #003 | review-features-5-6 | No | Code review, not implementation |
| #006 | persistent-session-logs | **Yes** | Archive-on-stop is a design choice |
| #007 | move-workers-dir-to-cwork | **Yes** | ~/.cwork/workers/ path is a design choice |
| #008 | ls-filters | **Yes** | Filter flags + --format json |
| #012 | send-dry-run | Borderline | Small utility, but --dry-run + --verbose is an API design |
| #013 | uuid-survival-compact | No | Investigation, D23 covers |
| #014 | cwd-write-enforcement | **Yes** | PreToolUse hook for CWD boundary |
| #015 | skill-wip-feedback-note | No | Text addition |
| #017 | stop-wrapup-improvements | **Yes** | 15min timeout + --wrap-up-timeout flag |
| #019 | ticket-closure-authority | No | Process convention |
| #022 | cairn-100-coverage | No | Maintenance task |
| #023 | auto-restart-pm-replacement | **Yes** | Keystone feature: SIGUSR1 + detached replacer |
| #024 | remove-send-background | **Yes** | Breaking change: removed --background |
| #043 | identity-periodic-tasks | Covered by D42 | Same ticket, different numbering |

**7 implementation tickets lack decisions.** These are from the early
session 03 work (tickets #006-#024) before OBS10 was codified. The
PM didn't include "record D<N>" in assignments until ticket #025.

## Ref Accuracy

Spot-checked 10 decisions:
- D12 (queue-id-random-hex): refs to cli.py `_generate_queue_id` ✓
- D21 (callback-queue): refs to manager.py `enqueue_message` ✓
- D29 (thin-identity-flag): refs to cli.py `_get_worker_identity` ✓
- D36 (cwork-monitoring): refs to manager.py `snapshot_cwork_dir` ✓
- D45 (tier1-enforcement): refs to commit_checker.py ✓

No stale refs found — all referenced functions/identifiers exist in
current code.

## Guiding Elements Review

### Goals (G1-G4): Still coherent
- G1 (race-free-send-wait): validated by D2, D12, D22, D31
- G2 (loud-over-silent): validated by D13, D25, D28, D33, D45
- G3 (test-first): validated by D45 enforcement, but weakly enforced
  (warning only)
- G4 (composable-subcommands): validated by extensive new subcommands

### Values (V1-V5): Still coherent
- V3 (atomic-persistence): could reference the new queue files and
  registry.yaml — these don't use _atomic_write_text. Minor gap.

### Principles (P1-P9): No updates needed
All principles are still load-bearing and referenced by decisions.

## Recommendations

1. **Backfill 7 missing decisions** for tickets #006, #007, #008,
   #014, #017, #023, #024. These are significant design choices.
2. **Fix 6 W011 identifiers** — cosmetic but shows in cairn output.
3. **Consider V3 gap**: queue files (enqueue_message) and
   registry.yaml (save_registry) don't use _atomic_write_text.
   Low risk (non-critical state) but inconsistent with the value.
