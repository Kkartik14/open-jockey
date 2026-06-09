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
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aidj import __version__
from aidj.audio import peaks as audio_peaks
from aidj.config import settings
from aidj.plugins.manifest import Hardware
from aidj.plugins.registry import registry
from aidj.plugins.runtime import Plugin, PluginError
from aidj.profile_builder import TrackNotFoundError, build_profile
from aidj.store import analysis_labels, analysis_runs, db, jobs, track_profiles, tracks
from aidj.store.models import (
    AnalysisLabel,
    AnalysisLabelKind,
    AnalysisRun,
    BeatGridAnalysis,
    Job,
    JobStatus,
    KeyAnalysis,
    Track,
    TrackProfile,
)

CLOUD_AUDIO_OPT_IN_ENV = "AIDJ_ALLOW_CLOUD_AUDIO"
# RUNNING rows older than ``2 * default_timeout`` are treated as stale (the
# backend probably crashed mid-run); the next analyze call auto-recovers.
_STALE_TIMEOUT_MULTIPLIER = 2.0

# Per-analyzer expected output schema. Validated once at the API boundary so
# downstream code (profile_builder, frontend parsers) inherits the guarantee
# instead of each layer re-implementing defensive parsing. Analyzers not in
# this map pass through unvalidated — the contract is opt-in per plugin name.
_ANALYZER_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "allin1": BeatGridAnalysis,
    "allin1_remote": BeatGridAnalysis,
    "librosa": BeatGridAnalysis,
    "essentia": KeyAnalysis,
}

# Methods that bypass the analyze lifecycle if reached via the generic
# plugin-call endpoint (no track lookup, no claim, no cloud-audio gate, no
# fail_run persistence). They MUST be invoked through
# ``POST /api/tracks/{hash}/analyze/{name}`` instead.
_RPC_RESERVED_METHODS: frozenset[str] = frozenset({"analyze"})

# Used by the audio-streaming endpoint to set Content-Type from the file's
# extension. Anything not in this map falls back to ``application/octet-stream``
# (browsers usually figure it out from the byte stream anyway).
_AUDIO_MEDIA_TYPES: dict[str, str] = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "aif": "audio/aiff",
    "aiff": "audio/aiff",
    "wma": "audio/x-ms-wma",
}

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


class UpdateTrackRequest(BaseModel):
    """Patch payload for ``PATCH /api/tracks/{hash}``.

    Today only ``genre`` is mutable post-ingest; the model is shaped for
    growth — when more user-mutable fields land (e.g. a manual ``name``
    override), they get added here and dispatched in ``update_track``.
    """
    model_config = ConfigDict(extra="forbid")

    genre: str | None = Field(default=None, max_length=100)


