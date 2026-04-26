"""Analysis-run repository.

One row per (track, analyzer_name, analyzer_version). Re-running the same
analyzer at the same version updates the row in place; bumping the version
creates a new row alongside the old one — useful for the bake-off, where we
compare multiple analyzer versions on the same track.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from aidj.store import db
from aidj.store.models import AnalysisRun, AnalysisStatus

log = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def upsert(
    *,
    track_hash: str,
    analyzer_name: str,
    analyzer_version: str,
    status: AnalysisStatus,
    output: dict[str, Any] | None = None,
    confidence: float | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> AnalysisRun:
    """Insert or update the row for ``(track_hash, analyzer_name, analyzer_version)``."""
    fields: dict[str, Any] = {
        "track_hash": track_hash,
        "analyzer_name": analyzer_name,
        "analyzer_version": analyzer_version,
        "status": status.value,
        "output_json": json.dumps(output) if output is not None else None,
        "confidence": confidence,
        "error": error,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    cols = ",".join(fields.keys())
    placeholders = ",".join(["?"] * len(fields))
    update_clause = ",".join(
        f"{k}=excluded.{k}"
        for k in fields
        if k not in {"track_hash", "analyzer_name", "analyzer_version"}
    )
    db.execute(
        f"INSERT INTO analysis_runs ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (track_hash, analyzer_name, analyzer_version) "
        f"DO UPDATE SET {update_clause}",
        tuple(fields.values()),
    )

    run = get(track_hash, analyzer_name, version=analyzer_version)
    if run is None:  # pragma: no cover — INSERT just succeeded
        raise RuntimeError(
            f"failed to read back analysis_run for {track_hash[:12]}/{analyzer_name}@{analyzer_version}"
        )
    return run


def get(
    track_hash: str,
    analyzer_name: str,
    *,
    version: str | None = None,
) -> AnalysisRun | None:
    """Return a run for (track, analyzer). If ``version`` is given, exact match;
    otherwise the most recently-created row for that (track, analyzer)."""
    if version is not None:
        row = db.fetch_one(
            "SELECT * FROM analysis_runs "
            "WHERE track_hash=? AND analyzer_name=? AND analyzer_version=?",
            (track_hash, analyzer_name, version),
        )
    else:
        row = db.fetch_one(
            "SELECT * FROM analysis_runs "
            "WHERE track_hash=? AND analyzer_name=? "
            "ORDER BY created_at DESC LIMIT 1",
            (track_hash, analyzer_name),
        )
    return AnalysisRun.from_row(row) if row else None


def get_completed(
    track_hash: str,
    analyzer_name: str,
    version: str,
) -> AnalysisRun | None:
    """Return a completed run for the exact (track, analyzer, version), or None."""
    row = db.fetch_one(
        "SELECT * FROM analysis_runs "
        "WHERE track_hash=? AND analyzer_name=? AND analyzer_version=? AND status=?",
        (track_hash, analyzer_name, version, AnalysisStatus.COMPLETED.value),
    )
    return AnalysisRun.from_row(row) if row else None


def list_for_track(track_hash: str) -> list[AnalysisRun]:
    rows = db.fetch_all(
        "SELECT * FROM analysis_runs WHERE track_hash=? ORDER BY analyzer_name, created_at DESC",
        (track_hash,),
    )
    return [AnalysisRun.from_row(r) for r in rows]


def delete(track_hash: str, analyzer_name: str, version: str) -> bool:
    cur = db.execute(
        "DELETE FROM analysis_runs WHERE track_hash=? AND analyzer_name=? AND analyzer_version=?",
        (track_hash, analyzer_name, version),
    )
    return cur.rowcount > 0
