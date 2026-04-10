"""Tests for project registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_worker.project_registry import (
    format_projects_table,
    load_registry,
    register_project,
    save_registry,
)


class TestRegisterProject:
    """register_project must add entries and update last_seen."""

    def test_register_new_project(self, tmp_path: Path):
        registry_path = tmp_path / "registry.yaml"
        register_project("/home/guy/project-a", path=registry_path)
        projects = load_registry(registry_path)
        assert len(projects) == 1
        assert projects[0]["slug"] == "project-a"
        assert projects[0]["path"] == "/home/guy/project-a"

    def test_idempotent_updates_last_seen(self, tmp_path: Path):
        registry_path = tmp_path / "registry.yaml"
        register_project("/home/guy/project-a", path=registry_path)
        first = load_registry(registry_path)[0]["last_seen"]

        import time

        time.sleep(0.01)
        register_project("/home/guy/project-a", path=registry_path)
        projects = load_registry(registry_path)
        assert len(projects) == 1  # still one entry
        assert projects[0]["last_seen"] >= first

    def test_multiple_projects(self, tmp_path: Path):
        registry_path = tmp_path / "registry.yaml"
        register_project("/home/guy/project-a", path=registry_path)
        register_project("/home/guy/project-b", path=registry_path)
        projects = load_registry(registry_path)
        assert len(projects) == 2
        slugs = [p["slug"] for p in projects]
        assert "project-a" in slugs
        assert "project-b" in slugs


class TestLoadRegistry:
    """load_registry handles missing/empty files."""

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert load_registry(tmp_path / "nonexistent.yaml") == []


class TestFormatProjectsTable:
    """format_projects_table produces readable output."""

    def test_empty_projects(self):
        output = format_projects_table([], [])
        assert "No projects registered" in output

    def test_project_with_workers(self):
        projects = [{"slug": "test", "path": "/tmp/test"}]
        workers = [
            {"name": "pm-test", "role": "pm", "cwd": "/tmp/test", "status": "waiting"}
        ]
        output = format_projects_table(projects, workers)
        assert "test" in output
        assert "PM" in output
