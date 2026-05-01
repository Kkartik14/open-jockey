"""analysis_labels repository + API route coverage."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aidj.api.main import app
from aidj.store import analysis_labels, analysis_runs, tracks
from aidj.store.models import AnalysisLabelKind, AnalysisStatus


@pytest.fixture
def client(tmp_aidj) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _make_completed_run(track_hash: str) -> int:
    """Insert a fake completed run we can attach labels to."""
    run = analysis_runs.upsert(
        track_hash=track_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.COMPLETED,
        output={"tempo": {"bpm": 120.0}, "beats": [], "sections": [], "duration_sec": 0.0},
        confidence=0.9,
        started_at=analysis_runs.utc_now_iso(),
        finished_at=analysis_runs.utc_now_iso(),
    )
    return run.id


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


def test_add_and_list_labels(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    run_id = _make_completed_run(track.content_hash)

    a = analysis_labels.add(analysis_run_id=run_id, kind=AnalysisLabelKind.CORRECT)
    b = analysis_labels.add(
        analysis_run_id=run_id, kind=AnalysisLabelKind.HALF_TIME, notes="early in first chorus"
    )
    listed = analysis_labels.list_for_run(run_id)
    assert {x.id for x in listed} == {a.id, b.id}
    assert {x.kind for x in listed} == {AnalysisLabelKind.CORRECT, AnalysisLabelKind.HALF_TIME}
    half = next(x for x in listed if x.kind is AnalysisLabelKind.HALF_TIME)
    assert half.notes == "early in first chorus"


def test_delete_label(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    run_id = _make_completed_run(track.content_hash)
    label = analysis_labels.add(analysis_run_id=run_id, kind=AnalysisLabelKind.CORRECT)

    assert analysis_labels.delete(label.id) is True
    assert analysis_labels.get(label.id) is None
    assert analysis_labels.delete(label.id) is False  # idempotent on missing


def test_counts_by_kind(tmp_aidj, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    run_id = _make_completed_run(track.content_hash)

    analysis_labels.add(analysis_run_id=run_id, kind=AnalysisLabelKind.CORRECT)
    analysis_labels.add(analysis_run_id=run_id, kind=AnalysisLabelKind.CORRECT)
    analysis_labels.add(analysis_run_id=run_id, kind=AnalysisLabelKind.HALF_TIME)

    counts = analysis_labels.counts_by_kind(run_id)
    assert counts[AnalysisLabelKind.CORRECT] == 2
    assert counts[AnalysisLabelKind.HALF_TIME] == 1


def test_labels_cascade_with_run_delete(tmp_aidj, sample_file: Path) -> None:
    """ON DELETE CASCADE: deleting an analysis_run drops its labels."""
    track = tracks.ingest(sample_file)
    run_id = _make_completed_run(track.content_hash)
    analysis_labels.add(analysis_run_id=run_id, kind=AnalysisLabelKind.CORRECT)

    assert analysis_runs.delete(track.content_hash, "echo", "0.1.0") is True
    assert analysis_labels.list_for_run(run_id) == []


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


def test_post_label_returns_201(client: TestClient, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    run_id = _make_completed_run(track.content_hash)

    r = client.post(
        f"/api/analyses/{run_id}/labels",
        json={"kind": "wrong_downbeat_phase", "notes": "off by one beat"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["kind"] == "wrong_downbeat_phase"
    assert body["notes"] == "off by one beat"
    assert body["analysis_run_id"] == run_id


def test_post_label_404_on_unknown_run(client: TestClient) -> None:
    r = client.post(
        "/api/analyses/9999999/labels",
        json={"kind": "correct"},
    )
    assert r.status_code == 404


def test_post_label_409_on_non_terminal_run(client: TestClient, sample_file: Path) -> None:
    """Labels are verification events; only meaningful on completed/failed runs.
    Writing a label to a RUNNING row would let direct API users create labels
    the UI deliberately hides."""
    track = tracks.ingest(sample_file)
    running = analysis_runs.upsert(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version="0.1.0",
        status=AnalysisStatus.RUNNING,
        started_at=analysis_runs.utc_now_iso(),
    )
    r = client.post(
        f"/api/analyses/{running.id}/labels",
        json={"kind": "correct"},
    )
    assert r.status_code == 409
    assert "completed or failed" in r.json()["detail"].lower()


def test_analyses_endpoint_embeds_labels(client: TestClient, sample_file: Path) -> None:
    """The list endpoint should return labels inline so the frontend doesn't
    have to fan out N requests per refresh."""
    track = tracks.ingest(sample_file)
    run_id = _make_completed_run(track.content_hash)
    client.post(f"/api/analyses/{run_id}/labels", json={"kind": "correct"})
    client.post(f"/api/analyses/{run_id}/labels", json={"kind": "double_time"})

    r = client.get(f"/api/tracks/{track.content_hash}/analyses")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    embedded = runs[0]["labels"]
    kinds = {x["kind"] for x in embedded}
    assert kinds == {"correct", "double_time"}


def test_post_label_validates_enum(client: TestClient, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    run_id = _make_completed_run(track.content_hash)
    r = client.post(
        f"/api/analyses/{run_id}/labels",
        json={"kind": "totally_made_up"},
    )
    assert r.status_code == 422


def test_get_labels_lists(client: TestClient, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    run_id = _make_completed_run(track.content_hash)
    client.post(f"/api/analyses/{run_id}/labels", json={"kind": "correct"})
    client.post(f"/api/analyses/{run_id}/labels", json={"kind": "double_time"})
    r = client.get(f"/api/analyses/{run_id}/labels")
    assert r.status_code == 200
    kinds = {x["kind"] for x in r.json()}
    assert kinds == {"correct", "double_time"}


def test_delete_label_204(client: TestClient, sample_file: Path) -> None:
    track = tracks.ingest(sample_file)
    run_id = _make_completed_run(track.content_hash)
    post = client.post(f"/api/analyses/{run_id}/labels", json={"kind": "correct"})
    label_id = post.json()["id"]

    delete = client.delete(f"/api/analyses/{run_id}/labels/{label_id}")
    assert delete.status_code == 204

    listed = client.get(f"/api/analyses/{run_id}/labels").json()
    assert listed == []


def test_delete_label_wrong_run_404(client: TestClient, sample_file: Path) -> None:
    """A label belongs to one run — using a different run_id in the URL is a 404."""
    track = tracks.ingest(sample_file)
    run_id = _make_completed_run(track.content_hash)
    post = client.post(f"/api/analyses/{run_id}/labels", json={"kind": "correct"})
    label_id = post.json()["id"]

    r = client.delete(f"/api/analyses/9999999/labels/{label_id}")
    assert r.status_code == 404
