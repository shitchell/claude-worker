# 083: `claude-worker subagents <name>` subcommand

## Problem

Phase B of the #081 design (worker tool-call visibility). When a
worker dispatches Task subagents, their JSONL logs live at
`~/.claude/projects/<project-slug>/<session-uuid>/subagents/agent-*.jsonl`
plus sibling `.meta.json` files. Operators have no tooling to
summarize these — they must `cat` raw JSONL to understand what
a subagent did.

After #080 (ephemeral workers) reduced the motivation for Task-based
delegation, subagent visibility is still useful for debugging
stuck Task calls in legacy code paths or third-party integrations
that haven't migrated to ephemeral workers.

## Proposed design

```
claude-worker subagents <worker-name> [--format text|json] [--tail N]
```

For each agent-*.jsonl under the worker's current session:
- Parse the paired meta.json for `agentType` and `description`
- Walk the JSONL for last user message, last assistant message,
  tool-use count, start/end timestamps
- Summarize per-agent; sort by last-activity descending

Text output (default):

```
worker: cw-lead
session: 86c9ce5a-8223-4164-a794-48a3b89a4901
  agent-a45f8406bf6ad9b40  Explore  "PID ancestry investigation"
    started 2m 14s ago, 3 tool calls, last: "Bash(cd .. && ls)"
  agent-a8eb304b0bc43bee5  general-purpose  "pytest baseline"
    started 45s ago, 1 tool call, last: "Bash(pytest tests/)"
```

JSON output: one object per agent with fields for scriptability.

## Design questions

1. **Project-slug resolution** — Claude Code maps a cwd to a slug
   by replacing `/` with `-`. That transformation lives in
   Claude Code's internals, not exposed via any public API. We
   need a helper that replicates it. Check for edge cases: leading
   slash, symlinks in the cwd, multiple consecutive slashes.

2. **Meta.json optional fields** — `agentType`, `description`,
   and sometimes a `parent_agent_id` are present. Tool should
   degrade gracefully if any are missing.

3. **Live subagents only?** — should the command show only
   in-flight subagents (last activity < 5min), or all subagents
   of the current session? Default to all, with `--tail N` to
   limit count.

4. **Cross-session view** — should the command optionally walk
   older sessions (via `runtime/sessions-history` if we added
   one), or strictly the current session? Current session only
   for Phase B.

## GVP alignment

- G2: loud-over-silent-failure (exposes the hidden state that
  caused the #074 cascade)
- Follows D97's pattern: visibility over blocking-the-caller

## Priority

Medium — ephemeral workers (D97) reduce the urgency. Still useful
for debugging Task usage that hasn't migrated.

## Origin

Spun off from #081 during TL scope-split (2026-04-16). Filed as
Phase B to keep #081 under the 500-line budget.
