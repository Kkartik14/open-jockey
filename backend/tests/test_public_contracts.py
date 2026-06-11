"""Public contract tests for the backend/frontend boundary.

These tests intentionally exercise the API surface as a client sees it instead
of calling internal helpers directly. The unit tests catch local bugs; this file
catches bad PRs that accidentally remove routes, loosen validation, or break the
ingest -> profile -> project -> candidate graph flow the frontend relies on.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aidj.api.main import CLOUD_AUDIO_OPT_IN_ENV, app
from aidj.store import analysis_runs, cache
from aidj.store.models import AnalysisStatus


@pytest.fixture
def client(tmp_aidj) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _beatgrid_output(*, bpm: float, beat_count: int = 24) -> dict:
    return {
        "tempo": {"bpm": bpm, "confidence": 0.8},
        "beats": [
            {"time_sec": round(i * 0.5, 3), "is_downbeat": i % 4 == 0} for i in range(beat_count)
        ],
        "sections": [
            {"start_sec": 0.0, "end_sec": 4.0, "label": "intro"},
            {"start_sec": 4.0, "end_sec": 12.0, "label": "verse"},
        ],
        "duration_sec": round(beat_count * 0.5, 3),
        "confidence": 0.75,
    }


def _completed_librosa_run(track_hash: str, *, bpm: float) -> int:
    run = analysis_runs.upsert(
        track_hash=track_hash,
        analyzer_name="librosa",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
        output=_beatgrid_output(bpm=bpm),
        confidence=0.75,
        started_at="2026-01-01 00:00:00",
        finished_at="2026-01-01 00:00:01",
    )
    return run.id


def _write_track(path: Path, body: bytes) -> Path:
    path.write_bytes(body)
    return path


def _response_schema_name(operation: dict, status: str) -> str:
    response = operation["responses"][status]
    schema = response["content"]["application/json"]["schema"]
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if schema.get("type") == "array" and "$ref" in schema.get("items", {}):
        return schema["items"]["$ref"].rsplit("/", 1)[-1]
    raise AssertionError(f"unsupported response schema shape for {status}: {schema}")


def test_openapi_keeps_public_routes_and_methods(client: TestClient) -> None:
    """Lock the route contract that the frontend and future docs depend on."""
    schema = client.get("/openapi.json").json()
    paths: dict[str, dict] = schema["paths"]
    expected: dict[str, set[str]] = {
        "/api/health": {"get"},
        "/api/plugins": {"get"},
        "/api/plugins/{name}/call": {"post"},
        "/api/tracks/ingest": {"post"},
        "/api/tracks": {"get"},
        "/api/tracks/{content_hash}": {"get", "patch"},
        "/api/tracks/{content_hash}/audio": {"get"},
        "/api/tracks/{content_hash}/peaks": {"get"},
        "/api/tracks/{content_hash}/analyze/{analyzer_name}": {"post"},
        "/api/tracks/{content_hash}/analyses": {"get"},
        "/api/tracks/{content_hash}/analyses/{analyzer_name}": {"get"},
        "/api/analyses/{run_id}/labels": {"get", "post"},
        "/api/analyses/{run_id}/labels/{label_id}": {"delete"},
        "/api/labels/rollup": {"get"},
        "/api/tracks/{content_hash}/profile": {"get"},
        "/api/tracks/{content_hash}/profile/build": {"post"},
        "/api/profiles/coverage": {"get"},
        "/api/projects": {"get", "post"},
        "/api/projects/{project_id}": {"get", "delete"},
        "/api/projects/{project_id}/candidates/build": {"post"},
        "/api/projects/{project_id}/candidates": {"get"},
        "/api/projects/{project_id}/candidates/{candidate_id}/render": {"post"},
        "/api/projects/{project_id}/renders": {"get"},
        "/api/renders/{render_id}": {"get", "delete"},
        "/api/renders/{render_id}/audio": {"get"},
        "/api/renders/{render_id}/cancel": {"post"},
        "/api/renders/{render_id}/labels": {"get", "post"},
        "/api/renders/{render_id}/labels/{label_id}": {"delete"},
        "/api/jobs": {"get", "post"},
    }

    for path, methods in expected.items():
        assert path in paths, f"missing public path {path}"
        actual_methods = {m for m in paths[path] if m in {"get", "post", "patch", "delete"}}
        assert actual_methods == methods, f"{path} methods drifted"


def test_openapi_keeps_critical_response_models_and_status_codes(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    paths: dict[str, dict] = schema["paths"]
    expected: dict[tuple[str, str], tuple[str, str]] = {
        ("/api/health", "get"): ("200", "HealthResponse"),
        ("/api/plugins", "get"): ("200", "PluginInfo"),
        ("/api/plugins/{name}/call", "post"): ("200", "PluginCallResponse"),
        ("/api/tracks/ingest", "post"): ("200", "Track"),
        ("/api/tracks", "get"): ("200", "Track"),
        ("/api/tracks/{content_hash}", "get"): ("200", "Track"),
        ("/api/tracks/{content_hash}", "patch"): ("200", "Track"),
        ("/api/tracks/{content_hash}/peaks", "get"): ("200", "PeaksResponse"),
        ("/api/tracks/{content_hash}/analyze/{analyzer_name}", "post"): (
            "200",
            "AnalysisRun",
        ),
        ("/api/tracks/{content_hash}/analyses", "get"): (
            "200",
            "AnalysisRunDetail",
        ),
        ("/api/tracks/{content_hash}/analyses/{analyzer_name}", "get"): (
            "200",
            "AnalysisRun",
        ),
        ("/api/analyses/{run_id}/labels", "post"): ("201", "AnalysisLabel"),
        ("/api/analyses/{run_id}/labels", "get"): ("200", "AnalysisLabel"),
        ("/api/labels/rollup", "get"): ("200", "LabelRollupResponse"),
        ("/api/tracks/{content_hash}/profile", "get"): ("200", "TrackProfile"),
        ("/api/tracks/{content_hash}/profile/build", "post"): (
            "200",
            "TrackProfile",
        ),
        ("/api/profiles/coverage", "get"): ("200", "ProfileCoverageResponse"),
        ("/api/projects", "post"): ("201", "Project"),
        ("/api/projects", "get"): ("200", "Project"),
        ("/api/projects/{project_id}", "get"): ("200", "Project"),
        ("/api/projects/{project_id}/candidates/build", "post"): (
            "200",
            "CandidateGraphBuildResult",
        ),
        ("/api/projects/{project_id}/candidates", "get"): (
            "200",
            "TransitionCandidate",
        ),
        ("/api/projects/{project_id}/candidates/{candidate_id}/render", "post"): (
            "200",
            "RenderArtifact",
        ),
        ("/api/projects/{project_id}/renders", "get"): ("200", "RenderArtifact"),
        ("/api/renders/{render_id}", "get"): ("200", "RenderArtifact"),
        ("/api/renders/{render_id}/cancel", "post"): ("200", "RenderArtifact"),
        ("/api/renders/{render_id}/labels", "post"): ("201", "RenderLabel"),
        ("/api/renders/{render_id}/labels", "get"): ("200", "RenderLabel"),
        ("/api/jobs", "post"): ("200", "EnqueueResponse"),
        ("/api/jobs", "get"): ("200", "Job"),
    }

    for (path, method), (status, model_name) in expected.items():
        operation = paths[path][method]
        assert status in operation["responses"], f"{method.upper()} {path} lost {status}"
        assert _response_schema_name(operation, status) == model_name

    expected_empty_responses = {
        ("/api/analyses/{run_id}/labels/{label_id}", "delete"): "204",
        ("/api/projects/{project_id}", "delete"): "204",
        ("/api/renders/{render_id}", "delete"): "204",
        ("/api/renders/{render_id}/labels/{label_id}", "delete"): "204",
    }
    for (path, method), status in expected_empty_responses.items():
        operation = paths[path][method]
        assert status in operation["responses"], f"{method.upper()} {path} lost {status}"
        assert "content" not in operation["responses"][status]


def test_request_models_all_forbid_extras() -> None:
    from aidj.api.main import (
        AddLabelRequest,
        AddRenderLabelRequest,
        AnalyzeRequest,
        BuildCandidateGraphRequest,
        CreateProjectRequest,
        EnqueueRequest,
        IngestRequest,
        PluginCallRequest,
        RenderCandidateRequest,
        UpdateTrackRequest,
    )

    request_models = [
        AddLabelRequest,
        AddRenderLabelRequest,
        AnalyzeRequest,
        BuildCandidateGraphRequest,
        CreateProjectRequest,
        EnqueueRequest,
        IngestRequest,
        PluginCallRequest,
        RenderCandidateRequest,
        UpdateTrackRequest,
    ]
    for model in request_models:
        assert model.model_config.get("extra") == "forbid", (
            f"{model.__name__} dropped extra='forbid'"
        )


def test_request_models_reject_contract_drift(client: TestClient) -> None:
    """Validation must be strict enough that clients cannot send silent junk."""
    project_with_extra = client.post(
        "/api/projects",
        json={"name": "bad", "intent": "test", "unexpected": True},
    )
    assert project_with_extra.status_code == 422

    project = client.post("/api/projects", json={"name": "contract graph"}).json()

    too_low = client.post(
        f"/api/projects/{project['id']}/candidates/build",
        json={"max_candidates_per_pair": 0},
    )
    too_high = client.post(
        f"/api/projects/{project['id']}/candidates/build",
        json={"max_candidates_per_pair": 6},
    )
    extra_field = client.post(
        f"/api/projects/{project['id']}/candidates/build",
        json={"force": True, "dry_run": True},
    )

    assert too_low.status_code == 422
    assert too_high.status_code == 422
    assert extra_field.status_code == 422


def test_profile_get_404s_distinguish_missing_track_from_missing_profile(
    client: TestClient,
    sample_file: Path,
) -> None:
    missing_track = client.get("/api/tracks/" + ("0" * 64) + "/profile")
    assert missing_track.status_code == 404
    assert "track not found" in missing_track.json()["detail"]

    track = client.post("/api/tracks/ingest", json={"path": str(sample_file)}).json()
    missing_profile = client.get(f"/api/tracks/{track['content_hash']}/profile")
    assert missing_profile.status_code == 404
    assert "no profile" in missing_profile.json()["detail"]
    assert missing_track.json()["detail"] != missing_profile.json()["detail"]


def test_generic_rpc_rejects_analyze_method(client: TestClient) -> None:
    response = client.post(
        "/api/plugins/echo/call",
        json={"method": "analyze", "params": {"audio_path": "/tmp/x"}},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "analyze" in detail
    assert "/api/tracks/" in detail


def test_generic_rpc_enforces_cloud_audio_gate(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CLOUD_AUDIO_OPT_IN_ENV, raising=False)

    response = client.post(
        "/api/plugins/allin1_remote/call",
        json={"method": "ping"},
    )

    assert response.status_code == 403
    assert "AIDJ_ALLOW_CLOUD_AUDIO" in response.json()["detail"]


def test_cache_rejects_path_traversal_at_contract_layer(tmp_aidj) -> None:
    with pytest.raises(ValueError):
        cache.path_for("peaks", "a" * 64, "../escape.json", create_parent=False)


def test_stale_running_analysis_auto_recovers_through_api(
    client: TestClient,
    sample_file: Path,
) -> None:
    track = client.post("/api/tracks/ingest", json={"path": str(sample_file)}).json()
    track_hash = track["content_hash"]
    stale = analysis_runs.upsert(
        track_hash=track_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.RUNNING,
        started_at="2000-01-01 00:00:00",
        claim_token="stale-token",
    )

    response = client.post(f"/api/tracks/{track_hash}/analyze/echo", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == stale.id
    assert body["status"] == "completed"
    assert body["error"] is None


def test_ingest_profile_project_candidate_contract_flow(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """End-to-end public contract: two tracks become two directed candidates.

    This does not pretend the analyzer result is musically true. It verifies the
    mechanical contract: API ingest, completed analyzer rows, profile build,
    project creation, graph build, provenance, validation warnings, and candidate
    persistence all agree on the same wire shapes.
    """
    left_path = _write_track(tmp_path / "left.mp3", b"ID3left-contract" * 4)
    right_path = _write_track(tmp_path / "right.mp3", b"ID3right-contract" * 4)

    left = client.post("/api/tracks/ingest", json={"path": str(left_path)}).json()
    right = client.post("/api/tracks/ingest", json={"path": str(right_path)}).json()
    left_hash = left["content_hash"]
    right_hash = right["content_hash"]

    left_run_id = _completed_librosa_run(left_hash, bpm=124.0)
    right_run_id = _completed_librosa_run(right_hash, bpm=126.0)

    for track_hash, run_id in ((left_hash, left_run_id), (right_hash, right_run_id)):
        profile_response = client.post(f"/api/tracks/{track_hash}/profile/build")
        assert profile_response.status_code == 200
        profile = profile_response.json()
        assert profile["readiness"] == "partial"
        assert profile["fields"]["has_beat_grid"] is True
        assert profile["tempo"]["provenance"] == {
            "source": "librosa@0.1.0",
            "analysis_run_id": run_id,
        }
        assert profile["beat_grid"]["provenance"] == profile["tempo"]["provenance"]

    project_response = client.post(
        "/api/projects",
        json={"name": "public contract", "intent": "candidate graph smoke"},
    )
    assert project_response.status_code == 201
    project = project_response.json()

    graph_response = client.post(
        f"/api/projects/{project['id']}/candidates/build",
        json={
            "track_hashes": [left_hash, right_hash],
            "force": True,
            "max_candidates_per_pair": 1,
        },
    )
    assert graph_response.status_code == 200
    graph = graph_response.json()

    assert graph["project"]["id"] == project["id"]
    assert graph["requested_tracks"] == 2
    assert graph["usable_tracks"] == 2
    assert graph["skipped_tracks"] == {}
    assert any("mechanical only" in warning for warning in graph["warnings"])
    assert any("unverified" in warning for warning in graph["warnings"])
    assert len(graph["candidates"]) == 2

    directions = {(c["from_track"], c["to_track"]) for c in graph["candidates"]}
    assert directions == {(left_hash, right_hash), (right_hash, left_hash)}

    for candidate in graph["candidates"]:
        assert candidate["id"] is not None
        assert candidate["project_id"] == project["id"]
        assert candidate["from_cue_bar"] >= 0
        assert candidate["to_cue_bar"] >= 0
        assert "phrase_swap" in candidate["allowed_techniques"]
        assert "long_crossfade" in candidate["allowed_techniques"]
        scores = candidate["scores"]
        assert 0.0 <= scores["score"] <= 1.0
        assert scores["from_source"] == "librosa@0.1.0"
        assert scores["to_source"] == "librosa@0.1.0"
        assert scores["verification"] == "unverified"
        assert "phrase_aligned" in scores["reasons"]
        assert "unverified_sources" in scores["reasons"]

    listed = client.get(f"/api/projects/{project['id']}/candidates")
    assert listed.status_code == 200
    assert [c["id"] for c in listed.json()] == [c["id"] for c in graph["candidates"]]
