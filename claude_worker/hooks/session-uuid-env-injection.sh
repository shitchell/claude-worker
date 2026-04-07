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

set -u

input="$(cat)"

# UUID regex: 8-4-4-4-12 lowercase hex with hyphens.
session_id="$(
    printf '%s' "$input" \
        | grep -oE '"session_id":"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"' \
        | head -n1 \
        | sed -E 's/^"session_id":"//;s/"$//'
)"

if [[ -z "$session_id" ]]; then
    # No session_id in payload — nothing to export. Exit quietly so we
    # don't block session startup.
    exit 0
fi

if [[ -z "${CLAUDE_ENV_FILE:-}" ]]; then
    # Older Claude Code or a non-SessionStart context. Nothing to write to.
    exit 0
fi

printf 'export CLAUDE_SESSION_UUID=%s\n' "$session_id" >> "$CLAUDE_ENV_FILE"
