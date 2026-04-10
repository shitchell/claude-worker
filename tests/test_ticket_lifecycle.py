"""Tests for ticket lifecycle validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_worker.ticket_lifecycle import validate_ticket_lifecycle


class TestValidateTicketLifecycle:
    """validate_ticket_lifecycle must detect structural gaps in done tickets."""

    def test_no_tickets_returns_empty(self, tmp_path: Path):
        assert validate_ticket_lifecycle(str(tmp_path)) == []

    def test_done_ticket_without_technical_md(self, tmp_path: Path):
        """Done ticket missing TECHNICAL.md → warning."""
        tickets = tmp_path / ".cwork" / "tickets"
        tickets.mkdir(parents=True)
        (tickets / "INDEX.md").write_text(
            "| ID | Slug | Status |\n"
            "|----|------|--------|\n"
            "| 001 | fix-bug | done |\n"
        )
        ticket_dir = tickets / "001-fix-bug"
        ticket_dir.mkdir()
        (ticket_dir / "TICKET.md").write_text("# Fix bug\nImplement the fix")

        warnings = validate_ticket_lifecycle(str(tmp_path))
        assert any("TECHNICAL.md" in w for w in warnings)

    def test_done_ticket_with_technical_md_clean(self, tmp_path: Path):
        """Done ticket with TECHNICAL.md → no warning for that check."""
        tickets = tmp_path / ".cwork" / "tickets"
        tickets.mkdir(parents=True)
        (tickets / "INDEX.md").write_text(
            "| ID | Slug | Status |\n"
            "|----|------|--------|\n"
            "| 001 | fix-bug | done |\n"
        )
        ticket_dir = tickets / "001-fix-bug"
        ticket_dir.mkdir()
        (ticket_dir / "TICKET.md").write_text("# Fix bug\nImplement the fix")
        (ticket_dir / "TECHNICAL.md").write_text("# Technical notes")

        warnings = validate_ticket_lifecycle(str(tmp_path))
        technical_warnings = [w for w in warnings if "TECHNICAL.md" in w]
        assert len(technical_warnings) == 0

    def test_todo_ticket_not_checked(self, tmp_path: Path):
        """Todo tickets should not trigger warnings."""
        tickets = tmp_path / ".cwork" / "tickets"
        tickets.mkdir(parents=True)
        (tickets / "INDEX.md").write_text(
            "| ID | Slug | Status |\n"
            "|----|------|--------|\n"
            "| 001 | pending-work | todo |\n"
        )
        warnings = validate_ticket_lifecycle(str(tmp_path))
        assert warnings == []
