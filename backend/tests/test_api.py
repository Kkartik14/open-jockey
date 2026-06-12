"""FastAPI surface — exercised through TestClient without a real network."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from aidj.api.main import app
from aidj.store import tracks


@pytest.fixture
def client(tmp_aidj) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient, tmp_aidj) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["schema_version"] == 7
    assert body["project_root"] == str(tmp_aidj.project_root)


def test_list_plugins_includes_echo(client: TestClient) -> None:
    r = client.get("/api/plugins")
    assert r.status_code == 200
    plugins = r.json()
    by_name = {p["name"]: p for p in plugins}
    assert "echo" in by_name
    # version comes from pyproject.toml, not manifest.yaml
    assert by_name["echo"]["version"] == "0.1.0"


def test_plugin_info_surfaces_policy_fields(client: TestClient) -> None:
    """The frontend needs concurrency_safe / default_timeout_sec / cloud_audio
    to warn users before they hit a 403 on cloud-audio plugins."""
    r = client.get("/api/plugins")
    assert r.status_code == 200
    by_name = {p["name"]: p for p in r.json()}

    echo = by_name["echo"]
    assert echo["concurrency_safe"] is False
    assert echo["default_timeout_sec"] == 60.0
    assert echo["cloud_audio"] is False

    remote = by_name["allin1_remote"]
    assert remote["concurrency_safe"] is True
    assert remote["default_timeout_sec"] == 600.0
    assert remote["cloud_audio"] is True


def test_plugin_call_round_trip(client: TestClient) -> None:
    r = client.post(
        "/api/plugins/echo/call",
        json={"method": "echo", "params": {"hi": "there"}},
    )
    assert r.status_code == 200
    assert r.json() == {"result": {"echo": {"hi": "there"}}}


def test_plugin_call_timeout_returns_504(client: TestClient) -> None:
    r = client.post(
        "/api/plugins/echo/call",
        json={"method": "sleep", "params": {"seconds": 5}, "timeout": 0.3},
    )
    assert r.status_code == 504
    detail = r.json()["detail"]
    assert detail["code"] == -32001
    assert "timed out" in detail["message"].lower()


def test_unknown_plugin_returns_404(client: TestClient) -> None:
    r = client.post("/api/plugins/nope/call", json={"method": "x"})
    assert r.status_code == 404


def test_track_ingest_then_list(client: TestClient, sample_file: Path) -> None:
    r = client.post("/api/tracks/ingest", json={"path": str(sample_file)})
    assert r.status_code == 200
    track = r.json()
    assert "content_hash" in track
    assert track["file_size"] == sample_file.stat().st_size

    r2 = client.get("/api/tracks")
    assert r2.status_code == 200
    hashes = [t["content_hash"] for t in r2.json()]
    assert track["content_hash"] in hashes


def test_ingest_rejects_missing_file(client: TestClient, tmp_path: Path) -> None:
    r = client.post("/api/tracks/ingest", json={"path": str(tmp_path / "nope.mp3")})
    assert r.status_code == 400


def test_job_enqueue_and_list(client: TestClient) -> None:
    r = client.post("/api/jobs", json={"kind": "test.demo", "payload": {"x": 1}})
    assert r.status_code == 200
    jid = r.json()["id"]

    r2 = client.get("/api/jobs")
    assert r2.status_code == 200
    ids = [j["id"] for j in r2.json()]
    assert jid in ids


def test_job_status_filter_validates_enum(client: TestClient) -> None:
    r = client.get("/api/jobs", params={"status": "bogus"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Audio streaming endpoint
# ---------------------------------------------------------------------------


def test_audio_stream_returns_file_with_octet_stream_for_unknown_ext(
    client: TestClient, sample_file: Path
) -> None:
    """The fixture file has a .bin extension → not a known audio type → falls
    back to application/octet-stream. The bytes still come through."""
    track = tracks.ingest(sample_file)
    r = client.get(f"/api/tracks/{track.content_hash}/audio")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.content == sample_file.read_bytes()


def test_audio_stream_uses_audio_content_type_for_known_extension(
    client: TestClient, tmp_path: Path
) -> None:
    p = tmp_path / "song.mp3"
    p.write_bytes(b"ID3\x04\x00fake-but-routable")
    track = tracks.ingest(p)
    r = client.get(f"/api/tracks/{track.content_hash}/audio")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mpeg")


def test_audio_stream_404_when_hash_unknown(client: TestClient) -> None:
    r = client.get("/api/tracks/" + ("0" * 64) + "/audio")
    assert r.status_code == 404


def test_audio_stream_410_when_source_file_missing(client: TestClient, tmp_path: Path) -> None:
    """Track was ingested but the source file is gone now → 410 Gone, not 500."""
    p = tmp_path / "song.wav"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt-fake")
    track = tracks.ingest(p)
    p.unlink()
    r = client.get(f"/api/tracks/{track.content_hash}/audio")
    assert r.status_code == 410
    assert "no longer present" in r.json()["detail"]


def test_audio_stream_uses_inline_content_disposition(client: TestClient, tmp_path: Path) -> None:
    """Browsers should play the file, not download it."""
    p = tmp_path / "song.mp3"
    p.write_bytes(b"ID3\x04\x00fake-but-routable")
    track = tracks.ingest(p)
    r = client.get(f"/api/tracks/{track.content_hash}/audio")
    assert r.status_code == 200
    disp = r.headers.get("content-disposition", "")
    # FastAPI / Starlette emits "inline; filename=..." when content_disposition_type='inline'.
    assert disp.startswith("inline"), f"expected inline disposition, got: {disp!r}"


# ---------------------------------------------------------------------------
# Single-track endpoint
# ---------------------------------------------------------------------------


def test_get_track_returns_track(client: TestClient, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    r = client.get(f"/api/tracks/{track.content_hash}")
    assert r.status_code == 200
    body = r.json()
    assert body["content_hash"] == track.content_hash
    assert body["source_path"] == track.source_path


def test_get_track_404_for_unknown(client: TestClient) -> None:
    r = client.get("/api/tracks/" + ("0" * 64))
    assert r.status_code == 404


def test_patch_track_sets_genre(client: TestClient, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    r = client.patch(f"/api/tracks/{track.content_hash}", json={"genre": "Bollywood"})
    assert r.status_code == 200
    assert r.json()["genre"] == "Bollywood"

    refetch = client.get(f"/api/tracks/{track.content_hash}")
    assert refetch.json()["genre"] == "Bollywood"


def test_patch_track_clears_genre_with_null(client: TestClient, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    client.patch(f"/api/tracks/{track.content_hash}", json={"genre": "rock"})
    r = client.patch(f"/api/tracks/{track.content_hash}", json={"genre": None})
    assert r.status_code == 200
    assert r.json()["genre"] is None


def test_patch_track_omitted_genre_is_noop(client: TestClient, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    client.patch(f"/api/tracks/{track.content_hash}", json={"genre": "rock"})

    r = client.patch(f"/api/tracks/{track.content_hash}", json={})
    assert r.status_code == 200
    assert r.json()["genre"] == "rock"


def test_patch_track_404_for_unknown(client: TestClient) -> None:
    r = client.patch("/api/tracks/" + ("0" * 64), json={"genre": "anything"})
    assert r.status_code == 404


def test_patch_track_rejects_unknown_fields(client: TestClient, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    r = client.patch(f"/api/tracks/{track.content_hash}", json={"mood": "happy"})
    assert r.status_code == 422


def test_patch_track_validates_max_length(client: TestClient, sample_file: Path) -> None:
    """Pydantic ``max_length=100`` rejects egregiously long genre strings."""
    track = tracks.ingest(sample_file)
    r = client.patch(
        f"/api/tracks/{track.content_hash}",
        json={"genre": "x" * 200},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Peaks endpoint
# ---------------------------------------------------------------------------


def test_peaks_404_for_unknown_track(client: TestClient) -> None:
    r = client.get("/api/tracks/" + ("0" * 64) + "/peaks")
    assert r.status_code == 404


def test_peaks_410_when_source_missing(client: TestClient, tmp_path: Path) -> None:
    p = tmp_path / "song.wav"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt-fake")
    track = tracks.ingest(p)
    p.unlink()
    r = client.get(f"/api/tracks/{track.content_hash}/peaks")
    assert r.status_code == 410


def test_peaks_503_when_ffmpeg_unavailable(client: TestClient, sample_file: Path) -> None:
    from aidj.audio import peaks as audio_peaks

    track = tracks.ingest(sample_file)
    with patch.object(audio_peaks, "is_ffmpeg_available", return_value=False):
        r = client.get(f"/api/tracks/{track.content_hash}/peaks")
    assert r.status_code == 503
    assert "ffmpeg" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Track profile endpoints (Phase 2)
# ---------------------------------------------------------------------------


def _beatgrid_output(*, bpm: float = 124.0, n_beats: int = 8) -> dict:
    return {
        "tempo": {"bpm": bpm},
        "beats": [
            {"time_sec": round(i * 0.5, 3), "is_downbeat": (i % 4 == 0)} for i in range(n_beats)
        ],
        "sections": [{"start_sec": 0.0, "end_sec": 4.0, "label": "intro"}],
        "duration_sec": round(n_beats * 0.5, 3),
    }


def _upsert_completed_run(track_hash: str, analyzer: str, version: str, output: dict):
    from aidj.store import analysis_runs as ar_module
    from aidj.store.models import AnalysisStatus

    return ar_module.upsert(
        track_hash=track_hash,
        analyzer_name=analyzer,
        analyzer_version=version,
        status=AnalysisStatus.COMPLETED,
        output=output,
        started_at="2026-01-01 00:00:00",
        finished_at="2026-01-01 00:00:01",
    )


def test_get_profile_404_for_unknown_track(client: TestClient) -> None:
    r = client.get("/api/tracks/" + ("0" * 64) + "/profile")
    assert r.status_code == 404
    assert "track not found" in r.json()["detail"]


def test_get_profile_404_when_track_exists_but_no_profile(
    client: TestClient, sample_file: Path
) -> None:
    """Distinct 404 message: the track is there, the profile isn't — caller
    needs to build, not ingest."""
    track = tracks.ingest(sample_file)
    r = client.get(f"/api/tracks/{track.content_hash}/profile")
    assert r.status_code == 404
    assert "no profile" in r.json()["detail"]


def test_post_profile_build_404_for_unknown_track(client: TestClient) -> None:
    """Builder raises TrackNotFoundError; the route maps it to 404 instead of
    leaking the underlying SQLite IntegrityError."""
    r = client.post("/api/tracks/" + ("0" * 64) + "/profile/build")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_post_profile_build_blocks_when_no_runs(client: TestClient, sample_file: Path) -> None:
    """An ingested-but-unanalyzed track yields a persisted blocked profile —
    coverage can distinguish 'tried, unusable' from 'never built'."""
    track = tracks.ingest(sample_file)
    r = client.post(f"/api/tracks/{track.content_hash}/profile/build")
    assert r.status_code == 200
    body = r.json()
    assert body["readiness"] == "blocked"
    assert body["tempo"] is None and body["beat_grid"] is None
    assert body["completeness_score"] == 0.0


def test_post_profile_build_partial_from_completed_librosa_run(
    client: TestClient, sample_file: Path
) -> None:
    track = tracks.ingest(sample_file)
    _upsert_completed_run(track.content_hash, "librosa", "0.1.0", _beatgrid_output(bpm=128.0))

    r = client.post(f"/api/tracks/{track.content_hash}/profile/build")
    assert r.status_code == 200
    body = r.json()
    assert body["readiness"] == "partial"
    assert body["tempo"]["bpm"] == 128.0
    assert body["beat_grid"]["provenance"]["source"] == "librosa@0.1.0"


def test_post_profile_build_force_reselects_when_staleness_would_not(
    client: TestClient, sample_file: Path
) -> None:
    """The route always uses force=True. Adding a higher-priority analyzer run
    whose finished_at predates built_at would NOT trigger staleness — yet POST
    must still pick the higher-priority source. This proves the force=True wire."""
    track = tracks.ingest(sample_file)
    _upsert_completed_run(track.content_hash, "librosa", "0.1.0", _beatgrid_output(bpm=100.0))

    first = client.post(f"/api/tracks/{track.content_hash}/profile/build").json()
    assert first["beat_grid"]["provenance"]["source"] == "librosa@0.1.0"

    # Past-dated finished_at → staleness check would say "not stale"; force=True
    # must override.
    _upsert_completed_run(track.content_hash, "allin1_remote", "0.1.0", _beatgrid_output(bpm=128.0))

    second = client.post(f"/api/tracks/{track.content_hash}/profile/build").json()
    assert second["beat_grid"]["provenance"]["source"] == "allin1_remote@0.1.0"
    assert second["tempo"]["bpm"] == 128.0


def test_profile_coverage_buckets_are_exhaustive(client: TestClient, tmp_path: Path) -> None:
    # Three ingested tracks; one blocked, one partial, one with no profile.
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    c = tmp_path / "c.bin"
    a.write_bytes(b"a" * 32)
    b.write_bytes(b"b" * 32)
    c.write_bytes(b"c" * 32)
    ta = tracks.ingest(a)
    tb = tracks.ingest(b)
    tracks.ingest(c)  # missing — never built

    # ta: build with no runs → blocked.
    client.post(f"/api/tracks/{ta.content_hash}/profile/build")
    # tb: librosa run → partial.
    _upsert_completed_run(tb.content_hash, "librosa", "0.1.0", _beatgrid_output())
    client.post(f"/api/tracks/{tb.content_hash}/profile/build")

    r = client.get("/api/profiles/coverage")
    assert r.status_code == 200
    assert r.json() == {"ready": 0, "partial": 1, "blocked": 1, "missing": 1}


# ---------------------------------------------------------------------------
# Projects + transition candidate graph endpoints (Phase 3)
# ---------------------------------------------------------------------------


def test_create_list_get_project(client: TestClient) -> None:
    r = client.post(
        "/api/projects",
        json={"name": " Truth Test ", "intent": "build candidate graph"},
    )
    assert r.status_code == 201
    project = r.json()
    assert project["name"] == "Truth Test"
    assert project["intent"] == "build candidate graph"

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()] == [project["id"]]

    fetched = client.get(f"/api/projects/{project['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Truth Test"


def test_delete_project_removes_project_and_candidates(
    client: TestClient,
    tmp_path: Path,
) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"a" * 32)
    b.write_bytes(b"b" * 32)
    ta = tracks.ingest(a)
    tb = tracks.ingest(b)
    _upsert_completed_run(ta.content_hash, "librosa", "0.1.0", _beatgrid_output(bpm=124.0))
    _upsert_completed_run(tb.content_hash, "librosa", "0.1.0", _beatgrid_output(bpm=126.0))
    client.post(f"/api/tracks/{ta.content_hash}/profile/build")
    client.post(f"/api/tracks/{tb.content_hash}/profile/build")
    project = client.post("/api/projects", json={"name": "delete me"}).json()
    built = client.post(
        f"/api/projects/{project['id']}/candidates/build",
        json={"track_hashes": [ta.content_hash, tb.content_hash]},
    )
    assert built.status_code == 200
    assert built.json()["candidates"]

    deleted = client.delete(f"/api/projects/{project['id']}")

    assert deleted.status_code == 204
    assert client.get(f"/api/projects/{project['id']}").status_code == 404
    assert client.get(f"/api/projects/{project['id']}/candidates").status_code == 404
    assert client.delete(f"/api/projects/{project['id']}").status_code == 404


def test_get_project_404_for_unknown_project(client: TestClient) -> None:
    r = client.get("/api/projects/999")
    assert r.status_code == 404


def test_candidate_build_404_for_unknown_project(client: TestClient) -> None:
    r = client.post("/api/projects/999/candidates/build", json={})
    assert r.status_code == 404


def test_candidate_build_creates_directed_edges(
    client: TestClient,
    tmp_path: Path,
) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"a" * 32)
    b.write_bytes(b"b" * 32)
    ta = tracks.ingest(a)
    tb = tracks.ingest(b)
    _upsert_completed_run(ta.content_hash, "librosa", "0.1.0", _beatgrid_output(bpm=124.0))
    _upsert_completed_run(tb.content_hash, "librosa", "0.1.0", _beatgrid_output(bpm=126.0))
    client.post(f"/api/tracks/{ta.content_hash}/profile/build")
    client.post(f"/api/tracks/{tb.content_hash}/profile/build")
    project = client.post("/api/projects", json={"name": "candidate test"}).json()

    r = client.post(
        f"/api/projects/{project['id']}/candidates/build",
        json={"track_hashes": [ta.content_hash, tb.content_hash], "max_candidates_per_pair": 1},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["requested_tracks"] == 2
    assert body["usable_tracks"] == 2
    assert len(body["candidates"]) == 2
    directions = {(c["from_track"], c["to_track"]) for c in body["candidates"]}
    assert directions == {
        (ta.content_hash, tb.content_hash),
        (tb.content_hash, ta.content_hash),
    }
    for candidate in body["candidates"]:
        assert candidate["from_track"] != candidate["to_track"]
        assert candidate["scores"]["from_source"] == "librosa@0.1.0"
        assert candidate["scores"]["to_source"] == "librosa@0.1.0"
        assert candidate["scores"]["verification"] == "unverified"
        assert candidate["allowed_techniques"]

    listed = client.get(f"/api/projects/{project['id']}/candidates")
    assert listed.status_code == 200
    assert len(listed.json()) == 2


def test_candidate_build_reports_skipped_tracks(
    client: TestClient,
    sample_file: Path,
) -> None:
    track = tracks.ingest(sample_file)
    project = client.post("/api/projects", json={"name": "skip test"}).json()

    r = client.post(
        f"/api/projects/{project['id']}/candidates/build",
        json={"track_hashes": [track.content_hash, "0" * 64]},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["usable_tracks"] == 0
    assert body["skipped_tracks"][track.content_hash] == "profile_missing"
    assert body["skipped_tracks"]["0" * 64] == "track_missing"


def test_list_candidates_404_for_unknown_project(client: TestClient) -> None:
    r = client.get("/api/projects/999/candidates")
    assert r.status_code == 404
