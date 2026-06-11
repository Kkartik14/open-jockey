"""Atomic ``analysis_runs.claim_running`` — decision matrix + concurrency."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aidj.store import analysis_runs, db, tracks
from aidj.store.models import AnalysisStatus

# ---------------------------------------------------------------------------
# Decision matrix — a single caller against varying prior states
# ---------------------------------------------------------------------------


def _claim(track_hash: str, *, force: bool = False, stale_after_sec: float = 60.0):
    return analysis_runs.claim_running(
        track_hash=track_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        force=force,
        stale_after_sec=stale_after_sec,
    )


def test_claim_with_no_existing_row_succeeds(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    result = _claim(track.content_hash)
    assert result.claimed is True
    assert result.run.status is AnalysisStatus.RUNNING
    assert result.run.started_at is not None


def test_claim_with_active_running_row_returns_existing(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    first = _claim(track.content_hash)
    assert first.claimed is True

    # Second call without force, same version → blocked.
    second = _claim(track.content_hash)
    assert second.claimed is False
    assert second.run.id == first.run.id
    assert second.run.status is AnalysisStatus.RUNNING


def test_claim_with_stale_running_row_recovers(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    _claim(track.content_hash)

    # Backdate started_at to simulate a row left over from a crashed backend.
    long_ago = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE analysis_runs SET started_at=? WHERE track_hash=?",
        (long_ago, track.content_hash),
    )

    # stale_after_sec=60 → 1-hour-old row is stale → claim succeeds.
    second = _claim(track.content_hash, stale_after_sec=60.0)
    assert second.claimed is True
    assert second.run.status is AnalysisStatus.RUNNING


def test_force_overrides_active_running(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    _claim(track.content_hash)

    # force=True bypasses the RUNNING short-circuit even when the row is fresh.
    second = _claim(track.content_hash, force=True)
    assert second.claimed is True


def test_claim_with_completed_row_returns_cached(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    analysis_runs.upsert(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
        output={"ok": True},
        started_at=analysis_runs.utc_now_iso(),
        finished_at=analysis_runs.utc_now_iso(),
    )
    result = _claim(track.content_hash)
    assert result.claimed is False
    assert result.run.status is AnalysisStatus.COMPLETED


def test_force_re_claims_completed(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    analysis_runs.upsert(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
        output={"ok": True},
    )
    result = _claim(track.content_hash, force=True)
    assert result.claimed is True
    assert result.run.status is AnalysisStatus.RUNNING
    # The cached output is cleared by the new RUNNING row.
    assert result.run.output is None


def test_claim_with_failed_row_re_claims(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    analysis_runs.upsert(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.FAILED,
        error="boom",
    )
    result = _claim(track.content_hash)
    assert result.claimed is True
    assert result.run.status is AnalysisStatus.RUNNING


# ---------------------------------------------------------------------------
# Concurrency — two threads racing for the same slot
# ---------------------------------------------------------------------------


def test_concurrent_claims_serialise(tmp_aidj, sample_file: Path) -> None:
    """Two threads calling claim_running at the same time: exactly one wins."""
    track = tracks.ingest(sample_file)
    barrier = threading.Barrier(2)
    results: list[analysis_runs.ClaimResult] = []
    results_lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        result = _claim(track.content_hash)
        with results_lock:
            results.append(result)

    t1 = threading.Thread(target=attempt)
    t2 = threading.Thread(target=attempt)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    claimed = [r for r in results if r.claimed]
    not_claimed = [r for r in results if not r.claimed]
    assert len(claimed) == 1
    assert len(not_claimed) == 1
    # The non-claimer sees the same row id the claimer just produced.
    assert not_claimed[0].run.id == claimed[0].run.id


# ---------------------------------------------------------------------------
# Token-conditional terminal writes
# ---------------------------------------------------------------------------


def test_claim_returns_unique_token_per_call(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    a = _claim(track.content_hash)
    b = _claim(track.content_hash, force=True)
    assert a.token and b.token
    assert a.token != b.token


def test_complete_run_with_matching_token_writes(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    claim = _claim(track.content_hash)
    assert claim.claimed

    final = analysis_runs.complete_run(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        claim_token=claim.token,
        output={"ok": True},
        confidence=0.9,
        finished_at=analysis_runs.utc_now_iso(),
    )
    assert final.status is AnalysisStatus.COMPLETED
    assert final.output == {"ok": True}
    assert final.confidence == 0.9
    # started_at preserved from the claim
    assert final.started_at == claim.run.started_at


def test_complete_run_with_mismatched_token_drops_result(
    tmp_aidj, sample_file: Path, caplog
) -> None:
    """If a newer claim took the slot, an old terminal write must not overwrite it."""
    import logging

    track = tracks.ingest(sample_file)
    claim_a = _claim(track.content_hash)
    assert claim_a.claimed

    # A force-claim takes over the slot under a new token.
    claim_b = _claim(track.content_hash, force=True)
    assert claim_b.claimed
    assert claim_b.token != claim_a.token

    # Now A's analyzer "finishes" and tries to record its result. This must be a
    # no-op on the row (B's RUNNING claim wins) and emit a warning.
    with caplog.at_level(logging.WARNING, logger="aidj.store.analysis_runs"):
        result = analysis_runs.complete_run(
            track_hash=track.content_hash,
            analyzer_name="echo",
            analyzer_version="0.1.0",
            claim_token=claim_a.token,
            output={"discarded": True},
            confidence=1.0,
            finished_at=analysis_runs.utc_now_iso(),
        )

    # Returned row reflects B's claim, not A's discarded write.
    assert result.status is AnalysisStatus.RUNNING
    assert result.output is None
    # The mismatch was logged (not silently swallowed).
    assert any("token mismatch" in r.message for r in caplog.records)


def test_fail_run_with_mismatched_token_drops(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    claim_a = _claim(track.content_hash)
    claim_b = _claim(track.content_hash, force=True)

    result = analysis_runs.fail_run(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        claim_token=claim_a.token,
        error="late failure that should be dropped",
        finished_at=analysis_runs.utc_now_iso(),
    )
    # B's RUNNING row wins, A's FAILED write is discarded.
    assert result.status is AnalysisStatus.RUNNING
    assert result.error is None
    assert result.run if False else result.id == claim_b.run.id


def test_full_lifecycle_with_token(tmp_aidj, sample_file: Path) -> None:
    """Sanity check: claim → complete with the same token → COMPLETED row."""
    track = tracks.ingest(sample_file)
    claim = _claim(track.content_hash)

    completed = analysis_runs.complete_run(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        claim_token=claim.token,
        output={"tempo": {"bpm": 120}},
        confidence=0.95,
        finished_at=analysis_runs.utc_now_iso(),
    )
    assert completed.status is AnalysisStatus.COMPLETED
    assert completed.confidence == 0.95