class EnqueueRequest(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    max_retries: int = 3


class EnqueueResponse(BaseModel):
    id: int


class PeaksResponse(BaseModel):
    duration_sec: float
    samples: int
    peaks: list[float]


class AnalyzeRequest(BaseModel):
    force: bool = Field(default=False, description="Re-run even if a completed run for this analyzer version exists.")
    timeout: float | None = Field(
        default=None,
        gt=0,
        description="Per-call timeout in seconds. None → plugin default.",
    )


class AnalysisRunDetail(AnalysisRun):
    """An analysis run with embedded verification labels.

    Returned by the listing endpoint so the frontend doesn't have to fan out N
    label requests after fetching N runs (the previous behaviour got noisy
    fast in a bake-off across many tracks). The single-run endpoint still
    returns plain ``AnalysisRun`` since label mutations live at separate paths.
    """

    labels: list[AnalysisLabel] = Field(default_factory=list)


class AddLabelRequest(BaseModel):
    kind: AnalysisLabelKind
    notes: str | None = Field(default=None, max_length=500)


class ProfileCoverageResponse(BaseModel):
    """Library-wide ``TrackProfile`` coverage. Drives the readiness summary.

    Buckets are exhaustive: ``ready + partial + blocked + missing`` equals the
    total number of ingested tracks. A track without a persisted profile
    counts as ``missing``.
    """
    ready: int = Field(ge=0)
    partial: int = Field(ge=0)
    blocked: int = Field(ge=0)
    missing: int = Field(ge=0)


class LabelRollupResponse(BaseModel):
    """Cross-track bake-off rollup. Drives the Library page's rollup table.

    ``by_analyzer`` is the global summary; ``by_analyzer_and_genre`` is the
    per-genre breakdown (tracks without a genre bucketed under
    ``analysis_labels.UNTAGGED_GENRE``). Both come from the same labels table
    so the totals are consistent.
    """
    by_analyzer: dict[str, dict[AnalysisLabelKind, int]]
    by_analyzer_and_genre: dict[str, dict[str, dict[AnalysisLabelKind, int]]]
    total_labels: int
    total_labeled_runs: int


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
    """Generic plugin RPC for ``info``/``ping``/admin methods.

    Deliberately NOT a way to run analysis: ``method="analyze"`` would skip
    the track lookup, atomic claim/token lifecycle, persisted failure
    handling, AND the cloud-audio opt-in gate — all of which the dedicated
    ``POST /api/tracks/{hash}/analyze/{name}`` route enforces. Reject it here
    so a misled caller can't bypass that surface.

    The cloud-audio opt-in gate is enforced on this route too: a plugin
    flagged ``cloud_audio: true`` may upload bytes on *any* method (not just
    ``analyze``), so a missing opt-in must block the whole RPC, not just
    ``analyze`` specifically.
    """
    if body.method in _RPC_RESERVED_METHODS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"method {body.method!r} must go through "
                f"POST /api/tracks/{{hash}}/analyze/{{name}} (atomic claim, "
                "claim-token-conditional terminal writes, cloud-audio gate, "
                "persisted failure handling)."
            ),
        )
    try:
        plugin = registry().get(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if plugin.manifest.manifest.cloud_audio and not _cloud_audio_allowed():
        raise HTTPException(
            status_code=403,
            detail=(
                f"plugin '{name}' is flagged cloud_audio; set "
                f"{CLOUD_AUDIO_OPT_IN_ENV}=1 in the backend's environment to opt in."
            ),
        )
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


@app.get("/api/tracks/{content_hash}", response_model=Track)
def get_track(content_hash: str) -> Track:
    t = tracks.get(content_hash)
    if t is None:
        raise HTTPException(status_code=404, detail=f"track not found: {content_hash}")
    return t


@app.patch("/api/tracks/{content_hash}", response_model=Track)
def update_track(content_hash: str, body: UpdateTrackRequest) -> Track:
    """Patch a track's user-editable metadata. Currently only ``genre``."""
    existing = tracks.get(content_hash)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"track not found: {content_hash}")

    # PATCH semantics: omitted fields mean "leave unchanged"; explicit null
    # means "clear". This matters once UpdateTrackRequest gains more fields.
    updated = existing
    if "genre" in body.model_fields_set:
        updated = tracks.set_genre(content_hash, body.genre)
        if updated is None:  # pragma: no cover - existence checked just above
            raise HTTPException(status_code=404, detail=f"track not found: {content_hash}")
    return updated


@app.get("/api/tracks/{content_hash}/audio")
def stream_track_audio(content_hash: str) -> FileResponse:
    """Stream the source audio for a track so the frontend's waveform / player
    can pull it without the user having to open file URLs.

    - 404 if the hash isn't in the store
    - 410 (Gone) if the row exists but the file's been moved/deleted on disk
    - Otherwise streams via Starlette's ``FileResponse`` with
      ``Content-Disposition: inline`` so browsers play instead of downloading.
      Range requests are honoured automatically (seek + buffered playback).
    """
    track = tracks.get(content_hash)
    if track is None:
        raise HTTPException(status_code=404, detail=f"track not found: {content_hash}")
    p = Path(track.source_path)
    if not p.is_file():
        raise HTTPException(
            status_code=410,
            detail=f"source file no longer present at {track.source_path}",
        )
    suffix = p.suffix.lstrip(".").lower()
    media_type = _AUDIO_MEDIA_TYPES.get(suffix, "application/octet-stream")
    # ``inline`` so browsers play instead of downloading. We pass ``filename``
    # so Starlette actually emits the header (without it, the disposition is
    # omitted entirely) and so a manual save-as gets the right filename.
    return FileResponse(
        p,
        media_type=media_type,
        filename=p.name,
        content_disposition_type="inline",
    )


