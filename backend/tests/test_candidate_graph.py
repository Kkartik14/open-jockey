"""Phase 3 Transition Candidate Graph."""

from __future__ import annotations

from pathlib import Path

import pytest

from aidj.candidate_graph import ProjectNotFoundError, build_candidate_graph
from aidj.store import (
    analysis_labels,
    analysis_runs,
    candidates,
    db,
    projects,
    track_profiles,
    tracks,
)
from aidj.store._timestamps import utc_now_iso
from aidj.store.models import (
    AnalysisLabelKind,
    AnalysisStatus,
    Beat,
    BeatGridBlock,
    CandidateVerification,
    CompletenessFields,
    FieldProvenance,
    KeyBlock,
    Readiness,
    TempoBlock,
    TrackProfile,
    TransitionTechnique,
)


def _track(tmp_path: Path, name: str) -> str:
    p = tmp_path / f"{name}.bin"
    p.write_bytes((name * 32).encode())
    return tracks.ingest(p).content_hash


def _analysis_run(track_hash: str, name: str = "librosa"):
    return analysis_runs.upsert(
        track_hash=track_hash,
        analyzer_name=name,
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
        output={
            "tempo": {"bpm": 120.0},
            "beats": [{"time_sec": 0.0}],
            "sections": [],
            "duration_sec": 1.0,
        },
        started_at="2026-01-01 00:00:00",
        finished_at="2026-01-01 00:00:01",
    )


def _profile(
    track_hash: str,
    *,
    bpm: float,
    run_id: int | None = None,
    camelot: str | None = None,
    readiness: Readiness = Readiness.PARTIAL,
) -> TrackProfile:
    prov = FieldProvenance(source="librosa@0.1.0", analysis_run_id=run_id)
    beats = [
        Beat(time_sec=round(i * (60.0 / bpm), 3), is_downbeat=(i % 4 == 0)) for i in range(128)
    ]
    key = (
        KeyBlock(key="C", scale="major", camelot=camelot, confidence=None, provenance=prov)
        if camelot
        else None
    )
    return TrackProfile(
        profile_version=1,
        track_hash=track_hash,
        built_at=utc_now_iso(),
        readiness=readiness,
        completeness_score=0.7 if key else 0.4,
        fields=CompletenessFields(
            has_beat_grid=readiness is not Readiness.BLOCKED,
            has_key=key is not None,
        ),
        tempo=TempoBlock(bpm=bpm, confidence=None, provenance=prov)
        if readiness is not Readiness.BLOCKED
        else None,
        beat_grid=BeatGridBlock(
            beats=beats,
            downbeat_count=sum(1 for b in beats if b.is_downbeat),
            duration_sec=beats[-1].time_sec + 2.0,
            provenance=prov,
        )
        if readiness is not Readiness.BLOCKED
        else None,
        key=key,
    )


def test_project_and_candidate_repository_roundtrip(tmp_aidj, tmp_path: Path) -> None:
    left = _track(tmp_path, "left")
    right = _track(tmp_path, "right")
    lrun = _analysis_run(left)
    rrun = _analysis_run(right)
    track_profiles.upsert(_profile(left, bpm=124.0, run_id=lrun.id))
    track_profiles.upsert(_profile(right, bpm=126.0, run_id=rrun.id))
    project = projects.create("  Truth Test Mix  ", intent="test graph")

    result = build_candidate_graph(project.id, track_hashes=[left, right])

    assert result.project.name == "Truth Test Mix"
    assert result.requested_tracks == 2
    assert result.usable_tracks == 2
    assert len(result.candidates) == 6  # three choices for each directed pair
    stored = candidates.list_for_project(project.id)
    assert len(stored) == 6
    first = stored[0]
    assert first.id is not None
    assert first.from_track in {left, right}
    assert first.to_track in {left, right}
    assert first.from_track != first.to_track
    assert first.from_cue_bar % 8 == 0
    assert first.to_cue_bar == 0
    assert TransitionTechnique.PHRASE_SWAP in first.allowed_techniques
    assert first.scores.score > 0.8
    assert first.scores.verification is CandidateVerification.UNVERIFIED


def test_candidate_repository_rejects_non_json_techniques(tmp_aidj, tmp_path: Path) -> None:
    left = _track(tmp_path, "left")
    right = _track(tmp_path, "right")
    track_profiles.upsert(_profile(left, bpm=124.0))
    track_profiles.upsert(_profile(right, bpm=126.0))
    project = projects.create("bad techniques")
    result = build_candidate_graph(project.id, track_hashes=[left, right])
    first_id = result.candidates[0].id
    assert first_id is not None

    db.execute(
        "UPDATE candidates SET allowed_techniques=? WHERE id=?",
        ("long_crossfade,echo_out", first_id),
    )

    with pytest.raises(ValueError):
        candidates.list_for_project(project.id)


def test_candidate_graph_prunes_tempo_incompatible_pairs(tmp_aidj, tmp_path: Path) -> None:
    left = _track(tmp_path, "left")
    right = _track(tmp_path, "right")
    track_profiles.upsert(_profile(left, bpm=124.0))
    track_profiles.upsert(_profile(right, bpm=92.0))
    project = projects.create("tempo prune")

    result = build_candidate_graph(project.id, track_hashes=[left, right])

    assert result.usable_tracks == 2
    assert result.candidates == []


def test_candidate_graph_skips_missing_and_blocked_profiles(tmp_aidj, tmp_path: Path) -> None:
    good = _track(tmp_path, "good")
    blocked = _track(tmp_path, "blocked")
    missing = _track(tmp_path, "missing")
    track_profiles.upsert(_profile(good, bpm=120.0))
    track_profiles.upsert(_profile(blocked, bpm=120.0, readiness=Readiness.BLOCKED))
    project = projects.create("skips")

    result = build_candidate_graph(
        project.id,
        track_hashes=[good, blocked, missing, "0" * 64],
    )

    assert result.usable_tracks == 1
    assert result.candidates == []
    assert result.skipped_tracks[blocked] == "profile_blocked"
    assert result.skipped_tracks[missing] == "profile_missing"
    assert result.skipped_tracks["0" * 64] == "track_missing"


