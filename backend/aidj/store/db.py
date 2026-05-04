"""SQLite connection + schema bootstrap + tiny query helpers.

Single-user, single-process app — sync sqlite3 is fine. We rely on
``check_same_thread=False`` plus FastAPI's threadpool handling for sync handlers.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from aidj.config import settings

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    content_hash TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    duration_sec REAL,
    sample_rate INTEGER,
    channels INTEGER,
    format TEXT,
    bitrate INTEGER,
    file_size INTEGER,
    genre TEXT,
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_source_path ON tracks(source_path);

-- claim_token is the per-claim identity used by complete_run/fail_run to
-- prevent stale terminal writes from overwriting a newer claim's row.
CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_hash TEXT NOT NULL REFERENCES tracks(content_hash) ON DELETE CASCADE,
    analyzer_name TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
    output_json TEXT,
    confidence REAL,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    claim_token TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (track_hash, analyzer_name, analyzer_version)
);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_track ON analysis_runs(track_hash);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_status ON analysis_runs(status);

CREATE TABLE IF NOT EXISTS stems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_hash TEXT NOT NULL REFERENCES tracks(content_hash) ON DELETE CASCADE,
    separator TEXT NOT NULL,
    separator_version TEXT NOT NULL,
    stem_name TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    size_bytes INTEGER,
    last_used_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (track_hash, separator, separator_version, stem_name)
);
CREATE INDEX IF NOT EXISTS idx_stems_cache_key ON stems(cache_key);
CREATE INDEX IF NOT EXISTS idx_stems_last_used ON stems(last_used_at);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    intent TEXT,
    plan_json TEXT,
    render_artifact_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_track TEXT NOT NULL REFERENCES tracks(content_hash),
    to_track TEXT NOT NULL REFERENCES tracks(content_hash),
    from_cue_bar INTEGER,
    to_cue_bar INTEGER,
    scores_json TEXT,
    allowed_techniques TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_candidates_project ON candidates(project_id);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')) DEFAULT 'queued',
    retries INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    error TEXT,
    result_json TEXT,
    queued_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_kind ON jobs(kind);

-- Bake-off verification labels: a user listens to a click track over the
-- detected beats and marks each analysis run with one or more failure-mode
-- tags. Multiple labels of the same kind are allowed (e.g. you might mark
-- "correct" twice if you listened twice).
CREATE TABLE IF NOT EXISTS analysis_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN (
        'correct','half_time','double_time','wrong_downbeat_phase',
        'early_by_ms','late_by_ms','wrong_section_labels','unusable'
    )),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_labels_run ON analysis_labels(analysis_run_id);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_conn: sqlite3.Connection | None = None
# We share a single sqlite3 connection across threads (FastAPI's threadpool +
# the test suite). sqlite3 allows ``check_same_thread=False`` but a *single*
# connection cannot interleave statements safely across threads — a
# non-transaction execute() from one thread can land inside another thread's
# in-flight transaction. So *every* helper here acquires the same lock.
#
# We use ``RLock`` so a thread that's holding the lock for a transaction can
# still call back into ``execute()`` / ``fetch_*()`` from inside the block
# without deadlocking itself. (Today no helper does that, but future code
# might, and the cost is zero.)
_db_lock = threading.RLock()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        s = settings()
        s.ensure_dirs()
        _conn = _connect(s.db_path)
        _bootstrap(_conn)
        log.debug("opened sqlite at %s (schema v%d)", s.db_path, SCHEMA_VERSION)
    return _conn


def _bootstrap(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    _migrate_in_place(conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )


def _migrate_in_place(conn: sqlite3.Connection) -> None:
    """Best-effort, idempotent migration for shapes the schema script can't add.

    SQLite's ``CREATE TABLE IF NOT EXISTS`` is no-op when the table already
    exists, so a column added to a later schema version won't appear in an old
    DB. Detect-and-add is enough for the scale we're at; full migration
    plumbing arrives if/when it's actually needed.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(analysis_runs)")}
    if "claim_token" not in cols:
        log.info("migrating analysis_runs: adding claim_token column")
        conn.execute("ALTER TABLE analysis_runs ADD COLUMN claim_token TEXT")

    # v4: ``tracks.genre`` for the per-genre bake-off rollup.
    track_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
    if "genre" not in track_cols:
        log.info("migrating tracks: adding genre column")
        conn.execute("ALTER TABLE tracks ADD COLUMN genre TEXT")

    # The CREATE TABLE in SCHEMA_SQL handles the new analysis_labels table on
    # fresh DBs and is no-op on existing ones — nothing else to migrate.


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Run a block in a transaction.

    ``immediate=True`` issues ``BEGIN IMMEDIATE``, which acquires SQLite's write
    lock at the start of the transaction rather than at first write. Use this
    for atomic claim-style operations (read-then-conditionally-write) where a
    deferred transaction would leave a TOCTOU window between the SELECT and the
    INSERT/UPDATE.

    Across threads, the module-level ``_db_lock`` (an ``RLock``) serialises
    every shared-connection statement, so concurrent ``BEGIN``/``execute``
    calls cannot interleave on the shared sqlite3 connection.
    """
    conn = get_conn()
    with _db_lock:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def reset_for_tests(db_path: Path) -> None:
    """Re-target the global connection at a different DB. Used by test fixtures."""
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = _connect(db_path)
    _bootstrap(_conn)


def close() -> None:
    """Close and clear the global connection. Used by test teardown."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ---------------------------------------------------------------------------
# Query helpers — every entry point holds ``_db_lock`` so non-transaction
# statements cannot interleave with an in-flight transaction on the shared
# connection.
# ---------------------------------------------------------------------------


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with _db_lock:
        return get_conn().execute(sql, params).fetchone()


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with _db_lock:
        return get_conn().execute(sql, params).fetchall()


def execute(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
    with _db_lock:
        return get_conn().execute(sql, params)
