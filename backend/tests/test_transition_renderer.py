"""Transition renderer contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from aidj.candidate_graph import build_candidate_graph
from aidj.store import (
    analysis_labels,
    analysis_runs,
    db,
    projects,
    render_artifacts,
    track_profiles,
    tracks,
)
from aidj.store._timestamps import utc_now_iso
from aidj.store.models import (
    AnalysisLabelKind,
    AnalysisStatus,
    Beat,
    BeatGridBlock,
    CompletenessFields,
    FieldProvenance,
    KeyBlock,
    Readiness,
    RenderStatus,
    RenderTechnique,
    TempoBlock,
    TrackProfile,
    TransitionTechnique,
)
from aidj.transition_renderer import (
    RenderValidationError,
    _render_technique,
    artifact_path,
    cancel_render,
    cleanup_orphan_render_files,
    prepare_render,
    recover_stale_running,
    render_candidate,
)
from tests.fixtures.audio import write_silence_wav, write_sine_click_wav


def _analysis_run(track_hash: str, *, bpm: float):
    return analysis_runs.upsert(
        track_hash=track_hash,
        analyzer_name="librosa",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
        output={
            "tempo": {"bpm": bpm, "confidence": 0.9},
            "beats": [{"time_sec": 0.0, "is_downbeat": True}],
            "sections": [],
            "duration_sec": 1.0,
        },
        confidence=0.9,
        started_at="2026-01-01 00:00:00",
        finished_at="2026-01-01 00:00:01",
    )


def _profile(track_hash: str, *, bpm: float, run_id: int | None) -> TrackProfile:
    prov = FieldProvenance(source="librosa@0.1.0", analysis_run_id=run_id)
    beats = [Beat(time_sec=round(i * (60.0 / bpm), 3), is_downbeat=(i % 4 == 0)) for i in range(64)]
    key_prov = FieldProvenance(source="essentia@0.1.0", analysis_run_id=None)
    return TrackProfile(
        profile_version=1,
        track_hash=track_hash,
        built_at=utc_now_iso(),
        readiness=Readiness.PARTIAL,
        completeness_score=0.7,
        fields=CompletenessFields(has_beat_grid=True, has_key=True),
        tempo=TempoBlock(bpm=bpm, confidence=0.9, provenance=prov),
        beat_grid=BeatGridBlock(
            beats=beats,
            downbeat_count=sum(1 for beat in beats if beat.is_downbeat),
            duration_sec=beats[-1].time_sec + 2.0,
            provenance=prov,
        ),
        key=KeyBlock(
            key="C",
            scale="major",
            camelot="8B",
            confidence=0.85,
            provenance=key_prov,
        ),
    )


def _render_project(
    tmp_path: Path,
    *,
    label_runs: bool = True,
    silence: bool = False,
    right_bpm: float = 124.5,
):
    if silence:
        left_path = write_silence_wav(tmp_path / "left.wav", duration_sec=30.0)
        right_path = write_silence_wav(tmp_path / "right.wav", duration_sec=31.0)
    else:
        left_path = write_sine_click_wav(
            tmp_path / "left.wav",
            bpm=124.0,
            duration_sec=30.0,
            frequency_hz=330.0,
        )
        right_path = write_sine_click_wav(
            tmp_path / "right.wav",
            bpm=right_bpm,
            duration_sec=30.0,
            frequency_hz=494.0,
        )
    left = tracks.ingest(left_path, probe={"genre": "house"}).content_hash
    right = tracks.ingest(right_path, probe={"genre": "house"}).content_hash
    left_run = _analysis_run(left, bpm=124.0)
    right_run = _analysis_run(right, bpm=right_bpm)
    if label_runs:
        analysis_labels.add(analysis_run_id=left_run.id, kind=AnalysisLabelKind.CORRECT)
        analysis_labels.add(analysis_run_id=right_run.id, kind=AnalysisLabelKind.CORRECT)
    track_profiles.upsert(_profile(left, bpm=124.0, run_id=left_run.id))
    track_profiles.upsert(_profile(right, bpm=right_bpm, run_id=right_run.id))
    project = projects.create("renderer")
    result = build_candidate_graph(project.id, track_hashes=[left, right])
    candidate = next(edge for edge in result.candidates if edge.from_track == left)
    assert candidate.id is not None
    return project, candidate


@pytest.mark.parametrize(
    ("technique", "right_bpm"),
    [
        (RenderTechnique.PHRASE_SWAP, 124.5),
        (RenderTechnique.FILTER_BLEND, 124.5),
        (RenderTechnique.LONG_CROSSFADE, 124.5),
        (RenderTechnique.ECHO_OUT, 111.0),
    ],
)
def test_render_candidate_completes_generated_audio_for_every_technique(
    tmp_aidj,
    tmp_path: Path,
    technique: RenderTechnique,
    right_bpm: float,
) -> None:
    project, candidate = _render_project(tmp_path, right_bpm=right_bpm)
    assert TransitionTechnique(technique.value) in candidate.allowed_techniques

    render = render_candidate(project.id, candidate.id, technique=technique, force=True)

    assert render.status is RenderStatus.COMPLETED
    assert render.duration_sec is not None and render.duration_sec > 1.0
    assert render.sample_rate == 44_100
    assert render.channels == 2
    assert render.actuals is not None
    assert render.actuals.output_loudness is not None
    assert artifact_path(render).is_file()
    assert not any("no human listening label" in warning for warning in render.warnings)


def test_render_candidate_reuses_completed_render_without_force(tmp_aidj, tmp_path: Path) -> None:
    project, candidate = _render_project(tmp_path)

    first = render_candidate(
        project.id,
        candidate.id,
        technique=RenderTechnique.LONG_CROSSFADE,
        force=True,
    )
    second = render_candidate(
        project.id,
        candidate.id,
        technique=RenderTechnique.LONG_CROSSFADE,
        force=False,
    )

    assert first.status is RenderStatus.COMPLETED
    assert second.id == first.id


def test_render_technique_mapping_rejects_future_graph_values() -> None:
    assert _render_technique("long_crossfade") is RenderTechnique.LONG_CROSSFADE
    assert _render_technique("future_ai_scratch") is None


def test_prepare_render_warns_when_analyzer_labels_are_missing(tmp_aidj, tmp_path: Path) -> None:
    project, candidate = _render_project(tmp_path, label_runs=False)

    prepared = prepare_render(
        project.id,
        candidate.id,
        technique=RenderTechnique.LONG_CROSSFADE,
    )

    assert any(
        "source beat-grid analysis has no human listening label" in w for w in prepared.warnings
    )
    assert any(
        "target beat-grid analysis has no human listening label" in w for w in prepared.warnings
    )
    assert prepared.request_config.confidence_snapshot.from_beat_labels == []
    assert prepared.request_config.tempo_match_ratio_source == "candidate"


def test_silent_render_fails_non_silence_gate(tmp_aidj, tmp_path: Path) -> None:
    project, candidate = _render_project(tmp_path, silence=True)

    render = render_candidate(
        project.id,
        candidate.id,
        technique=RenderTechnique.LONG_CROSSFADE,
        force=True,
    )

    assert render.status is RenderStatus.FAILED
    assert render.error is not None
    assert "silent" in render.error


def test_cancel_queued_render(tmp_aidj, tmp_path: Path) -> None:
    project, candidate = _render_project(tmp_path)
    prepared = prepare_render(
        project.id,
        candidate.id,
        technique=RenderTechnique.LONG_CROSSFADE,
    )
    queued = render_artifacts.create_queued(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.LONG_CROSSFADE,
        request_config=prepared.request_config,
    )

    cancelled = cancel_render(queued.id)

    assert cancelled.status is RenderStatus.CANCELLED


def test_stale_running_render_auto_fails(tmp_aidj, tmp_path: Path) -> None:
    project, candidate = _render_project(tmp_path)
    prepared = prepare_render(
        project.id,
        candidate.id,
        technique=RenderTechnique.LONG_CROSSFADE,
    )
    running = render_artifacts.create_running(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.LONG_CROSSFADE,
        request_config=prepared.request_config,
    )
    db.execute(
        "UPDATE render_artifacts SET started_at=? WHERE id=?",
        ("2020-01-01 00:00:00", running.id),
    )

    assert recover_stale_running() == 1
    recovered = render_artifacts.get(running.id)
    assert recovered is not None
    assert recovered.status is RenderStatus.FAILED
    assert "RUNNING" in (recovered.error or "")


def test_artifact_path_rejects_store_escape(tmp_aidj, tmp_path: Path) -> None:
    project, candidate = _render_project(tmp_path)
    prepared = prepare_render(
        project.id,
        candidate.id,
        technique=RenderTechnique.LONG_CROSSFADE,
    )
    queued = render_artifacts.create_queued(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.LONG_CROSSFADE,
        request_config=prepared.request_config,
    )
    db.execute(
        "UPDATE render_artifacts SET artifact_key=? WHERE id=?", ("../escape.m4a", queued.id)
    )
    escaped = render_artifacts.get(queued.id)
    assert escaped is not None

    with pytest.raises(RenderValidationError):
        artifact_path(escaped)


def test_cleanup_orphan_render_files(tmp_aidj) -> None:
    orphan = tmp_aidj.projects_root / "1" / "renders" / "render-999-999-long_crossfade.m4a"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    assert cleanup_orphan_render_files() == 1
    assert not orphan.exists()
