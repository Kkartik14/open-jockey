"""Track-profile repository + schema contract tests (Phase 2, step 1)."""

from __future__ import annotations

from pathlib import Path

from aidj.store import analysis_runs, db, track_profiles, tracks
from aidj.store._timestamps import utc_now_iso
from aidj.store.models import (
    CURRENT_PROFILE_VERSION,
    AnalysisStatus,
    Beat,
    BeatGridBlock,
    CompletenessFields,
    EnergyBlock,
    FieldProvenance,
    KeyBlock,
    Readiness,
    Section,
    SectionLabel,
    SectionsBlock,
    TempoBlock,
    TrackProfile,
    VocalWindow,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ingested_track(tmp_path: Path, byte: bytes = b"x") -> str:
    """Ingest a unique tiny file and return its content hash."""
    p = tmp_path / f"sample-{byte.hex()}.bin"
    p.write_bytes(byte * 32)
    return tracks.ingest(p).content_hash


def _provenance(
    *, source: str = "allin1@1.1.0", analysis_run_id: int | None = None
) -> FieldProvenance:
    return FieldProvenance(source=source, analysis_run_id=analysis_run_id)


def _minimal_beatgrid_profile(track_hash: str, *, run_id: int | None = None) -> TrackProfile:
    """A profile with tempo + beat_grid + sections filled, key/energy/vocals absent."""
    prov = _provenance(analysis_run_id=run_id)
    return TrackProfile(
        profile_version=CURRENT_PROFILE_VERSION,
        track_hash=track_hash,
        built_at=utc_now_iso(),
        readiness=Readiness.PARTIAL,
        completeness_score=0.4,
        fields=CompletenessFields(
            has_beat_grid=True,
            has_sections=True,
            has_key=False,
            has_energy=False,
            has_vocals=False,
        ),
        tempo=TempoBlock(bpm=124.0, confidence=0.83, provenance=prov),
        beat_grid=BeatGridBlock(
            beats=[Beat(time_sec=0.0, is_downbeat=True), Beat(time_sec=0.5)],
            downbeat_count=1,
            duration_sec=240.0,
            provenance=prov,
        ),
        sections=SectionsBlock(
            items=[Section(start_sec=0.0, end_sec=8.0, label=SectionLabel.INTRO)],
            provenance=prov,
        ),
    )


# ---------------------------------------------------------------------------
# Schema / CRUD
# ---------------------------------------------------------------------------


def test_health_reports_schema_v6(tmp_aidj) -> None:
    """Sanity check that the schema bump is wired through schema_meta."""
    row = db.fetch_one("SELECT value FROM schema_meta WHERE key='schema_version'")
    assert row is not None and int(row["value"]) == 6


def test_upsert_then_get_roundtrips_full_profile(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    profile = _minimal_beatgrid_profile(track_hash)
    profile = profile.model_copy(
        update={
            "fields": profile.fields.model_copy(update={"has_key": True}),
            "key": KeyBlock(
                key="C",
                scale="major",
                camelot="8B",
                confidence=0.7,
                provenance=_provenance(source="essentia@0.1.0"),
            ),
        }
    )

    track_profiles.upsert(profile)
    fetched = track_profiles.get(track_hash)
    assert fetched is not None
    # Full JSON round-trip preserves every block and its provenance.
    assert fetched.model_dump() == profile.model_dump()


def test_upsert_revalidates_before_persisting(tmp_aidj, tmp_path: Path) -> None:
    """Pydantic model_copy can create an invalid frozen model; the repository
    must reject it before profile_json becomes the persistent source of truth."""
    track_hash = _ingested_track(tmp_path)
    profile = _minimal_beatgrid_profile(track_hash)
    invalid = profile.model_copy(
        update={
            "key": KeyBlock(
                key="C",
                scale="major",
                camelot="8B",
                confidence=0.7,
                provenance=_provenance(source="essentia@0.1.0"),
            ),
        }
    )

    try:
        track_profiles.upsert(invalid)
    except ValueError as exc:
        assert "completeness fields" in str(exc)
    else:
        raise AssertionError("expected invalid copied profile to be rejected")

    count = db.fetch_one("SELECT COUNT(*) AS n FROM track_profiles")
    assert count["n"] == 0


def test_upsert_is_idempotent_replaces_in_place(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    first = _minimal_beatgrid_profile(track_hash)
    track_profiles.upsert(first)

    # Same primary key, different readiness/score → replaces.
    second = first.model_copy(
        update={
            "readiness": Readiness.READY,
            "completeness_score": 1.0,
        }
    )
    track_profiles.upsert(second)

    fetched = track_profiles.get(track_hash)
    assert fetched is not None
    assert fetched.readiness is Readiness.READY
    assert fetched.completeness_score == 1.0
    # And only one row in the table.
    count = db.fetch_one("SELECT COUNT(*) AS n FROM track_profiles")
    assert count["n"] == 1


def test_delete_removes_profile(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    track_profiles.upsert(_minimal_beatgrid_profile(track_hash))
    assert track_profiles.delete(track_hash) is True
    assert track_profiles.get(track_hash) is None
    assert track_profiles.delete(track_hash) is False


def test_track_delete_cascades_to_profile(tmp_aidj, tmp_path: Path) -> None:
    """Phase 2 schema FK has ON DELETE CASCADE — same as analysis_runs."""
    track_hash = _ingested_track(tmp_path)
    track_profiles.upsert(_minimal_beatgrid_profile(track_hash))

    assert tracks.delete(track_hash) is True
    assert track_profiles.get(track_hash) is None


def test_list_all_orders_by_most_recently_built(tmp_aidj, tmp_path: Path) -> None:
    a = _ingested_track(tmp_path, byte=b"a")
    b = _ingested_track(tmp_path, byte=b"b")

    older = _minimal_beatgrid_profile(a).model_copy(update={"built_at": "2026-01-01 00:00:00"})
    newer = _minimal_beatgrid_profile(b).model_copy(update={"built_at": "2026-05-01 00:00:00"})
    track_profiles.upsert(older)
    track_profiles.upsert(newer)

    listed = track_profiles.list_all()
    assert [p.track_hash for p in listed] == [b, a]


def test_readiness_check_constraint_rejects_invalid_value(tmp_aidj, tmp_path: Path) -> None:
    """The DB CHECK constraint mirrors the Readiness enum; SQLite refuses
    anything outside ('ready','partial','blocked')."""
    import sqlite3

    track_hash = _ingested_track(tmp_path)
    try:
        db.execute(
            "INSERT INTO track_profiles "
            "(track_hash, profile_version, profile_json, readiness, "
            " completeness_score, built_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (track_hash, CURRENT_PROFILE_VERSION, "{}", "bogus", 0.5, utc_now_iso()),
        )
    except sqlite3.IntegrityError as exc:
        assert "CHECK" in str(exc) or "constraint" in str(exc).lower()
    else:
        raise AssertionError("expected CHECK constraint to reject 'bogus' readiness")


def test_profile_model_rejects_inconsistent_downbeat_count() -> None:
    profile = _minimal_beatgrid_profile("a" * 64)
    raw = profile.model_dump()
    raw["beat_grid"]["downbeat_count"] = 99
    try:
        TrackProfile.model_validate(raw)
    except ValueError as exc:
        assert "downbeat_count" in str(exc)
    else:
        raise AssertionError("expected invalid downbeat_count to be rejected")


def test_profile_model_rejects_completeness_block_mismatch() -> None:
    profile = _minimal_beatgrid_profile("b" * 64)
    raw = profile.model_dump()
    raw["fields"]["has_key"] = True

    try:
        TrackProfile.model_validate(raw)
    except ValueError as exc:
        assert "completeness fields" in str(exc)
    else:
        raise AssertionError("expected mismatched completeness fields to be rejected")


def test_energy_block_requires_normalised_values() -> None:
    try:
        EnergyBlock(
            sample_rate_hz=2.0,
            values=[0.0, 0.5, 1.2],
            provenance=_provenance(source="aidj.energy@0.1.0"),
        )
    except ValueError as exc:
        assert "normalised" in str(exc)
    else:
        raise AssertionError("expected out-of-range energy value to be rejected")


def test_vocal_window_requires_positive_duration() -> None:
    try:
        VocalWindow(start_sec=10.0, end_sec=10.0, is_vocal=True)
    except ValueError as exc:
        assert "end_sec" in str(exc)
    else:
        raise AssertionError("expected zero-length vocal window to be rejected")


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_is_stale_true_when_no_profile(tmp_aidj, tmp_path: Path) -> None:
    track_hash = _ingested_track(tmp_path)
    assert track_profiles.is_stale(track_hash) is True


def test_is_stale_false_for_current_profile_with_no_runs(tmp_aidj, tmp_path: Path) -> None:
    """Backend-derived blocks (analysis_run_id=None) leave nothing to compare
    against — a current-version profile is considered fresh."""
    track_hash = _ingested_track(tmp_path)
    track_profiles.upsert(_minimal_beatgrid_profile(track_hash, run_id=None))
    assert track_profiles.is_stale(track_hash) is False


def test_is_stale_true_when_profile_version_below_current(tmp_aidj, tmp_path: Path) -> None:
    """A profile written by an older builder is stale by version alone, even
    if no analyzer has re-run since."""
    track_hash = _ingested_track(tmp_path)
    profile = _minimal_beatgrid_profile(track_hash)
    track_profiles.upsert(profile)
    # Backdoor an older version into the row to simulate a builder upgrade.
    db.execute(
        "UPDATE track_profiles SET profile_version=? WHERE track_hash=?",
        (CURRENT_PROFILE_VERSION - 1, track_hash),
    )
    assert track_profiles.is_stale(track_hash) is True


def test_is_stale_true_when_source_run_finished_after_profile(
    tmp_aidj,
    tmp_path: Path,
) -> None:
    """If an analyzer the profile cites has finished newer than ``built_at``,
    the profile is stale — the builder needs to re-pick its sources."""
    track_hash = _ingested_track(tmp_path)
    run = analysis_runs.upsert(
        track_hash=track_hash,
        analyzer_name="allin1",
        analyzer_version="1.1.0",
        status=AnalysisStatus.COMPLETED,
        output={"tempo": {"bpm": 124.0}, "beats": [], "sections": [], "duration_sec": 0.0},
        started_at="2026-01-01 00:00:00",
        finished_at="2026-01-01 00:00:01",
    )

    profile = _minimal_beatgrid_profile(track_hash, run_id=run.id).model_copy(
        update={"built_at": "2026-01-01 00:00:02"},
    )
    track_profiles.upsert(profile)
    assert track_profiles.is_stale(track_hash) is False

    # Re-run the analyzer (newer finished_at).
    db.execute(
        "UPDATE analysis_runs SET finished_at=? WHERE id=?",
        ("2026-05-01 00:00:00", run.id),
    )
    assert track_profiles.is_stale(track_hash) is True


def test_is_stale_true_when_unreferenced_run_finished_after_profile(
    tmp_aidj,
    tmp_path: Path,
) -> None:
    """A newer analyzer run can change source selection even if the old profile
    did not reference it, so any newer completed run for the track stales the
    profile."""
    track_hash = _ingested_track(tmp_path)
    old_run = analysis_runs.upsert(
        track_hash=track_hash,
        analyzer_name="librosa",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
        output={"tempo": {"bpm": 124.0}, "beats": [], "sections": [], "duration_sec": 0.0},
        started_at="2026-01-01 00:00:00",
        finished_at="2026-01-01 00:00:01",
    )
    profile = _minimal_beatgrid_profile(track_hash, run_id=old_run.id).model_copy(
        update={"built_at": "2026-01-01 00:00:02"},
    )
    track_profiles.upsert(profile)
    assert track_profiles.is_stale(track_hash) is False

    analysis_runs.upsert(
        track_hash=track_hash,
        analyzer_name="allin1_remote",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
        output={"tempo": {"bpm": 125.0}, "beats": [], "sections": [], "duration_sec": 0.0},
        started_at="2026-05-01 00:00:00",
        finished_at="2026-05-01 00:00:01",
    )
    assert track_profiles.is_stale(track_hash) is True


def test_is_stale_true_when_source_run_deleted(tmp_aidj, tmp_path: Path) -> None:
    """Broken provenance should force a rebuild instead of silently treating
    the existing profile as authoritative."""
    track_hash = _ingested_track(tmp_path)
    run = analysis_runs.upsert(
        track_hash=track_hash,
        analyzer_name="allin1",
        analyzer_version="1.1.0",
        status=AnalysisStatus.COMPLETED,
        output={"tempo": {"bpm": 124.0}, "beats": [], "sections": [], "duration_sec": 0.0},
        started_at="2026-01-01 00:00:00",
        finished_at="2026-01-01 00:00:01",
    )
    track_profiles.upsert(
        _minimal_beatgrid_profile(track_hash, run_id=run.id).model_copy(
            update={"built_at": "2026-01-01 00:00:02"},
        )
    )
    assert track_profiles.is_stale(track_hash) is False

    assert analysis_runs.delete(track_hash, "allin1", "1.1.0") is True
    assert track_profiles.is_stale(track_hash) is True


# ---------------------------------------------------------------------------
# Coverage summary (drives Library readiness panel)
# ---------------------------------------------------------------------------


def test_coverage_counts_empty_when_no_tracks(tmp_aidj) -> None:
    counts = track_profiles.coverage_counts()
    assert counts == {"ready": 0, "partial": 0, "blocked": 0, "missing": 0}


def test_coverage_counts_buckets_tracks_without_profiles_as_missing(
    tmp_aidj,
    tmp_path: Path,
) -> None:
    a = _ingested_track(tmp_path, byte=b"a")
    b = _ingested_track(tmp_path, byte=b"b")
    _ingested_track(tmp_path, byte=b"c")

    track_profiles.upsert(
        _minimal_beatgrid_profile(a).model_copy(update={"readiness": Readiness.READY})
    )
    track_profiles.upsert(
        _minimal_beatgrid_profile(b).model_copy(update={"readiness": Readiness.PARTIAL})
    )
    # c has no profile.

    counts = track_profiles.coverage_counts()
    assert counts == {"ready": 1, "partial": 1, "blocked": 0, "missing": 1}
