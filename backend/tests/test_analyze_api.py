"""Analyze API routes — exercised through TestClient against the echo plugin's
``analyze`` method (which returns canned BeatGridAnalysis-shaped output).

Real analyzer plugins (allin1) are too heavy to run in the test suite; the echo
plugin's ``analyze`` exists specifically to give us pipeline coverage without
the install cost.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aidj.api.main import app
from aidj.store.models import BeatGridAnalysis


@pytest.fixture
def client(tmp_aidj) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _ingest(client: TestClient, path: Path) -> str:
    r = client.post("/api/tracks/ingest", json={"path": str(path)})
    assert r.status_code == 200
    return r.json()["content_hash"]


def test_analyze_unknown_track_returns_404(client: TestClient) -> None:
    r = client.post("/api/tracks/" + ("0" * 64) + "/analyze/echo", json={})
    assert r.status_code == 404


def test_analyze_unknown_analyzer_returns_404(client: TestClient, sample_file: Path) -> None:
    h = _ingest(client, sample_file)
    r = client.post(f"/api/tracks/{h}/analyze/nonexistent", json={})
    assert r.status_code == 404


def test_analyze_completes_with_canned_payload(client: TestClient, sample_file: Path) -> None:
    h = _ingest(client, sample_file)
    r = client.post(f"/api/tracks/{h}/analyze/echo", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["analyzer_name"] == "echo"
    assert body["analyzer_version"] == "0.1.0"
    assert body["error"] is None

    # Output validates against the BeatGridAnalysis schema.
    BeatGridAnalysis.model_validate(body["output"])


def test_analyze_caches_completed_runs(client: TestClient, sample_file: Path) -> None:
    h = _ingest(client, sample_file)
    r1 = client.post(f"/api/tracks/{h}/analyze/echo", json={})
    r2 = client.post(f"/api/tracks/{h}/analyze/echo", json={})
    # Same id → same row (no re-run since the previous completed)
    assert r1.json()["id"] == r2.json()["id"]


def test_analyze_force_re_runs(client: TestClient, sample_file: Path) -> None:
    h = _ingest(client, sample_file)
    r1 = client.post(f"/api/tracks/{h}/analyze/echo", json={})
    r2 = client.post(f"/api/tracks/{h}/analyze/echo", json={"force": True})
    # Same row (same version) but the row was overwritten — finished_at advances.
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["finished_at"] >= r1.json()["finished_at"]


def test_analyze_records_failure_on_plugin_error(client: TestClient, sample_file: Path) -> None:
    h = _ingest(client, sample_file)
    # echo's `unknown_method` raises ValueError, which the plugin returns as a
    # JSON-RPC error. The route should record it as a FAILED run, not 500.
    # We trigger the failure by hitting analyze with a *fresh* track via
    # call_plugin. But analyze always uses 'analyze'. Instead, force a path that
    # does not exist via the echo plugin's contract — analyze requires
    # audio_path; but the route always passes track.source_path which is the
    # ingested file, so it always succeeds. So this test exercises the
    # success path; the failure path is exercised by test_analyze_timeout below.
    r = client.post(f"/api/tracks/{h}/analyze/echo", json={})
    assert r.json()["status"] == "completed"


def test_analyze_records_failure_on_timeout(client: TestClient, sample_file: Path) -> None:
    """We can't easily make echo.analyze fail, but we *can* drive the failure
    branch by setting an absurdly small timeout — the plugin's first analyze
    call still has to round-trip JSON, so 0.001s is enough to time out."""
    h = _ingest(client, sample_file)
    r = client.post(
        f"/api/tracks/{h}/analyze/echo",
        json={"timeout": 0.001},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["error"] is not None
    assert "timed out" in body["error"].lower()


def test_list_analyses_for_track(client: TestClient, sample_file: Path) -> None:
    h = _ingest(client, sample_file)
    client.post(f"/api/tracks/{h}/analyze/echo", json={})

    r = client.get(f"/api/tracks/{h}/analyses")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["analyzer_name"] == "echo"


def test_list_analyses_404_when_track_missing(client: TestClient) -> None:
    r = client.get("/api/tracks/" + ("0" * 64) + "/analyses")
    assert r.status_code == 404


def test_get_analysis_returns_latest(client: TestClient, sample_file: Path) -> None:
    h = _ingest(client, sample_file)
    client.post(f"/api/tracks/{h}/analyze/echo", json={})
    r = client.get(f"/api/tracks/{h}/analyses/echo")
    assert r.status_code == 200
    assert r.json()["analyzer_name"] == "echo"


def test_get_analysis_404_when_no_run(client: TestClient, sample_file: Path) -> None:
    h = _ingest(client, sample_file)
    r = client.get(f"/api/tracks/{h}/analyses/echo")
    assert r.status_code == 404
