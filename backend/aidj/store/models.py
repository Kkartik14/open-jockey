"""Domain models for the project store.

These are the canonical shapes of every entity the store persists. They live
here (not next to SQL helpers) so handlers, repositories, the API surface, and
tests all import the same definitions — no second copy in HTTP-response code,
no third copy in the frontend type file (the frontend hand-mirrors these
intentionally; if they ever drift, an OpenAPI codegen step is the right fix).

Conversions from sqlite3.Row are defined as ``from_row`` classmethods so the
repository functions (``store.tracks``, ``store.jobs``, ``store.analysis_runs``)
stay tiny.
"""

from __future__ import annotations

import json
import sqlite3
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AnalysisStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AnalysisLabelKind(StrEnum):
    """Verification labels a user can attach to an analysis run during bake-off.

    The canonical failure-mode set the friend audit asked for: not just
    correct/✗, but the *kind* of mistake an analyzer made so we can roll up
    per-genre and per-analyzer stats when picking a default.
    """

    CORRECT = "correct"
    HALF_TIME = "half_time"
    DOUBLE_TIME = "double_time"
    WRONG_DOWNBEAT_PHASE = "wrong_downbeat_phase"
    EARLY_BY_MS = "early_by_ms"
    LATE_BY_MS = "late_by_ms"
    WRONG_SECTION_LABELS = "wrong_section_labels"
    UNUSABLE = "unusable"


class SectionLabel(StrEnum):
    """Normalised structure-segment label set.

    Different analyzers emit different vocabularies (allin1: intro/verse/chorus/
    bridge/inst/outro; MSAF: numeric clusters). Plugins are responsible for
    mapping their native labels onto this enum.
    """

    INTRO = "intro"
    VERSE = "verse"
    CHORUS = "chorus"
    BRIDGE = "bridge"
    DROP = "drop"
    BREAKDOWN = "breakdown"
    INSTRUMENTAL = "instrumental"
    OUTRO = "outro"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class _ModelBase(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)


# ---------------------------------------------------------------------------
# Persisted entities
# ---------------------------------------------------------------------------


class Track(_ModelBase):
    content_hash: str
    source_path: str
    duration_sec: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    format: str | None = None
    bitrate: int | None = None
    file_size: int | None = None
    genre: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls.model_validate(dict(row))


class Job(_ModelBase):
    id: int
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus
    retries: int
    max_retries: int
    error: str | None = None
    result: dict[str, Any] | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        d = dict(row)
        return cls(
            id=d["id"],
            kind=d["kind"],
            payload=json.loads(d["payload_json"]) if d.get("payload_json") else {},
            status=JobStatus(d["status"]),
            retries=d["retries"],
            max_retries=d["max_retries"],
            error=d.get("error"),
            result=json.loads(d["result_json"]) if d.get("result_json") else None,
        )


class Project(_ModelBase):
    """A mix project. Phase 3 uses this as the owner for candidate graph edges."""

    id: int
    name: str
    intent: str | None = None
    plan: dict[str, Any] | None = None
    render_artifact_key: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        d = dict(row)
        return cls(
            id=d["id"],
            name=d["name"],
            intent=d.get("intent"),
            plan=json.loads(d["plan_json"]) if d.get("plan_json") else None,
            render_artifact_key=d.get("render_artifact_key"),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
        )


class AnalysisRun(_ModelBase):
    id: int
    track_hash: str
    analyzer_name: str
    analyzer_version: str
    status: AnalysisStatus
    output: dict[str, Any] | None = None
    confidence: float | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        d = dict(row)
        return cls(
            id=d["id"],
            track_hash=d["track_hash"],
            analyzer_name=d["analyzer_name"],
            analyzer_version=d["analyzer_version"],
            status=AnalysisStatus(d["status"]),
            output=json.loads(d["output_json"]) if d.get("output_json") else None,
            confidence=d.get("confidence"),
            error=d.get("error"),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
        )


class AnalysisLabel(_ModelBase):
    id: int
    analysis_run_id: int
    kind: AnalysisLabelKind
    notes: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        d = dict(row)
        return cls(
            id=d["id"],
            analysis_run_id=d["analysis_run_id"],
            kind=AnalysisLabelKind(d["kind"]),
            notes=d.get("notes"),
            created_at=d.get("created_at"),
        )


