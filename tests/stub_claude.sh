#!/usr/bin/env bash
#
# stub_claude.sh — wrapper that invokes the Python stub, passing all
# arguments through so unknown flags (like --replay-user-messages) are
# accepted (and ignored) without error.
#
# The stub itself doesn't parse flags — it only speaks the stream-json
# protocol on stdin/stdout. Flags are consumed by this wrapper and
# dropped, mimicking a real claude binary that accepts its own CLI
# surface.
#
# Tests set CLAUDE_WORKER_CLAUDE_BIN to the absolute path of this
# script, and manager.py's _resolve_claude_bin() picks it up.

exec python3 "$(dirname "$0")/stub_claude.py" "$@"
