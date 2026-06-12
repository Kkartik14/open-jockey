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
    render_artifacts,
    render_labels,
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
    RenderConfidenceSnapshot,
    RenderLabelKind,
    RenderRequestConfig,
    RenderTechnique,
    SourceAnchorPolicy,
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


def _render_request_config() -> RenderRequestConfig:
    return RenderRequestConfig(
        source_anchor_policy=SourceAnchorPolicy.KEEP_OUTGOING_TEMPO,
        from_cue_sec=10.0,
        to_cue_sec=4.0,
        from_bpm=124.0,
        to_bpm=124.5,
        tempo_match_ratio=0.996,
        tempo_match_ratio_source="candidate",
        transition_length_sec=8.0,
        source_lead_in_sec=12.0,
        target_tail_sec=24.0,
        loudness_target_lufs=-14.0,
        output_sample_rate=44_100,
        output_channels=2,
        confidence_snapshot=RenderConfidenceSnapshot(
            from_beat_source="librosa@0.1.0",
            to_beat_source="librosa@0.1.0",
        ),
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


def test_candidate_graph_rebuild_preserves_matching_candidate_ids(tmp_aidj, tmp_path: Path) -> None:
    a = _track(tmp_path, "a")
    b = _track(tmp_path, "b")
    for h, bpm in [(a, 120.0), (b, 121.0)]:
        track_profiles.upsert(_profile(h, bpm=bpm))
    project = projects.create("stable ids")
    first = build_candidate_graph(project.id, track_hashes=[a, b])
    ids_by_key = {
        (c.from_track, c.to_track, c.from_cue_bar, c.to_cue_bar): c.id for c in first.candidates
    }

    # Rebuilding the same natural keys with changed scores must not churn ids.
    track_profiles.upsert(_profile(b, bpm=120.5))
    second = build_candidate_graph(project.id, track_hashes=[a, b])

    assert {
        (c.from_track, c.to_track, c.from_cue_bar, c.to_cue_bar): c.id for c in second.candidates
    } == ids_by_key


def test_candidate_graph_rebuild_deletes_only_candidates_no_longer_produced(
    tmp_aidj, tmp_path: Path
) -> None:
    a = _track(tmp_path, "a")
    b = _track(tmp_path, "b")
    c = _track(tmp_path, "c")
    for h, bpm in [(a, 120.0), (b, 121.0), (c, 122.0)]:
        track_profiles.upsert(_profile(h, bpm=bpm))
    project = projects.create("stable delete")
    first = build_candidate_graph(project.id, track_hashes=[a, b, c])
    ab_ids = {
        (edge.from_track, edge.to_track, edge.from_cue_bar, edge.to_cue_bar): edge.id
        for edge in first.candidates
        if c not in {edge.from_track, edge.to_track}
    }

    second = build_candidate_graph(project.id, track_hashes=[a, b])

    assert len(second.candidates) == 6
    assert {
        (edge.from_track, edge.to_track, edge.from_cue_bar, edge.to_cue_bar): edge.id
        for edge in second.candidates
    } == ab_ids
    assert all(c not in {edge.from_track, edge.to_track} for edge in second.candidates)


def test_candidate_graph_stores_tempo_match_ratio_for_half_time_pairs(
    tmp_aidj, tmp_path: Path
) -> None:
    full_time = _track(tmp_path, "full-time")
    half_time = _track(tmp_path, "half-time")
    track_profiles.upsert(_profile(full_time, bpm=124.0))
    track_profiles.upsert(_profile(half_time, bpm=62.0))
    project = projects.create("tempo ratio")

    result = build_candidate_graph(project.id, track_hashes=[full_time, half_time])

    forward = next(c for c in result.candidates if c.from_track == full_time)
    backward = next(c for c in result.candidates if c.from_track == half_time)
    assert forward.scores.tempo_delta_pct == 0.0
    assert forward.scores.tempo_match_ratio == 2.0
    assert backward.scores.tempo_match_ratio == 0.5


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


def test_candidate_graph_stops_advertising_hard_failed_render_technique(
    tmp_aidj, tmp_path: Path
) -> None:
    left = _track(tmp_path, "left")
    right = _track(tmp_path, "right")
    track_profiles.upsert(_profile(left, bpm=124.0))
    track_profiles.upsert(_profile(right, bpm=124.5))
    tracks.set_genre(left, "house")
    tracks.set_genre(right, "house")
    project = projects.create("feedback")
    first = build_candidate_graph(project.id, track_hashes=[left, right])
    candidate = next(c for c in first.candidates if c.from_track == left)
    assert candidate.id is not None
    assert TransitionTechnique.LONG_CROSSFADE in candidate.allowed_techniques

    render = render_artifacts.create_running(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.LONG_CROSSFADE,
        request_config=_render_request_config(),
    )
    for _ in range(3):
        render_labels.add(render_id=render.id, kind=RenderLabelKind.TOO_ABRUPT)

    rebuilt = build_candidate_graph(project.id, track_hashes=[left, right])

    matching = [c for c in rebuilt.candidates if c.from_track == left]
    assert matching
    assert all(TransitionTechnique.LONG_CROSSFADE not in c.allowed_techniques for c in matching)


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
