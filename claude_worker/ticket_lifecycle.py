"""Ticket lifecycle validation.

Checks ticket directories for structural compliance:
1. Done tickets should have TECHNICAL.md (planning wasn't skipped)
2. Done tickets should have corresponding D<N> in project.yaml

Called as a periodic check or from cmd_stop. Returns a list of
warning strings.
"""

from __future__ import annotations

import re
from pathlib import Path


def validate_ticket_lifecycle(cwd: str) -> list[str]:
    """Validate ticket directories against lifecycle expectations.

    Returns a list of warning strings. Empty list = all clean.
    """
    warnings = []
    tickets_dir = Path(cwd) / ".cwork" / "tickets"
    index_file = tickets_dir / "INDEX.md"

    if not index_file.exists():
        return []

    # Parse INDEX.md for done tickets
    done_tickets: list[tuple[str, str]] = []  # (id, slug)
    try:
        for line in index_file.read_text().splitlines():
            if not line.startswith("|"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5:
                continue
            ticket_id = parts[1]
            slug = parts[2]
            status = parts[3]
            if status == "done" and ticket_id.isdigit():
                done_tickets.append((ticket_id, slug))
    except OSError:
        return []

    # Check each done ticket
    for ticket_id, slug in done_tickets:
        ticket_dir = tickets_dir / f"{ticket_id}-{slug}"
        if not ticket_dir.exists():
            continue

        # Check 1: TECHNICAL.md exists
        if not (ticket_dir / "TECHNICAL.md").exists():
            # Exempt: identity/docs tickets that don't need technical notes
            if (ticket_dir / "TICKET.md").exists():
                ticket_text = (ticket_dir / "TICKET.md").read_text()
                if (
                    "identity" not in ticket_text.lower()
                    and "docs" not in ticket_text.lower()
                ):
                    warnings.append(
                        f"Ticket #{ticket_id} ({slug}) is done but has no "
                        f"TECHNICAL.md — was planning skipped?"
                    )

    # Check: do done implementation tickets have D<N> refs?
    gvp_file = Path(cwd) / ".gvp" / "library" / "project.yaml"
    if gvp_file.exists():
        try:
            gvp_text = gvp_file.read_text()
            for ticket_id, slug in done_tickets:
                # Look for the ticket ID referenced in a decision's origin
                pattern = f"#{ticket_id}"
                if pattern not in gvp_text:
                    # Not every ticket needs a decision (research, docs, etc.)
                    # Only warn for tickets that look like implementations
                    ticket_md = tickets_dir / f"{ticket_id}-{slug}" / "TICKET.md"
                    if ticket_md.exists():
                        text = ticket_md.read_text().lower()
                        if any(
                            kw in text
                            for kw in ["implement", "add", "fix", "feature", "refactor"]
                        ):
                            warnings.append(
                                f"Ticket #{ticket_id} ({slug}) is done but "
                                f"has no D<N> referencing it in project.yaml"
                            )
        except OSError:
            pass

    return warnings
