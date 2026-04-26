"""Concurrent access on the shared sqlite3 connection.

Without ``_db_lock`` wrapping every helper, a non-transaction ``execute()``
from one thread could land inside another thread's in-flight transaction on
the same connection. These tests verify the lock keeps that from happening
under threading load.
"""
from __future__ import annotations

import threading
from pathlib import Path

from aidj.store import db, tracks
from aidj.store.models import AnalysisStatus


def test_fetch_during_transaction_does_not_interleave(tmp_aidj, sample_file: Path) -> None:
    """While one thread is mid-transaction, another thread doing fetch_* should
    block on _db_lock rather than executing inside the transaction."""
    track = tracks.ingest(sample_file)

    inside_tx = threading.Event()
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
            # Hold the transaction until the fetcher has had a chance to try.
            proceed.wait(timeout=2.0)

    def fetcher() -> None:
        inside_tx.wait(timeout=2.0)
        # This must block on _db_lock (held by writer) — only completes after
        # the writer commits and releases.
        rows = db.fetch_all(
            "SELECT * FROM analysis_runs WHERE track_hash=?",
            (track.content_hash,),
        )
        # By the time we get here, the transaction has committed and the row
        # is visible.
        assert any(dict(r)["analyzer_name"] == "echo" for r in rows)
        fetcher_done.set()

    w = threading.Thread(target=writer)
    f = threading.Thread(target=fetcher)
    w.start()
    f.start()

    # Give the fetcher a moment to actually call fetch_all and block.
    inside_tx.wait(timeout=2.0)
    # The fetcher should NOT have completed yet — it's blocked on _db_lock.
    assert not fetcher_done.is_set()

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
