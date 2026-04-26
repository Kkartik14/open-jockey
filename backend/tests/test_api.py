"""FastAPI surface — exercised through TestClient without a real network."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aidj.api.main import app


@pytest.fixture
def client(tmp_aidj) -> TestClient:
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient, tmp_aidj) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["schema_version"] == 1
    assert body["project_root"] == str(tmp_aidj.project_root)


def test_list_plugins_includes_echo(client: TestClient) -> None:
    r = client.get("/api/plugins")
    assert r.status_code == 200
    names = [p["name"] for p in r.json()]
    assert "echo" in names


def test_plugin_call_round_trip(client: TestClient) -> None:
    r = client.post(
        "/api/plugins/echo/call",
        json={"method": "echo", "params": {"hi": "there"}},
    )
    assert r.status_code == 200
    assert r.json() == {"result": {"echo": {"hi": "there"}}}


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
