"""Token tracking CSV for session analyses.

Maintains a global summary.csv at ~/.cwork/analyses/ with one row per
analyzed session. Called by the analyze-session skill or wrap-up code
after producing an analysis.

Also provides a summary reader for the ``stats`` subcommand.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

SUMMARY_CSV_PATH: Path = Path.home() / ".cwork" / "analyses" / "summary.csv"

CSV_COLUMNS: list[str] = [
    "date",
    "worker_name",
    "identity",
    "project",
    "task_description",
    "input_tokens",
    "output_tokens",
    "cache_read",
    "cache_create",
    "duration_minutes",
    "estimated_cost_usd",
    "session_id",
    "analysis_file",
]


def append_session_row(
    date: str,
    worker_name: str,
    identity: str,
    project: str,
    task_description: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_create: int,
    duration_minutes: float,
    estimated_cost_usd: float,
    session_id: str,
    analysis_file: str,
    csv_path: Path | None = None,
) -> None:
    """Append a row to the token tracking CSV.

    Creates the file with headers if it doesn't exist.
    """
    path = csv_path or SUMMARY_CSV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    write_header = not path.exists() or path.stat().st_size == 0

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "date": date,
                "worker_name": worker_name,
                "identity": identity,
                "project": project,
                "task_description": task_description,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read": cache_read,
                "cache_create": cache_create,
                "duration_minutes": duration_minutes,
                "estimated_cost_usd": estimated_cost_usd,
                "session_id": session_id,
                "analysis_file": analysis_file,
            }
        )


def read_summary(csv_path: Path | None = None) -> list[dict]:
    """Read all rows from the summary CSV. Returns list of dicts."""
    path = csv_path or SUMMARY_CSV_PATH
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def format_stats(rows: list[dict]) -> str:
    """Format summary statistics from CSV rows as a readable table."""
    if not rows:
        return "No session data recorded yet."

    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_create = 0
    total_minutes = 0.0
    by_identity: dict[str, list[float]] = {}
    by_project: dict[str, list[float]] = {}

    for row in rows:
        cost = float(row.get("estimated_cost_usd", 0) or 0)
        total_cost += cost
        total_input += int(row.get("input_tokens", 0) or 0)
        total_output += int(row.get("output_tokens", 0) or 0)
        total_cache_read += int(row.get("cache_read", 0) or 0)
        total_cache_create += int(row.get("cache_create", 0) or 0)
        total_minutes += float(row.get("duration_minutes", 0) or 0)

        identity = row.get("identity", "unknown")
        by_identity.setdefault(identity, []).append(cost)

        project = row.get("project", "unknown")
        by_project.setdefault(project, []).append(cost)

    lines = [
        f"Sessions: {len(rows)}",
        f"Total cost: ${total_cost:.2f}",
        f"Total duration: {total_minutes:.0f} minutes",
        f"Total tokens: input={total_input:,} output={total_output:,} "
        f"cache_read={total_cache_read:,} cache_create={total_cache_create:,}",
        "",
        "By identity:",
    ]
    for identity, costs in sorted(by_identity.items()):
        avg = sum(costs) / len(costs)
        lines.append(
            f"  {identity}: {len(costs)} sessions, ${sum(costs):.2f} total, ${avg:.2f} avg"
        )

    lines.append("")
    lines.append("By project:")
    for project, costs in sorted(by_project.items()):
        avg = sum(costs) / len(costs)
        proj_short = project.split("/")[-1] if "/" in project else project
        lines.append(
            f"  {proj_short}: {len(costs)} sessions, ${sum(costs):.2f} total, ${avg:.2f} avg"
        )

    return "\n".join(lines)
