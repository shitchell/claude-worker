# AI-Driven Development Reference Guide

Living document. Update as patterns are discovered or disproven.
Started 2026-04-08 from observations across the claude-worker
development sessions.

## Core truth

AI agents are surprisingly capable implementers but surprisingly
bad at knowing their own limitations. Experimentation is the name
of the game — don't assume an AI agent will behave the way you
expect in a given context. Test, observe, adapt.

## What AI agents are good at

- **Bounded implementation tasks** with clear specs. "Add a --flag
  to this CLI that does X, with tests" is an ideal AI task.
- **Red-green testing.** Write failing test → implement until green
  is a natural AI workflow. The test provides unambiguous success
  criteria.
- **Mechanical refactors.** Rename X to Y everywhere, extract helper
  from duplicated code, add type hints. Grep-driven, low ambiguity.
- **Delegation.** An AI PM delegating to AI workers consistently
  outperforms an AI trying to do everything itself. Fresh context
  windows are an asset.
- **Following established patterns.** "Do this the same way we did
  it for feature Y" works well when the pattern is clear.

## What AI agents are bad at

- **Self-assessment.** They don't reliably know when they're wrong,
  when they're stuck, or when their approach is flawed. Build
  external checks (tests, validation, review) rather than trusting
  the agent's self-report.