@app.get("/api/tracks/{content_hash}/peaks", response_model=PeaksResponse)
def get_track_peaks(
    content_hash: str,
    samples: int = Query(2048, ge=64, le=10_000),
) -> PeaksResponse:
    """Return precomputed waveform peaks (and duration) for a track.

    The frontend uses this to render the waveform without fetching the entire
    audio file. Per (track_hash, samples) the result is cached in the project
    store so subsequent calls are essentially free.

    - 404 if the hash isn't in the store
    - 410 if the source file is gone
    - 503 if ffmpeg/ffprobe aren't available or the decode fails
    """
    track = tracks.get(content_hash)
    if track is None:
        raise HTTPException(status_code=404, detail=f"track not found: {content_hash}")
    p = Path(track.source_path)
    if not p.is_file():
        raise HTTPException(
            status_code=410,
            detail=f"source file no longer present at {track.source_path}",
        )
    try:
        data = audio_peaks.get_or_compute_peaks(track.content_hash, p, samples=samples)
    except audio_peaks.PeaksError as exc:
        raise HTTPException(status_code=503, detail=f"could not compute peaks: {exc}") from exc

    return PeaksResponse(
        duration_sec=data.duration_sec,
        samples=data.samples,
        peaks=data.peaks,
    )


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


def _validate_output_or_none(
    analyzer_name: str, output: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Enforce the per-analyzer output schema at the API boundary.

    Returns ``(output_dict, None)`` when valid (or when no schema is registered
    for this analyzer — opt-in by plugin name). Returns ``(None, error)`` when
    a registered analyzer returns something that doesn't match its schema; the
    caller persists that as a ``FAILED`` run rather than letting garbage land
    in ``analysis_runs.output_json`` for the profile builder + frontend to
    re-discover later.
    """
    schema = _ANALYZER_OUTPUT_SCHEMAS.get(analyzer_name)
    if schema is None:
        # Unknown plugin: pass through unchanged. The existing
        # ``{"raw": ...}`` wrapping for non-dict output still happens at the
        # caller — this function only enforces *known* contracts.
        if not isinstance(output, dict):
            return {"raw": output}, None
        return output, None
    if not isinstance(output, dict):
        return None, (
            f"{analyzer_name} returned a non-dict output "
            f"(type={type(output).__name__}); expected {schema.__name__}"
        )
    try:
        schema.model_validate(output)
    except ValidationError as exc:
        return None, (
            f"{analyzer_name} output failed {schema.__name__} validation: "
            f"{exc.errors(include_url=False, include_input=False)}"
        )
    return output, None


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

    validated, validation_error = _validate_output_or_none(plugin.name, output)
    if validation_error is not None:
        # The plugin succeeded at the wire level but produced output that
        # violates its declared schema. Persist a FAILED run so the profile
        # builder doesn't have to re-litigate this downstream.
        log.warning(
            "analyzer output validation failed: track=%s analyzer=%s err=%s",
            track.content_hash[:12], plugin.name, validation_error,
        )
        return analysis_runs.fail_run(
            track_hash=track.content_hash,
            analyzer_name=plugin.name,
            analyzer_version=version,
            claim_token=claim_token,
            error=validation_error,
            finished_at=analysis_runs.utc_now_iso(),
        )

    raw_confidence = validated.get("confidence") if validated is not None else None
    return analysis_runs.complete_run(
        track_hash=track.content_hash,
        analyzer_name=plugin.name,
        analyzer_version=version,
        claim_token=claim_token,
        output=validated,
        confidence=_coerce_confidence(raw_confidence),
        finished_at=analysis_runs.utc_now_iso(),
    )


@app.get("/api/tracks/{content_hash}/analyses", response_model=list[AnalysisRunDetail])
def list_track_analyses(content_hash: str) -> list[AnalysisRunDetail]:
    if tracks.get(content_hash) is None:
        raise HTTPException(status_code=404, detail=f"track not found: {content_hash}")
    runs = analysis_runs.list_for_track(content_hash)
    labels_map = analysis_labels.list_for_runs([r.id for r in runs])
    return [
        AnalysisRunDetail(**r.model_dump(), labels=labels_map.get(r.id, []))
        for r in runs
    ]


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
# Analysis labels (bake-off verification)
# ---------------------------------------------------------------------------


_TERMINAL_STATUSES = frozenset({"completed", "failed"})


def _get_run_or_404(run_id: int) -> dict[str, Any]:
    row = db.fetch_one("SELECT id, status FROM analysis_runs WHERE id=?", (run_id,))
    if row is None:
        raise HTTPException(status_code=404, detail=f"analysis run not found: {run_id}")
    return dict(row)


@app.post(
    "/api/analyses/{run_id}/labels",
    response_model=AnalysisLabel,
    status_code=201,
)
def add_analysis_label(run_id: int, body: AddLabelRequest) -> AnalysisLabel:
    run = _get_run_or_404(run_id)
    if run["status"] not in _TERMINAL_STATUSES:
        # Verification events only make sense once the analyzer has produced
        # something to verify. Reject early so direct API callers can't create
        # labels the UI deliberately hides.
        raise HTTPException(
            status_code=409,
            detail=(
                f"can only label completed or failed runs (run is "
                f"{run['status']}); wait for the analyzer to finish."
            ),
        )
    return analysis_labels.add(
        analysis_run_id=run_id,
        kind=body.kind,
        notes=body.notes,
    )


@app.get("/api/analyses/{run_id}/labels", response_model=list[AnalysisLabel])
def list_analysis_labels(run_id: int) -> list[AnalysisLabel]:
    _get_run_or_404(run_id)
    return analysis_labels.list_for_run(run_id)


@app.delete("/api/analyses/{run_id}/labels/{label_id}", status_code=204)
def delete_analysis_label(run_id: int, label_id: int) -> None:
    label = analysis_labels.get(label_id)
    if label is None or label.analysis_run_id != run_id:
        raise HTTPException(status_code=404, detail=f"label not found: {label_id}")
    analysis_labels.delete(label_id)


@app.get("/api/labels/rollup", response_model=LabelRollupResponse)
def get_label_rollup() -> LabelRollupResponse:
    """Cross-track bake-off summary. Empty dicts when no labels exist yet."""
    total_labels_row = db.fetch_one("SELECT COUNT(*) AS n FROM analysis_labels")
    total_runs_row = db.fetch_one(
        "SELECT COUNT(DISTINCT analysis_run_id) AS n FROM analysis_labels"
    )
    return LabelRollupResponse(
        by_analyzer=analysis_labels.rollup_by_analyzer(),
        by_analyzer_and_genre=analysis_labels.rollup_by_analyzer_and_genre(),
        total_labels=int(total_labels_row["n"]) if total_labels_row else 0,
        total_labeled_runs=int(total_runs_row["n"]) if total_runs_row else 0,
    )


# ---------------------------------------------------------------------------
# Track profiles (Phase 2)
# ---------------------------------------------------------------------------


@app.get(
    "/api/tracks/{content_hash}/profile",
    response_model=TrackProfile,
)
def get_track_profile(content_hash: str) -> TrackProfile:
    """Read-only fetch of the persisted profile for a track.

    Never builds on read — explicit builds go through
    ``POST .../profile/build``. Two distinct 404s so the caller knows
    whether to ingest first or build first.
    """
    if tracks.get(content_hash) is None:
        raise HTTPException(status_code=404, detail=f"track not found: {content_hash}")
    profile = track_profiles.get(content_hash)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail=f"no profile for {content_hash[:12]}; build via POST .../profile/build",
        )
    return profile


@app.post(
    "/api/tracks/{content_hash}/profile/build",
    response_model=TrackProfile,
)
def build_track_profile(content_hash: str) -> TrackProfile:
    """Synchronously (re)build the canonical profile from current analyzer runs.

    Always passes ``force=True``: an explicit user/API request must not depend
    on second-resolution staleness — if a same-second analyzer completion
    finishes after the profile is built, the next un-forced check would treat
    the profile as fresh and return stale data. Force sidesteps that entirely.
    Maps :class:`TrackNotFoundError` to 404.
    """
    try:
        return build_profile(content_hash, force=True)
    except TrackNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/profiles/coverage", response_model=ProfileCoverageResponse)
def get_profile_coverage() -> ProfileCoverageResponse:
    """Counts per readiness bucket plus ``missing`` for tracks without a profile."""
    counts = track_profiles.coverage_counts()
    return ProfileCoverageResponse(
        ready=counts["ready"],
        partial=counts["partial"],
        blocked=counts["blocked"],
        missing=counts["missing"],
    )


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
