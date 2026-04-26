"""Project Store: hashing, track CRUD, job queue, cache."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from aidj.store import cache, jobs, tracks
from aidj.store.hashing import derivation_key, hash_bytes, hash_file
from aidj.store.models import Job, JobStatus, Track

# -----------------------------
# hashing
# -----------------------------


def test_hash_file_matches_hash_bytes(tmp_path: Path) -> None:
    payload = b"deterministic"
    p = tmp_path / "x.bin"
    p.write_bytes(payload)
    assert hash_file(p) == hash_bytes(payload)


def test_derivation_key_is_stable_and_order_invariant() -> None:
    a = derivation_key({"separator": "htdemucs", "stem": "vocals", "track": "abc"})
    b = derivation_key({"track": "abc", "stem": "vocals", "separator": "htdemucs"})
    assert a == b
    assert len(a) == 64  # sha256 hex


# -----------------------------
# tracks
# -----------------------------


def test_track_ingest_idempotent(tmp_aidj, sample_file: Path) -> None:
    t1 = tracks.ingest(sample_file)
    t2 = tracks.ingest(sample_file)

    assert t1.content_hash == t2.content_hash
    assert t1.source_path == str(sample_file.resolve())
    assert t1.file_size == sample_file.stat().st_size

    # only one row
    assert [t.content_hash for t in tracks.list_all()] == [t1.content_hash]


def test_track_get_returns_none_for_unknown(tmp_aidj) -> None:
    assert tracks.get("0" * 64) is None


def test_track_delete_removes_row(tmp_aidj, sample_file: Path) -> None:
    t = tracks.ingest(sample_file)
    assert tracks.delete(t.content_hash) is True
    assert tracks.get(t.content_hash) is None
    assert tracks.delete(t.content_hash) is False


def test_track_ingest_rejects_directory(tmp_aidj, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        tracks.ingest(tmp_path)


def test_track_returned_is_pydantic_model(tmp_aidj, sample_file: Path) -> None:
    t = tracks.ingest(sample_file)
    assert isinstance(t, Track)
    # frozen — mutation raises ValidationError, not generic Exception
    with pytest.raises(ValidationError):
        t.content_hash = "x"  # type: ignore[misc]


def test_track_probe_allows_whitelisted_keys(tmp_aidj, sample_file: Path) -> None:
    t = tracks.ingest(
        sample_file,
        probe={"duration_sec": 123.4, "sample_rate": 44100, "channels": 2, "bitrate": 320},
    )
    assert t.duration_sec == 123.4
    assert t.sample_rate == 44100
    assert t.channels == 2
    assert t.bitrate == 320


def test_track_probe_rejects_unknown_keys(tmp_aidj, sample_file: Path) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        tracks.ingest(sample_file, probe={"unknown_column": 1})


def test_track_probe_cannot_override_identity_fields(tmp_aidj, sample_file: Path) -> None:
    for key in ("content_hash", "source_path", "file_size", "format"):
        with pytest.raises(ValueError, match="not allowed"):
            tracks.ingest(sample_file, probe={key: "spoofed"})


# -----------------------------
# jobs
# -----------------------------


def test_job_lifecycle(tmp_aidj) -> None:
    jid = jobs.enqueue("test.echo", {"x": 1})
    j = jobs.get(jid)
    assert j is not None and j.status is JobStatus.QUEUED and j.payload == {"x": 1}

    claimed = jobs.claim_next("test.echo")
    assert claimed is not None and claimed.id == jid and claimed.status is JobStatus.RUNNING

    jobs.complete(claimed.id, {"ok": True})
    final = jobs.get(jid)
    assert final is not None
    assert final.status is JobStatus.COMPLETED
    assert final.result == {"ok": True}


def test_job_claim_filters_by_kind(tmp_aidj) -> None:
    jobs.enqueue("alpha", {})
    jobs.enqueue("beta", {})
    j = jobs.claim_next("beta")
    assert j is not None and j.kind == "beta"


def test_job_claim_returns_none_when_empty(tmp_aidj) -> None:
    assert jobs.claim_next() is None


def test_job_retry_then_terminal_failure(tmp_aidj) -> None:
    jid = jobs.enqueue("flaky", {}, max_retries=2)
    j = jobs.claim_next("flaky")
    assert j is not None

    jobs.fail(j.id, "boom")
    after_first = jobs.get(jid)
    assert after_first is not None
    assert after_first.status is JobStatus.QUEUED
    assert after_first.retries == 1

    j2 = jobs.claim_next("flaky")
    assert j2 is not None
    jobs.fail(j2.id, "boom2")
    final = jobs.get(jid)
    assert final is not None and final.status is JobStatus.FAILED


def test_list_recent_filters_by_status(tmp_aidj) -> None:
    jid_a = jobs.enqueue("a", {})
    jid_b = jobs.enqueue("b", {})
    j = jobs.claim_next("a")
    assert j is not None
    jobs.complete(j.id)

    queued = jobs.list_recent(status=JobStatus.QUEUED)
    completed = jobs.list_recent(status=JobStatus.COMPLETED)
    assert {j.id for j in queued} == {jid_b}
    assert {j.id for j in completed} == {jid_a}


def test_job_returned_is_pydantic_model(tmp_aidj) -> None:
    jid = jobs.enqueue("k", {})
    j = jobs.get(jid)
    assert isinstance(j, Job)


# -----------------------------
# cache
# -----------------------------


def test_cache_put_get_roundtrip(tmp_aidj) -> None:
    key = "a" * 64
    cache.put_bytes("stems", key, "vocals.wav", b"\x00\x01\x02")
    assert cache.exists("stems", key, "vocals.wav")
    assert cache.get_bytes("stems", key, "vocals.wav") == b"\x00\x01\x02"


def test_cache_delete(tmp_aidj) -> None:
    key = "b" * 64
    cache.put_bytes("stems", key, "drums.wav", b"x")
    assert cache.delete("stems", key, "drums.wav") == 1
    assert not cache.exists("stems", key, "drums.wav")


def test_cache_short_key_rejected(tmp_aidj) -> None:
    with pytest.raises(ValueError):
        cache.path_for("stems", "ab", "x.wav", create_parent=False)
