"""Concurrent access on the shared sqlite3 connection.

Without ``_db_lock`` wrapping every helper, a non-transaction ``execute()``
from one thread could land inside another thread's in-flight transaction on
the same connection. These tests verify the lock keeps that from happening
under threading load.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from aidj.store import db, tracks
from aidj.store.models import AnalysisStatus


def test_fetch_during_transaction_does_not_interleave(tmp_aidj, sample_file: Path) -> None:
    """While one thread is mid-transaction, another thread doing fetch_* should
    block on _db_lock rather than executing inside the transaction.

    The previous version only checked ``not fetcher_done.is_set()`` once after
    a single ``inside_tx.wait()``, which could trivially pass if the fetcher
    thread hadn't been scheduled yet — a no-op assertion. This version:

      1. waits for the fetcher to *positively signal* it is about to call
         ``fetch_all`` (so we know the next thing it does is the blocking op),
      2. then asserts ``fetcher_done.wait(0.3) is False`` — a real, positive
         claim that the fetcher remained blocked for ~300ms while the writer
         held the lock. Without the lock, ``fetch_all`` would complete in
         microseconds and ``wait`` would return True.
    """
    track = tracks.ingest(sample_file)

    inside_tx = threading.Event()
    fetcher_about_to_block = threading.Event()
    fetcher_done = threading.Event()
    proceed = threading.Event()

    def writer() -> None:
        with db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO analysis_runs "
                "(track_hash, analyzer_name, analyzer_version, status) "
                "VALUES (?, ?, ?, ?)",
                (track.content_hash, "echo", "0.1.0", AnalysisStatus.RUNNING.value),
            )
            inside_tx.set()
            # Hold the transaction until we are told to release.
            proceed.wait(timeout=2.0)

    def fetcher() -> None:
        inside_tx.wait(timeout=2.0)
        fetcher_about_to_block.set()
        rows = db.fetch_all(
            "SELECT * FROM analysis_runs WHERE track_hash=?",
            (track.content_hash,),
        )
        assert any(dict(r)["analyzer_name"] == "echo" for r in rows)
        fetcher_done.set()

    w = threading.Thread(target=writer)
    f = threading.Thread(target=fetcher)
    w.start()
    f.start()

    inside_tx.wait(timeout=2.0)
    fetcher_about_to_block.wait(timeout=2.0)
    # Positive assertion: fetch_all stays blocked for ~300ms while the writer
    # holds the lock. ``Event.wait(timeout)`` returns False iff the event was
    # not set within the timeout — exactly what we want to assert here.
    assert not fetcher_done.wait(0.3), (
        "fetch_all completed while writer held the immediate-tx lock — "
        "_db_lock is not actually serialising"
    )

    # Release the writer; the fetcher proceeds.
    proceed.set()
    w.join(timeout=3.0)
    f.join(timeout=3.0)
    assert fetcher_done.is_set()


def test_many_concurrent_writes_serialise(tmp_aidj, sample_file: Path) -> None:
    """Twenty threads upserting tracks should not corrupt the DB or raise."""
    n_threads = 20
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def writer(ix: int) -> None:
        try:
            barrier.wait()
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO jobs(kind, payload_json) VALUES (?, ?)",
                    (f"test.{ix}", "{}"),
                )
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"unexpected errors under concurrent load: {errors!r}"
    rows = db.fetch_all("SELECT kind FROM jobs ORDER BY kind")
    kinds = {dict(r)["kind"] for r in rows}
    assert kinds == {f"test.{i}" for i in range(n_threads)}


def test_schema_migration_adds_claim_token_to_old_db(tmp_path: Path) -> None:
    """An older DB without claim_token should be migrated in place on next open."""
    import sqlite3

    db_path = tmp_path / "legacy.db"

    # Create a v1-shaped analysis_runs table without claim_token.
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE tracks (content_hash TEXT PRIMARY KEY, source_path TEXT NOT NULL);
        CREATE TABLE analysis_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_hash TEXT NOT NULL,
            analyzer_name TEXT NOT NULL,
            analyzer_version TEXT NOT NULL,
            status TEXT NOT NULL,
            output_json TEXT,
            confidence REAL,
            error TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT,
            UNIQUE (track_hash, analyzer_name, analyzer_version)
        );
        """
    )
    legacy.commit()
    legacy.close()

    db.reset_for_tests(db_path)
    cols = {row["name"] for row in db.fetch_all("PRAGMA table_info(analysis_runs)")}
    assert "claim_token" in cols, f"migration did not add claim_token; columns: {sorted(cols)}"