# ---------------------------------------------------------------------------
# Analyzer output schemas (stored as JSON in analysis_runs.output_json)
# ---------------------------------------------------------------------------


class Beat(_ModelBase):
    """One detected beat. ``time_sec`` is the onset; downbeats are flagged."""

    time_sec: float
    is_downbeat: bool = False
    confidence: float | None = None


class Section(_ModelBase):
    """One structure segment (intro, verse, drop, etc.)."""

    start_sec: float
    end_sec: float
    label: SectionLabel
    confidence: float | None = None


class TempoEstimate(_ModelBase):
    """Detected tempo with optional confidence and half/double-time hints."""

    bpm: float
    confidence: float | None = None
    half_time_likely: bool = False
    double_time_likely: bool = False


class BeatGridAnalysis(_ModelBase):
    """Output schema for beat-and-structure analyzers (allin1, madmom+MSAF).

    A plugin's ``analyze`` method returns JSON conforming to this shape; the API
    stores it in ``analysis_runs.output_json``.
    """

    tempo: TempoEstimate
    beats: list[Beat]
    sections: list[Section]
    duration_sec: float
    confidence: float | None = Field(
        default=None,
        description="Overall confidence in the analysis. Surfaces in analysis_runs.confidence.",
    )


class KeyAnalysis(_ModelBase):
    """Output schema for key-detection analyzers (essentia)."""

    key: str = Field(..., description='Tonic, e.g. "C", "F#", "Bb".')
    scale: str = Field(..., description='"major" or "minor".')
    camelot: str | None = Field(
        default=None,
        description='Camelot wheel notation, e.g. "8B" for C major.',
    )
    confidence: float | None = None


# ---------------------------------------------------------------------------
# Canonical TrackProfile (Phase 2)
# ---------------------------------------------------------------------------
#
# Funnel between the analyzer layer and every downstream layer. Per-analyzer
# JSON in ``analysis_runs`` is fine for the bake-off but the planner, candidate
# graph, and renderer all want ONE trusted, normalised view of a track. The
# builder (step 3) materialises a TrackProfile from analysis_runs; the
# repository in ``store.track_profiles`` persists it; this module just defines
# the shape.

# Bumped when the builder logic, completeness scoring, source-selection rules,
# or this JSON shape changes. A persisted profile is stale if its
# ``profile_version`` is less than this constant.
CURRENT_PROFILE_VERSION = 1


class Readiness(StrEnum):
    """Hard gate downstream layers use to decide what's allowed.

    ``ready``: every required field is filled; full transition generation OK.
    ``partial``: enough is filled for simple transitions (long crossfade, etc.)
    but not stem-aware techniques; downstream layers can degrade gracefully.
    ``blocked``: not usable — the candidate graph should refuse this track.
    """

    READY = "ready"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class FieldProvenance(_ModelBase):
    """Where a profile block's data actually came from.

    ``source`` is ``"<analyzer-name>@<analyzer-version>"`` for plugin-sourced
    blocks (matches the strings PluginInfo exposes), or
    ``"<backend-module>@<version>"`` for backend-derived blocks (e.g.
    ``"aidj.energy@0.1.0"``).

    ``analysis_run_id`` links back to the row that produced the data so the
    staleness check can ask "is that analysis_run still the most recent?";
    None for backend-derived blocks that don't go through analysis_runs.
    """

    source: str
    analysis_run_id: int | None = None


class CompletenessFields(_ModelBase):
    """One boolean per canonical field — drives ``Readiness`` and the UI badge.

    ``completeness_score`` on the TrackProfile is derived from this; the
    booleans are kept explicit so downstream code can gate per-field
    (``if profile.fields.has_vocals: ...``) without parsing a float.
    """

    has_beat_grid: bool = False
    has_key: bool = False
    has_sections: bool = False
    has_energy: bool = False
    has_vocals: bool = False


class TempoBlock(_ModelBase):
    """Tempo + its provenance. Conceptually coupled to ``BeatGridBlock`` —
    the builder must ensure both blocks share the same provenance so a profile
    can't end up with BPM from one analyzer and beats from another."""

    bpm: float = Field(gt=0.0)
    confidence: float | None = None
    provenance: FieldProvenance