def test_candidate_graph_replaces_existing_candidates(tmp_aidj, tmp_path: Path) -> None:
    a = _track(tmp_path, "a")
    b = _track(tmp_path, "b")
    c = _track(tmp_path, "c")
    for h, bpm in [(a, 120.0), (b, 121.0), (c, 122.0)]:
        track_profiles.upsert(_profile(h, bpm=bpm))
    project = projects.create("replace")

    first = build_candidate_graph(project.id, track_hashes=[a, b])
    assert len(first.candidates) == 6
    second = build_candidate_graph(project.id, track_hashes=[a, b, c])

    assert len(second.candidates) == 18
    assert len(candidates.list_for_project(project.id)) == 18


def test_candidate_graph_pre_prunes_targets_per_source(tmp_aidj, tmp_path: Path) -> None:
    hashes: list[str] = []
    for i in range(15):
        h = _track(tmp_path, f"track-{i}")
        hashes.append(h)
        track_profiles.upsert(_profile(h, bpm=124.0 + i * 0.1))
    project = projects.create("pre-prune")

    result = build_candidate_graph(project.id, track_hashes=hashes)

    assert result.usable_tracks == 15
    assert len(result.candidates) == 15 * 12 * 3


def test_candidate_graph_respects_force_false(tmp_aidj, tmp_path: Path) -> None:
    a = _track(tmp_path, "a")
    b = _track(tmp_path, "b")
    c = _track(tmp_path, "c")
    for h, bpm in [(a, 120.0), (b, 121.0), (c, 122.0)]:
        track_profiles.upsert(_profile(h, bpm=bpm))
    project = projects.create("cached")
    first = build_candidate_graph(project.id, track_hashes=[a, b])

    cached = build_candidate_graph(project.id, track_hashes=[a, b, c], force=False)

    assert [c.id for c in cached.candidates] == [c.id for c in first.candidates]
    assert "returned existing candidates" in cached.warnings[0]


def test_candidate_graph_uses_label_verification_status(tmp_aidj, tmp_path: Path) -> None:
    left = _track(tmp_path, "left")
    right = _track(tmp_path, "right")
    lrun = _analysis_run(left)
    rrun = _analysis_run(right)
    analysis_labels.add(analysis_run_id=lrun.id, kind=AnalysisLabelKind.CORRECT)
    analysis_labels.add(analysis_run_id=rrun.id, kind=AnalysisLabelKind.CORRECT)
    track_profiles.upsert(_profile(left, bpm=124.0, run_id=lrun.id, camelot="8B"))
    track_profiles.upsert(_profile(right, bpm=124.5, run_id=rrun.id, camelot="9B"))
    project = projects.create("verified")

    result = build_candidate_graph(project.id, track_hashes=[left, right])

    assert result.candidates[0].scores.verification is CandidateVerification.VERIFIED
    assert result.candidates[0].scores.key_compatible is True
    assert "verified_sources" in result.candidates[0].scores.reasons


def test_candidate_graph_accepts_relative_camelot_modes(tmp_aidj, tmp_path: Path) -> None:
    left = _track(tmp_path, "left")
    right = _track(tmp_path, "right")
    track_profiles.upsert(_profile(left, bpm=124.0, camelot="8A"))
    track_profiles.upsert(_profile(right, bpm=124.5, camelot="8B"))
    project = projects.create("relative modes")

    result = build_candidate_graph(project.id, track_hashes=[left, right])

    assert result.candidates
    assert {c.scores.key_compatible for c in result.candidates} == {True}
    assert all("harmonic_match" in c.scores.reasons for c in result.candidates)


def test_candidate_graph_penalizes_failure_labeled_sources(tmp_aidj, tmp_path: Path) -> None:
    left = _track(tmp_path, "left")
    right = _track(tmp_path, "right")
    lrun = _analysis_run(left)
    rrun = _analysis_run(right)
    analysis_labels.add(analysis_run_id=lrun.id, kind=AnalysisLabelKind.CORRECT)
    analysis_labels.add(analysis_run_id=rrun.id, kind=AnalysisLabelKind.WRONG_DOWNBEAT_PHASE)
    track_profiles.upsert(_profile(left, bpm=124.0, run_id=lrun.id))
    track_profiles.upsert(_profile(right, bpm=124.5, run_id=rrun.id))
    project = projects.create("failure")

    result = build_candidate_graph(project.id, track_hashes=[left, right])

    assert result.candidates[0].scores.verification is CandidateVerification.HAS_FAILURE_LABEL
    assert "failure_labeled_source" in result.candidates[0].scores.reasons


def test_candidate_graph_rejects_unknown_project(tmp_aidj) -> None:
    try:
        build_candidate_graph(999)
    except ProjectNotFoundError as exc:
        assert "999" in str(exc)
    else:
        raise AssertionError("expected ProjectNotFoundError")


def test_track_delete_cascades_candidate_edges(tmp_aidj, tmp_path: Path) -> None:
    left = _track(tmp_path, "left")
    right = _track(tmp_path, "right")
    track_profiles.upsert(_profile(left, bpm=124.0))
    track_profiles.upsert(_profile(right, bpm=126.0))
    project = projects.create("cascade")
    result = build_candidate_graph(project.id, track_hashes=[left, right])
    assert result.candidates

    assert tracks.delete(left) is True

    assert candidates.list_for_project(project.id) == []
