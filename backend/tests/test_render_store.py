"""Render artifact + label store contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from aidj.candidate_graph import build_candidate_graph
from aidj.store import projects, render_artifacts, render_labels, track_profiles, tracks
from aidj.store._timestamps import utc_now_iso
from aidj.store.models import (
    Beat,
    BeatGridBlock,
    CompletenessFields,
    FieldProvenance,
    Readiness,
    RenderActuals,
    RenderConfidenceSnapshot,
    RenderLabelKind,
    RenderLoudnessSummary,
    RenderRequestConfig,
    RenderStatus,
    RenderTechnique,
    SourceAnchorPolicy,
    TempoBlock,
    TrackProfile,
)


def _track(tmp_path: Path, name: str) -> str:
    p = tmp_path / f"{name}.bin"
    p.write_bytes((name * 64).encode())
    return tracks.ingest(p, probe={"genre": "house"}).content_hash


def _profile(track_hash: str, *, bpm: float) -> TrackProfile:
    prov = FieldProvenance(source="librosa@0.1.0", analysis_run_id=None)
    beats = [Beat(time_sec=round(i * (60.0 / bpm), 3), is_downbeat=(i % 4 == 0)) for i in range(96)]
    return TrackProfile(
        profile_version=1,
        track_hash=track_hash,
        built_at=utc_now_iso(),
        readiness=Readiness.PARTIAL,
        completeness_score=0.4,
        fields=CompletenessFields(has_beat_grid=True),
        tempo=TempoBlock(bpm=bpm, confidence=None, provenance=prov),
        beat_grid=BeatGridBlock(
            beats=beats,
            downbeat_count=sum(1 for beat in beats if beat.is_downbeat),
            duration_sec=beats[-1].time_sec + 2.0,
            provenance=prov,
        ),
    )


def _candidate(tmp_path: Path):
    left = _track(tmp_path, "left")
    right = _track(tmp_path, "right")
    track_profiles.upsert(_profile(left, bpm=124.0))
    track_profiles.upsert(_profile(right, bpm=125.0))
    project = projects.create("renders")
    result = build_candidate_graph(project.id, track_hashes=[left, right])
    return project, result.candidates[0]


def _request_config() -> RenderRequestConfig:
    return RenderRequestConfig(
        source_anchor_policy=SourceAnchorPolicy.KEEP_OUTGOING_TEMPO,
        from_cue_sec=10.0,
        to_cue_sec=4.0,
        from_bpm=124.0,
        to_bpm=125.0,
        tempo_match_ratio=0.992,
        tempo_match_ratio_source="candidate",
        transition_length_sec=8.0,
        source_lead_in_sec=12.0,
        target_tail_sec=24.0,
        loudness_target_lufs=-14.0,
        output_sample_rate=44_100,
        output_channels=2,
        confidence_snapshot=RenderConfidenceSnapshot(
            from_tempo_confidence=None,
            to_tempo_confidence=None,
            from_key_confidence=None,
            to_key_confidence=None,
            from_beat_source="librosa@0.1.0",
            to_beat_source="librosa@0.1.0",
        ),
    )


def _actuals() -> RenderActuals:
    summary = RenderLoudnessSummary(
        integrated_lufs=-14.2,
        loudness_range=2.0,
        true_peak_dbfs=-1.0,
        clipping_detected=False,
    )
    return RenderActuals(
        source_lufs=-13.0,
        target_lufs=-15.0,
        ffmpeg_version="ffmpeg test",
        source_loudness=summary,
        target_loudness=summary,
        output_loudness=summary,
        source_loudness_origin="fresh",
        target_loudness_origin="fresh",
    )


def test_render_artifact_lifecycle_roundtrip(tmp_aidj, tmp_path: Path) -> None:
    project, candidate = _candidate(tmp_path)
    assert candidate.id is not None

    render = render_artifacts.create_running(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.LONG_CROSSFADE,
        request_config=_request_config(),
        warnings=["source analyzer labels missing"],
    )

    assert render.status is RenderStatus.RUNNING
    assert render.claim_token
    assert render.artifact_key == (
        f"projects/{project.id}/renders/render-{render.id}-{candidate.id}-long_crossfade.m4a"
    )

    completed = render_artifacts.complete(
        render_id=render.id,
        claim_token=render.claim_token,
        duration_sec=42.0,
        sample_rate=44_100,
        channels=2,
        actuals=_actuals(),
        warnings=render.warnings,
    )

    assert completed.status is RenderStatus.COMPLETED
    assert completed.duration_sec == 42.0
    assert completed.actuals is not None
    assert (
        render_artifacts.latest_completed(candidate.id, RenderTechnique.LONG_CROSSFADE).id
        == render.id
    )


def test_partial_unique_index_blocks_second_running_render(tmp_aidj, tmp_path: Path) -> None:
    project, candidate = _candidate(tmp_path)
    assert candidate.id is not None
    render_artifacts.create_running(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.LONG_CROSSFADE,
        request_config=_request_config(),
    )

    with pytest.raises(render_artifacts.RunningRenderExists) as exc:
        render_artifacts.create_running(
            project_id=project.id,
            candidate_id=candidate.id,
            from_track=candidate.from_track,
            to_track=candidate.to_track,
            technique=RenderTechnique.LONG_CROSSFADE,
            request_config=_request_config(),
        )

    assert exc.value.active.status is RenderStatus.RUNNING


def test_render_labels_roll_up_by_pair_family(tmp_aidj, tmp_path: Path) -> None:
    project, candidate = _candidate(tmp_path)
    assert candidate.id is not None
    render = render_artifacts.create_running(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.FILTER_BLEND,
        request_config=_request_config(),
    )
    render_labels.add(render_id=render.id, kind=RenderLabelKind.GOOD)
    render_labels.add(render_id=render.id, kind=RenderLabelKind.TOO_ABRUPT)

    family = render_labels.pair_family_key(
        from_beat_source="librosa@0.1.0",
        to_beat_source="librosa@0.1.0",
        from_genre="house",
        to_genre="house",
    )
    rollup = render_labels.rollup_by_technique_and_pair()

    assert rollup[(RenderTechnique.FILTER_BLEND, family)] == {
        RenderLabelKind.GOOD: 1,
        RenderLabelKind.TOO_ABRUPT: 1,
    }


def test_render_pass_requires_good_and_no_failure_labels(tmp_aidj, tmp_path: Path) -> None:
    project, candidate = _candidate(tmp_path)
    assert candidate.id is not None
    render = render_artifacts.create_running(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.PHRASE_SWAP,
        request_config=_request_config(),
    )

    assert render_labels.counts_as_pass(render.id) is False
    good = render_labels.add(render_id=render.id, kind=RenderLabelKind.GOOD)
    assert render_labels.counts_as_pass(render.id) is True
    render_labels.add(render_id=render.id, kind=RenderLabelKind.OFF_BEAT)
    assert render_labels.counts_as_pass(render.id) is False
    assert render_labels.delete(good.id) is True
