#!/usr/bin/env bash
#
# session-uuid-env-injection.sh
#
# Claude Code SessionStart hook that injects CLAUDE_SESSION_UUID into the
# environment of subsequent Bash tool calls for the session.
#
# Reads the SessionStart JSON payload from stdin, extracts session_id, and
# appends `export CLAUDE_SESSION_UUID=<id>` to the file pointed to by
# $CLAUDE_ENV_FILE (which Claude Code sources for Bash tool calls).
#
# This is useful independently of claude-worker — any tool that wants to
# identify the calling Claude session can read $CLAUDE_SESSION_UUID.
#
# Pure bash: no jq or external JSON parser required. The SessionStart
# payload embeds session_id as a UUID string, which is easy to extract
# with a regex.
#
# Debugging: set CLAUDE_WORKER_HOOK_DEBUG=1 in the environment to see
# diagnostic output on stderr.
#

set -u

_debug() {
    if [[ "${CLAUDE_WORKER_HOOK_DEBUG:-}" == "1" ]]; then
        printf '[session-uuid-env-injection] %s\n' "$*" >&2
    fi
}

input="$(cat)"

# UUID regex: RFC 4122 shape 8-4-4-4-12, case-insensitive. The grep pattern
# below accepts both uppercase and lowercase hex because RFC 4122 allows
# either and pre-2026 Claude Code is known to emit lowercase.
#
# Whitespace-tolerant: claude emits compact JSON ("key":"value") but
# pretty-printed payloads with spaces after the colon ("key": "value")
# should also parse. [[:space:]]* handles both.
#
# Extraction works in three stages:
#   1. grep finds all matching "session_id"..."UUID" substrings
#   2. head -n1 takes only the first (top-of-payload) occurrence
#   3. sed strips the "session_id":..."..." wrapper to leave the bare UUID
session_id="$(
    printf '%s' "$input" \
        | grep -oE '"session_id"[[:space:]]*:[[:space:]]*"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"' \
        | head -n1 \
        | sed -E 's/^"session_id"[[:space:]]*:[[:space:]]*"//;s/"$//'
)"

if [[ -z "$session_id" ]]; then
    _debug "no session_id found in payload — exiting without writing"
    exit 0
fi

_debug "extracted session_id: $session_id"

if [[ -z "${CLAUDE_ENV_FILE:-}" ]]; then
    _debug "CLAUDE_ENV_FILE is unset — nothing to write to"
    exit 0
fi

# Writability guard: refuse to append to anything that isn't a regular
# writable file. Prevents a malicious env-setter from redirecting the
# export to a device file, a directory, a symlink chain, or a path we
# can't write. Claude Code creates CLAUDE_ENV_FILE as a regular file
# so any departure from that is suspicious.
if [[ ! -f "$CLAUDE_ENV_FILE" ]]; then
    _debug "CLAUDE_ENV_FILE ($CLAUDE_ENV_FILE) is not a regular file — refusing to write"
    exit 0
fi
if [[ ! -w "$CLAUDE_ENV_FILE" ]]; then
    _debug "CLAUDE_ENV_FILE ($CLAUDE_ENV_FILE) is not writable — refusing to write"
    exit 0
fi

printf 'export CLAUDE_SESSION_UUID=%s\n' "$session_id" >> "$CLAUDE_ENV_FILE"
_debug "wrote export line to $CLAUDE_ENV_FILE"