- **Novel system investigation.** When an AI needs to understand an
  unfamiliar system (a new API, a library it hasn't used), it tends
  to guess based on training data rather than reading the actual
  source. Delegate investigation to a fresh agent with explicit
  "read these files, report what you find" instructions.
- **Shell escaping.** Heredocs with nested backticks, command
  substitution inside quotes, multi-line strings in CLI arguments —
  AI agents routinely get these wrong, and the failures are silent
  (the shell collapses the string, the command succeeds with wrong
  input). **Use file-based input** (`cat file.md | command`) for
  anything beyond simple strings.
- **Timing-sensitive test fixtures.** AI agents write tests that
  pass by coincidence of timing, then break under load or on slower
  machines. Common anti-pattern: monkey-patching a timeout to 0.2s
  for speed, which also disables the condition being tested.
- **Knowing when to stop.** AI agents will keep trying approaches
  indefinitely. Build explicit limits: the 3-reset rule (3 failed
  attempts → stop and report), context threshold notifications,
  time budgets.
- **Remembering across context windows.** Each session starts fresh.
  Don't rely on an AI "remembering" a decision from a prior session.
  Write it down (GVP library, handoff files, CLAUDE.md). If it's
  not written, it doesn't exist for the next session.

## Testing strategy for AI-driven projects

### The fundamental requirement

An AI agent must be able to **fully interface with and verify its
work on every component of the project**. If there's a component
where the only way to verify correctness is a human looking at a
screen, that's a testing gap that blocks AI-driven development.

### Testing checklist

For each project component, ask:

1. **Can an AI run the tests?** Is `pytest`/`npm test`/etc.
   available and does it run without human interaction?
2. **Can an AI verify the output?** Do tests produce machine-
   readable pass/fail, or do they require visual inspection?
3. **Can an AI test the full pipeline?** End-to-end tests that
   exercise the real system, not just unit tests with everything
   mocked.
4. **Can an AI test UI/TUI components?** If the project has a
   terminal UI, do we have tmux-based testing (send-keys +
   capture-pane)? If it has a web UI, do we have browser
   automation (Playwright, CDP)?
5. **Can an AI test against external services?** Stubs/mocks that
   mimic real service behavior without requiring credentials or
   network access.

### Stub harnesses

The claude-worker project's `stub_claude.py` is the canonical
example: a fake `claude` binary that mimics the real stream-json
protocol. It enables:
- End-to-end tests without a real Claude API call
- Controllable timing (`CLAUDE_STUB_DELAY_MS`)
- Deterministic session IDs (`CLAUDE_STUB_SESSION_ID`)
- Scripted responses (`CLAUDE_STUB_SCRIPT`)

Every external dependency should have a stub. The stub should:
- Accept the same CLI flags as the real tool (ignore unknown ones)
- Produce output in the same format
- Be controllable via environment variables
- Be fast (no real network, no real computation)

### Test anti-patterns to watch for

1. **Monkey-patching away the tested condition.** If you speed up
   a test by patching `THRESHOLD = 0.2`, ask: "does my speedup
   remove the very condition this test exercises?" If yes, the
   test is broken — it'll pass even if the production code is
   wrong.

2. **Synchronizing on the wrong signal.** In async systems, sync
   on the strongest completion signal, not the first visible
   output. Example: waiting for assistant text (mid-turn) vs.
   waiting for a result message (turn-end). The weak signal
   causes intermittent false-positive test passes.

3. **Background tasks without owners.** Fire-and-forget background
   tasks in tests create orphan processes, pollute later test runs,
   and produce stale notifications that waste context tokens. Every
   background task needs an explicit cleanup path.

4. **Tests that pass for the wrong reason.** If a test builds its
   own filter config (duplicating production logic), it tests the
   test, not the production code. Drive tests through the real
   production code path to catch regressions.

## Worker briefing best practices

When the TL briefs an implementation worker:

1. **Self-contained.** The worker has zero context from your
   conversation. Include everything it needs.
2. **Clear deliverable.** "Add X to Y, write tests in Z, run the
   suite, report the commit hash."
3. **GVP context.** "This relates to decision D-15; don't change
   the approach described there without flagging it."
4. **File-based for long briefs.** Write the brief to a file, pipe
   via stdin: `cat brief.md | claude-worker thread send worker-name`.
   Never use heredocs for briefs with backticks or special chars.
5. **Explicit boundaries.** "Only modify files in src/auth/ and
   tests/test_auth.py. Don't touch the API surface."
6. **Test-first instruction.** "Write a failing test first, then
   implement. The test must fail without your change and pass with
   it."

## Context window management

- **Monitor usage.** `claude-worker tokens NAME` and `read --context`
  show current context window usage.
- **Delegate before you're squeezed.** At 70% context, start
  thinking about what to delegate vs. what to keep.
- **At 80%, start wrap-up.** The context threshold notification
  (when implemented) will automate this. Until then, check manually.
- **Fresh workers are cheap.** Don't try to squeeze one more task
  into a nearly-full context. Launch a new worker.
- **The PM should never hit 90%.** If a PM is at 90%, it didn't
  delegate enough. The PM's context should be spent on decisions
  and coordination, not implementation detail.

## Patterns discovered in this project

These are documented in `~/.claude-session-analyses/patterns/` with
full context. Key ones for TLs:

- **fix-class-of-bug-not-instance** — when fixing a bug, grep for
  all sites with the same shape and fix them all.
- **test-fixture-masks-real-bug** — see anti-pattern #1 above.
- **try-before-look** — when something fails unexpectedly, inspect
  the current state before trying a different approach. `print()`
  is cheaper than another attempt.
- **publish-dont-discover** — when a test needs a value the code
  already has (a PID, a UUID), have the code publish it to a file.
  Don't reverse-engineer it from /proc or parsing logs.
- **delegation-beats-self-discovery** — for investigating unfamiliar
  systems, a fresh-context agent outperforms a loaded-context agent
  trying to reason from memory.

## Open questions (update as we learn)

- How well do AI agents handle multi-step TUI interactions via
  tmux send-keys? (Seems to work but needs more testing)
- Can AI agents reliably debug race conditions, or do they need
  explicit reproduction scripts?
- What's the optimal worker brief length? Too short = ambiguity,
  too long = the agent ignores parts of it.
- How should we handle AI agents that confidently produce wrong
  code? (Current answer: tests catch it, but what if the test
  is also wrong?)
