# 028: Technical Assessment — Guaranteed Inter-Agent Message Delivery

## The Core Problem

`send --show-response` waits for ONE turn boundary and returns. When
the recipient needs multiple turns (delegates to its own TL, runs a
subagent, waits for an external response), the sender gets the first
ack and loses the actual answer. The answer sits in the recipient's
log, undelivered.

Two consumer types to support:
- **Workers**: have a manager, FIFO, hooks, and a log file
- **Plain sessions**: `claude` in a terminal, no manager infrastructure

## Mechanism Assessment

### 1. Callback Pattern

**How it works:** Sender includes a reply-to address in the message
(e.g., `[reply-to:sender-worker-name]`). Recipient, when it has the
final answer, runs `claude-worker send <sender-name> "answer"`.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Reliability | HIGH | Recipient explicitly sends the answer back — no timing dependency |
| Latency | Variable | Depends on when the recipient finishes — could be minutes |
| Complexity | LOW | No infrastructure changes — just a convention in the message |
| Workers | YES | Recipient runs `claude-worker send` to reply |
| Plain sessions | PARTIAL | Plain session recipient would need claude-worker installed to reply |
| Multi-turn | SOLVED | Sender doesn't wait — callback arrives whenever the answer is ready |
| Claude Code changes | NONE | Pure convention |

**Key insight:** This is the most natural pattern. The sender says
"when you have the answer, send it to me at X." The recipient follows
through. It's how humans email — you don't wait on the phone while
they research.

**Challenge:** The sender must be ready to *receive* the callback.
For workers, this means the callback arrives as a new user message in
their FIFO. For plain sessions, the callback has no delivery path
(no FIFO). This is the fundamental asymmetry.

**Verdict:** Best for worker-to-worker. Needs augmentation for plain
sessions.

### 2. Message Queue with Polling

**How it works:** Persistent queue files at
`~/.cwork/queues/<worker-name>/` (one file per pending message). The
manager's FIFO thread checks for new queue files on each poll cycle
and injects them as synthetic user messages.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Reliability | HIGH | Persistent files survive crashes |
| Latency | 1-5s | Depends on poll interval |
| Complexity | MEDIUM | Queue file format, ordering, dedup, cleanup |
| Workers | YES | Manager polls the queue directory |
| Plain sessions | NO | No manager to poll |
| Multi-turn | SOLVED | Callback writes to queue; manager delivers on next poll |
| Claude Code changes | NONE | File-based, uses existing FIFO injection |