def test_schema_migration_adds_track_profiles_to_old_db(tmp_path: Path) -> None:
    """A pre-v5 DB without track_profiles should pick up the new table on
    next open. Existing tracks remain intact and queryable through the new
    profile repo (which just returns None for tracks without a profile)."""
    import sqlite3

    db_path = tmp_path / "legacy_no_profiles.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE tracks (
            content_hash TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            duration_sec REAL,
            genre TEXT,
            last_seen TEXT,
            created_at TEXT
        );
        INSERT INTO tracks(content_hash, source_path)
            VALUES ('cc' || hex(randomblob(31)), '/tmp/legacy.wav');
        """
    )
    legacy.commit()
    legacy.close()

    db.reset_for_tests(db_path)
    tables = {
        row["name"]
        for row in db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "track_profiles" in tables

    # Existing tracks still readable; the new table starts empty.
    surviving = db.fetch_all("SELECT content_hash FROM tracks")
    assert len(surviving) == 1
    empty = db.fetch_all("SELECT * FROM track_profiles")
    assert empty == []


def test_schema_migration_adds_candidate_track_cascades(tmp_path: Path) -> None:
    """A pre-v6 DB should rebuild candidates with endpoint-track cascades.

    Invalid legacy orphans are dropped during the rebuild; otherwise the
    migration itself would fail when the new FK actions are enforced.
    """
    import sqlite3

    db_path = tmp_path / "legacy_candidate_fks.db"
    left = "a" * 64
    right = "b" * 64
    missing = "c" * 64
    scores = {
        "score": 0.9,
        "tempo_delta_pct": 1.0,
        "from_bpm": 124.0,
        "to_bpm": 126.0,
        "from_cue_sec": 30.0,
        "to_cue_sec": 0.0,
        "phrase_bars": 8,
        "key_compatible": None,
        "verification": "unverified",
        "from_source": "librosa@0.1.0",
        "to_source": "librosa@0.1.0",
        "reasons": [],
    }

    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE tracks (
            content_hash TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            duration_sec REAL,
            sample_rate INTEGER,
            channels INTEGER,
            format TEXT,
            bitrate INTEGER,
            file_size INTEGER,
            genre TEXT,
            last_seen TEXT,
            created_at TEXT
        );
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            intent TEXT,
            plan_json TEXT,
            render_artifact_key TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            from_track TEXT NOT NULL REFERENCES tracks(content_hash),
            to_track TEXT NOT NULL REFERENCES tracks(content_hash),
            from_cue_bar INTEGER,
            to_cue_bar INTEGER,
            scores_json TEXT,
            allowed_techniques TEXT,
            created_at TEXT
        );
        CREATE INDEX idx_candidates_project ON candidates(project_id);
        """
    )
    legacy.execute(
        "INSERT INTO tracks(content_hash, source_path) VALUES (?, ?), (?, ?)",
        (left, "/tmp/left.wav", right, "/tmp/right.wav"),
    )
    legacy.execute("INSERT INTO projects(id, name) VALUES (1, 'legacy')")
    for candidate_id, from_track, to_track in [(1, left, right), (2, missing, right)]:
        legacy.execute(
            "INSERT INTO candidates "
            "(id, project_id, from_track, to_track, from_cue_bar, to_cue_bar, "
            "scores_json, allowed_techniques) "
            "VALUES (?, 1, ?, ?, 0, 0, ?, ?)",
            (
                candidate_id,
                from_track,
                to_track,
                json.dumps(scores),
                json.dumps(["long_crossfade"]),
            ),
        )
    legacy.commit()
    legacy.close()

    db.reset_for_tests(db_path)

    track_fks = {
        row["from"]: row["on_delete"]
        for row in db.fetch_all("PRAGMA foreign_key_list(candidates)")
        if row["table"] == "tracks"
    }
    assert track_fks == {"to_track": "CASCADE", "from_track": "CASCADE"}
    surviving = db.fetch_all("SELECT id FROM candidates")
    assert [row["id"] for row in surviving] == [1]

    assert tracks.delete(left) is True
    assert db.fetch_all("SELECT id FROM candidates") == []


def test_schema_migration_adds_genre_to_old_tracks_table(tmp_path: Path) -> None:
    """A pre-v4 DB whose ``tracks`` table predates the genre column should
    migrate in place rather than crashing on the first SELECT."""
    import sqlite3

    db_path = tmp_path / "legacy_no_genre.db"

    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE tracks (
            content_hash TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            duration_sec REAL,
            sample_rate INTEGER,
            channels INTEGER,
            format TEXT,
            bitrate INTEGER,
            file_size INTEGER,
            last_seen TEXT,
            created_at TEXT
        );
        INSERT INTO tracks(content_hash, source_path) VALUES ('aa' || hex(randomblob(31)), '/tmp/x');
        """
    )
    legacy.commit()
    legacy.close()

    db.reset_for_tests(db_path)
    cols = {row["name"] for row in db.fetch_all("PRAGMA table_info(tracks)")}
    assert "genre" in cols, f"migration did not add genre; columns: {sorted(cols)}"

    # Pre-existing rows survive the migration with NULL genre.
    rows = db.fetch_all("SELECT genre FROM tracks")
    assert len(rows) == 1 and rows[0]["genre"] is None
