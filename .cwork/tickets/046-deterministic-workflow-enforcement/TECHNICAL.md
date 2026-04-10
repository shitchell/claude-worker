# 046: Deterministic Workflow Enforcement — Audit & Design

## Principle

Every rule that CAN be a hook SHOULD be a hook. Rules that can't be
hooks should be in GVP. Pure prose in identity files is the last resort.

## Current Enforcement Inventory

What's already enforced (not prose):

| Mechanism | What it enforces | Type |
|-----------|-----------------|------|
| cwd_guard.py | CWD write boundary | PreToolUse hook |
| context_threshold.py | 80% context wrap-up trigger | Stop hook |
| permission_grant.py | Sensitive file edit gating | PreToolUse hook |
| ticket_watcher.py | Ticket change notifications | PostToolUse hook |
| Status gate in cmd_send | Reject send to busy worker | CLI gate |
| after_uuid marker (D2) | Race-free send-wait | Protocol |
| Atomic writes (D4) | Crash-safe state persistence | Code pattern |
| Chat tag monitoring | Missing tag detection | Read side-effect |
| FIFO pending check (D31) | False-idle prevention | Status function |
| Queue drain | Guaranteed message delivery | Manager poll |

## Workflow Audit Table

### PM Workflows

| Step | Currently | Proposed Enforcement | LOE | Priority |
|------|-----------|---------------------|-----|----------|
| Request evaluation (7-step checklist) | Prose | Review agent: PostToolUse on `claude-worker send` checks if delegation included GVP alignment | 40 | Medium |
| Bug triage (verify env before delegating) | Prose | Script: `claude-worker delegate-bug` subcommand that prompts for env check | 30 | Low |
| Post-fix GVP review | Prose | Post-commit hook: after `git commit`, check if cairn validate --coverage worsened | 20 | High |
| Chat tag in final response | Prose | Stop hook: check if assistant response has [chat:*] matching the incoming tag | 25 | Medium |
| LOG.md append on actions | Prose | PostToolUse hook: on Write to LOG.md, validate format | 15 | Low |
| Decision recording (always, no exceptions) | Prose | **Post-commit detection** (see Detection section) | 15 | **High** |
| Backlog processing (don't go idle) | Prose | Stop hook: check INDEX.md for open todos before allowing idle | 20 | Medium |
| Handoff before wrap-up | Prose | Stop hook: check handoff file mtime before [system:stop-requested] completes | 15 | Medium |
| Consumer notification on wrap-up | Prose | Manager: check active chat tags, warn if no notification sent | 25 | Low |

### TL Workflows

| Step | Currently | Proposed Enforcement | LOE | Priority |
|------|-----------|---------------------|-----|----------|
| **Write tests per G3** | Prose (OBS10) | **Post-commit hook**: count test files touched, warn if 0 | 15 | **Highest** |
| **Record D\<N\> in project.yaml** | Prose (OBS10) | **Post-commit hook**: check if .gvp/library/ modified in commit | 15 | **Highest** |
| TECHNICAL.md before implementation | Prose | Script: `claude-worker start-ticket` checks for TECHNICAL.md | 20 | Medium |
| Run tests before reporting | Prose | Post-commit hook: verify pytest ran in session log | 15 | High |
| Run cairn validate | Prose | Post-commit hook: verify cairn validate in session log | 15 | High |
| Push when done | Prose | Post-commit hook or Stop hook: check unpushed commits | 10 | Medium |
| Report via claude-worker send | Prose | Already standard; no enforcement needed | 0 | Done |

### Generic Worker Workflows

| Step | Currently | Proposed Enforcement | LOE | Priority |
|------|-----------|---------------------|-----|----------|
| CWD write boundary | **Enforced** (cwd_guard.py) | Done | 0 | Done |
| Context threshold wrap-up | **Enforced** (context_threshold.py) | Done | 0 | Done |
| [system:*] prefix on synthetic messages | **Enforced** (D28) | Done | 0 | Done |

## OBS10 Deep Dive: Tests + GVP Decisions

The most consistently forgotten steps. This PM sent the TL back
twice in one session for missing these. Root cause: the TL's identity
file says "you don't implement" but the TL IS implementing (directly,
not via workers). The G3/D\<N\> reminders are in the PM's Backlog
Processing section, not in the TL's identity.

### Enforcement options for OBS10:

**Option 1: Post-commit git hook (RECOMMENDED)**

A pre-push or post-commit hook that checks:
```bash
# Did this commit touch test files?
test_files=$(git diff --name-only HEAD~1 | grep "^tests/" | wc -l)
if [ "$test_files" -eq 0 ]; then
    echo "WARNING: No test files in this commit. G3 requires tests."
fi

# Did this commit touch .gvp/library/?
gvp_files=$(git diff --name-only HEAD~1 | grep "^.gvp/library/" | wc -l)
if [ "$gvp_files" -eq 0 ]; then
    echo "WARNING: No GVP library update in this commit."
fi
```

This is a WARNING, not a block — some commits legitimately don't need
tests (docs, identity files). But it surfaces the omission immediately.

**Option 2: Stop hook review check**

A Stop hook that, after each turn, checks if a `git commit` was made
in the session and whether the commit included test + GVP files. More
invasive but catches the issue before the PM sees the report.

**Option 3: claude-worker commit subcommand**

Replace raw `git commit` with `claude-worker commit` that enforces
G3 + D\<N\> checks before committing. Most reliable but changes the
workflow.

**Recommendation**: Start with Option 1 (post-commit hook) — lowest
friction, immediate feedback, doesn't block. Upgrade to Option 3 if
Option 1 is insufficient.

## Detection Mechanisms

### 1. Post-commit hooks

Lightweight checks after every commit:
- Test files touched? (G3)
- GVP library updated? (D\<N\>)
- cairn validate passes? (coverage)
- black formatting clean? (already enforced)

Implementation: git hook at `.git/hooks/post-commit` or via the user's
git wrapper system. ~20 LOE.

### 2. Session analysis patterns

Extend the analyze-session skill to flag workflow violations:
- "PM delegated without bug triage step" — detect `claude-worker send`
  to TL for a bug without a prior env-check question
- "TL committed without tests" — detect `git commit` without `pytest`
  in the session log between the commit and the prior user message
- "No GVP decision recorded" — detect implementation commit without
  `.gvp/library/` modification

These become patterns in the session analysis system. The pattern-
reviewer agent aggregates across sessions. ~30 LOE per pattern.

### 3. Review agents

A PostToolUse hook on Bash that checks for `git commit` or `git push`
commands. When detected, spawns a lightweight Task agent that:
- Reads the diff
- Checks for test files
- Checks for GVP refs
- Reports findings as a [system:review] message

Most powerful but most expensive (spawns an agent per commit). ~40 LOE.
Reserve for high-value commits (e.g., identity file changes).

### 4. Log analysis

Patterns in the worker log that indicate skipped steps:
- Commit message without preceding `pytest` output → tests not run
- Commit message without preceding `cairn validate` → coverage not
  checked
- `claude-worker send` to PM with "committed as" but no mention of
  "D\<N\> recorded" → GVP decision skipped

Can be implemented as a PostToolUse hook on Bash that pattern-matches
the command. ~15 LOE.

### 5. Ticket lifecycle checks

Structural validation of ticket directories:
- Implementation commit exists but no TECHNICAL.md → planning skipped
- Status=done but no REVIEW.md → review skipped
- Status=done but no D\<N\> refs in project.yaml → decision not recorded

Can be run as a periodic task or as part of cairn validate. ~20 LOE.

## Detection → Enforcement Flywheel

```
Detection finds violation
  → Pattern recorded in session analysis
  → If pattern recurs 2+ times:
     → File ticket to add enforcement (hook/script/gate)
     → Implement enforcement
     → Detection now catches edge cases the enforcement missed
     → Enforcement improves
```

## Recommended Implementation Order

### Tier 1 — Highest value, lowest friction (~50 LOE)

1. **Post-commit warning hook** for G3 + D\<N\> (Option 1) — 20 LOE
2. **Log analysis pattern matching** for tests/GVP in session log — 15 LOE
3. **Ticket lifecycle validation** as periodic check — 15 LOE

### Tier 2 — Medium value, medium friction (~70 LOE)

4. **Stop hook for backlog processing** — 20 LOE
5. **Stop hook for handoff validation** — 15 LOE
6. **Post-commit cairn validate check** — 15 LOE
7. **Session analysis workflow patterns** — 20 LOE

### Tier 3 — High value, high friction (~80 LOE)

8. **claude-worker commit subcommand** — 30 LOE
9. **Review agent on commit** — 40 LOE
10. **Chat tag enforcement Stop hook** — 10 LOE
