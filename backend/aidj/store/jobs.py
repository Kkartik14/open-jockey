"""SQLite-backed job queue.

Single-user local app — no Redis/Celery. Workers poll for queued jobs by kind,
claim one atomically, run it, and update status. Retries are counted per-job;
failures past ``max_retries`` are terminal.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aidj.store import db
from aidj.store.models import Job, JobStatus

log = logging.getLogger(__name__)


def enqueue(kind: str, payload: dict[str, Any] | None = None, *, max_retries: int = 3) -> int:
    cur = db.execute(
        "INSERT INTO jobs(kind, payload_json, max_retries) VALUES (?, ?, ?)",
        (kind, json.dumps(payload or {}), max_retries),
    )
    job_id = int(cur.lastrowid)
    log.debug("enqueued job %d kind=%s", job_id, kind)
    return job_id


def claim_next(kind: str | None = None) -> Job | None:
    """Atomically claim the oldest queued job, optionally filtered by kind."""
    sql = (
        f"UPDATE jobs SET status='{JobStatus.RUNNING}', started_at=datetime('now') "
        f"WHERE id = (SELECT id FROM jobs WHERE status='{JobStatus.QUEUED}'"
    )
    args: tuple[Any, ...] = ()
    if kind is not None:
        sql += " AND kind=?"
        args = (kind,)
    sql += " ORDER BY id LIMIT 1) RETURNING *"
    row = db.execute(sql, args).fetchone()
    return Job.from_row(row) if row else None


def complete(job_id: int, result: dict[str, Any] | None = None) -> None:
    db.execute(
        f"UPDATE jobs SET status='{JobStatus.COMPLETED}', finished_at=datetime('now'), result_json=? WHERE id=?",
        (json.dumps(result) if result is not None else None, job_id),
    )


def fail(job_id: int, error: str, *, retry: bool = True) -> None:
    """Mark a job failed. Re-queue if retries remain and ``retry`` is true."""
    row = db.fetch_one("SELECT retries, max_retries FROM jobs WHERE id=?", (job_id,))
    if row is None:
        return
    if retry and row["retries"] + 1 < row["max_retries"]:
        db.execute(
            f"UPDATE jobs SET status='{JobStatus.QUEUED}', retries=retries+1, error=?, started_at=NULL "
            f"WHERE id=?",
            (error, job_id),
        )
        log.info(
            "job %d requeued (retries=%d/%d): %s",
            job_id,
            row["retries"] + 1,
            row["max_retries"],
            error,
        )
    else:
        db.execute(
            f"UPDATE jobs SET status='{JobStatus.FAILED}', finished_at=datetime('now'), error=? WHERE id=?",
            (error, job_id),
        )
        log.warning("job %d failed terminally: %s", job_id, error)


def cancel(job_id: int) -> None:
    db.execute(
        f"UPDATE jobs SET status='{JobStatus.CANCELLED}', finished_at=datetime('now') WHERE id=?",
        (job_id,),
    )


def get(job_id: int) -> Job | None:
    row = db.fetch_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    return Job.from_row(row) if row else None


def list_recent(*, limit: int = 50, status: JobStatus | None = None) -> list[Job]:
    sql = "SELECT * FROM jobs"
    args: tuple[Any, ...] = ()
    if status is not None:
        sql += " WHERE status=?"
        args = (status.value,)
    sql += " ORDER BY id DESC LIMIT ?"
    args = (*args, limit)
    return [Job.from_row(r) for r in db.fetch_all(sql, args)]
