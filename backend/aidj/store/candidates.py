"""Transition-candidate repository for Phase 3.

The candidate graph builder produces typed ``TransitionCandidate`` objects; this
module persists them under a project and reads them back for API/frontend use.
"""

from __future__ import annotations

import json

from aidj.store import db
from aidj.store.models import TransitionCandidate

CandidateKey = tuple[int, str, str, int, int]


def list_for_project(project_id: int, *, limit: int = 1000) -> list[TransitionCandidate]:
    # Fine for the current single-user store. If candidate volume grows, promote
    # score to a denormalised column or add a SQLite expression index.
    rows = db.fetch_all(
        "SELECT * FROM candidates WHERE project_id=? "
        "ORDER BY json_extract(scores_json, '$.score') DESC, id ASC LIMIT ?",
        (project_id, limit),
    )
    return [TransitionCandidate.from_row(r) for r in rows]


def get(candidate_id: int) -> TransitionCandidate | None:
    row = db.fetch_one("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    return TransitionCandidate.from_row(row) if row else None


def get_for_project(project_id: int, candidate_id: int) -> TransitionCandidate | None:
    row = db.fetch_one(
        "SELECT * FROM candidates WHERE project_id=? AND id=?",
        (project_id, candidate_id),
    )
    return TransitionCandidate.from_row(row) if row else None


def delete_for_project(project_id: int) -> int:
    cur = db.execute("DELETE FROM candidates WHERE project_id=?", (project_id,))
    return cur.rowcount


def replace_for_project(
    project_id: int,
    built: list[TransitionCandidate],
) -> list[TransitionCandidate]:
    """Reconcile candidates for ``project_id`` while preserving stable ids.

    Natural-key matches are updated in place. New keys are inserted. Existing
    keys no longer produced by the builder are deleted, which intentionally
    lets dependent render rows cascade only for genuinely removed candidates.
    """
    desired_keys = {_key(candidate) for candidate in built}
    with db.transaction():
        existing = db.get_conn().execute(
            "SELECT id, project_id, from_track, to_track, from_cue_bar, to_cue_bar "
            "FROM candidates WHERE project_id=?",
            (project_id,),
        ).fetchall()
        for row in existing:
            key = (
                row["project_id"],
                row["from_track"],
                row["to_track"],
                row["from_cue_bar"],
                row["to_cue_bar"],
            )
            if key not in desired_keys:
                db.get_conn().execute("DELETE FROM candidates WHERE id=?", (row["id"],))
        for candidate in built:
            _upsert(candidate)
    return list_for_project(project_id)


def _key(candidate: TransitionCandidate) -> CandidateKey:
    return (
        candidate.project_id,
        candidate.from_track,
        candidate.to_track,
        candidate.from_cue_bar,
        candidate.to_cue_bar,
    )


def _upsert(candidate: TransitionCandidate) -> None:
    techniques_json = json.dumps([tech.value for tech in candidate.allowed_techniques])
    db.get_conn().execute(
        "INSERT INTO candidates "
        "(project_id, from_track, to_track, from_cue_bar, to_cue_bar, "
        " scores_json, allowed_techniques) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(project_id, from_track, to_track, from_cue_bar, to_cue_bar) "
        "DO UPDATE SET "
        "  scores_json=excluded.scores_json, "
        "  allowed_techniques=excluded.allowed_techniques",
        (
            candidate.project_id,
            candidate.from_track,
            candidate.to_track,
            candidate.from_cue_bar,
            candidate.to_cue_bar,
            candidate.scores.model_dump_json(),
            techniques_json,
        ),
    )
