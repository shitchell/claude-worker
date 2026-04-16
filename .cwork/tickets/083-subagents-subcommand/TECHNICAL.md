# TECHNICAL — #083 subagents-subcommand

## Project-slug resolution

Claude Code maps a cwd to a project slug at
`~/.claude/projects/<slug>/<session-uuid>/`. Sampling existing
slugs on disk confirms the algorithm:

| Original cwd                                   | Slug                                           |
|------------------------------------------------|------------------------------------------------|
| `/home/guy`                                    | `-home-guy`                                    |
| `/home/guy/devops/docker/assetsuite`           | `-home-guy-devops-docker-assetsuite`           |
| `/home/guy/git/dev.azure.com/trinoor/TAShelix` | `-home-guy-git-dev-azure-com-trinoor-TAShelix` |
| `/home/guy/git/…/TAStest/.worktrees/dwp3-checklist` | `…-TAStest--worktrees-dwp3-checklist`     |
| `/home/guy/git/github.com/shitchell/claude-worker` | `-home-guy-git-github-com-shitchell-claude-worker` |

Algorithm: replace every `/` and `.` with `-`. Nothing else is
transformed (case preserved, no collapsing, no trailing trim).
The `TAStest/.worktrees/` example produces `--worktrees` (double
dash) because slash + dot both map to `-` independently — so
this is literal, not a bug.

```python
def _cwd_to_project_slug(cwd: str) -> str:
    """Claude Code's cwd → project-slug mapping (matches ~/.claude/projects/)."""
    if not cwd:
        return ""
    # Use the stored cwd verbatim — Claude Code does NOT resolve symlinks
    # before computing the slug, so we shouldn't either.
    return cwd.replace("/", "-").replace(".", "-")
```

## Subcommand surface

```
claude-worker subagents <worker-name> [--format text|json] [--limit N]
```

- `name` — worker name (positional, required)
- `--format text|json` — default text; json emits one object per
  agent for scripting
- `--limit N` — cap the number of agents shown (default: all)

Output sorted by most-recent-activity descending.

### Text output

```
worker: cw-lead
session: 86c9ce5a-8223-4164-a794-48a3b89a4901
subagents: 4

  agent-a45f8406bf6ad9b40  Explore
    description: "PID ancestry investigation"
    started 2m 14s ago, 12 tool calls, last: Bash(ps -ef | head)

  agent-a8eb304b0bc43bee5  general-purpose
    description: "pytest baseline"
    started 45s ago, 1 tool call, last: Bash(pytest tests/)
```

When the worker has no subagents:

```
worker: cw-lead
session: 86c9ce5a-8223-4164-a794-48a3b89a4901
subagents: 0  (no subagents for this session)
```

When the worker has no session yet:

```
Error: worker 'cw-lead' has no session yet (not started).
```

### JSON output

```json
{"worker": "...", "session": "...", "subagents": [
  {"agent_id": "a45f8406bf6ad9b40", "type": "Explore",
   "description": "PID ancestry investigation",
   "started_at": "2026-04-16T09:00:00Z",
   "last_action_at": "2026-04-16T09:02:14Z",
   "tool_call_count": 12,
   "last_action": "Bash(ps -ef | head)"}
]}
```

One JSON envelope per invocation (not per agent) — simpler for
scripts that want the whole summary.

## Implementation

New module: none needed — it all fits in `cli.py` alongside other
subcommands. Helpers:

```python
def _cwd_to_project_slug(cwd: str) -> str: ...
def _resolve_subagents_dir(name: str) -> Path | None: ...
def _summarize_subagent(meta_path: Path, jsonl_path: Path, now: float) -> dict: ...
def cmd_subagents(args: argparse.Namespace) -> None: ...
```

### `_resolve_subagents_dir`

1. Read `runtime/session` → session_uuid
2. Read `.sessions.json` → cwd
3. slug = `_cwd_to_project_slug(cwd)`
4. Return `Path.home() / ".claude" / "projects" / slug / session_uuid / "subagents"`
5. Return None if any step fails (worker not started, no session,
   or the directory doesn't exist)

### `_summarize_subagent`

For each `agent-<id>.jsonl` paired with `agent-<id>.meta.json`:

- Parse meta.json: `agentType`, `description`
- Walk the JSONL:
  - first entry's `timestamp` → started_at
  - last entry's `timestamp` → last_action_at
  - count `tool_use` blocks across all assistant messages
  - most recent `tool_use` → last_action (via existing
    `_format_tool_call`)
- Gracefully handle missing fields (degraded agent file, partial
  writes, schema drift)

Reuses `_format_tool_call` from #081/D98 — no duplicate logic.

### `cmd_subagents`

1. Resolve dir via `_resolve_subagents_dir`
2. If None → print error and exit 1
3. Enumerate `agent-*.meta.json`, pair each with `agent-*.jsonl`
4. Build summaries, sort by last_action_at desc
5. Apply `--limit` if given
6. Render text or json per `--format`
7. Exit 0

## Tests

`tests/test_subagents_command.py`:

1. `test_cwd_to_slug` — various cwds produce expected slugs
   (including the dot-in-path edge case).
2. `test_cwd_to_slug_empty` — empty cwd → empty slug.
3. `test_resolve_dir_missing_session_returns_none` — worker
   with no session file → None.
4. `test_resolve_dir_existing` — build a fake project layout,
   resolve correctly.
5. `test_summarize_subagent_basic` — synthetic agent JSONL
   with 2 tool_use calls → correct count + last_action.
6. `test_summarize_subagent_missing_meta` — no `.meta.json` →
   agentType "unknown", still summarizes the jsonl.
7. `test_summarize_subagent_empty_jsonl` — empty jsonl file →
   returns a summary with zero tool calls.
8. `test_cmd_subagents_text_output` — full command with
   fake_worker + synthetic subagents dir → expected text lines.
9. `test_cmd_subagents_json_output` — same with `--format json`
   → valid JSON envelope with expected fields.
10. `test_cmd_subagents_no_session` — no session file → error
    message + exit code 1.
11. `test_cmd_subagents_no_subagents_dir` — session exists but
    no subagents dir → clean "0 subagents" output, exit 0.
12. `test_cmd_subagents_limit` — `--limit 2` on 5 subagents →
    only 2 shown.

## LOE

- cli.py: ~160 lines (4 helpers + cmd_subagents + parser)
- tests: ~220 lines
- README: ~30 lines
- D100 in project.yaml: ~50 lines

Total: ~460 lines. Within the 500-line budget.

## Risks

1. **Slug algorithm mismatch**: if Claude Code updates its
   algorithm (unlikely — it's a fixed transformation), this
   breaks silently. Mitigation: the command returns a clear
   "no subagents found at <path>" error when the resolved dir
   doesn't exist, so operators can inspect and file a bug.
2. **Schema drift in meta.json / JSONL**: Claude Code's
   subagent format is not a public contract. All field reads
   are defensive (`dict.get()`), missing fields render as
   "unknown" or empty strings.
3. **Large subagent directories**: a long-running session can
   accumulate many agents. `--limit` bounds the output for
   humans; scripts can read `--format json` and slice.
4. **Read contention**: Claude Code writes to these files
   while we read. Each read is a single-pass iteration —
   partial lines at the tail are skipped via JSONDecodeError
   handling.

## GVP alignment

- G2 (loud-over-silent-failure): exposes subagent state that
  was previously only visible via raw file inspection.
- Complements D98 (current-tool-call in ls) — ls tells you
  "what tool is the worker in", this tells you "what's inside
  the Task subagent".

New decision `D100` records the slug algorithm + command
surface.
