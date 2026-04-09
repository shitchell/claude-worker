# 027: Technical Notes — Send Broadcast

## Design

### Flag on send, not a new subcommand

`--broadcast` on `cmd_send` is the right fit — it composes with
existing send flags (--queue, --show-response, --chat, --dry-run,
--verbose). A separate subcommand would duplicate all of those.

### Filter reuse

Extract _get_worker_info + filter logic from cmd_list into a shared
helper `_find_matching_workers(args)` that both cmd_list and
cmd_send --broadcast can call.

### Modes

**Fire-and-forget (default):** write to each target's FIFO without
waiting. Skip the status gate — broadcast is "best effort to all
matching workers." Workers that are busy get the message queued in
their FIFO pipe buffer.

**--show-response:** wait for each target sequentially, print
responses labeled by worker name. This reuses `_wait_for_turn` per
target.

**--queue:** composes naturally — each target gets a unique queue ID,
and we wait for each tagged response.

### Self-exclusion

Same pattern as ticket_watcher: walk PID ancestry to find the
caller's claude-pid, exclude matching worker from targets.

### Implementation

1. Extract `_collect_filtered_workers(args)` from cmd_list
2. In cmd_send, when --broadcast:
   a. Collect targets via _collect_filtered_workers
   b. Self-exclude via _find_worker_by_ancestry
   c. For each target: write message to FIFO
   d. If --show-response: wait + print per target
   e. Print summary: "Sent to N workers: name1, name2, ..."

### LOE

- Refactor cmd_list: ~10 lines (extract filter helper)
- cmd_send broadcast path: ~40 lines
- Argparse: ~15 lines (add filter flags to send when --broadcast)
- Tests: ~30 lines
- Total: ~95 lines
