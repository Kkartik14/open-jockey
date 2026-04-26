"""Analysis-run repository: upsert, idempotency, version separation, listing."""
from __future__ import annotations

import pytest

from aidj.store import analysis_runs, tracks
from aidj.store.models import AnalysisRun, AnalysisStatus


@pytest.fixture
def ingested_track(tmp_aidj, sample_file):
    return tracks.ingest(sample_file)


def test_upsert_inserts_then_updates_in_place(tmp_aidj, ingested_track) -> None:
    run = analysis_runs.upsert(
        track_hash=ingested_track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.RUNNING,
        started_at="2026-04-26 10:00:00",
    )
    assert isinstance(run, AnalysisRun)
    assert run.status is AnalysisStatus.RUNNING
    first_id = run.id

    completed = analysis_runs.upsert(
        track_hash=ingested_track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
        output={"bpm": 120.0},
        confidence=0.9,
        started_at="2026-04-26 10:00:00",
        finished_at="2026-04-26 10:00:05",
    )
    # Same row updated, not a new row
    assert completed.id == first_id
    assert completed.status is AnalysisStatus.COMPLETED
    assert completed.output == {"bpm": 120.0}
    assert completed.confidence == 0.9


def test_different_versions_create_separate_rows(tmp_aidj, ingested_track) -> None:
    a = analysis_runs.upsert(
        track_hash=ingested_track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
        output={"bpm": 120.0},
    )
    b = analysis_runs.upsert(
        track_hash=ingested_track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.2.0",
        status=AnalysisStatus.COMPLETED,
        output={"bpm": 121.0},
    )
    assert a.id != b.id

    runs = analysis_runs.list_for_track(ingested_track.content_hash)
    versions = sorted(r.analyzer_version for r in runs)
    assert versions == ["0.1.0", "0.2.0"]


def test_get_with_version_returns_exact_match(tmp_aidj, ingested_track) -> None:
    analysis_runs.upsert(
        track_hash=ingested_track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
    )
    assert analysis_runs.get(
        ingested_track.content_hash, "echo", version="0.1.0"
    ) is not None
    assert analysis_runs.get(
        ingested_track.content_hash, "echo", version="9.9.9"
    ) is None


def test_get_completed_skips_non_completed(tmp_aidj, ingested_track) -> None:
    analysis_runs.upsert(
        track_hash=ingested_track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.FAILED,
        error="boom",
    )
    assert analysis_runs.get_completed(
        ingested_track.content_hash, "echo", "0.1.0"
    ) is None

    analysis_runs.upsert(
        track_hash=ingested_track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
        output={"ok": True},
    )
    completed = analysis_runs.get_completed(
        ingested_track.content_hash, "echo", "0.1.0"
    )
    assert completed is not None and completed.status is AnalysisStatus.COMPLETED


def test_delete_removes_row(tmp_aidj, ingested_track) -> None:
    analysis_runs.upsert(
        track_hash=ingested_track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
    )
    assert analysis_runs.delete(ingested_track.content_hash, "echo", "0.1.0") is True
    assert analysis_runs.get(ingested_track.content_hash, "echo", version="0.1.0") is None
    assert analysis_runs.delete(ingested_track.content_hash, "echo", "0.1.0") is False