**How it composes with callbacks:** The callback pattern (#1) writes
to the message queue instead of calling `claude-worker send`. The
manager delivers it on the next poll. This gives callbacks a
persistent delivery path that survives sender busy states.

**Verdict:** Strong complement to callbacks. Solves the "sender is
busy" problem. Doesn't help plain sessions.

### 3. MCP Server

**How it works:** A shared MCP (Model Context Protocol) server that
acts as a message broker. Agents connect as MCP clients and use tools
like `send_message(to, content)` and `check_messages()`. Claude Code
natively supports MCP.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Reliability | HIGH | Server-mediated, persistent storage |
| Latency | <1s | Real-time via MCP notifications |
| Complexity | HIGH | Need an MCP server implementation, connection management |
| Workers | YES | MCP configured in worker settings.json |
| Plain sessions | YES | MCP configured in user settings.json |
| Multi-turn | SOLVED | Messages persist in server until consumed |
| Claude Code changes | NONE | MCP is a standard Claude Code feature |

**Key advantage:** This is the ONLY mechanism that naturally supports
plain sessions. Both workers and plain sessions can connect to the
same MCP server and exchange messages. The MCP server persists
messages until the recipient reads them.

**Challenge:** Building an MCP server is significant work — it's a
separate process that needs lifecycle management, storage, and the
MCP protocol implementation. However, there are MCP server frameworks
(Python `mcp` package) that reduce boilerplate.

**Verdict:** Most complete solution but highest implementation cost.
Worth it if cross-context delivery is a hard requirement.

### 4. Hooks-Based Injection

**How it works:** A Stop or PostToolUse hook checks a pending-messages
directory for files addressed to the current session. If found, the
hook echoes the message content to stdout (Claude sees it as hook
output).

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Reliability | MEDIUM | Hook fires on each turn — message waits until next turn |
| Latency | Variable | Up to one full turn delay |
| Complexity | LOW | Reuses existing hook infrastructure |
| Workers | YES | Worker hooks wired via settings.json |
| Plain sessions | PARTIAL | Need hook installed in user settings — possible but invasive |
| Multi-turn | SOLVED | Message sits in pending dir until hook delivers it |
| Claude Code changes | NONE | Uses existing hook system |

**How it works for delivery:** Sender writes a file to
`~/.cwork/messages/<recipient-session-id>/msg-<timestamp>.txt`. The
recipient's Stop hook checks this directory after each turn. If files
exist, echoes them to stdout and deletes them.

**Challenge:** Recipient must be actively working (hooks fire on
tool use / stop, not when idle). If the recipient is idle, the
message waits until someone sends them a message to trigger a turn.

**Verdict:** Good for "eventually delivered" but not "delivered now."
Adequate for callbacks where latency tolerance is high.

### 5. `send --wait-turns N`

**How it works:** Simple extension — `_wait_for_turn` loops N times
instead of returning after the first turn boundary.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Reliability | LOW | How do you know N? If N is too small, you miss the answer |
| Latency | Good | Returns as soon as the Nth turn completes |
| Complexity | TRIVIAL | 3-line change to _wait_for_turn |
| Workers | YES | Sender blocks longer |
| Plain sessions | NO | Only works for sender, not recipient |
| Multi-turn | PARTIAL | Only if you guess N correctly |
| Claude Code changes | NONE | |

**Verdict:** Too fragile. The sender doesn't know how many turns
the recipient needs. N=2 might work for "delegate then answer" but
fails for "delegate, wait for TL, get answer, synthesize, respond"
(4+ turns).

### 6. Pub/Sub with Topics

**How it works:** Agents subscribe to topics (e.g., project UUIDs,
role channels). Messages published to a topic are delivered to all
subscribers.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Reliability | HIGH | Persistent subscriptions with message store |
| Latency | Variable | Depends on delivery mechanism (MCP, polling, hooks) |
| Complexity | HIGH | Subscription management, topic routing, persistence |
| Workers | YES | Via queue polling or MCP |
| Plain sessions | PARTIAL | Via MCP or hooks |
| Multi-turn | SOLVED | Pub/sub is inherently asynchronous |
| Claude Code changes | NONE | |

**Verdict:** Over-engineered for the current use case. Pub/sub is
for many-to-many; we need point-to-point (one sender, one recipient).
Could be revisited when there are 10+ agents needing coordination.

---

## Recommendation: Callback + Message Queue (with MCP as future upgrade)

### Phase 1: Callback Convention + Queue (immediate, ~80 LOE)

**The callback pattern solves the multi-turn problem.** The sender
doesn't wait for the full answer — it asks the recipient to call back.
The message queue provides a persistent delivery path for callbacks.

Implementation:
1. **Message convention:** Senders include `[reply-to:<worker-name>]`
   in messages that may require multi-turn work.
2. **Queue directory:** `~/.cwork/queues/<worker-name>/` — one JSONL
   file per pending message.
3. **Manager queue drain:** Add a check in `fifo_to_stdin_body` —
   on each `select()` timeout, scan the queue dir for new files and
   inject them as synthetic user messages.
4. **`claude-worker reply`:** New subcommand that writes a message to
   a worker's queue directory (no FIFO needed, works even if the
   recipient is busy).
5. **Identity guidance:** PM and TL identities instruct: "When you
   receive a `[reply-to:X]` message that requires multi-turn work,
   complete the work and then `claude-worker reply X 'answer'`."

This covers worker-to-worker fully. Plain sessions are left for
Phase 2.

### Phase 2: MCP Message Broker (future, ~300 LOE)

When cross-context delivery becomes a hard requirement (plain sessions
needing to receive messages from workers), build a lightweight MCP
server:
- Exposes `send_message(to, content)` and `get_messages()` tools
- Persistent message store at `~/.cwork/messages/`
- Auto-configured in worker settings.json and optionally in user
  settings
- Replaces the queue directory with proper message routing

### Why not hooks alone?

Hooks require the recipient to be actively working. An idle agent
doesn't receive hook-delivered messages until its next turn. The
queue + manager poll approach delivers messages even when the agent
is idle (the manager injects them as user messages, triggering a
new turn).

### Why not --wait-turns?

Too fragile — the sender can't predict how many turns the recipient
needs. The callback pattern decouples the sender from the recipient's
turn count entirely.

## Summary

| Mechanism | Worker→Worker | Worker→Plain | Complexity | Recommendation |
|-----------|---------------|--------------|------------|----------------|
| Callback + Queue | FULL | NO | LOW-MED | **Phase 1** |
| MCP Server | FULL | FULL | HIGH | **Phase 2** |
| Hooks injection | PARTIAL | PARTIAL | LOW | Supplement |
| --wait-turns | PARTIAL | NO | TRIVIAL | Reject |
| Pub/sub | FULL | PARTIAL | HIGH | Defer |

**Decision needed from PM:** Approve Phase 1 (callback + queue) for
implementation? Or skip to MCP? Or a different combination?