class BeatGridBlock(_ModelBase):
    """Beats + downbeats + duration as one structurally-coupled block.

    ``downbeat_count`` is denormalised from ``beats`` for convenience (avoids
    counting client-side); the builder writes it once and the UI reads it.
    """

    beats: list[Beat]
    downbeat_count: int = Field(ge=0)
    duration_sec: float = Field(ge=0.0)
    provenance: FieldProvenance

    @model_validator(mode="after")
    def _downbeat_count_matches_beats(self) -> Self:
        actual = sum(1 for beat in self.beats if beat.is_downbeat)
        if self.downbeat_count != actual:
            raise ValueError(
                f"downbeat_count={self.downbeat_count} does not match beats ({actual})"
            )
        return self


class KeyBlock(_ModelBase):
    """Tonic + scale + Camelot, sourced together from a key-detection analyzer."""

    key: str
    scale: str
    camelot: str | None = None
    confidence: float | None = None
    provenance: FieldProvenance


class SectionsBlock(_ModelBase):
    """Structural segments. Independent provenance from beat_grid because some
    analyzer pairs (e.g. madmom + MSAF) split beats and sections across two
    different upstreams."""

    items: list[Section]
    provenance: FieldProvenance


class EnergyBlock(_ModelBase):
    """Time-aligned energy curve + derived markers. Filled by the backend
    energy utility in step 4; profiles built before then have ``energy=None``.

    ``values`` is a smoothed, normalised-to-[0,1] curve sampled at
    ``sample_rate_hz``. The shape is intentionally simple — the planner only
    needs to know energy *over time*, not raw waveform peaks (those live in
    the peaks cache).
    """

    sample_rate_hz: float = Field(gt=0.0)
    values: list[float]
    integrated_lufs: float | None = None
    section_energy: dict[str, float] = Field(default_factory=dict)
    drop_times_sec: list[float] = Field(default_factory=list)
    build_times_sec: list[float] = Field(default_factory=list)
    provenance: FieldProvenance

    @model_validator(mode="after")
    def _values_are_normalised(self) -> Self:
        bad = [v for v in self.values if v < 0.0 or v > 1.0]
        if bad:
            raise ValueError("energy values must be normalised to [0, 1]")
        return self


class VocalWindow(_ModelBase):
    """One contiguous time window classified as vocal-heavy or vocal-free.

    ``is_vocal=True`` means the planner should avoid layering another vocal
    track over this window. ``is_vocal=False`` is a candidate for
    ``vocal_avoid_layer`` style transitions where the incoming vocal can
    safely play over the outgoing instrumental.
    """

    start_sec: float = Field(ge=0.0)
    end_sec: float
    is_vocal: bool
    confidence: float | None = None

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end_sec <= self.start_sec:
            raise ValueError("vocal window end_sec must be greater than start_sec")
        return self


class VocalsBlock(_ModelBase):
    """Vocal windows derived from the demucs vocal stem. Filled by step 5;
    profiles built before then have ``vocals=None``.

    ``stem_cache_key`` links back to the cached demucs stem so a downstream
    re-derivation (different VAD threshold, say) can find the source bytes
    without re-running demucs.
    """

    windows: list[VocalWindow]
    stem_cache_key: str | None = None
    provenance: FieldProvenance


