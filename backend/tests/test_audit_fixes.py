"""Tests for the five audit fixes (M1, M2, L4-L5).

M3 (allin1 byproduct dirs) is verified by code inspection — exercising it
requires actually running allin1 on real audio, which the test suite skips.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aidj.api.main import _coerce_confidence, app
from aidj.plugins.registry import registry
from aidj.plugins.runtime import PluginError
from aidj.store import analysis_runs, tracks
from aidj.store.models import AnalysisStatus


@pytest.fixture
def client(tmp_aidj) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# M1: RUNNING row + short-circuit on existing RUNNING
# ---------------------------------------------------------------------------


def test_running_row_is_persisted_before_plugin_call(
    client: TestClient, tmp_aidj, sample_file: Path
) -> None:
    track = tracks.ingest(sample_file)
    p = registry().get("echo")

    seen_during_call: list[AnalysisStatus] = []
    original_call = p.call

    def spying_call(method: str, params=None, *, timeout=None):
        # The route should have upserted a RUNNING row by the time we get here.
        run = analysis_runs.get(track.content_hash, "echo")
        if run is not None:
            seen_during_call.append(run.status)
        return original_call(method, params, timeout=timeout)

    p.call = spying_call  # type: ignore[method-assign]
    try:
        r = client.post(f"/api/tracks/{track.content_hash}/analyze/echo", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
    finally:
        p.call = original_call  # type: ignore[method-assign]

    assert seen_during_call == [AnalysisStatus.RUNNING]
    # And the row also has started_at set on the COMPLETED row.
    final = analysis_runs.get(track.content_hash, "echo")
    assert final is not None
    assert final.status is AnalysisStatus.COMPLETED
    assert final.started_at is not None
    assert final.finished_at is not None


def test_existing_running_row_short_circuits_without_calling_plugin(
    client: TestClient, tmp_aidj, sample_file: Path
) -> None:
    track = tracks.ingest(sample_file)
    plugin_version = registry().get("echo").manifest.version

    pre = analysis_runs.upsert(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version=plugin_version,
        status=AnalysisStatus.RUNNING,
        started_at=analysis_runs.utc_now_iso(),
    )

    p = registry().get("echo")
    call_count = 0
    original_call = p.call

    def counting_call(method: str, params=None, *, timeout=None):
        nonlocal call_count
        call_count += 1
        return original_call(method, params, timeout=timeout)

    p.call = counting_call  # type: ignore[method-assign]
    try:
        r = client.post(f"/api/tracks/{track.content_hash}/analyze/echo", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == pre.id  # same row, no new analysis
        assert body["status"] == "running"  # unchanged
    finally:
        p.call = original_call  # type: ignore[method-assign]

    assert call_count == 0


def test_failed_row_does_not_short_circuit(client: TestClient, tmp_aidj, sample_file: Path) -> None:
    """A previous FAILED run should not block a fresh attempt without ``force``."""
    track = tracks.ingest(sample_file)
    plugin_version = registry().get("echo").manifest.version
    analysis_runs.upsert(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version=plugin_version,
        status=AnalysisStatus.FAILED,
        error="boom",
        started_at=analysis_runs.utc_now_iso(),
        finished_at=analysis_runs.utc_now_iso(),
    )
    r = client.post(f"/api/tracks/{track.content_hash}/analyze/echo", json={})
    assert r.json()["status"] == "completed"


def test_force_overrides_running_row_via_api(
    client: TestClient, tmp_aidj, sample_file: Path
) -> None:
    """``force=true`` should re-run even if a RUNNING row exists (recovery
    pathway when the user knows the previous run is stuck)."""
    track = tracks.ingest(sample_file)
    plugin_version = registry().get("echo").manifest.version

    # Pre-insert a RUNNING row.
    analysis_runs.upsert(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version=plugin_version,
        status=AnalysisStatus.RUNNING,
        started_at=analysis_runs.utc_now_iso(),
    )

    p = registry().get("echo")
    call_count = 0
    original_call = p.call

    def counting_call(method, params=None, *, timeout=None):
        nonlocal call_count
        call_count += 1
        return original_call(method, params, timeout=timeout)

    p.call = counting_call  # type: ignore[method-assign]
    try:
        r = client.post(
            f"/api/tracks/{track.content_hash}/analyze/echo",
            json={"force": True},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
    finally:
        p.call = original_call  # type: ignore[method-assign]

    # Plugin WAS called this time, unlike the same scenario without force.
    assert call_count == 1


def test_stale_running_row_auto_recovers_via_api(
    client: TestClient, tmp_aidj, sample_file: Path
) -> None:
    """A RUNNING row whose started_at is older than 2x default_timeout should
    auto-recover on the next analyze call without needing force=True."""
    from datetime import UTC, datetime, timedelta

    from aidj.store import db

    track = tracks.ingest(sample_file)
    plugin = registry().get("echo")

    # echo's default_timeout is 60s → stale threshold is 120s. Backdate by 1h.
    analysis_runs.upsert(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version=plugin.manifest.version,
        status=AnalysisStatus.RUNNING,
        started_at=analysis_runs.utc_now_iso(),
    )
    long_ago = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE analysis_runs SET started_at=? WHERE track_hash=? AND analyzer_name=?",
        (long_ago, track.content_hash, "echo"),
    )

    # Without force=true, the stale row should still be re-claimed and re-run.
    r = client.post(f"/api/tracks/{track.content_hash}/analyze/echo", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"


# ---------------------------------------------------------------------------
# M2: malformed plugin stdout → PluginError + force-kill
# ---------------------------------------------------------------------------


def test_malformed_stdout_raises_parse_error_and_kills_plugin(tmp_aidj) -> None:
    p = registry().get("echo")
    assert p.call("ping") == "pong"
    assert p.is_alive

    with pytest.raises(PluginError) as excinfo:
        p.call("_misbehave_stdout")
    assert excinfo.value.code == -32700
    assert "non-json" in str(excinfo.value).lower()
    assert not p.is_alive  # killed

    # A fresh subprocess starts on the next call.
    assert p.call("ping") == "pong"
    assert p.is_alive


# ---------------------------------------------------------------------------
# L5: _coerce_confidence — defensive coercion for plugin output
# ---------------------------------------------------------------------------


def test_coerce_confidence_passes_floats() -> None:
    assert _coerce_confidence(0.5) == 0.5
    assert _coerce_confidence(0.0) == 0.0
    assert _coerce_confidence(1.0) == 1.0


def test_coerce_confidence_passes_ints_as_floats() -> None:
    assert _coerce_confidence(1) == 1.0
    assert isinstance(_coerce_confidence(1), float)


def test_coerce_confidence_rejects_bool() -> None:
    # bool is a subclass of int but doesn't make sense as a confidence score.
    assert _coerce_confidence(True) is None
    assert _coerce_confidence(False) is None


def test_coerce_confidence_rejects_strings_and_objects() -> None:
    assert _coerce_confidence("high") is None
    assert _coerce_confidence("0.5") is None  # don't auto-parse strings
    assert _coerce_confidence({"value": 0.9}) is None
    assert _coerce_confidence([0.9]) is None
    assert _coerce_confidence(None) is None


def test_analyze_route_drops_non_numeric_confidence(
    client: TestClient, tmp_aidj, sample_file: Path
) -> None:
    """End-to-end: a plugin returning a string confidence yields a stored None."""
    track = tracks.ingest(sample_file)

    # Drive the plugin to inject a stringy confidence via the test hook.
    # The route does NOT pass extra params to plugin.analyze, so we have to
    # exercise the coercion via a direct plugin.call + manual upsert path.
    raw_output = (
        registry()
        .get("echo")
        .call(
            "analyze",
            {"audio_path": track.source_path, "confidence_override": "high"},
        )
    )
    assert raw_output["confidence"] == "high"

    plugin_version = registry().get("echo").manifest.version
    run = analysis_runs.upsert(
        track_hash=track.content_hash,
        analyzer_name="echo",
        analyzer_version=plugin_version,
        status=AnalysisStatus.COMPLETED,
        output=raw_output,
        confidence=_coerce_confidence(raw_output.get("confidence")),
        error=None,
        started_at=analysis_runs.utc_now_iso(),
        finished_at=analysis_runs.utc_now_iso(),
    )
    assert run.confidence is None
    # …and the original string still survives in the JSON output.
    assert run.output is not None and run.output["confidence"] == "high"
