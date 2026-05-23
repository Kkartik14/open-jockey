"""Analysis-run repository.

One row per (track, analyzer_name, analyzer_version). Re-running the same
analyzer at the same version updates the row in place; bumping the version
creates a new row alongside the old one — useful for the bake-off, where we
compare multiple analyzer versions on the same track.

The lifecycle is:

1. ``claim_running`` is the atomic entry-point: callers ask "may I run this
   analyzer on this track?" and the function answers (claimed=True with a fresh
   RUNNING row + a ``claim_token``) or (claimed=False with the existing row
   that owns the slot). The underlying transaction is BEGIN IMMEDIATE so two
   concurrent callers can never both claim — one wins, the other gets back
   the winner's row.
2. ``complete_run`` / ``fail_run`` perform the *conditional* terminal write:
   they only update the row if the supplied ``claim_token`` matches the row's
   current token. If a newer ``claim_running`` (e.g. ``force=True`` or
   stale-recovery) has taken the slot in the meantime, the stale terminal
   write is dropped and the current row is returned. This prevents an
   in-flight analyzer that finishes late from clobbering a newer claim's row.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aidj.store import db
from aidj.store._timestamps import TIMESTAMP_FMT, utc_now_iso
from aidj.store.models import AnalysisRun, AnalysisStatus

# Re-exported so callers that already do ``analysis_runs.utc_now_iso()`` keep
# working — the canonical home is ``aidj.store._timestamps``.
__all__ = ["utc_now_iso"]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Atomic claim
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of an attempt to take the RUNNING slot for a (track, analyzer).

    Attributes
    ----------
    claimed:
        True iff *we* upserted the RUNNING row and the caller should now invoke
        the analyzer plugin. False if the slot was already held (running or
        cached completion) and the caller should return ``run`` directly.
    run:
        The persisted row — ours when ``claimed`` is True, the prior owner's
        row otherwise.
    token:
        The opaque token associated with our claim attempt. Pass it back to
        ``complete_run`` / ``fail_run`` to write a terminal status; the write
        is silently dropped if the row's token has changed since (i.e. another
        caller force-claimed the slot or auto-recovery took over). Empty when
        ``claimed`` is False — there's nothing to terminally write.
    """

    claimed: bool
    run: AnalysisRun
    token: str = ""


def claim_running(
    *,
    track_hash: str,
    analyzer_name: str,
    analyzer_version: str,
    force: bool,
    stale_after_sec: float,
) -> ClaimResult:
    """Atomically attempt to take the RUNNING slot for (track, analyzer, version).

    Decision matrix:

    | existing.status | force | stale | outcome                |
    | --------------- | ----- | ----- | ---------------------- |
    | none            | -     | -     | claim                  |
    | RUNNING         | False | False | hands off, return existing |
    | RUNNING         | False | True  | claim (auto-recovery)  |
    | RUNNING         | True  | -     | claim (explicit override) |
    | COMPLETED       | False | -     | hands off, return cached  |
    | COMPLETED       | True  | -     | claim (force re-run)   |
    | FAILED, PENDING | -     | -     | claim                  |

    The whole operation runs under ``BEGIN IMMEDIATE`` so two concurrent callers
    can never both proceed to invoke the plugin.
    """
    new_token = uuid.uuid4().hex
    started_at = utc_now_iso()
    with db.transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT * FROM analysis_runs "
            "WHERE track_hash=? AND analyzer_name=? AND analyzer_version=?",
            (track_hash, analyzer_name, analyzer_version),
        ).fetchone()

        if row is not None:
            existing = AnalysisRun.from_row(row)
            if existing.status is AnalysisStatus.RUNNING:
                if not force and not _is_stale_running(existing, stale_after_sec):
                    return ClaimResult(claimed=False, run=existing)
            elif existing.status is AnalysisStatus.COMPLETED and not force:
                return ClaimResult(claimed=False, run=existing)
            # else: FAILED, PENDING, or fall-through cases — claim it.

        conn.execute(
            "INSERT INTO analysis_runs "
            "(track_hash, analyzer_name, analyzer_version, status, started_at, "
            "finished_at, output_json, confidence, error, claim_token) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?) "
            "ON CONFLICT (track_hash, analyzer_name, analyzer_version) "
            "DO UPDATE SET "
            "  status='running', "
            "  started_at=excluded.started_at, "
            "  finished_at=NULL, "
            "  output_json=NULL, "
            "  confidence=NULL, "
            "  error=NULL, "
            "  claim_token=excluded.claim_token",
            (
                track_hash,
                analyzer_name,
                analyzer_version,
                AnalysisStatus.RUNNING.value,
                started_at,
                new_token,
            ),
        )
        new_row = conn.execute(
            "SELECT * FROM analysis_runs "
            "WHERE track_hash=? AND analyzer_name=? AND analyzer_version=?",
            (track_hash, analyzer_name, analyzer_version),
        ).fetchone()

    if new_row is None:  # pragma: no cover — INSERT just succeeded
        raise RuntimeError(
            f"failed to read back claim for {track_hash[:12]}/{analyzer_name}@{analyzer_version}"
        )
    return ClaimResult(claimed=True, run=AnalysisRun.from_row(new_row), token=new_token)


