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
    """A second call without force must NOT re-run: the cached row is returned
    byte-for-byte (same id, same timestamps). Previously this only checked id,
    which a silent re-run would still satisfy."""
    h = _ingest(client, sample_file)
    r1 = client.post(f"/api/tracks/{h}/analyze/echo", json={}).json()
    r2 = client.post(f"/api/tracks/{h}/analyze/echo", json={}).json()
    assert r1["id"] == r2["id"]
    # Caching is real iff the row wasn't rewritten — any silent rerun would
    # advance these fields, even within the same second.
    assert r1["started_at"] == r2["started_at"]
    assert r1["finished_at"] == r2["finished_at"]


def test_analyze_force_re_runs(client: TestClient, sample_file: Path) -> None:
    """``force=True`` must actually reissue the plugin call. ``finished_at >=``
    is too weak — same-second second-resolution timestamps can pass even if
    the row was never touched. The ``claim_token`` is regenerated on every
    claim, so a real rerun has a new token."""
    from aidj.store import db as _db

    h = _ingest(client, sample_file)
    r1 = client.post(f"/api/tracks/{h}/analyze/echo", json={}).json()
    token1 = _db.fetch_one(
        "SELECT claim_token FROM analysis_runs WHERE track_hash=? AND analyzer_name=?",
        (h, "echo"),
    )["claim_token"]

    r2 = client.post(f"/api/tracks/{h}/analyze/echo", json={"force": True}).json()
    token2 = _db.fetch_one(
        "SELECT claim_token FROM analysis_runs WHERE track_hash=? AND analyzer_name=?",
        (h, "echo"),
    )["claim_token"]

    # Same primary key (track, analyzer, version) → same row id.
    assert r1["id"] == r2["id"]
    # The decisive evidence the row was re-claimed and the plugin re-invoked.
    assert token1 != token2
    assert token1 and token2  # neither is empty


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


# ---------------------------------------------------------------------------
# Output validation at persistence (per-analyzer schemas)
# ---------------------------------------------------------------------------


def test_validate_output_passes_valid_beat_grid() -> None:
    from aidj.api.main import _validate_output_or_none

    good = {
        "tempo": {"bpm": 120.0},
        "beats": [{"time_sec": 0.0, "is_downbeat": True}],
        "sections": [],
        "duration_sec": 1.0,
    }
    out, err = _validate_output_or_none("librosa", good)
    assert err is None and out == good


def test_validate_output_rejects_malformed_for_registered_analyzer() -> None:
    """A registered analyzer that returns garbage must produce a validation
    error string — the API turns this into a FAILED run instead of writing
    garbage into ``analysis_runs.output_json``."""
    from aidj.api.main import _validate_output_or_none

    out, err = _validate_output_or_none("librosa", {"tempo": "not a dict"})
    assert out is None
    assert err is not None and "librosa" in err and "BeatGridAnalysis" in err


def test_validate_output_rejects_non_dict_for_registered_analyzer() -> None:
    from aidj.api.main import _validate_output_or_none

    out, err = _validate_output_or_none("essentia", "definitely not json")
    assert out is None and err is not None and "essentia" in err


def test_validate_output_passes_through_unknown_analyzer() -> None:
    """``echo`` and any custom plugin not in the schema map pass through —
    the contract is opt-in per plugin name."""
    from aidj.api.main import _validate_output_or_none

    out, err = _validate_output_or_none("echo", {"random": "stuff"})
    assert err is None and out == {"random": "stuff"}


def test_validate_output_wraps_non_dict_for_unknown_analyzer() -> None:
    from aidj.api.main import _validate_output_or_none

    out, err = _validate_output_or_none("echo", "scalar")
    assert err is None and out == {"raw": "scalar"}


# ---------------------------------------------------------------------------
# Generic plugin RPC hardening
# ---------------------------------------------------------------------------


def test_call_plugin_rejects_analyze_method(client: TestClient) -> None:
    """The generic RPC must refuse ``analyze`` — it would skip the atomic
    claim, claim-token-conditional terminal write, cloud-audio gate, and
    persisted failure handling that the dedicated analyze route enforces."""
    r = client.post(
        "/api/plugins/echo/call",
        json={"method": "analyze", "params": {"audio_path": "/tmp/whatever"}},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "analyze" in detail
    assert "/api/tracks/" in detail


def test_call_plugin_enforces_cloud_audio_gate(client: TestClient) -> None:
    """A cloud-audio plugin can upload bytes on any method, not just
    ``analyze`` — the gate must block the whole RPC when opt-in is missing."""
    r = client.post(
        "/api/plugins/allin1_remote/call",
        json={"method": "ping"},
    )
    assert r.status_code == 403
    assert "AIDJ_ALLOW_CLOUD_AUDIO" in r.json()["detail"]