class TrackProfile(_ModelBase):
    """The canonical view of a track. One row per track in ``track_profiles``.

    Blocks are optional — a profile can ship with only ``beat_grid`` + ``key``
    if energy/vocals haven't been computed yet; ``readiness`` and
    ``completeness_score`` summarise what's there. Downstream layers should
    read this object, not raw analysis_runs.
    """

    profile_version: int
    track_hash: str
    built_at: str
    readiness: Readiness
    completeness_score: float = Field(ge=0.0, le=1.0)
    fields: CompletenessFields
    tempo: TempoBlock | None = None
    beat_grid: BeatGridBlock | None = None
    key: KeyBlock | None = None
    sections: SectionsBlock | None = None
    energy: EnergyBlock | None = None
    vocals: VocalsBlock | None = None

    @model_validator(mode="after")
    def _completeness_flags_match_blocks(self) -> Self:
        expected = {
            "has_beat_grid": self.tempo is not None and self.beat_grid is not None,
            "has_key": self.key is not None,
            "has_sections": self.sections is not None,
            "has_energy": self.energy is not None,
            "has_vocals": self.vocals is not None,
        }
        mismatches = [
            name for name, present in expected.items() if getattr(self.fields, name) != present
        ]
        if mismatches:
            raise ValueError(
                "completeness fields do not match profile blocks: " + ", ".join(mismatches)
            )
        return self

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Profile data lives entirely in the JSON column; other columns are
        denormalised for indexed queries. The JSON is the truth."""
        return cls.model_validate(json.loads(dict(row)["profile_json"]))


# ---------------------------------------------------------------------------
# Transition Candidate Graph (Phase 3)
# ---------------------------------------------------------------------------


class CandidateVerification(StrEnum):
    """Whether the analyzer evidence under an edge has human listening labels."""

    VERIFIED = "verified"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    HAS_FAILURE_LABEL = "has_failure_label"


class TransitionTechnique(StrEnum):
    """Renderer technique names a candidate can legally advertise.

    The renderer is not built yet; these are contract names only. Candidates may
    list several compatible techniques so a future planner can choose among
    deterministic render paths without inventing cue points.
    """

    PHRASE_SWAP = "phrase_swap"
    FILTER_BLEND = "filter_blend"
    LONG_CROSSFADE = "long_crossfade"
    ECHO_OUT = "echo_out"


class TransitionScores(_ModelBase):
    """Stored scoring/explanation payload for one candidate edge."""

    score: float = Field(ge=0.0, le=1.0)
    tempo_delta_pct: float = Field(ge=0.0)
    tempo_match_ratio: float | None = Field(default=None, gt=0.0)
    from_bpm: float = Field(gt=0.0)
    to_bpm: float = Field(gt=0.0)
    from_cue_sec: float = Field(ge=0.0)
    to_cue_sec: float = Field(ge=0.0)
    phrase_bars: int = Field(ge=1)
    key_compatible: bool | None = None
    verification: CandidateVerification = CandidateVerification.UNVERIFIED
    from_source: str
    to_source: str
    reasons: list[str] = Field(default_factory=list)


class TransitionCandidate(_ModelBase):
    """One directed transition edge from ``from_track`` to ``to_track``."""

    id: int | None = None
    project_id: int
    from_track: str
    to_track: str
    from_cue_bar: int = Field(ge=0)
    to_cue_bar: int = Field(ge=0)
    scores: TransitionScores
    allowed_techniques: list[TransitionTechnique]
    created_at: str | None = None

    @model_validator(mode="after")
    def _edge_is_usable(self) -> Self:
        if self.from_track == self.to_track:
            raise ValueError("transition candidate cannot point from a track to itself")
        if not self.allowed_techniques:
            raise ValueError("transition candidate needs at least one allowed technique")
        return self

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        d = dict(row)
        raw_techniques = d.get("allowed_techniques") or "[]"
        techniques = json.loads(raw_techniques)
        if not isinstance(techniques, list):
            raise ValueError("allowed_techniques must be a JSON list")
        return cls(
            id=d["id"],
            project_id=d["project_id"],
            from_track=d["from_track"],
            to_track=d["to_track"],
            from_cue_bar=d["from_cue_bar"],
            to_cue_bar=d["to_cue_bar"],
            scores=TransitionScores.model_validate(json.loads(d["scores_json"] or "{}")),
            allowed_techniques=[TransitionTechnique(t) for t in techniques],
            created_at=d.get("created_at"),
        )


class CandidateGraphBuildResult(_ModelBase):
    """Result of one deterministic Phase 3 graph build."""

    project: Project
    requested_tracks: int
    usable_tracks: int
    skipped_tracks: dict[str, str] = Field(default_factory=dict)
    candidates: list[TransitionCandidate]
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Transition Renders
# ---------------------------------------------------------------------------


class RenderStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RenderTechnique(StrEnum):
    PHRASE_SWAP = "phrase_swap"
    FILTER_BLEND = "filter_blend"
    LONG_CROSSFADE = "long_crossfade"
    ECHO_OUT = "echo_out"


class RenderLabelKind(StrEnum):
    GOOD = "good"
    OFF_BEAT = "off_beat"
    BAD_CUE = "bad_cue"
    BAD_ENERGY = "bad_energy"
    BAD_KEY = "bad_key"
    CLIPPING = "clipping"
    WRONG_TEMPO_MATCH = "wrong_tempo_match"
    TOO_ABRUPT = "too_abrupt"
    TOO_LONG = "too_long"
    BORING = "boring"
    UNUSABLE = "unusable"


class SourceAnchorPolicy(StrEnum):
    KEEP_OUTGOING_TEMPO = "keep_outgoing_tempo"
    KEEP_INCOMING_TEMPO = "keep_incoming_tempo"
    MEET_IN_MIDDLE = "meet_in_middle"


class RenderConfidenceSnapshot(_ModelBase):
    from_tempo_confidence: float | None = None
    to_tempo_confidence: float | None = None
    from_key_confidence: float | None = None
    to_key_confidence: float | None = None
    from_beat_source: str
    to_beat_source: str
    from_key_source: str | None = None
    to_key_source: str | None = None
    from_beat_labels: list[AnalysisLabelKind] = Field(default_factory=list)
    to_beat_labels: list[AnalysisLabelKind] = Field(default_factory=list)


class RenderLoudnessSummary(_ModelBase):
    integrated_lufs: float | None = None
    loudness_range: float | None = None
    true_peak_dbfs: float | None = None
    clipping_detected: bool = False


class RenderRequestConfig(_ModelBase):
    source_anchor_policy: SourceAnchorPolicy = SourceAnchorPolicy.KEEP_OUTGOING_TEMPO
    from_cue_sec: float = Field(ge=0.0)
    to_cue_sec: float = Field(ge=0.0)
    from_bpm: float = Field(gt=0.0)
    to_bpm: float = Field(gt=0.0)
    tempo_match_ratio: float = Field(gt=0.0)
    tempo_match_ratio_source: Literal["candidate", "renderer_recomputed"]
    transition_length_sec: float = Field(gt=0.0)
    source_lead_in_sec: float = Field(ge=0.0)
    target_tail_sec: float = Field(ge=0.0)
    loudness_target_lufs: float
    output_sample_rate: int = Field(gt=0)
    output_channels: int = Field(gt=0)
    confidence_snapshot: RenderConfidenceSnapshot


class RenderActuals(_ModelBase):
    source_lufs: float | None = None
    target_lufs: float | None = None
    ffmpeg_version: str
    source_loudness: RenderLoudnessSummary | None = None
    target_loudness: RenderLoudnessSummary | None = None
    output_loudness: RenderLoudnessSummary | None = None
    source_loudness_origin: Literal["fresh", "cache", "unavailable"] = "unavailable"
    target_loudness_origin: Literal["fresh", "cache", "unavailable"] = "unavailable"


class RenderArtifact(_ModelBase):
    id: int
    project_id: int
    candidate_id: int
    from_track: str
    to_track: str
    technique: RenderTechnique
    status: RenderStatus
    artifact_key: str | None = None
    duration_sec: float | None = Field(default=None, ge=0.0)
    sample_rate: int | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0)
    claim_token: str | None = None
    request_config: RenderRequestConfig
    actuals: RenderActuals | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        d = dict(row)
        return cls(
            id=d["id"],
            project_id=d["project_id"],
            candidate_id=d["candidate_id"],
            from_track=d["from_track"],
            to_track=d["to_track"],
            technique=RenderTechnique(d["technique"]),
            status=RenderStatus(d["status"]),
            artifact_key=d.get("artifact_key"),
            duration_sec=d.get("duration_sec"),
            sample_rate=d.get("sample_rate"),
            channels=d.get("channels"),
            claim_token=d.get("claim_token"),
            request_config=RenderRequestConfig.model_validate(json.loads(d["request_config_json"])),
            actuals=RenderActuals.model_validate(json.loads(d["actuals_json"]))
            if d.get("actuals_json")
            else None,
            warnings=json.loads(d["warnings_json"] or "[]"),
            error=d.get("error"),
            created_at=d["created_at"],
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
        )


class RenderLabel(_ModelBase):
    id: int
    render_id: int
    kind: RenderLabelKind
    notes: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        d = dict(row)
        return cls(
            id=d["id"],
            render_id=d["render_id"],
            kind=RenderLabelKind(d["kind"]),
            notes=d.get("notes"),
            created_at=d.get("created_at"),
        )
