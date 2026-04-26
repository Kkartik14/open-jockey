"""FastAPI app — health checks, plugin RPC, track ingest, job inspection.

Every route declares a ``response_model`` so the OpenAPI schema is real and the
output is validated. Domain models from ``aidj.store.models`` and
``aidj.plugins.manifest`` are used directly — no second copy of the wire shape
lives in this module.
"""
from __future__ import annotations

import logging
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
from aidj.plugins.runtime import PluginError
from aidj.store import db, jobs, tracks
from aidj.store.models import Job, JobStatus, Track

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
