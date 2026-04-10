"""Tests for token tracking CSV (append, read, stats)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from claude_worker.token_tracking import (
    CSV_COLUMNS,
    append_session_row,
    format_stats,
    read_summary,
)


class TestAppendSessionRow:
    """append_session_row must create CSV with headers and append rows."""

    def test_creates_csv_with_headers(self, tmp_path: Path):
        csv_path = tmp_path / "summary.csv"
        append_session_row(
            date="2026-04-10",
            worker_name="pm-test",
            identity="pm",
            project="/home/guy/project",
            task_description="test session",
            input_tokens=100,
            output_tokens=200,
            cache_read=5000,
            cache_create=1000,
            duration_minutes=30.0,
            estimated_cost_usd=1.50,
            session_id="abc123",
            analysis_file="2026-04-10-test.md",
            csv_path=csv_path,
        )
        assert csv_path.exists()
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["worker_name"] == "pm-test"
        assert rows[0]["estimated_cost_usd"] == "1.5"

    def test_appends_without_duplicate_headers(self, tmp_path: Path):
        csv_path = tmp_path / "summary.csv"
        for i in range(3):
            append_session_row(
                date=f"2026-04-{10+i}",
                worker_name=f"worker-{i}",
                identity="worker",
                project="/tmp",
                task_description=f"task {i}",
                input_tokens=i * 100,
                output_tokens=i * 50,
                cache_read=0,
                cache_create=0,
                duration_minutes=10.0,
                estimated_cost_usd=0.5,
                session_id=f"sess-{i}",
                analysis_file=f"analysis-{i}.md",
                csv_path=csv_path,
            )
        with open(csv_path) as f:
            lines = f.readlines()
        # 1 header + 3 data rows
        assert len(lines) == 4


class TestReadSummary:
    """read_summary must return list of dicts from the CSV."""

    def test_reads_existing_csv(self, tmp_path: Path):
        csv_path = tmp_path / "summary.csv"
        append_session_row(
            date="2026-04-10",
            worker_name="test",
            identity="pm",
            project="/tmp",
            task_description="t",
            input_tokens=0,
            output_tokens=0,
            cache_read=0,
            cache_create=0,
            duration_minutes=0,
            estimated_cost_usd=0,
            session_id="s",
            analysis_file="a.md",
            csv_path=csv_path,
        )
        rows = read_summary(csv_path)
        assert len(rows) == 1
        assert rows[0]["identity"] == "pm"

    def test_missing_csv_returns_empty(self, tmp_path: Path):
        rows = read_summary(tmp_path / "nonexistent.csv")
        assert rows == []


class TestFormatStats:
    """format_stats must produce readable summaries."""

    def test_empty_rows(self):
        assert "No session data" in format_stats([])

    def test_aggregates_correctly(self):
        rows = [
            {
                "identity": "pm",
                "project": "/a",
                "estimated_cost_usd": "10.0",
                "input_tokens": "100",
                "output_tokens": "50",
                "cache_read": "1000",
                "cache_create": "500",
                "duration_minutes": "30",
            },
            {
                "identity": "tl",
                "project": "/b",
                "estimated_cost_usd": "5.0",
                "input_tokens": "200",
                "output_tokens": "100",
                "cache_read": "2000",
                "cache_create": "600",
                "duration_minutes": "15",
            },
        ]
        output = format_stats(rows)
        assert "Sessions: 2" in output
        assert "$15.00" in output  # total cost
        assert "pm:" in output
        assert "tl:" in output
