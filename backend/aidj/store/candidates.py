"""Transition-candidate repository for Phase 3.

The candidate graph builder produces typed ``TransitionCandidate`` objects; this
module persists them under a project and reads them back for API/frontend use.
"""
from __future__ import annotations

import json

from aidj.store import db
from aidj.store.models import TransitionCandidate


def list_for_project(project_id: int, *, limit: int = 1000) -> list[TransitionCandidate]:
    # Fine for the current single-user store. If candidate volume grows, promote
    # score to a denormalised column or add a SQLite expression index.
    rows = db.fetch_all(
        "SELECT * FROM candidates WHERE project_id=? "
        "ORDER BY json_extract(scores_json, '$.score') DESC, id ASC LIMIT ?",
        (project_id, limit),
    )
    return [TransitionCandidate.from_row(r) for r in rows]


def delete_for_project(project_id: int) -> int:
    cur = db.execute("DELETE FROM candidates WHERE project_id=?", (project_id,))
    return cur.rowcount


def replace_for_project(
    project_id: int,
    built: list[TransitionCandidate],
) -> list[TransitionCandidate]:
    """Replace all candidates for ``project_id`` atomically."""
    with db.transaction():
        db.get_conn().execute("DELETE FROM candidates WHERE project_id=?", (project_id,))
        for candidate in built:
            _insert(candidate)
    return list_for_project(project_id)


def _insert(candidate: TransitionCandidate) -> None:
    db.get_conn().execute(
        "INSERT INTO candidates "
        "(project_id, from_track, to_track, from_cue_bar, to_cue_bar, "
        " scores_json, allowed_techniques) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            candidate.project_id,
            candidate.from_track,
            candidate.to_track,
            candidate.from_cue_bar,
            candidate.to_cue_bar,
            candidate.scores.model_dump_json(),
            json.dumps([tech.value for tech in candidate.allowed_techniques]),
        ),
    )
