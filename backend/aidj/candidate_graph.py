"""Transition Candidate Graph builder — Phase 3.

This layer is deterministic: it reads canonical ``TrackProfile`` rows and emits
ordered transition edges. It does not listen to music, choose a set order, or
render audio. Human listening labels remain the truth source for whether a beat
grid is actually trustworthy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aidj.store import analysis_labels, candidates, projects, render_labels, track_profiles, tracks
from aidj.store.models import (
    AnalysisLabelKind,
    CandidateGraphBuildResult,
    CandidateVerification,
    Readiness,
    RenderLabelKind,
    RenderTechnique,
    TrackProfile,
    TransitionCandidate,
    TransitionScores,
    TransitionTechnique,
)

PHRASE_BARS = 8
MAX_TEMPO_DELTA_PCT = 12.0
DEFAULT_MAX_CANDIDATES_PER_PAIR = 3
MAX_TARGETS_PER_TRACK = 12


class ProjectNotFoundError(LookupError):
    """Raised when a candidate graph is requested for an unknown project."""


@dataclass(frozen=True)
class CuePoint:
    bar: int
    time_sec: float


def build_candidate_graph(
    project_id: int,
    *,
    track_hashes: list[str] | None = None,
    max_candidates_per_pair: int = DEFAULT_MAX_CANDIDATES_PER_PAIR,
    force: bool = True,
) -> CandidateGraphBuildResult:
    """Build and persist transition candidates for a project.

    If ``track_hashes`` is omitted, every ingested track is considered. Only
    tracks with an existing non-blocked profile containing tempo + beat grid can
    participate. ``force=True`` replaces previous candidates; ``force=False``
    returns existing candidates when present.
    """
    project = projects.get(project_id)
    if project is None:
        raise ProjectNotFoundError(f"project not found: {project_id}")
    if max_candidates_per_pair < 1:
        raise ValueError("max_candidates_per_pair must be >= 1")

    requested = _requested_track_hashes(track_hashes)
    existing = candidates.list_for_project(project_id)
    if existing and not force:
        profiles, skipped = _usable_profiles(requested)
        return CandidateGraphBuildResult(
            project=project,
            requested_tracks=len(requested),
            usable_tracks=len(profiles),
            skipped_tracks=skipped,
            candidates=existing,
            warnings=["returned existing candidates; force=true to rebuild"],
        )

    profiles, skipped = _usable_profiles(requested)
    labels_by_run = _labels_for_profiles(profiles)
    genres_by_hash = _genres_for_profiles(profiles)
    render_feedback = render_labels.rollup_by_technique_and_pair()

    built: list[TransitionCandidate] = []
    for source in profiles:
        for target in _candidate_targets(source, profiles):
            pair_candidates = _build_pair_candidates(
                project_id,
                source,
                target,
                labels_by_run,
                genres_by_hash,
                render_feedback,
                max_candidates=max_candidates_per_pair,
            )
            built.extend(pair_candidates)

    persisted = candidates.replace_for_project(project_id, built)
    warnings = [
        "candidate graph is mechanical only; beat-grid listening labels remain truth",
    ]
    if any(c.scores.verification is not CandidateVerification.VERIFIED for c in persisted):
        warnings.append("one or more candidates use unverified or failure-labeled analyzer output")

    return CandidateGraphBuildResult(
        project=project,
        requested_tracks=len(requested),
        usable_tracks=len(profiles),
        skipped_tracks=skipped,
        candidates=persisted,
        warnings=warnings,
    )


def _requested_track_hashes(track_hashes: list[str] | None) -> list[str]:
    if track_hashes is not None:
        seen: set[str] = set()
        out: list[str] = []
        for h in track_hashes:
            if h not in seen:
                seen.add(h)
                out.append(h)
        return out
    return [t.content_hash for t in tracks.list_all(limit=10_000)]


def _candidate_targets(
    source: TrackProfile,
    profiles: list[TrackProfile],
) -> list[TrackProfile]:
    """Pre-prune each source to its nearest tempo-compatible targets.

    This still scans the usable profile set, but it bounds cue expansion and
    persisted edges before a real-library batch produces a wall of near-dupes.
    """
    assert source.tempo is not None
    ranked: list[tuple[float, str, TrackProfile]] = []
    for target in profiles:
        if source.track_hash == target.track_hash:
            continue
        assert target.tempo is not None
        tempo_delta = _tempo_delta_pct(source.tempo.bpm, target.tempo.bpm)
        if tempo_delta <= MAX_TEMPO_DELTA_PCT:
            ranked.append((tempo_delta, target.track_hash, target))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [target for _, _, target in ranked[:MAX_TARGETS_PER_TRACK]]


def _usable_profiles(track_hashes: list[str]) -> tuple[list[TrackProfile], dict[str, str]]:
    profiles: list[TrackProfile] = []
    skipped: dict[str, str] = {}
    for track_hash in track_hashes:
        if tracks.get(track_hash) is None:
            skipped[track_hash] = "track_missing"
            continue
        profile = track_profiles.get(track_hash)
        if profile is None:
            skipped[track_hash] = "profile_missing"
            continue
        if profile.readiness is Readiness.BLOCKED:
            skipped[track_hash] = "profile_blocked"
            continue
        if profile.tempo is None or profile.beat_grid is None:
            skipped[track_hash] = "missing_tempo_or_beat_grid"
            continue
        if not _bar_cues(profile):
            skipped[track_hash] = "no_phrase_cues"
            continue
        profiles.append(profile)
    return profiles, skipped


def _labels_for_profiles(
    profiles: list[TrackProfile],
) -> dict[int, list[AnalysisLabelKind]]:
    run_ids: list[int] = []
    for profile in profiles:
        if profile.beat_grid and profile.beat_grid.provenance.analysis_run_id is not None:
            run_ids.append(profile.beat_grid.provenance.analysis_run_id)
    labels = analysis_labels.list_for_runs(run_ids)
    return {run_id: [label.kind for label in items] for run_id, items in labels.items()}


def _genres_for_profiles(profiles: list[TrackProfile]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for profile in profiles:
        track = tracks.get(profile.track_hash)
        out[profile.track_hash] = track.genre if track is not None else None
    return out


def _build_pair_candidates(
    project_id: int,
    source: TrackProfile,
    target: TrackProfile,
    labels_by_run: dict[int, list[AnalysisLabelKind]],
    genres_by_hash: dict[str, str | None],
    render_feedback: dict[tuple[RenderTechnique, str], dict[RenderLabelKind, int]],
    *,
    max_candidates: int,
) -> list[TransitionCandidate]:
    assert source.tempo is not None and source.beat_grid is not None
    assert target.tempo is not None and target.beat_grid is not None

    tempo_delta = _tempo_delta_pct(source.tempo.bpm, target.tempo.bpm)
    techniques = _allowed_techniques(tempo_delta)
    family = render_labels.pair_family_key(
        from_beat_source=source.beat_grid.provenance.source,
        to_beat_source=target.beat_grid.provenance.source,
        from_genre=genres_by_hash.get(source.track_hash),
        to_genre=genres_by_hash.get(target.track_hash),
    )
    techniques = _filter_techniques_by_render_feedback(techniques, family, render_feedback)
    if not techniques:
        return []

    source_cues = _outgoing_cues(source)
    target_cues = _incoming_cues(target)
    verification = _verification_status(source, target, labels_by_run)
    key_compatible = _key_compatible(source, target)
    pair: list[TransitionCandidate] = []
    for from_cue in source_cues:
        for to_cue in target_cues:
            scores = _score_candidate(
                source,
                target,
                from_cue,
                to_cue,
                tempo_delta,
                key_compatible,
                verification,
            )
            pair.append(
                TransitionCandidate(
                    project_id=project_id,
                    from_track=source.track_hash,
                    to_track=target.track_hash,
                    from_cue_bar=from_cue.bar,
                    to_cue_bar=to_cue.bar,
                    scores=scores,
                    allowed_techniques=techniques,
                )
            )
    pair.sort(key=lambda c: c.scores.score, reverse=True)
    return pair[:max_candidates]


def _score_candidate(
    source: TrackProfile,
    target: TrackProfile,
    from_cue: CuePoint,
    to_cue: CuePoint,
    tempo_delta: float,
    key_compatible: bool | None,
    verification: CandidateVerification,
) -> TransitionScores:
    assert source.tempo is not None and source.beat_grid is not None
    assert target.tempo is not None and target.beat_grid is not None

    reasons = ["phrase_aligned", f"tempo_delta={tempo_delta:.2f}%"]
    score = 1.0 - (tempo_delta / MAX_TEMPO_DELTA_PCT) * 0.55
    if key_compatible is True:
        score += 0.08
        reasons.append("harmonic_match")
    elif key_compatible is False:
        score -= 0.12
        reasons.append("harmonic_mismatch")
    else:
        reasons.append("key_unknown")

    if verification is CandidateVerification.VERIFIED:
        score += 0.08
        reasons.append("verified_sources")
    elif verification is CandidateVerification.HAS_FAILURE_LABEL:
        score -= 0.2
        reasons.append("failure_labeled_source")
    elif verification is CandidateVerification.PARTIAL:
        reasons.append("partially_verified_sources")
    else:
        reasons.append("unverified_sources")

    return TransitionScores(
        score=round(min(1.0, max(0.0, score)), 6),
        tempo_delta_pct=round(tempo_delta, 6),
        tempo_match_ratio=round(tempo_match_ratio(source.tempo.bpm, target.tempo.bpm), 6),
        from_bpm=source.tempo.bpm,
        to_bpm=target.tempo.bpm,
        from_cue_sec=from_cue.time_sec,
        to_cue_sec=to_cue.time_sec,
        phrase_bars=PHRASE_BARS,
        key_compatible=key_compatible,
        verification=verification,
        from_source=source.beat_grid.provenance.source,
        to_source=target.beat_grid.provenance.source,
        reasons=reasons,
    )


def _allowed_techniques(tempo_delta_pct: float) -> list[TransitionTechnique]:
    if tempo_delta_pct <= 3.0:
        return [
            TransitionTechnique.PHRASE_SWAP,
            TransitionTechnique.FILTER_BLEND,
            TransitionTechnique.LONG_CROSSFADE,
        ]
    if tempo_delta_pct <= 6.0:
        return [TransitionTechnique.FILTER_BLEND, TransitionTechnique.LONG_CROSSFADE]
    if tempo_delta_pct <= 10.0:
        return [TransitionTechnique.LONG_CROSSFADE]
    if tempo_delta_pct <= MAX_TEMPO_DELTA_PCT:
        return [TransitionTechnique.ECHO_OUT]
    return []


def _filter_techniques_by_render_feedback(
    techniques: list[TransitionTechnique],
    family: str,
    render_feedback: dict[tuple[RenderTechnique, str], dict[RenderLabelKind, int]],
) -> list[TransitionTechnique]:
    return [
        technique
        for technique in techniques
        if not _technique_hard_failed(
            render_feedback.get((RenderTechnique(technique.value), family), {})
        )
    ]


def _technique_hard_failed(counts: dict[RenderLabelKind, int]) -> bool:
    bad = sum(counts.get(kind, 0) for kind in render_labels.BAD_RENDER_LABELS)
    total = sum(counts.values())
    return bad >= 3 and total > 0 and (bad / total) >= 0.6


def _tempo_delta_pct(from_bpm: float, to_bpm: float) -> float:
    if not math.isfinite(from_bpm) or not math.isfinite(to_bpm) or from_bpm <= 0 or to_bpm <= 0:
        return math.inf
    candidates = (to_bpm * 0.5, to_bpm, to_bpm * 2.0)
    best = min(abs(from_bpm - candidate) / from_bpm for candidate in candidates)
    return best * 100.0


def tempo_match_ratio(from_bpm: float, to_bpm: float) -> float:
    """Playback ratio that stretches the incoming track toward the outgoing BPM."""
    if not math.isfinite(from_bpm) or not math.isfinite(to_bpm) or from_bpm <= 0 or to_bpm <= 0:
        raise ValueError("from_bpm and to_bpm must be finite positive numbers")
    return from_bpm / to_bpm


def _bar_cues(profile: TrackProfile) -> list[CuePoint]:
    assert profile.beat_grid is not None
    beats = sorted(profile.beat_grid.beats, key=lambda b: b.time_sec)
    downbeats = [b.time_sec for b in beats if b.is_downbeat]
    if len(downbeats) >= 2:
        return [CuePoint(bar=i, time_sec=t) for i, t in enumerate(downbeats)]
    # Fallback assumes common-time material: every fourth beat is treated as a
    # bar boundary. 6/8 or other meters need explicit downbeat detection.
    fallback = beats[::4]
    return [CuePoint(bar=i, time_sec=b.time_sec) for i, b in enumerate(fallback)]


def _incoming_cues(profile: TrackProfile) -> list[CuePoint]:
    cues = _phrase_boundary_cues(profile)
    return cues[:2] if cues else []


def _outgoing_cues(profile: TrackProfile) -> list[CuePoint]:
    cues = _phrase_boundary_cues(profile)
    if not cues:
        return []
    return list(reversed(cues[-2:]))


def _phrase_boundary_cues(profile: TrackProfile) -> list[CuePoint]:
    cues = _bar_cues(profile)
    if not cues:
        return []
    phrase_cues = [cue for cue in cues if cue.bar % PHRASE_BARS == 0]
    return phrase_cues or [cues[0]]


def _verification_status(
    source: TrackProfile,
    target: TrackProfile,
    labels_by_run: dict[int, list[AnalysisLabelKind]],
) -> CandidateVerification:
    source_labels = _profile_labels(source, labels_by_run)
    target_labels = _profile_labels(target, labels_by_run)
    if _has_failure_label(source_labels) or _has_failure_label(target_labels):
        return CandidateVerification.HAS_FAILURE_LABEL
    source_ok = AnalysisLabelKind.CORRECT in source_labels
    target_ok = AnalysisLabelKind.CORRECT in target_labels
    if source_ok and target_ok:
        return CandidateVerification.VERIFIED
    if source_ok or target_ok or source_labels or target_labels:
        return CandidateVerification.PARTIAL
    return CandidateVerification.UNVERIFIED


def _profile_labels(
    profile: TrackProfile,
    labels_by_run: dict[int, list[AnalysisLabelKind]],
) -> list[AnalysisLabelKind]:
    if profile.beat_grid is None:
        return []
    run_id = profile.beat_grid.provenance.analysis_run_id
    if run_id is None:
        return []
    return labels_by_run.get(run_id, [])


def _has_failure_label(labels: list[AnalysisLabelKind]) -> bool:
    return any(label is not AnalysisLabelKind.CORRECT for label in labels)


def _key_compatible(source: TrackProfile, target: TrackProfile) -> bool | None:
    if source.key is None or target.key is None:
        return None
    left = _parse_camelot(source.key.camelot)
    right = _parse_camelot(target.key.camelot)
    if left is None or right is None:
        return None
    left_num, left_mode = left
    right_num, right_mode = right
    if left_num == right_num:
        return True
    return (
        left_mode == right_mode
        and min(
            (left_num - right_num) % 12,
            (right_num - left_num) % 12,
        )
        == 1
    )


def _parse_camelot(value: str | None) -> tuple[int, str] | None:
    if not value or len(value) < 2:
        return None
    mode = value[-1].upper()
    number = value[:-1]
    if mode not in {"A", "B"} or not number.isdigit():
        return None
    parsed = int(number)
    if parsed < 1 or parsed > 12:
        return None
    return parsed, mode
