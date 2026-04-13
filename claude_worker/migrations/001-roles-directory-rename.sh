#!/usr/bin/env bash
# Migration 001: Rename identity runtime dirs to roles/
# Moves .cwork/pm/ → .cwork/roles/pm/
# Moves .cwork/technical-lead/ → .cwork/roles/tl/
set -euo pipefail

PROJECT="$1"
CWORK="$PROJECT/.cwork"

# Idempotent: skip if already done
if [[ -d "$CWORK/roles/pm" ]] && [[ ! -d "$CWORK/pm/handoffs" ]]; then
    exit 0
fi
if [[ -d "$CWORK/roles/tl" ]] && [[ ! -d "$CWORK/technical-lead/handoffs" ]]; then
    exit 0
fi

# Move pm/ → roles/pm/ (if exists and has identity content)
if [[ -d "$CWORK/pm" ]] && [[ -d "$CWORK/pm/handoffs" ]]; then
    mkdir -p "$CWORK/roles"
    mv "$CWORK/pm" "$CWORK/roles/pm"
fi

# Move technical-lead/ → roles/tl/ (if exists and has identity content)
if [[ -d "$CWORK/technical-lead" ]] && [[ -d "$CWORK/technical-lead/handoffs" ]]; then
    mkdir -p "$CWORK/roles"
    mv "$CWORK/technical-lead" "$CWORK/roles/tl"
fi
