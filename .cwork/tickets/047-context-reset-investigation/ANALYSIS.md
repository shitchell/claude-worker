# Session Analysis: cw-lead session 01 (1ac90ea7)

**CORRECTION (2026-04-11)**: This analysis originally misidentified
system/init messages as compaction events. system/init fires every turn
in -p stream-json mode. The real compaction indicator is compact_boundary.
The old TL session had 0 actual compactions. The 151 init events were
normal per-turn behavior.

**Log**: `/tmp/claude-workers/1000/cw-lead.20260411T015340.1ac90ea7/log`
**Size**: 27 MB, 4025 lines
**Session ID**: 1ac90ea7-db69-4f1f-9703-4012dc03db65

## Executive Summary

The TL's first session suffered unbounded context growth caused by
direct implementation instead of delegation. 0 actual compactions
occurred (the 151 per-turn system/init messages were misidentified as
compactions in the original analysis). Context grew monotonically from
26K to 757K tokens without ever being compacted. 89% of context content
was tool output (file reads, test results, diffs). Only 4 of 1342
tool calls were delegations.

## Token Usage

| Metric | Value |
|--------|-------|
| Context window (final) | 757K / 1M (75.7%) |
| Session cache_read total | 578,685,032 |
| Session cache_creation total | 5,323,865 |
| Session output total | 41,372 |
| Unique API calls | 1,392 |
| Avg context per API call | ~416K |

## system/init Analysis (NOT compactions)

| Metric | Value |
|--------|-------|
| system/init messages | 152 (normal per-turn behavior in -p stream-json mode) |
| compact_boundary messages | 0 (no actual compactions occurred) |
| Unique session IDs | 1 |

**Note**: system/init fires every turn in `-p` stream-json mode. These
are NOT compaction events. The real compaction indicator is
`compact_boundary` (type: system, subtype: compact_boundary). This
session had zero actual compactions.

### system/init timing

init messages appeared throughout the log as expected for per-turn
protocol behavior:
- First init: line 1 (session start)
- Subsequent inits: one per turn (normal -p stream-json behavior)

### Context grew without compaction

Context was never compacted — it grew monotonically because each turn
added tool output without any compaction to reduce it:

| Turn range | Approx ctx | Growth pattern |
|------------|-----------|----------------|
| Start | 25,972 | Initial |
| Early | ~80K-120K | Rapid (large file reads) |
| Mid | ~400K-500K | Steady (tool output accumulation) |
| Late | ~700K-757K | Continued growth, no compaction |

The context grew continuously because each turn's tool output was
retained in full. Without compaction ever firing, there was no
mechanism to reduce the accumulated context.

## Content Breakdown

| Content type | Size | % |
|--------------|------|---|
| Tool results (user replay msgs) | 1,088,222 chars | 89.0% |
| Assistant text output | 109,268 chars | 8.9% |
| Thinking blocks | 25,636 chars | 2.1% |
| **Total** | **1,223,126 chars** | |

**89% of all content was tool output.** The TL was reading files,
running tests, and reading diffs — all of which dump large output
into context.

## Tool Usage

| Tool | Calls | Avg input (chars) |
|------|-------|-------------------|
| Bash | 466 | 470 |
| Read | 439 | 119 |
| Edit | 234 | 1,317 |
| Grep | 135 | 164 |
| Write | 63 | 3,909 |
| Agent | 4 | 932 |
| Skill | 1 | 30 |
| **Total** | **1,342** | |

### Delegation rate

- **4 Agent calls out of 1,342 total** (0.3%)
- 80 task_started events (from PM side, not TL delegations)
- 234 Edit + 63 Write = **297 direct code modifications**
- The TL was functioning as an implementer, not a coordinator

## Message Statistics

| Type | Count |
|------|-------|
| assistant | 2,019 |
| user (replay) | 1,430 |
| result/success | 151 |
| system/init | 152 |
| system/task_started | 80 |
| system/task_progress | 88 |
| system/task_notification | 80 |
| rate_limit_event | 23 |

User messages: 1,430 (all subtype `<none>` — replay messages, no
`user-input` subtype). 283 messages over 1K chars, 16 over 10K chars.

## The Context Growth Spiral (mechanism)

**Note**: The original analysis called this a "compaction death spiral."
It was actually an unbounded context growth spiral — no compactions
occurred. The system/init messages were normal per-turn protocol
behavior, not compaction events.

1. **Turn starts**: TL reads files (Read), runs commands (Bash),
   edits code (Edit). Each tool call dumps output into context.
2. **Context grows**: A single turn adds 5-15K tokens of tool output.
   With no compaction occurring, context accumulates monotonically.
3. **No compaction fires**: Despite context growing from 26K to 757K,
   no compact_boundary event was recorded. The context was never
   reduced.
4. **Identity diluted**: As context grew, the TL's behavioral guidance
   (delegate, don't implement directly) became a smaller fraction of
   the total context. The TL reverted to default behavior: direct
   implementation.
5. **Repeat**: Next turn, same pattern. Context grows further,
   identity guidance becomes proportionally smaller.

The fundamental issue: **unbounded context growth from direct
implementation**. The TL was reading files, running tests, and editing
code directly instead of delegating. Each tool call added output to
context with no mechanism to reduce it.

## Key Answers

### 1. Token usage pattern

Average ~416K context per API call. Context grew from 26K to 757K
over 151 turns. Each turn added 5-15K tokens. No compaction ever
occurred, so context grew monotonically without any reduction.

### 2. Tool output vs coordination

89% tool output, 8.9% text, 2.1% thinking. The session was almost
entirely tool I/O. Very little coordination or reasoning text — the
TL was in "do mode," not "think mode."

### 3. Were there compactions?

No. Zero compact_boundary events in the log. The 151 system/init
messages were normal per-turn behavior in -p stream-json mode, not
compaction indicators. Context grew from 26K to 757K without ever
being compacted.

### 4. Was the TL delegating?

No. 4 Agent calls out of 1,342 tool calls (0.3%). 297 direct code
modifications (234 Edit + 63 Write). The TL was doing 100% direct
implementation — exactly the anti-pattern the identity is supposed
to prevent.

## Recommendations

- **Hard context limit**: At 70-80%, refuse new tool calls and
  force wrap-up. Don't suggest — enforce. (See ticket #054.)
- **Tool output budgets**: Cap Read/Bash output at N tokens. If
  output exceeds the budget, truncate with a note.
- **Delegation enforcement**: Count direct Edit/Write/Bash calls.
  If an identity worker exceeds a threshold, inject a warning.
- **Compaction awareness**: The real problem was unbounded context
  growth from direct implementation, not compaction. If compaction
  does occur (compact_boundary events), detect and handle it — but
  preventing context growth through delegation is the primary fix.

## Lessons

- **system/init is NOT a compaction indicator.** It fires every turn
  in -p stream-json mode. The real compaction signal is
  compact_boundary (type: system, subtype: compact_boundary).
- **Hook-based detection is correct.** The compaction_detector.py
  hook uses SessionStart matcher_value="compact", which is the right
  mechanism for real-time detection.
- **Log analysis must use compact_boundary.** Any post-hoc analysis
  counting compactions must look for compact_boundary, never init.
