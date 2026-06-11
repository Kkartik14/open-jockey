"""Project repository.

Projects own Phase 3 transition candidates and later planner/render artifacts.
The table already existed as a schema stub; this module turns it into a real
store boundary without changing the schema.
"""
from __future__ import annotations

import json
from typing import Any

from aidj.store import db
from aidj.store.models import Project


def create(
    name: str,
    *,
    intent: str | None = None,
    plan: dict[str, Any] | None = None,
) -> Project:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("project name must be non-empty")
    cur = db.execute(
        "INSERT INTO projects(name, intent, plan_json) VALUES (?, ?, ?)",
        (
            clean_name,
            intent.strip() if isinstance(intent, str) and intent.strip() else None,
            json.dumps(plan) if plan is not None else None,
        ),
    )
    project = get(int(cur.lastrowid))
    if project is None:  # pragma: no cover - just inserted
        raise RuntimeError("created project could not be loaded")
    return project


def get(project_id: int) -> Project | None:
    row = db.fetch_one("SELECT * FROM projects WHERE id=?", (project_id,))
    return Project.from_row(row) if row else None


def list_recent(*, limit: int = 100) -> list[Project]:
    rows = db.fetch_all(
        "SELECT * FROM projects ORDER BY updated_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    return [Project.from_row(r) for r in rows]


def delete(project_id: int) -> bool:
    cur = db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return cur.rowcount > 0
