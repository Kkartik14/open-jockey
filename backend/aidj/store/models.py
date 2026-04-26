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
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

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