def _is_stale_running(run: AnalysisRun, stale_after_sec: float) -> bool:
    """A RUNNING row is stale if its ``started_at`` is older than the threshold.

    Used so a backend crash that left a row stuck at RUNNING auto-recovers on
    the next request rather than blocking that (track, analyzer) forever.
    """
    if run.started_at is None:
        return True
    try:
        started = datetime.strptime(run.started_at, TIMESTAMP_FMT).replace(tzinfo=UTC)
    except ValueError:
        return True
    age = (datetime.now(UTC) - started).total_seconds()
    return age > stale_after_sec


# ---------------------------------------------------------------------------
# Token-conditional terminal writes
# ---------------------------------------------------------------------------


def complete_run(
    *,
    track_hash: str,
    analyzer_name: str,
    analyzer_version: str,
    claim_token: str,
    output: dict[str, Any] | None,
    confidence: float | None,
    finished_at: str,
) -> AnalysisRun:
    """Conditionally transition a row to COMPLETED.

    The UPDATE only matches when ``claim_token`` equals the row's current
    token. If a newer claim has taken over since (force or stale recovery),
    rowcount is 0, the result is dropped with a warning, and the *current* row
    is returned. The caller's API client therefore sees what the slot is
    actually doing now, not the result that was discarded.
    """
    return _terminal_write(
        track_hash=track_hash,
        analyzer_name=analyzer_name,
        analyzer_version=analyzer_version,
        claim_token=claim_token,
        status=AnalysisStatus.COMPLETED,
        output=output,
        confidence=confidence,
        error=None,
        finished_at=finished_at,
    )


def fail_run(
    *,
    track_hash: str,
    analyzer_name: str,
    analyzer_version: str,
    claim_token: str,
    error: str,
    finished_at: str,
) -> AnalysisRun:
    """Conditionally transition a row to FAILED. See ``complete_run`` for the
    token-mismatch behaviour."""
    return _terminal_write(
        track_hash=track_hash,
        analyzer_name=analyzer_name,
        analyzer_version=analyzer_version,
        claim_token=claim_token,
        status=AnalysisStatus.FAILED,
        output=None,
        confidence=None,
        error=error,
        finished_at=finished_at,
    )


def _terminal_write(
    *,
    track_hash: str,
    analyzer_name: str,
    analyzer_version: str,
    claim_token: str,
    status: AnalysisStatus,
    output: dict[str, Any] | None,
    confidence: float | None,
    error: str | None,
    finished_at: str,
) -> AnalysisRun:
    cur = db.execute(
        "UPDATE analysis_runs SET "
        "  status=?, "
        "  output_json=?, "
        "  confidence=?, "
        "  error=?, "
        "  finished_at=? "
        "WHERE track_hash=? AND analyzer_name=? AND analyzer_version=? AND claim_token=?",
        (
            status.value,
            json.dumps(output) if output is not None else None,
            confidence,
            error,
            finished_at,
            track_hash,
            analyzer_name,
            analyzer_version,
            claim_token,
        ),
    )
    if cur.rowcount == 0:
        log.warning(
            "analysis result discarded — claim token mismatch (track=%s analyzer=%s "
            "version=%s status=%s); a newer claim has taken over the slot",
            track_hash[:12], analyzer_name, analyzer_version, status.value,
        )

    current = get(track_hash, analyzer_name, version=analyzer_version)
    if current is None:  # pragma: no cover — row pre-existed
        raise RuntimeError(
            f"analysis_run vanished for {track_hash[:12]}/{analyzer_name}@{analyzer_version}"
        )
    return current


# ---------------------------------------------------------------------------
# Plain CRUD (used by tests + administrative paths)
# ---------------------------------------------------------------------------


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
    claim_token: str | None = None,
) -> AnalysisRun:
    """Unconditional insert-or-update for a row.

    Production analyze flows go through ``claim_running`` + ``complete_run`` /
    ``fail_run`` so concurrent callers serialise correctly and stale results
    don't overwrite newer claims. ``upsert`` is here for tests, fixtures, and
    administrative imports where you simply want to set a row's state.
    """
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
        "claim_token": claim_token,
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
    """Return runs for a track, **most recent first** across all analyzers.

    We order by ``started_at`` (which is rewritten on every claim — including
    forced reruns — unlike ``created_at`` which sticks to the first INSERT) and
    tie-break by id so a re-run still surfaces above a previous run with the
    same timestamp. ``COALESCE`` covers admin-inserted rows that never went
    through ``claim_running`` and lack a ``started_at``.
    """
    rows = db.fetch_all(
        "SELECT * FROM analysis_runs WHERE track_hash=? "
        "ORDER BY COALESCE(started_at, created_at) DESC, id DESC",
        (track_hash,),
    )
    return [AnalysisRun.from_row(r) for r in rows]


def delete(track_hash: str, analyzer_name: str, version: str) -> bool:
    cur = db.execute(
        "DELETE FROM analysis_runs WHERE track_hash=? AND analyzer_name=? AND analyzer_version=?",
        (track_hash, analyzer_name, version),
    )
    return cur.rowcount > 0
