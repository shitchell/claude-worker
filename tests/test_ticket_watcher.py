"""Tests for ticket_watcher PostToolUse hook.

Covers path matching, ticket info extraction, and target discovery.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_worker.ticket_watcher import (
    _extract_ticket_info,
    _find_notification_targets,
)


class TestExtractTicketInfo:
    """_extract_ticket_info must identify ticket files and extract metadata."""

    def test_index_md(self, tmp_path: Path):
        cwd = str(tmp_path)
        tickets = tmp_path / ".cwork" / "tickets"
        tickets.mkdir(parents=True)
        info = _extract_ticket_info(str(tickets / "INDEX.md"), cwd)
        assert info is not None
        assert info["action"] == "index updated"

    def test_ticket_md(self, tmp_path: Path):
        cwd = str(tmp_path)
        ticket_dir = tmp_path / ".cwork" / "tickets" / "005-review-delivery"
        ticket_dir.mkdir(parents=True)
        info = _extract_ticket_info(str(ticket_dir / "TICKET.md"), cwd)
        assert info is not None
        assert info["ticket_id"] == "005"
        assert info["slug"] == "review-delivery"
        assert "created or updated" in info["action"]

    def test_technical_md(self, tmp_path: Path):
        cwd = str(tmp_path)
        ticket_dir = tmp_path / ".cwork" / "tickets" / "007-move-dir"
        ticket_dir.mkdir(parents=True)
        info = _extract_ticket_info(str(ticket_dir / "TECHNICAL.md"), cwd)
        assert info is not None
        assert info["ticket_id"] == "007"
        assert "technical notes" in info["action"]

    def test_outside_tickets_returns_none(self, tmp_path: Path):
        cwd = str(tmp_path)
        info = _extract_ticket_info(str(tmp_path / "README.md"), cwd)
        assert info is None

    def test_cwork_but_not_tickets_returns_none(self, tmp_path: Path):
        cwd = str(tmp_path)
        pm_dir = tmp_path / ".cwork" / "pm"
        pm_dir.mkdir(parents=True)
        info = _extract_ticket_info(str(pm_dir / "LOG.md"), cwd)
        assert info is None


class TestFindNotificationTargets:
    """_find_notification_targets must find PM/TL workers with same CWD."""

    def test_no_sessions_returns_empty(self, tmp_path: Path, monkeypatch):
        from claude_worker import manager as cw_manager

        monkeypatch.setattr(cw_manager, "_load_sessions", lambda: {})
        targets = _find_notification_targets(str(tmp_path))
        assert targets == []

    def test_plain_worker_excluded(self, tmp_path: Path, monkeypatch):
        """Non-PM, non-TL workers should not be notification targets."""
        from claude_worker import manager as cw_manager

        sessions = {
            "plain-worker": {"cwd": str(tmp_path), "pm": False, "team_lead": False}
        }
        monkeypatch.setattr(cw_manager, "_load_sessions", lambda: sessions)
        targets = _find_notification_targets(str(tmp_path))
        assert targets == []
