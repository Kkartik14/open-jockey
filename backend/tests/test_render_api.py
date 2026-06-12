"""Render API surface."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aidj.api import main as api_main
from aidj.api.main import app
from aidj.candidate_graph import build_candidate_graph
from aidj.store import db, projects, render_artifacts, track_profiles, tracks
from aidj.store._timestamps import utc_now_iso
from aidj.store.models import (
    Beat,
    BeatGridBlock,
    CompletenessFields,
    FieldProvenance,
    KeyBlock,
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
from aidj.transition_renderer import RenderConflictError, artifact_path


@pytest.fixture
def client(tmp_aidj) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _track(tmp_path: Path, name: str) -> str:
    p = tmp_path / f"{name}.bin"
    p.write_bytes((name * 64).encode())
    return tracks.ingest(p).content_hash


def _profile(track_hash: str, *, bpm: float) -> TrackProfile:
    prov = FieldProvenance(source="librosa@0.1.0", analysis_run_id=None)
    beats = [Beat(time_sec=round(i * (60.0 / bpm), 3), is_downbeat=(i % 4 == 0)) for i in range(64)]
    key_prov = FieldProvenance(source="essentia@0.1.0", analysis_run_id=None)
    return TrackProfile(
        profile_version=1,
        track_hash=track_hash,
        built_at=utc_now_iso(),
        readiness=Readiness.PARTIAL,
        completeness_score=0.7,
        fields=CompletenessFields(has_beat_grid=True, has_key=True),
        tempo=TempoBlock(bpm=bpm, confidence=0.8, provenance=prov),
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
            confidence=0.8,
            provenance=key_prov,
        ),
    )


def _project_candidate(tmp_path: Path):
    left = _track(tmp_path, "left")
    right = _track(tmp_path, "right")
    track_profiles.upsert(_profile(left, bpm=124.0))
    track_profiles.upsert(_profile(right, bpm=124.5))
    project = projects.create("render api")
    result = build_candidate_graph(project.id, track_hashes=[left, right])
    candidate = result.candidates[0]
    assert candidate.id is not None
    return project, candidate


def _request_config() -> RenderRequestConfig:
    return RenderRequestConfig(
        source_anchor_policy=SourceAnchorPolicy.KEEP_OUTGOING_TEMPO,
        from_cue_sec=10.0,
        to_cue_sec=0.0,
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


def _actuals() -> RenderActuals:
    summary = RenderLoudnessSummary(
        integrated_lufs=-14.0,
        loudness_range=1.0,
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


def _completed_render(tmp_path: Path):
    project, candidate = _project_candidate(tmp_path)
    running = render_artifacts.create_running(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.LONG_CROSSFADE,
        request_config=_request_config(),
        warnings=["test warning"],
    )
    assert running.claim_token is not None
    completed = render_artifacts.complete(
        render_id=running.id,
        claim_token=running.claim_token,
        duration_sec=12.0,
        sample_rate=44_100,
        channels=2,
        actuals=_actuals(),
        warnings=running.warnings,
    )
    return project, candidate, completed


def test_post_render_route_invokes_renderer(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, candidate, completed = _completed_render(tmp_path)
    seen = {}

    def fake_render_candidate(project_id, candidate_id, *, technique=None, force=False):
        seen["args"] = (project_id, candidate_id, technique, force)
        return completed

    monkeypatch.setattr(api_main, "render_candidate", fake_render_candidate)

    response = client.post(
        f"/api/projects/{project.id}/candidates/{candidate.id}/render",
        json={"technique": "long_crossfade", "force": True},
    )

    assert response.status_code == 200
    assert response.json()["id"] == completed.id
    assert seen["args"] == (
        project.id,
        candidate.id,
        RenderTechnique.LONG_CROSSFADE,
        True,
    )


def test_post_render_route_maps_conflict(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, candidate = _project_candidate(tmp_path)

    def fake_render_candidate(*args, **kwargs):
        raise RenderConflictError("already running", active_render_id=42)

    monkeypatch.setattr(api_main, "render_candidate", fake_render_candidate)

    response = client.post(
        f"/api/projects/{project.id}/candidates/{candidate.id}/render",
        json={"technique": "long_crossfade"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["active_render_id"] == 42


def test_render_audio_list_get_and_delete(
    client: TestClient,
    tmp_path: Path,
) -> None:
    project, _, completed = _completed_render(tmp_path)
    path = artifact_path(completed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake m4a bytes")

    listed = client.get(f"/api/projects/{project.id}/renders")
    fetched = client.get(f"/api/renders/{completed.id}")
    audio = client.get(f"/api/renders/{completed.id}/audio")
    deleted = client.delete(f"/api/renders/{completed.id}")

    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [completed.id]
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "completed"
    assert audio.status_code == 200
    assert audio.content == b"fake m4a bytes"
    assert audio.headers["content-disposition"].startswith("inline")
    assert deleted.status_code == 204
    assert not path.exists()


def test_render_audio_rejects_incomplete_and_missing_artifact(
    client: TestClient,
    tmp_path: Path,
) -> None:
    project, candidate = _project_candidate(tmp_path)
    queued = render_artifacts.create_queued(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.LONG_CROSSFADE,
        request_config=_request_config(),
    )
    _, _, completed = _completed_render(tmp_path)

    incomplete = client.get(f"/api/renders/{queued.id}/audio")
    missing_file = client.get(f"/api/renders/{completed.id}/audio")

    assert incomplete.status_code == 409
    assert missing_file.status_code == 410


def test_render_labels_api_roundtrip(client: TestClient, tmp_path: Path) -> None:
    _, _, completed = _completed_render(tmp_path)

    created = client.post(
        f"/api/renders/{completed.id}/labels",
        json={"kind": "good", "notes": "worked"},
    )
    listed = client.get(f"/api/renders/{completed.id}/labels")
    deleted = client.delete(f"/api/renders/{completed.id}/labels/{created.json()['id']}")

    assert created.status_code == 201
    assert created.json()["kind"] == RenderLabelKind.GOOD.value
    assert listed.status_code == 200
    assert [label["kind"] for label in listed.json()] == ["good"]
    assert deleted.status_code == 204
    assert client.get(f"/api/renders/{completed.id}/labels").json() == []


def test_cancel_queued_render_route(client: TestClient, tmp_path: Path) -> None:
    project, candidate = _project_candidate(tmp_path)
    queued = render_artifacts.create_queued(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.LONG_CROSSFADE,
        request_config=_request_config(),
    )

    response = client.post(f"/api/renders/{queued.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == RenderStatus.CANCELLED.value


def test_lifespan_recovers_stale_running_render(tmp_aidj, tmp_path: Path) -> None:
    project, candidate = _project_candidate(tmp_path)
    running = render_artifacts.create_running(
        project_id=project.id,
        candidate_id=candidate.id,
        from_track=candidate.from_track,
        to_track=candidate.to_track,
        technique=RenderTechnique.LONG_CROSSFADE,
        request_config=_request_config(),
    )
    db.execute(
        "UPDATE render_artifacts SET started_at=? WHERE id=?",
        ("2020-01-01 00:00:00", running.id),
    )

    with TestClient(app) as c:
        response = c.get(f"/api/renders/{running.id}")

    assert response.status_code == 200
    assert response.json()["status"] == RenderStatus.FAILED.value
    assert "RUNNING" in response.json()["error"]


def test_render_request_rejects_extra_fields(client: TestClient, tmp_path: Path) -> None:
    project, candidate = _project_candidate(tmp_path)

    response = client.post(
        f"/api/projects/{project.id}/candidates/{candidate.id}/render",
        json={"technique": "long_crossfade", "dry_run": True},
    )

    assert response.status_code == 422
