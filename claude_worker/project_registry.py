"""Project registry for claude-worker.

Maintains ~/.cwork/projects/registry.yaml linking all projects that
have been started with an identity worker (--pm, --team-lead, --identity).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

REGISTRY_PATH: Path = Path.home() / ".cwork" / "projects" / "registry.yaml"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_gvp_id(cwd: str) -> str | None:
    """Read project_id from <cwd>/.gvp/config.yaml if it exists."""
    config = Path(cwd) / ".gvp" / "config.yaml"
    if not config.exists():
        return None
    try:
        import yaml

        data = yaml.safe_load(config.read_text())
        return data.get("project_id") if data else None
    except Exception:
        return None


def load_registry(path: Path | None = None) -> list[dict]:
    """Load the project registry. Returns [] if missing."""
    p = path or REGISTRY_PATH
    if not p.exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(p.read_text())
        if isinstance(data, dict):
            return data.get("projects", [])
        return []
    except Exception:
        return []


def save_registry(projects: list[dict], path: Path | None = None) -> None:
    """Save the project registry."""
    p = path or REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml

        content = yaml.dump(
            {"projects": projects}, default_flow_style=False, sort_keys=False
        )
        p.write_text(content)
    except Exception:
        pass


def register_project(cwd: str, path: Path | None = None) -> None:
    """Register a project CWD in the registry. Idempotent."""
    resolved = os.path.realpath(cwd)
    projects = load_registry(path)

    # Update last_seen if already registered
    for proj in projects:
        if os.path.realpath(proj.get("path", "")) == resolved:
            proj["last_seen"] = _now_iso()
            save_registry(projects, path)
            return

    # New registration
    slug = Path(resolved).name
    projects.append(
        {
            "slug": slug,
            "path": resolved,
            "gvp_id": _read_gvp_id(resolved),
            "registered": _now_iso(),
            "last_seen": _now_iso(),
        }
    )
    save_registry(projects, path)


def format_projects_table(projects: list[dict], workers: list[dict]) -> str:
    """Format registered projects with active worker info.

    workers: list of dicts from _collect_filtered_workers (with name, role, cwd).
    """
    if not projects:
        return "No projects registered. Start a worker with --identity to register."

    lines = []
    for proj in projects:
        slug = proj.get("slug", "?")
        proj_path = proj.get("path", "?")

        # Abbreviate path
        home = os.path.expanduser("~")
        display_path = proj_path
        if proj_path.startswith(home):
            display_path = "~" + proj_path[len(home) :]

        # Cross-reference workers by CWD
        resolved_proj = os.path.realpath(proj_path)
        active = [
            w
            for w in workers
            if os.path.realpath(w.get("cwd", "")) == resolved_proj
            and w.get("status") != "dead"
        ]
        roles = sorted(set(w.get("role", "worker") for w in active))
        role_str = ", ".join(r.upper() for r in roles) if roles else "no workers"

        # Ticket count from .cwork/tickets/INDEX.md
        index = Path(proj_path) / ".cwork" / "tickets" / "INDEX.md"
        ticket_count = 0
        if index.exists():
            try:
                for line in index.read_text().splitlines():
                    if (
                        line.startswith("|")
                        and not line.startswith("| ID")
                        and not line.startswith("|--")
                    ):
                        ticket_count += 1
            except OSError:
                pass

        lines.append(
            f"  {slug:<20} {display_path:<45} [{role_str}]  {ticket_count} tickets"
        )

    return "\n".join(lines)
