"""FastAPI app — health checks, plugin RPC, track ingest, job inspection.

Every route declares a ``response_model`` so the OpenAPI schema is real and the
output is validated. Domain models from ``aidj.store.models`` and
``aidj.plugins.manifest`` are used directly — no second copy of the wire shape
lives in this module.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from aidj import __version__
from aidj.config import settings
from aidj.plugins.manifest import Hardware
from aidj.plugins.registry import registry
from aidj.plugins.runtime import Plugin, PluginError
from aidj.store import analysis_runs, db, jobs, tracks
from aidj.store.models import AnalysisRun, Job, JobStatus, Track

CLOUD_AUDIO_OPT_IN_ENV = "AIDJ_ALLOW_CLOUD_AUDIO"
# RUNNING rows older than ``2 * default_timeout`` are treated as stale (the
# backend probably crashed mid-run); the next analyze call auto-recovers.
_STALE_TIMEOUT_MULTIPLIER = 2.0

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response shapes that aren't already domain models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    version: str
    project_root: str
    store_root: str
    schema_version: int | None


class PluginInfo(BaseModel):
    name: str
    version: str
    description: str
    python: str
    hardware: Hardware
    concurrency_safe: bool
    default_timeout_sec: float
    cloud_audio: bool


class PluginCallRequest(BaseModel):
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    timeout: float | None = Field(
        default=None,
        gt=0,
        description="Per-call timeout in seconds. None → plugin default.",
    )


class PluginCallResponse(BaseModel):
    result: Any


class IngestRequest(BaseModel):
    path: str


class EnqueueRequest(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    max_retries: int = 3


class EnqueueResponse(BaseModel):
    id: int


class AnalyzeRequest(BaseModel):
    force: bool = Field(default=False, description="Re-run even if a completed run for this analyzer version exists.")
    timeout: float | None = Field(
        default=None,
        gt=0,
        description="Per-call timeout in seconds. None → plugin default.",
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = settings()
    s.ensure_dirs()
    db.get_conn()
    log.info("aidj %s ready (store=%s)", __version__, s.store_root)
    try:
        yield
    finally:
        registry().stop_all()


app = FastAPI(title="aidj", version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    s = settings()
    row = db.fetch_one("SELECT value FROM schema_meta WHERE key='schema_version'")
    return HealthResponse(
        status="ok",
        version=__version__,
        project_root=str(s.project_root),
        store_root=str(s.store_root),
        schema_version=int(row["value"]) if row else None,
    )


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------


@app.get("/api/plugins", response_model=list[PluginInfo])
def list_plugins() -> list[PluginInfo]:
    return [
        PluginInfo(
            name=lm.name,
            version=lm.version,
            description=lm.manifest.description,
            python=lm.manifest.python,
            hardware=lm.manifest.hardware,
            concurrency_safe=lm.manifest.concurrency_safe,
            default_timeout_sec=lm.manifest.default_timeout_sec,
            cloud_audio=lm.manifest.cloud_audio,
        )
        for lm in registry().manifests()
    ]


@app.post("/api/plugins/{name}/call", response_model=PluginCallResponse)
def call_plugin(name: str, body: PluginCallRequest) -> PluginCallResponse:
    try:
        plugin = registry().get(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        result = plugin.call(body.method, body.params, timeout=body.timeout)
    except PluginError as exc:
        # Map runtime errors to HTTP. -32001 = timeout → 504; others → 500.
        status_code = 504 if exc.code == -32001 else 500
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message, "trace": exc.trace},
        ) from exc
    return PluginCallResponse(result=result)


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------


@app.post("/api/tracks/ingest", response_model=Track)
def ingest_track(body: IngestRequest) -> Track:
    p = Path(body.path).expanduser()
    if not p.is_file():
        raise HTTPException(status_code=400, detail=f"not a file: {p}")
    return tracks.ingest(p)


@app.get("/api/tracks", response_model=list[Track])
def list_tracks(limit: int = Query(1000, ge=1, le=10_000)) -> list[Track]:
    return tracks.list_all(limit=limit)


# ---------------------------------------------------------------------------
# Analysis runs
# ---------------------------------------------------------------------------


def _coerce_confidence(value: Any) -> float | None:
    """Coerce a plugin-supplied confidence to a SQLite REAL or None.

    Plugins may return strings (``"high"``), nested objects, or sentinel values.
    Only proper int/float (excluding ``bool``, which is a subclass of int) is
    acceptable here; everything else is dropped to ``None``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _cloud_audio_allowed() -> bool:
    return os.environ.get(CLOUD_AUDIO_OPT_IN_ENV, "").strip() == "1"


