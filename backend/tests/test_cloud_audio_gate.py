"""Tests for the AIDJ_ALLOW_CLOUD_AUDIO opt-in gate on the analyze route."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from aidj.api.main import CLOUD_AUDIO_OPT_IN_ENV, app
from aidj.plugins.registry import registry
from aidj.store import tracks


@pytest.fixture
def client(tmp_aidj) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _patched_call(_self, _method, _params=None, *, timeout=None) -> dict[str, Any]:
    """Stand-in for ``Plugin.call`` so the cloud-audio gate tests don't have to
    install the modal client + actually deploy a Modal function."""
    return {
        "tempo": {"bpm": 120.0, "confidence": 0.9},
        "beats": [{"time_sec": 0.0, "is_downbeat": True}],
        "sections": [{"start_sec": 0.0, "end_sec": 4.0, "label": "intro"}],
        "duration_sec": 4.0,
        "confidence": 0.9,
    }


def test_cloud_audio_plugin_blocked_without_env(
    client: TestClient, sample_file: Path, monkeypatch
) -> None:
    monkeypatch.delenv(CLOUD_AUDIO_OPT_IN_ENV, raising=False)
    track = tracks.ingest(sample_file)
    r = client.post(f"/api/tracks/{track.content_hash}/analyze/allin1_remote", json={})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "AIDJ_ALLOW_CLOUD_AUDIO" in detail


def test_cloud_audio_plugin_blocked_with_wrong_env(
    client: TestClient, sample_file: Path, monkeypatch
) -> None:
    monkeypatch.setenv(CLOUD_AUDIO_OPT_IN_ENV, "true")  # not "1"
    track = tracks.ingest(sample_file)
    r = client.post(f"/api/tracks/{track.content_hash}/analyze/allin1_remote", json={})
    assert r.status_code == 403


def test_cloud_audio_plugin_proceeds_with_env(
    client: TestClient, sample_file: Path, monkeypatch
) -> None:
    monkeypatch.setenv(CLOUD_AUDIO_OPT_IN_ENV, "1")
    track = tracks.ingest(sample_file)

    # Don't actually invoke the Modal-backed plugin (would need modal-client +
    # a deployed function). Stub Plugin.call to return canned BeatGridAnalysis.
    from aidj.plugins.runtime import Plugin

    with patch.object(Plugin, "call", _patched_call):
        r = client.post(f"/api/tracks/{track.content_hash}/analyze/allin1_remote", json={})

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["analyzer_name"] == "allin1_remote"


def test_non_cloud_plugin_unaffected_by_env(
    client: TestClient, sample_file: Path, monkeypatch
) -> None:
    """Echo (cloud_audio=False) ignores the env var entirely."""
    monkeypatch.delenv(CLOUD_AUDIO_OPT_IN_ENV, raising=False)
    track = tracks.ingest(sample_file)
    # Warm up the echo plugin so its first-call latency doesn't blow the route's timeout.
    registry().get("echo").call("ping")
    r = client.post(f"/api/tracks/{track.content_hash}/analyze/echo", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
