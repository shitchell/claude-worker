# 031: Technical Notes — REPL Verbose Output

## Approach: --verbose flag on repl subcommand (option a)

### Why not (b) /verbose toggle
Would require rebuilding the RenderConfig mid-session. The config is
built once at REPL start and shared across all turns. Adding a toggle
would mean tracking state and rebuilding. Overkill — users know
upfront if they want verbose.

### Why not (c) always verbose
Too noisy for the default REPL experience. Tool calls dominate the
output and make it hard to follow the conversation.

### Implementation
Line 3562: `hidden = {"timestamps", "metadata", "thinking", "tools"}`
When --verbose: `hidden = {"timestamps", "metadata", "progress",
"file-history-snapshot", "last-prompt"}` (same as read --verbose),
no show_only filter.

~5 lines changed + argparse flag.

### LOE
~10 lines total (flag + conditional hidden/show_only).
