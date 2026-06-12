"""Render-artifact repository.

Rows in ``render_artifacts`` describe generated transition audio. The store
owns lifecycle state, claim-token conditional writes, and label-safe deletion;
the renderer owns ffmpeg command construction and file cleanup.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from aidj.store import db
from aidj.store._timestamps import TIMESTAMP_FMT, utc_now_iso
from aidj.store.models import (
    RenderActuals,
    RenderArtifact,
    RenderRequestConfig,
    RenderStatus,
    RenderTechnique,
)

log = logging.getLogger(__name__)


class RunningRenderExists(RuntimeError):
    """Raised when a same-candidate/technique render is already RUNNING."""

    def __init__(self, active: RenderArtifact) -> None:
        super().__init__(f"render already running: {active.id}")
        self.active = active


def artifact_key_for(
    render_id: int, project_id: int, candidate_id: int, technique: RenderTechnique
) -> str:
    return f"projects/{project_id}/renders/render-{render_id}-{candidate_id}-{technique.value}.m4a"


def get(render_id: int) -> RenderArtifact | None:
    row = db.fetch_one("SELECT * FROM render_artifacts WHERE id=?", (render_id,))
    return RenderArtifact.from_row(row) if row else None


def list_for_project(project_id: int, *, limit: int = 1000) -> list[RenderArtifact]:
    rows = db.fetch_all(
        "SELECT * FROM render_artifacts WHERE project_id=? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (project_id, limit),
    )
    return [RenderArtifact.from_row(row) for row in rows]


def list_all(*, limit: int = 10_000) -> list[RenderArtifact]:
    rows = db.fetch_all(
        "SELECT * FROM render_artifacts ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    return [RenderArtifact.from_row(row) for row in rows]


def latest_completed(candidate_id: int, technique: RenderTechnique) -> RenderArtifact | None:
    row = db.fetch_one(
        "SELECT * FROM render_artifacts "
        "WHERE candidate_id=? AND technique=? AND status='completed' "
        "ORDER BY finished_at DESC, id DESC LIMIT 1",
        (candidate_id, technique.value),
    )
    return RenderArtifact.from_row(row) if row else None


def find_running(candidate_id: int, technique: RenderTechnique) -> RenderArtifact | None:
    row = db.fetch_one(
        "SELECT * FROM render_artifacts "
        "WHERE candidate_id=? AND technique=? AND status='running' "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (candidate_id, technique.value),
    )
    return RenderArtifact.from_row(row) if row else None


def create_queued(
    *,
    project_id: int,
    candidate_id: int,
    from_track: str,
    to_track: str,
    technique: RenderTechnique,
    request_config: RenderRequestConfig,
    warnings: list[str] | None = None,
) -> RenderArtifact:
    cur = db.execute(
        "INSERT INTO render_artifacts "
        "(project_id, candidate_id, from_track, to_track, technique, status, "
        " request_config_json, warnings_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            candidate_id,
            from_track,
            to_track,
            technique.value,
            RenderStatus.QUEUED.value,
            request_config.model_dump_json(),
            json.dumps(warnings or []),
            utc_now_iso(),
        ),
    )
    render_id = int(cur.lastrowid or 0)
    render = get(render_id)
    if render is None:  # pragma: no cover - INSERT just succeeded
        raise RuntimeError(f"failed to read back render id={render_id}")
    return render


def create_running(
    *,
    project_id: int,
    candidate_id: int,
    from_track: str,
    to_track: str,
    technique: RenderTechnique,
    request_config: RenderRequestConfig,
    warnings: list[str] | None = None,
) -> RenderArtifact:
    """Create a RUNNING row and claim token atomically.

    The partial unique index prevents two same-candidate/technique RUNNING rows
    even if two callers race into this function at the same time.
    """
    token = uuid.uuid4().hex
    now = utc_now_iso()
    with db.transaction(immediate=True) as conn:
        active_row = conn.execute(
            "SELECT * FROM render_artifacts "
            "WHERE candidate_id=? AND technique=? AND status='running' "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (candidate_id, technique.value),
        ).fetchone()
        if active_row is not None:
            raise RunningRenderExists(RenderArtifact.from_row(active_row))

        cur = conn.execute(
            "INSERT INTO render_artifacts "
            "(project_id, candidate_id, from_track, to_track, technique, status, "
            " claim_token, request_config_json, warnings_json, created_at, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                candidate_id,
                from_track,
                to_track,
                technique.value,
                RenderStatus.RUNNING.value,
                token,
                request_config.model_dump_json(),
                json.dumps(warnings or []),
                now,
                now,
            ),
        )
        render_id = int(cur.lastrowid or 0)
        artifact_key = artifact_key_for(render_id, project_id, candidate_id, technique)
        conn.execute(
            "UPDATE render_artifacts SET artifact_key=? WHERE id=?",
            (artifact_key, render_id),
        )
        row = conn.execute("SELECT * FROM render_artifacts WHERE id=?", (render_id,)).fetchone()

    if row is None:  # pragma: no cover - INSERT just succeeded
        raise RuntimeError(f"failed to read back render id={render_id}")
    return RenderArtifact.from_row(row)


def complete(
    *,
    render_id: int,
    claim_token: str,
    duration_sec: float,
    sample_rate: int,
    channels: int,
    actuals: RenderActuals,
    warnings: list[str],
) -> RenderArtifact:
    return _terminal_write(
        render_id=render_id,
        claim_token=claim_token,
        status=RenderStatus.COMPLETED,
        duration_sec=duration_sec,
        sample_rate=sample_rate,
        channels=channels,
        actuals=actuals,
        warnings=warnings,
        error=None,
    )


def fail(
    *,
    render_id: int,
    claim_token: str,
    error: str,
    actuals: RenderActuals | None = None,
    warnings: list[str] | None = None,
) -> RenderArtifact:
    return _terminal_write(
        render_id=render_id,
        claim_token=claim_token,
        status=RenderStatus.FAILED,
        duration_sec=None,
        sample_rate=None,
        channels=None,
        actuals=actuals,
        warnings=warnings,
        error=error,
    )


def _terminal_write(
    *,
    render_id: int,
    claim_token: str,
    status: RenderStatus,
    duration_sec: float | None,
    sample_rate: int | None,
    channels: int | None,
    actuals: RenderActuals | None,
    warnings: list[str] | None,
    error: str | None,
) -> RenderArtifact:
    cur = db.execute(
        "UPDATE render_artifacts SET "
        "  status=?, "
        "  duration_sec=?, "
        "  sample_rate=?, "
        "  channels=?, "
        "  actuals_json=?, "
        "  warnings_json=?, "
        "  error=?, "
        "  finished_at=? "
        "WHERE id=? AND claim_token=? AND status='running'",
        (
            status.value,
            duration_sec,
            sample_rate,
            channels,
            actuals.model_dump_json() if actuals is not None else None,
            json.dumps(warnings or []),
            error,
            utc_now_iso(),
            render_id,
            claim_token,
        ),
    )
    if cur.rowcount == 0:
        log.warning(
            "render terminal write discarded - claim/status mismatch (render=%s status=%s)",
            render_id,
            status.value,
        )
    current = get(render_id)
    if current is None:  # pragma: no cover - row pre-existed
        raise RuntimeError(f"render vanished before terminal write: {render_id}")
    return current


def cancel(render_id: int, *, error: str | None = None) -> RenderArtifact | None:
    now = utc_now_iso()
    cur = db.execute(
        "UPDATE render_artifacts SET status='cancelled', error=?, finished_at=? "
        "WHERE id=? AND status IN ('queued', 'running')",
        (error, now, render_id),
    )
    if cur.rowcount == 0:
        return get(render_id)
    return get(render_id)


def delete(render_id: int) -> bool:
    cur = db.execute("DELETE FROM render_artifacts WHERE id=?", (render_id,))
    return cur.rowcount > 0


def recover_stale_running(*, stale_after_sec: float) -> int:
    """Mark stale RUNNING renders failed after a crash/reload."""
    rows = db.fetch_all("SELECT * FROM render_artifacts WHERE status='running'")
    count = 0
    for row in rows:
        render = RenderArtifact.from_row(row)
        if not _is_stale(render, stale_after_sec):
            continue
        db.execute(
            "UPDATE render_artifacts SET status='failed', error=?, finished_at=? WHERE id=?",
            (
                f"render marked failed after being RUNNING for more than {stale_after_sec:.0f}s",
                utc_now_iso(),
                render.id,
            ),
        )
        count += 1
    return count


def _is_stale(render: RenderArtifact, stale_after_sec: float) -> bool:
    if render.started_at is None:
        return True
    try:
        started = datetime.strptime(render.started_at, TIMESTAMP_FMT).replace(tzinfo=UTC)
    except ValueError:
        return True
    return (datetime.now(UTC) - started).total_seconds() > stale_after_sec