@app.post(
    "/api/tracks/{content_hash}/analyze/{analyzer_name}",
    response_model=AnalysisRun,
)
def analyze_track(
    content_hash: str,
    analyzer_name: str,
    body: AnalyzeRequest,
) -> AnalysisRun:
    """Run ``analyzer_name`` on the given track. Returns the resulting AnalysisRun.

    Lifecycle:

    - 404 if the track or analyzer plugin doesn't exist.
    - 403 if the plugin's manifest declares ``cloud_audio: true`` and the
      backend env doesn't have ``AIDJ_ALLOW_CLOUD_AUDIO=1`` — explicit opt-in
      for plugins that send audio bytes off the local machine.
    - The (track, analyzer, version) slot is acquired atomically via
      ``analysis_runs.claim_running``:
        * **RUNNING** elsewhere and not stale → return that row (caller polls).
        * **COMPLETED** without ``force`` → return cached row.
        * **RUNNING** older than ``2 × default_timeout`` → auto-recovery, claim it.
        * **force=true** overrides RUNNING/COMPLETED and re-runs.
    - Once claimed, the plugin is invoked and the row transitions to
      ``COMPLETED`` or ``FAILED``. Plugin failures are persisted (HTTP 200 with
      ``status="failed"``); the caller reads ``status``.
    """
    track = tracks.get(content_hash)
    if track is None:
        raise HTTPException(status_code=404, detail=f"track not found: {content_hash}")
    try:
        plugin = registry().get(analyzer_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if plugin.manifest.manifest.cloud_audio and not _cloud_audio_allowed():
        raise HTTPException(
            status_code=403,
            detail=(
                f"analyzer '{analyzer_name}' uploads audio to a remote service. "
                f"Set {CLOUD_AUDIO_OPT_IN_ENV}=1 in the backend's environment to opt in."
            ),
        )

    stale_after_sec = plugin.default_timeout * _STALE_TIMEOUT_MULTIPLIER
    claim = analysis_runs.claim_running(
        track_hash=content_hash,
        analyzer_name=analyzer_name,
        analyzer_version=plugin.manifest.version,
        force=body.force,
        stale_after_sec=stale_after_sec,
    )
    if not claim.claimed:
        return claim.run

    return _execute_with_claim(track, plugin, body.timeout, claim.token)


def _execute_with_claim(
    track: Track,
    plugin: Plugin,
    timeout: float | None,
    claim_token: str,
) -> AnalysisRun:
    """Invoke the plugin under an already-claimed RUNNING row.

    Terminal writes (COMPLETED / FAILED) are conditional on ``claim_token``: if
    a newer ``claim_running`` (force or stale-recovery) has reused the slot
    while we were waiting on the plugin, our result is dropped and the current
    row state is returned instead.
    """
    version = plugin.manifest.version

    try:
        output = plugin.call(
            "analyze",
            {"audio_path": track.source_path},
            timeout=timeout,
        )
    except PluginError as exc:
        log.warning(
            "analysis failed: track=%s analyzer=%s code=%s msg=%s",
            track.content_hash[:12], plugin.name, exc.code, exc.message,
        )
        return analysis_runs.fail_run(
            track_hash=track.content_hash,
            analyzer_name=plugin.name,
            analyzer_version=version,
            claim_token=claim_token,
            error=f"[{exc.code}] {exc.message}",
            finished_at=analysis_runs.utc_now_iso(),
        )

    raw_confidence = output.get("confidence") if isinstance(output, dict) else None
    return analysis_runs.complete_run(
        track_hash=track.content_hash,
        analyzer_name=plugin.name,
        analyzer_version=version,
        claim_token=claim_token,
        output=output if isinstance(output, dict) else {"raw": output},
        confidence=_coerce_confidence(raw_confidence),
        finished_at=analysis_runs.utc_now_iso(),
    )


@app.get("/api/tracks/{content_hash}/analyses", response_model=list[AnalysisRun])
def list_track_analyses(content_hash: str) -> list[AnalysisRun]:
    if tracks.get(content_hash) is None:
        raise HTTPException(status_code=404, detail=f"track not found: {content_hash}")
    return analysis_runs.list_for_track(content_hash)


@app.get(
    "/api/tracks/{content_hash}/analyses/{analyzer_name}",
    response_model=AnalysisRun,
)
def get_track_analysis(content_hash: str, analyzer_name: str) -> AnalysisRun:
    if tracks.get(content_hash) is None:
        raise HTTPException(status_code=404, detail=f"track not found: {content_hash}")
    run = analysis_runs.get(content_hash, analyzer_name)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail=f"no analysis run for {content_hash[:12]}/{analyzer_name}",
        )
    return run


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@app.post("/api/jobs", response_model=EnqueueResponse)
def enqueue_job(body: EnqueueRequest) -> EnqueueResponse:
    return EnqueueResponse(
        id=jobs.enqueue(body.kind, body.payload, max_retries=body.max_retries),
    )


@app.get("/api/jobs", response_model=list[Job])
def list_jobs(
    status: JobStatus | None = None,
    limit: int = Query(50, ge=1, le=1000),
) -> list[Job]:
    return jobs.list_recent(limit=limit, status=status)
