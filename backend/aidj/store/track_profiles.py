"""Track-profile repository — one canonical TrackProfile per track.

Phase 2 (Canonical Track Intelligence) layer. Every downstream system
(Candidate Graph, Renderer, Planner) consumes ``TrackProfile`` objects from
here; nothing downstream looks at raw ``analysis_runs`` rows. The Builder
(step 3) is what materialises a profile from analysis_runs and writes it
through this module; today this file is just CRUD + staleness.

Staleness rule (used by the builder to decide "do I need to rebuild?"):

    A persisted profile is stale if ANY of:
    - it doesn't exist yet
    - ``profile_version < CURRENT_PROFILE_VERSION`` (builder logic changed)
    - any ``analysis_run.finished_at`` for this track is newer than the
      profile's ``built_at`` (source selection might change)
    - any source ``analysis_run`` referenced by the profile's provenance has
      been deleted (provenance is broken)

The staleness check is self-contained: callers only pass a track hash.
"""
from __future__ import annotations

import logging

from aidj.store import db
from aidj.store.models import CURRENT_PROFILE_VERSION, TrackProfile

log = logging.getLogger(__name__)


def get(track_hash: str) -> TrackProfile | None:
    """Load the persisted profile for ``track_hash``, or None."""
    row = db.fetch_one(
        "SELECT profile_json FROM track_profiles WHERE track_hash=?",
        (track_hash,),
    )
    return TrackProfile.from_row(row) if row else None


def upsert(profile: TrackProfile) -> TrackProfile:
    """Insert or replace the profile for a track.

    The top-level columns (``readiness``, ``completeness_score``,
    ``profile_version``, ``built_at``) are denormalised from the JSON so the
    Library page's coverage query and the staleness check can filter without
    parsing every blob.
    """
    # ``model_copy(update=...)`` does not re-run validators, so repository
    # boundaries revalidate before the JSON blob becomes persistent truth.
    profile = TrackProfile.model_validate(profile.model_dump())
    db.execute(
        "INSERT INTO track_profiles "
        "(track_hash, profile_version, profile_json, readiness, "
        " completeness_score, built_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(track_hash) DO UPDATE SET "
        "  profile_version=excluded.profile_version, "
        "  profile_json=excluded.profile_json, "
        "  readiness=excluded.readiness, "
        "  completeness_score=excluded.completeness_score, "
        "  built_at=excluded.built_at",
        (
            profile.track_hash,
            profile.profile_version,
            profile.model_dump_json(),
            profile.readiness.value,
            profile.completeness_score,
            profile.built_at,
        ),
    )
    return profile


def delete(track_hash: str) -> bool:
    cur = db.execute(
        "DELETE FROM track_profiles WHERE track_hash=?", (track_hash,)
    )
    return cur.rowcount > 0


def list_all(*, limit: int = 1000) -> list[TrackProfile]:
    """Most-recently-built first. Used by the Library coverage view."""
    rows = db.fetch_all(
        "SELECT profile_json FROM track_profiles "
        "ORDER BY built_at DESC, track_hash LIMIT ?",
        (limit,),
    )
    return [TrackProfile.from_row(r) for r in rows]


def is_stale(track_hash: str) -> bool:
    """Return True if the persisted profile needs rebuilding.

    See module docstring for the staleness rule. Returns True for tracks
    without a profile so the builder treats them uniformly.
    """
    row = db.fetch_one(
        "SELECT profile_version, profile_json, built_at "
        "FROM track_profiles WHERE track_hash=?",
        (track_hash,),
    )
    if row is None:
        return True  # nothing built yet
    if int(row["profile_version"]) < CURRENT_PROFILE_VERSION:
        return True

    profile = TrackProfile.from_row(row)
    source_run_ids = _collect_source_run_ids(profile)
    if source_run_ids and _has_missing_source_run(source_run_ids):
        return True

    newest = db.fetch_one(
        "SELECT MAX(finished_at) AS newest FROM analysis_runs "
        "WHERE track_hash=? AND finished_at IS NOT NULL",
        (track_hash,),
    )
    if newest is None or newest["newest"] is None:
        return False
    return str(newest["newest"]) > str(row["built_at"])


def _has_missing_source_run(run_ids: list[int]) -> bool:
    """A profile that points at a deleted analysis_run has broken provenance."""
    unique_ids = sorted(set(run_ids))
    placeholders = ",".join(["?"] * len(unique_ids))
    newest = db.fetch_one(
        f"SELECT COUNT(*) AS n FROM analysis_runs "
        f"WHERE id IN ({placeholders})",
        tuple(unique_ids),
    )
    return newest is None or int(newest["n"]) != len(unique_ids)


def coverage_counts() -> dict[str, int]:
    """Library-wide profile coverage. Drives the readiness summary in the UI.

    Returns counts for each ``Readiness`` value plus a ``missing`` bucket for
    tracks that have no profile yet. Missing is computed as
    ``total_tracks - sum(readiness_counts)`` so a deleted profile shows up as
    missing immediately.
    """
    rows = db.fetch_all(
        "SELECT readiness, COUNT(*) AS n FROM track_profiles GROUP BY readiness"
    )
    counts: dict[str, int] = {"ready": 0, "partial": 0, "blocked": 0}
    for r in rows:
        counts[r["readiness"]] = int(r["n"])

    total_tracks_row = db.fetch_one("SELECT COUNT(*) AS n FROM tracks")
    total = int(total_tracks_row["n"]) if total_tracks_row else 0
    counts["missing"] = max(0, total - sum(counts.values()))
    return counts


def _collect_source_run_ids(profile: TrackProfile) -> list[int]:
    """Pull every ``provenance.analysis_run_id`` out of a profile's blocks.

    Blocks sourced from backend utilities (energy, vocals derived locally)
    have ``analysis_run_id=None`` — they don't go through ``analysis_runs``
    and so can't be stale relative to it; skipped here.
    """
    blocks = (
        profile.tempo,
        profile.beat_grid,
        profile.key,
        profile.sections,
        profile.energy,
        profile.vocals,
    )
    out: list[int] = []
    for block in blocks:
        if block is None:
            continue
        rid = block.provenance.analysis_run_id
        if rid is not None:
            out.append(rid)
    return out
