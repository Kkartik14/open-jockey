"""Profile builder — Phase 2, step 3 (Canonical Track Intelligence).

Deterministic funnel: read a track's completed ``analysis_runs``, pick the best
available output per field by a hardcoded source priority, normalise it, and
write one canonical :class:`TrackProfile`. Downstream layers (Candidate Graph,
Renderer, Planner) consume the profile; they never look at raw analyzer JSON.

Discipline (see ``private/plan.md`` — the Phase 2 truth test):

    This builder is *plumbing*, not validation. A green builder over synthetic
    or unverified analyzer output proves the selection logic is correct; it
    proves NOTHING about whether the analyzer's beat grid is musically right.
    During the truth test, the human's listening labels are the source of
    truth, and this profile is only derived evidence. That is also why the
    builder never emits ``Readiness.READY`` yet — a confident profile would be
    a lie until the analyzer layer has been heard.

Selection is by fixed priority for now (step 3); step 8 replaces it with
label-driven selection that reads the bake-off rollups. The builder tolerates
missing and malformed data: one bad analyzer output is skipped, never fatal,
so a partially-analyzed track still yields a usable (partial) profile.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from pydantic import ValidationError

from aidj.store import analysis_runs, track_profiles, tracks
from aidj.store._timestamps import utc_now_iso
from aidj.store.models import (
    CURRENT_PROFILE_VERSION,
    AnalysisRun,
    AnalysisStatus,
    Beat,
    BeatGridBlock,
    CompletenessFields,
    FieldProvenance,
    KeyAnalysis,
    KeyBlock,
    Readiness,
    Section,
    SectionLabel,
    SectionsBlock,
    TempoBlock,
    TempoEstimate,
    TrackProfile,
)

log = logging.getLogger(__name__)


class TrackNotFoundError(LookupError):
    """Raised by :func:`build_profile` when the track hash is unknown.

    The API layer maps this to 404 — without an explicit check, the FK on
    ``track_profiles.track_hash`` would surface as a raw SQLite
    ``IntegrityError`` which is opaque to anyone not reading SQL.
    """


# Highest-trust beat-grid source first. ``echo`` is *excluded* from production
# selection — it is the synthetic smoke-test plugin, and allowing it as a
# canonical source would mean a profile could compress fake input into the
# appearance of truth. ``echo`` runs may still sit in ``analysis_runs``; the
# builder simply won't pick them. Step 8 replaces this constant with
# label-driven selection.
BEAT_GRID_SOURCE_PRIORITY: tuple[str, ...] = ("allin1_remote", "allin1", "librosa")

# Key detection has a single source today.
KEY_SOURCE_PRIORITY: tuple[str, ...] = ("essentia",)

# completeness_score weights — one per canonical field. Sum to 1.0 so a fully
# populated profile scores 1.0. Beat grid dominates because nothing downstream
# can plan a transition without it.
_COMPLETENESS_WEIGHTS: dict[str, float] = {
    "has_beat_grid": 0.4,
    "has_key": 0.2,
    "has_energy": 0.2,
    "has_sections": 0.1,
    "has_vocals": 0.1,
}


def build_profile(track_hash: str, *, force: bool = False) -> TrackProfile:
    """Build (or rebuild) and persist the canonical profile for ``track_hash``.

    Idempotent: when ``force`` is False and a current, non-stale profile already
    exists it is returned untouched. Otherwise the profile is rebuilt from the
    track's newest completed run per analyzer and upserted (replacing in place).

    A track with no usable beat grid still gets a persisted ``blocked`` profile
    so library coverage can distinguish "tried, unusable" from "never built".
    Raises :class:`TrackNotFoundError` if the track row doesn't exist — guarded
    here rather than only at the API so CLI/test callers get the same error.
    """
    if tracks.get(track_hash) is None:
        raise TrackNotFoundError(f"track {track_hash[:12]}… not found")

    if not force:
        existing = track_profiles.get(track_hash)
        if existing is not None and not track_profiles.is_stale(track_hash):
            log.debug("profile for %s is current; skipping rebuild", track_hash[:12])
            return existing

    by_analyzer = _newest_completed_by_analyzer(track_hash)

    tempo_block: TempoBlock | None = None
    beat_grid_block: BeatGridBlock | None = None
    sections_block: SectionsBlock | None = None
    selection = _select_beat_grid(by_analyzer)
    if selection is not None:
        tempo_block, beat_grid_block, sections_block = selection
    key_block = _select_key(by_analyzer)

    fields = CompletenessFields(
        has_beat_grid=tempo_block is not None and beat_grid_block is not None,
        has_key=key_block is not None,
        has_sections=sections_block is not None,
        has_energy=False,  # filled by the energy analyser (step 4)
        has_vocals=False,  # filled by stem/vocal intelligence (step 5)
    )
    profile = TrackProfile(
        profile_version=CURRENT_PROFILE_VERSION,
        track_hash=track_hash,
        built_at=utc_now_iso(),
        readiness=_readiness(fields),
        completeness_score=_completeness_score(fields),
        fields=fields,
        tempo=tempo_block,
        beat_grid=beat_grid_block,
        key=key_block,
        sections=sections_block,
        energy=None,
        vocals=None,
    )
    return track_profiles.upsert(profile)


# ---------------------------------------------------------------------------
# Run selection
# ---------------------------------------------------------------------------


def _newest_completed_by_analyzer(track_hash: str) -> dict[str, AnalysisRun]:
    """Most-recent COMPLETED run per analyzer name.

    ``list_for_track`` is newest-first, so the first row seen for an analyzer is
    its latest run; later (older) rows and non-completed runs are ignored.
    """
    by_analyzer: dict[str, AnalysisRun] = {}
    for run in analysis_runs.list_for_track(track_hash):
        if run.status is not AnalysisStatus.COMPLETED:
            continue
        by_analyzer.setdefault(run.analyzer_name, run)
    return by_analyzer


def _select_beat_grid(
    by_analyzer: dict[str, AnalysisRun],
) -> tuple[TempoBlock, BeatGridBlock, SectionsBlock | None] | None:
    """First priority source with a usable beat grid wins. Tempo and beat grid
    share one provenance so a profile can never mix BPM and beats across
    analyzers."""
    for name in BEAT_GRID_SOURCE_PRIORITY:
        run = by_analyzer.get(name)
        if run is None:
            continue
        blocks = _parse_beat_grid_blocks(run)
        if blocks is not None:
            return blocks
    return None


def _select_key(by_analyzer: dict[str, AnalysisRun]) -> KeyBlock | None:
    for name in KEY_SOURCE_PRIORITY:
        run = by_analyzer.get(name)
        if run is None:
            continue
        block = _parse_key_block(run)
        if block is not None:
            return block
    return None


# ---------------------------------------------------------------------------
# Parsing / normalisation (defensive — malformed output is skipped, not fatal)
# ---------------------------------------------------------------------------


def _parse_beat_grid_blocks(
    run: AnalysisRun,
) -> tuple[TempoBlock, BeatGridBlock, SectionsBlock | None] | None:
    """Normalise a beat-grid run into (tempo, beat_grid, sections|None).

    Returns None when tempo/beats/duration are unusable so the builder falls
    through to the next-priority analyzer. Sections are best-effort: a malformed
    section list yields ``sections=None`` but does NOT reject the beat grid.
    """
    output = run.output
    if not isinstance(output, dict):
        return None

    tempo = _parse_tempo(output.get("tempo"))
    if tempo is None:
        return None

    beats = _parse_beats(output.get("beats"))
    # Two beats *at the same time* don't define a grid any more than one does —
    # require at least two distinct moments. Sorted ascending by _parse_beats,
    # so distinctness ⇔ beats[-1].time_sec > beats[0].time_sec.
    if len({b.time_sec for b in beats}) < 2:
        return None
    last_beat_time = beats[-1].time_sec

    duration = _coerce_duration(output.get("duration_sec"), last_beat_time)
    # Duration must be strictly positive AND at least as long as the last beat
    # — a duration shorter than the last beat is a contradiction (the grid
    # claims a beat past the end of the track), so we reject the source rather
    # than try to repair it.
    if duration is None or duration <= 0 or duration < last_beat_time:
        return None

    prov = _provenance(run)
    downbeat_count = sum(1 for b in beats if b.is_downbeat)
    try:
        tempo_block = TempoBlock(bpm=tempo.bpm, confidence=tempo.confidence, provenance=prov)
        beat_grid_block = BeatGridBlock(
            beats=beats,
            downbeat_count=downbeat_count,
            duration_sec=duration,
            provenance=prov,
        )
    except ValidationError:
        return None

    sections_block = _parse_sections(output.get("sections"), prov)
    return tempo_block, beat_grid_block, sections_block


def _parse_tempo(raw: Any) -> TempoEstimate | None:
    if not isinstance(raw, dict):
        return None
    try:
        tempo = TempoEstimate.model_validate(raw)
    except ValidationError:
        return None
    if not math.isfinite(tempo.bpm) or tempo.bpm <= 0:
        return None
    return tempo


def _parse_beats(raw: Any) -> list[Beat]:
    """Parse, drop non-finite/negative-time beats, and sort. Normalisation, not
    rejection — the builder repairs ordering rather than discarding the source."""
    if not isinstance(raw, list):
        return []
    out: list[Beat] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            beat = Beat.model_validate(item)
        except ValidationError:
            continue
        if not math.isfinite(beat.time_sec) or beat.time_sec < 0:
            continue
        out.append(beat)
    out.sort(key=lambda b: b.time_sec)
    return out


def _coerce_duration(raw: Any, last_beat_time: float) -> float | None:
    """Return a candidate duration in seconds, or None if the source is unusable.

    Three cases:

    - **reported as a positive finite number** → use it. Caller still enforces
      ``duration >= last_beat_time``.
    - **reported as a number ≤ 0 / NaN / inf** → return None (do NOT fall back).
      An explicit-but-bad value is a sign the analyzer is wrong; silently
      substituting the last beat would hide that failure.
    - **missing (None) or non-numeric type** → derive from the last beat when
      possible. A missing field is different from a lying one.
    """
    if raw is None:
        return float(last_beat_time) if last_beat_time > 0 else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if math.isfinite(raw) and raw > 0:
            return float(raw)
        return None
    # Non-numeric, non-None (string, dict, list…) — treat as missing.
    return float(last_beat_time) if last_beat_time > 0 else None


def _parse_sections(raw: Any, prov: FieldProvenance) -> SectionsBlock | None:
    if not isinstance(raw, list):
        return None
    items: list[Section] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        section = _coerce_section(item)
        if section is not None:
            items.append(section)
    if not items:
        return None
    return SectionsBlock(items=items, provenance=prov)


def _coerce_section(item: dict[str, Any]) -> Section | None:
    """One section, best-effort. Unknown labels fall back to ``unknown`` rather
    than dropping the boundary (the boundary is what the planner needs); a
    zero/negative-length or out-of-range window is dropped."""
    data = dict(item)
    label = data.get("label")
    if not _is_known_section_label(label):
        data["label"] = SectionLabel.UNKNOWN.value
    try:
        section = Section.model_validate(data)
    except ValidationError:
        return None
    if section.start_sec < 0 or section.end_sec <= section.start_sec:
        return None
    return section


def _is_known_section_label(label: Any) -> bool:
    if not isinstance(label, str):
        return False
    try:
        SectionLabel(label)
    except ValueError:
        return False
    return True


def _parse_key_block(run: AnalysisRun) -> KeyBlock | None:
    output = run.output
    if not isinstance(output, dict):
        return None
    try:
        key = KeyAnalysis.model_validate(output)
    except ValidationError:
        return None
    if not key.key.strip() or not key.scale.strip():
        return None
    return KeyBlock(
        key=key.key,
        scale=key.scale,
        camelot=key.camelot,
        confidence=key.confidence,
        provenance=_provenance(run),
    )


def _provenance(run: AnalysisRun) -> FieldProvenance:
    """``"<analyzer>@<version>"`` — matches the strings PluginInfo exposes —
    plus the run id so the staleness check can trace the source."""
    return FieldProvenance(
        source=f"{run.analyzer_name}@{run.analyzer_version}",
        analysis_run_id=run.id,
    )


# ---------------------------------------------------------------------------
# Derived summary fields
# ---------------------------------------------------------------------------


def _readiness(fields: CompletenessFields) -> Readiness:
    """No ``ready`` yet — see module docstring. A track is ``partial`` once it
    has a beat grid (enough for non-stem transitions), ``blocked`` without one
    (the Candidate Graph can't place a single cue point)."""
    if not fields.has_beat_grid:
        return Readiness.BLOCKED
    return Readiness.PARTIAL


def _completeness_score(fields: CompletenessFields) -> float:
    score = sum(
        weight for name, weight in _COMPLETENESS_WEIGHTS.items() if getattr(fields, name)
    )
    return round(score, 6)  # tame float drift; keeps the value within [0, 1]
