"""Verification-label repository for the analyzer bake-off.

Each row is one user-applied tag against an analysis run. Multiple rows of the
same ``kind`` against the same run are allowed by design — a row counts as one
verification event, and the per-kind count is what matters in the rollup.
"""
from __future__ import annotations

import logging

from aidj.store import db
from aidj.store.models import AnalysisLabel, AnalysisLabelKind

log = logging.getLogger(__name__)


def add(
    *,
    analysis_run_id: int,
    kind: AnalysisLabelKind,
    notes: str | None = None,
) -> AnalysisLabel:
    cur = db.execute(
        "INSERT INTO analysis_labels(analysis_run_id, kind, notes) VALUES (?, ?, ?)",
        (analysis_run_id, kind.value, notes),
    )
    new_id = int(cur.lastrowid or 0)
    label = get(new_id)
    if label is None:  # pragma: no cover — INSERT just succeeded
        raise RuntimeError(f"failed to read back label id={new_id}")
    return label


def get(label_id: int) -> AnalysisLabel | None:
    row = db.fetch_one("SELECT * FROM analysis_labels WHERE id=?", (label_id,))
    return AnalysisLabel.from_row(row) if row else None


def list_for_run(analysis_run_id: int) -> list[AnalysisLabel]:
    rows = db.fetch_all(
        "SELECT * FROM analysis_labels WHERE analysis_run_id=? ORDER BY created_at, id",
        (analysis_run_id,),
    )
    return [AnalysisLabel.from_row(r) for r in rows]


def list_for_runs(run_ids: list[int]) -> dict[int, list[AnalysisLabel]]:
    """Batch-fetch labels for many runs in a single query.

    The route that lists analyses calls this once instead of N times so the
    frontend's polling refresh stays cheap as the bake-off accumulates labels.
    Returns a dict mapping every requested run_id to its (possibly empty)
    label list — keys with no labels still appear so callers can ``[]``-default.
    """
    if not run_ids:
        return {}
    placeholders = ",".join(["?"] * len(run_ids))
    rows = db.fetch_all(
        f"SELECT * FROM analysis_labels "
        f"WHERE analysis_run_id IN ({placeholders}) "
        f"ORDER BY analysis_run_id, created_at, id",
        tuple(run_ids),
    )
    out: dict[int, list[AnalysisLabel]] = {rid: [] for rid in run_ids}
    for r in rows:
        label = AnalysisLabel.from_row(r)
        out.setdefault(label.analysis_run_id, []).append(label)
    return out


def delete(label_id: int) -> bool:
    cur = db.execute("DELETE FROM analysis_labels WHERE id=?", (label_id,))
    return cur.rowcount > 0


def counts_by_kind(analysis_run_id: int) -> dict[AnalysisLabelKind, int]:
    rows = db.fetch_all(
        "SELECT kind, COUNT(*) AS n FROM analysis_labels WHERE analysis_run_id=? GROUP BY kind",
        (analysis_run_id,),
    )
    return {AnalysisLabelKind(r["kind"]): int(r["n"]) for r in rows}


# ---------------------------------------------------------------------------
# Cross-track bake-off rollups
# ---------------------------------------------------------------------------


# Sentinel for tracks without a genre set, used as a dict key in the per-genre
# rollup so SQL NULL doesn't have to leak as a Python ``None`` key (which would
# JSON-serialise as the string "None" — surprising for the frontend).
UNTAGGED_GENRE = "(untagged)"


def rollup_by_analyzer() -> dict[str, dict[AnalysisLabelKind, int]]:
    """Per-analyzer label counts across the whole library.

    Returns ``{analyzer_name: {kind: count}}``. Empty dict if no labels exist.
    Analyzers with zero labels of a given kind simply omit that key — the
    frontend ``[k] ?? 0`` defaults are how the table cells render dashes.
    """
    rows = db.fetch_all(
        "SELECT r.analyzer_name AS analyzer_name, l.kind AS kind, COUNT(*) AS n "
        "FROM analysis_labels l "
        "JOIN analysis_runs r ON r.id = l.analysis_run_id "
        "GROUP BY r.analyzer_name, l.kind"
    )
    out: dict[str, dict[AnalysisLabelKind, int]] = {}
    for row in rows:
        analyzer = row["analyzer_name"]
        out.setdefault(analyzer, {})[AnalysisLabelKind(row["kind"])] = int(row["n"])
    return out


def rollup_by_analyzer_and_genre() -> dict[str, dict[str, dict[AnalysisLabelKind, int]]]:
    """Per-analyzer, per-genre label counts.

    Returns ``{analyzer_name: {genre: {kind: count}}}``. Tracks without a
    ``genre`` set are bucketed under ``UNTAGGED_GENRE`` so they're still
    visible in the rollup.
    """
    rows = db.fetch_all(
        "SELECT r.analyzer_name AS analyzer_name, "
        "       t.genre AS genre, "
        "       l.kind AS kind, "
        "       COUNT(*) AS n "
        "FROM analysis_labels l "
        "JOIN analysis_runs r ON r.id = l.analysis_run_id "
        "JOIN tracks t ON t.content_hash = r.track_hash "
        "GROUP BY r.analyzer_name, t.genre, l.kind"
    )
    out: dict[str, dict[str, dict[AnalysisLabelKind, int]]] = {}
    for row in rows:
        analyzer = row["analyzer_name"]
        genre = row["genre"] or UNTAGGED_GENRE
        out.setdefault(analyzer, {}).setdefault(genre, {})[AnalysisLabelKind(row["kind"])] = int(row["n"])
    return out
