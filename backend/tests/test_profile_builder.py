"""Profile builder tests (Phase 2, step 3).

These exercise the deterministic selection/normalisation logic against
synthetic ``analysis_runs`` fixtures. They prove the funnel is correct — NOT
that any analyzer is musically right (that's the human listening test in
``private/plan.md``). Synthetic green here is plumbing, not validation.
"""

from __future__ import annotations

from pathlib import Path

from aidj import profile_builder
from aidj.profile_builder import TrackNotFoundError
from aidj.store import analysis_runs, db, track_profiles, tracks
from aidj.store.models import (
    CURRENT_PROFILE_VERSION,
    AnalysisStatus,
    Readiness,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ingested_track(tmp_path: Path, byte: bytes = b"x") -> str:
    p = tmp_path / f"sample-{byte.hex()}.bin"
    p.write_bytes(byte * 32)
    return tracks.ingest(p).content_hash


def _beatgrid_output(*, bpm: float = 124.0, n_beats: int = 8, with_sections: bool = True) -> dict:
    """A BeatGridAnalysis-shaped dict like the allin1/librosa plugins emit."""
    beats = [{"time_sec": round(i * 0.5, 3), "is_downbeat": (i % 4 == 0)} for i in range(n_beats)]
    out: dict = {
        "tempo": {"bpm": bpm},
        "beats": beats,
        "sections": [],
        "duration_sec": round(n_beats * 0.5, 3),
    }
    if with_sections:
        out["sections"] = [
            {"start_sec": 0.0, "end_sec": 4.0, "label": "intro"},
            {"start_sec": 4.0, "end_sec": 8.0, "label": "verse"},
        ]
    return out


def _key_output() -> dict:
    return {"key": "C", "scale": "major", "camelot": "8B", "confidence": 0.7}


def _upsert_run(
    track_hash: str,
    name: str,
    version: str,
    output: dict | None,
    *,
    status: AnalysisStatus = AnalysisStatus.COMPLETED,
    finished_at: str | None = "2026-01-01 00:00:01",
):
    return analysis_runs.upsert(
        track_hash=track_hash,
        analyzer_name=name,
        analyzer_version=version,
        status=status,
        output=output if status is AnalysisStatus.COMPLETED else None,
        started_at="2026-01-01 00:00:00",
        finished_at=finished_at if status is AnalysisStatus.COMPLETED else None,
    )


# ---------------------------------------------------------------------------
# Core selection
# ---------------------------------------------------------------------------


def test_builds_blocked_profile_when_no_runs(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)

    profile = profile_builder.build_profile(track_hash)

    assert profile.readiness is Readiness.BLOCKED
    assert profile.tempo is None and profile.beat_grid is None
    assert profile.completeness_score == 0.0
    assert profile.fields.has_beat_grid is False
    # Persisted, so coverage can tell "tried, unusable" from "never built".
    assert track_profiles.get(track_hash) is not None


def test_builds_partial_profile_from_librosa(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    _upsert_run(track_hash, "librosa", "0.1.0", _beatgrid_output(bpm=128.0))

    profile = profile_builder.build_profile(track_hash)

    assert profile.readiness is Readiness.PARTIAL
    assert profile.fields.has_beat_grid is True
    assert profile.fields.has_sections is True
    assert profile.tempo is not None and profile.tempo.bpm == 128.0
    assert profile.beat_grid is not None
    assert profile.beat_grid.provenance.source == "librosa@0.1.0"
    assert profile.sections is not None and profile.sections.provenance.source == "librosa@0.1.0"
    # beat_grid + sections, no key/energy/vocals.
    assert profile.completeness_score == 0.5


def test_prefers_allin1_remote_over_librosa(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    _upsert_run(track_hash, "librosa", "0.1.0", _beatgrid_output(bpm=100.0))
    _upsert_run(track_hash, "allin1_remote", "0.1.0", _beatgrid_output(bpm=128.0))

    profile = profile_builder.build_profile(track_hash)

    assert profile.beat_grid is not None
    assert profile.beat_grid.provenance.source == "allin1_remote@0.1.0"
    assert profile.tempo is not None and profile.tempo.bpm == 128.0


def test_selects_essentia_key(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    _upsert_run(track_hash, "librosa", "0.1.0", _beatgrid_output())
    _upsert_run(track_hash, "essentia", "0.1.0", _key_output())

    profile = profile_builder.build_profile(track_hash)

    assert profile.fields.has_key is True
    assert profile.key is not None
    assert profile.key.key == "C" and profile.key.scale == "major"
    assert profile.key.camelot == "8B"
    assert profile.key.provenance.source == "essentia@0.1.0"
    # beat_grid + sections + key = 0.4 + 0.1 + 0.2.
    assert profile.completeness_score == 0.7


def test_preserves_provenance_run_ids(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    grid_run = _upsert_run(track_hash, "librosa", "0.1.0", _beatgrid_output())
    key_run = _upsert_run(track_hash, "essentia", "0.1.0", _key_output())

    profile = profile_builder.build_profile(track_hash)

    assert profile.beat_grid is not None
    assert profile.tempo is not None
    assert profile.beat_grid.provenance.analysis_run_id == grid_run.id
    assert profile.tempo.provenance.analysis_run_id == grid_run.id
    assert profile.key is not None
    assert profile.key.provenance.analysis_run_id == key_run.id


# ---------------------------------------------------------------------------
# Tolerance: non-completed and malformed runs
# ---------------------------------------------------------------------------


def test_ignores_non_completed_runs(tmp_aidj, tmp_path: Path) -> None:
    """A FAILED higher-priority run must not win over a COMPLETED lower one."""
    track_hash = _ingested_track(tmp_path)
    _upsert_run(track_hash, "allin1_remote", "0.1.0", None, status=AnalysisStatus.FAILED)
    _upsert_run(track_hash, "librosa", "0.1.0", _beatgrid_output(bpm=99.0))

    profile = profile_builder.build_profile(track_hash)

    assert profile.beat_grid is not None
    assert profile.beat_grid.provenance.source == "librosa@0.1.0"
    assert profile.tempo is not None and profile.tempo.bpm == 99.0


def test_ignores_running_runs(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    _upsert_run(track_hash, "librosa", "0.1.0", None, status=AnalysisStatus.RUNNING)

    profile = profile_builder.build_profile(track_hash)

    assert profile.readiness is Readiness.BLOCKED


def test_ignores_malformed_output_without_crashing(tmp_aidj, tmp_path: Path) -> None:
    """Garbage from a high-priority analyzer is skipped; the funnel falls
    through to the next usable source."""
    track_hash = _ingested_track(tmp_path)
    _upsert_run(track_hash, "allin1_remote", "0.1.0", {"raw": "not a beat grid"})
    _upsert_run(track_hash, "allin1", "1.1.0", {"tempo": {"bpm": -5.0}, "beats": []})
    _upsert_run(track_hash, "librosa", "0.1.0", _beatgrid_output(bpm=123.0))

    profile = profile_builder.build_profile(track_hash)

    assert profile.beat_grid is not None
    assert profile.beat_grid.provenance.source == "librosa@0.1.0"
    assert profile.tempo is not None and profile.tempo.bpm == 123.0


def test_single_beat_is_not_a_grid(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    one_beat = _beatgrid_output(n_beats=1)
    _upsert_run(track_hash, "librosa", "0.1.0", one_beat)

    profile = profile_builder.build_profile(track_hash)

    assert profile.readiness is Readiness.BLOCKED


def test_duplicate_beat_times_are_not_a_grid(tmp_aidj, tmp_path: Path) -> None:
    """Two entries at the same time_sec collapse to one distinct moment — not
    enough to span a grid, even though len(beats) == 2."""
    track_hash = _ingested_track(tmp_path)
    out = _beatgrid_output()
    out["beats"] = [
        {"time_sec": 0.0, "is_downbeat": True},
        {"time_sec": 0.0, "is_downbeat": False},
    ]
    _upsert_run(track_hash, "librosa", "0.1.0", out)

    profile = profile_builder.build_profile(track_hash)

    assert profile.readiness is Readiness.BLOCKED


def test_zero_duration_is_rejected(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    out = _beatgrid_output()
    out["duration_sec"] = 0.0
    _upsert_run(track_hash, "librosa", "0.1.0", out)

    profile = profile_builder.build_profile(track_hash)

    assert profile.readiness is Readiness.BLOCKED


def test_duration_shorter_than_last_beat_is_rejected(tmp_aidj, tmp_path: Path) -> None:
    """A reported duration that ends before the last detected beat is internally
    contradictory; the grid is dropped rather than silently repaired."""
    track_hash = _ingested_track(tmp_path)
    out = _beatgrid_output()  # 8 beats, last at 3.5s
    out["duration_sec"] = 1.0
    _upsert_run(track_hash, "librosa", "0.1.0", out)

    profile = profile_builder.build_profile(track_hash)

    assert profile.readiness is Readiness.BLOCKED


# ---------------------------------------------------------------------------
# Missing-track guard
# ---------------------------------------------------------------------------


def test_build_raises_track_not_found_for_unknown_hash(tmp_aidj) -> None:
    """Builder is a public entry point; an unknown hash must surface as a
    predictable domain error rather than a raw SQLite IntegrityError."""
    try:
        profile_builder.build_profile("0" * 64)
    except TrackNotFoundError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected TrackNotFoundError for unknown track hash")

    # And nothing was written for the phantom hash.
    count = db.fetch_one("SELECT COUNT(*) AS n FROM track_profiles")
    assert count["n"] == 0


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_sections_absent_does_not_block_beat_grid(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    _upsert_run(track_hash, "librosa", "0.1.0", _beatgrid_output(with_sections=False))

    profile = profile_builder.build_profile(track_hash)

    assert profile.fields.has_beat_grid is True
    assert profile.fields.has_sections is False
    assert profile.sections is None
    assert profile.completeness_score == 0.4  # beat grid only


def test_unsorted_beats_are_sorted(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    out = _beatgrid_output()
    out["beats"] = list(reversed(out["beats"]))
    _upsert_run(track_hash, "librosa", "0.1.0", out)

    profile = profile_builder.build_profile(track_hash)

    assert profile.beat_grid is not None
    times = [b.time_sec for b in profile.beat_grid.beats]
    assert times == sorted(times)


def test_duration_derived_from_last_beat_when_missing(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    out = _beatgrid_output(n_beats=8)
    out.pop("duration_sec")
    _upsert_run(track_hash, "librosa", "0.1.0", out)

    profile = profile_builder.build_profile(track_hash)

    assert profile.beat_grid is not None
    assert profile.beat_grid.duration_sec == 3.5  # last beat at index 7 → 3.5s


def test_unknown_section_label_falls_back_to_unknown(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    out = _beatgrid_output()
    out["sections"] = [{"start_sec": 0.0, "end_sec": 4.0, "label": "pre-chorus"}]
    _upsert_run(track_hash, "librosa", "0.1.0", out)

    profile = profile_builder.build_profile(track_hash)

    assert profile.sections is not None
    assert profile.sections.items[0].label.value == "unknown"


# ---------------------------------------------------------------------------
# Idempotency / staleness
# ---------------------------------------------------------------------------


def test_idempotent_returns_cached_when_fresh(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    _upsert_run(track_hash, "librosa", "0.1.0", _beatgrid_output())

    first = profile_builder.build_profile(track_hash)
    second = profile_builder.build_profile(track_hash)

    assert second.built_at == first.built_at  # not rebuilt
    count = db.fetch_one("SELECT COUNT(*) AS n FROM track_profiles")
    assert count["n"] == 1


def test_force_rebuild_reselects_higher_priority_source(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    _upsert_run(track_hash, "librosa", "0.1.0", _beatgrid_output(bpm=100.0))
    first = profile_builder.build_profile(track_hash)
    assert first.beat_grid is not None
    assert first.beat_grid.provenance.source == "librosa@0.1.0"

    # A higher-priority analyzer arrives; force a rebuild.
    _upsert_run(track_hash, "allin1_remote", "0.1.0", _beatgrid_output(bpm=128.0))
    rebuilt = profile_builder.build_profile(track_hash, force=True)

    assert rebuilt.beat_grid is not None
    assert rebuilt.beat_grid.provenance.source == "allin1_remote@0.1.0"
    assert rebuilt.tempo is not None and rebuilt.tempo.bpm == 128.0
    count = db.fetch_one("SELECT COUNT(*) AS n FROM track_profiles")
    assert count["n"] == 1


def test_stale_profile_is_rebuilt_without_force(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    _upsert_run(track_hash, "librosa", "0.1.0", _beatgrid_output())
    profile_builder.build_profile(track_hash)

    # Simulate a builder-version bump → existing profile is stale by version.
    db.execute(
        "UPDATE track_profiles SET profile_version=? WHERE track_hash=?",
        (CURRENT_PROFILE_VERSION - 1, track_hash),
    )
    assert track_profiles.is_stale(track_hash) is True

    rebuilt = profile_builder.build_profile(track_hash)
    assert rebuilt.profile_version == CURRENT_PROFILE_VERSION
    assert track_profiles.is_stale(track_hash) is False
