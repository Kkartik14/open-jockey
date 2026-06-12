"""Transition renderer.

Turns a persisted ``TransitionCandidate`` into a stored audio artifact. This
module owns timing, warnings, ffmpeg execution, probing, non-silence checks,
and file lifecycle. Store modules only persist state.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from aidj.candidate_graph import tempo_match_ratio as recompute_tempo_match_ratio
from aidj.config import settings
from aidj.store import (
    analysis_labels,
    candidates,
    projects,
    render_artifacts,
    track_profiles,
    tracks,
)
from aidj.store.models import (
    AnalysisLabelKind,
    CandidateVerification,
    RenderActuals,
    RenderArtifact,
    RenderConfidenceSnapshot,
    RenderLoudnessSummary,
    RenderRequestConfig,
    RenderStatus,
    RenderTechnique,
    SourceAnchorPolicy,
    Track,
    TrackProfile,
    TransitionCandidate,
)

log = logging.getLogger(__name__)

MAX_EXPECTED_RENDER_TIME_SEC = 300.0
STALE_RUNNING_AFTER_SEC = 2.0 * MAX_EXPECTED_RENDER_TIME_SEC
LOUDNESS_TARGET_LUFS = -14.0
OUTPUT_SAMPLE_RATE = 44_100
OUTPUT_CHANNELS = 2
SILENCE_THRESHOLD_DBFS = -60.0
HIGH_STRETCH_WARNING_PCT = 6.0

_PREFERRED_TRANSITION_SEC = 8.0
_MIN_TRANSITION_SEC = 2.0
_PREFERRED_SOURCE_LEAD_IN_SEC = 12.0
_PREFERRED_TARGET_TAIL_SEC = 24.0

_PHRASE_TRANSITION_SEC = 0.75
_PHRASE_MIN_TRANSITION_SEC = 0.25
_PHRASE_SOURCE_LEAD_IN_SEC = 4.0
_PHRASE_TARGET_TAIL_SEC = 12.0

_ECHO_TRANSITION_SEC = 4.0
_ECHO_SOURCE_LEAD_IN_SEC = 2.0
_ECHO_TARGET_TAIL_SEC = 18.0

SUPPORTED_TECHNIQUES: frozenset[RenderTechnique] = frozenset(RenderTechnique)

_render_semaphore = threading.BoundedSemaphore(value=1)
_process_lock = threading.Lock()
_processes: dict[int, subprocess.Popen[bytes]] = {}

_LOUDNESS_METHOD_VERSION = "ffmpeg-loudnorm-v1"
_loudness_cache: dict[tuple[str, float, int, str], RenderLoudnessSummary | None] = {}


class RenderError(RuntimeError):
    """Base class for renderer failures mapped by the API layer."""


class RenderNotFoundError(RenderError):
    """Requested project/candidate/render does not exist."""


class RenderValidationError(RenderError):
    """Render input is invalid or unsupported."""


class RenderConflictError(RenderError):
    """Render cannot start because another render owns the slot."""

    def __init__(self, message: str, *, active_render_id: int | None = None) -> None:
        super().__init__(message)
        self.active_render_id = active_render_id


@dataclass(frozen=True)
class AudioProbe:
    duration_sec: float
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class RenderTiming:
    source_start_sec: float
    source_duration_sec: float
    target_start_sec: float
    target_input_duration_sec: float
    target_output_duration_sec: float
    transition_length_sec: float
    source_lead_in_sec: float
    target_tail_sec: float


@dataclass(frozen=True)
class PreparedRender:
    candidate: TransitionCandidate
    technique: RenderTechnique
    from_track: Track
    to_track: Track
    from_path: Path
    to_path: Path
    request_config: RenderRequestConfig
    timing: RenderTiming
    warnings: list[str]


@dataclass(frozen=True)
class LoudnessMeasurement:
    summary: RenderLoudnessSummary | None
    origin: str


def render_candidate(
    project_id: int,
    candidate_id: int,
    *,
    technique: RenderTechnique | None = None,
    force: bool = False,
) -> RenderArtifact:
    """Render one candidate synchronously and persist the artifact."""
    recover_stale_running()
    prepared = prepare_render(project_id, candidate_id, technique=technique)

    if not force:
        cached = render_artifacts.latest_completed(candidate_id, prepared.technique)
        if cached is not None:
            return cached

    active = render_artifacts.find_running(candidate_id, prepared.technique)
    if active is not None:
        raise RenderConflictError(
            f"render already running for candidate {candidate_id}/{prepared.technique.value}",
            active_render_id=active.id,
        )

    if not _render_semaphore.acquire(blocking=False):
        raise RenderConflictError("another render is already running")

    render: RenderArtifact | None = None
    try:
        try:
            render = render_artifacts.create_running(
                project_id=project_id,
                candidate_id=candidate_id,
                from_track=prepared.candidate.from_track,
                to_track=prepared.candidate.to_track,
                technique=prepared.technique,
                request_config=prepared.request_config,
                warnings=prepared.warnings,
            )
        except render_artifacts.RunningRenderExists as exc:
            raise RenderConflictError(
                f"render already running for candidate {candidate_id}/{prepared.technique.value}",
                active_render_id=exc.active.id,
            ) from exc

        output_path = artifact_path(render)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return _execute_render(prepared, render, output_path)
    finally:
        _render_semaphore.release()


def prepare_render(
    project_id: int,
    candidate_id: int,
    *,
    technique: RenderTechnique | None = None,
) -> PreparedRender:
    project = projects.get(project_id)
    if project is None:
        raise RenderNotFoundError(f"project not found: {project_id}")
    candidate = candidates.get_for_project(project_id, candidate_id)
    if candidate is None:
        raise RenderNotFoundError(f"candidate not found in project {project_id}: {candidate_id}")
    selected = _select_technique(candidate, technique)

    from_track = tracks.get(candidate.from_track)
    to_track = tracks.get(candidate.to_track)
    if from_track is None or to_track is None:
        raise RenderNotFoundError("candidate endpoint track no longer exists")

    from_path = Path(from_track.source_path)
    to_path = Path(to_track.source_path)
    if not from_path.is_file():
        raise RenderValidationError(f"source file missing: {from_track.source_path}")
    if not to_path.is_file():
        raise RenderValidationError(f"target file missing: {to_track.source_path}")

    from_probe = probe_audio(from_path)
    to_probe = probe_audio(to_path)
    warnings = _base_warnings(candidate)
    snapshot = _confidence_snapshot(candidate, warnings)

    ratio_source = "candidate"
    ratio = candidate.scores.tempo_match_ratio
    if ratio is None:
        ratio = recompute_tempo_match_ratio(candidate.scores.from_bpm, candidate.scores.to_bpm)
        ratio_source = "renderer_recomputed"
    stretch_pct = abs(ratio - 1.0) * 100.0
    if stretch_pct > HIGH_STRETCH_WARNING_PCT:
        warnings.append(f"tempo stretch is high: {stretch_pct:.1f}%")

    timing = _derive_timing(
        technique=selected,
        from_cue_sec=candidate.scores.from_cue_sec,
        to_cue_sec=candidate.scores.to_cue_sec,
        from_duration_sec=from_probe.duration_sec,
        to_duration_sec=to_probe.duration_sec,
        tempo_match_ratio=ratio,
        warnings=warnings,
    )
    request_config = RenderRequestConfig(
        source_anchor_policy=SourceAnchorPolicy.KEEP_OUTGOING_TEMPO,
        from_cue_sec=candidate.scores.from_cue_sec,
        to_cue_sec=candidate.scores.to_cue_sec,
        from_bpm=candidate.scores.from_bpm,
        to_bpm=candidate.scores.to_bpm,
        tempo_match_ratio=ratio,
        tempo_match_ratio_source=ratio_source,
        transition_length_sec=timing.transition_length_sec,
        source_lead_in_sec=timing.source_lead_in_sec,
        target_tail_sec=timing.target_tail_sec,
        loudness_target_lufs=LOUDNESS_TARGET_LUFS,
        output_sample_rate=OUTPUT_SAMPLE_RATE,
        output_channels=OUTPUT_CHANNELS,
        confidence_snapshot=snapshot,
    )
    return PreparedRender(
        candidate=candidate,
        technique=selected,
        from_track=from_track,
        to_track=to_track,
        from_path=from_path,
        to_path=to_path,
        request_config=request_config,
        timing=timing,
        warnings=warnings,
    )


def cancel_render(render_id: int) -> RenderArtifact:
    render = render_artifacts.get(render_id)
    if render is None:
        raise RenderNotFoundError(f"render not found: {render_id}")
    if render.status not in {RenderStatus.QUEUED, RenderStatus.RUNNING}:
        raise RenderConflictError(f"cannot cancel render in status {render.status.value}")
    with _process_lock:
        process = _processes.get(render_id)
    if render.status is RenderStatus.RUNNING and process is None:
        raise RenderConflictError(
            "running render is not owned by this backend worker; run single-worker uvicorn"
        )
    if process is not None and process.poll() is None:
        process.terminate()
    cancelled = render_artifacts.cancel(render_id, error="cancelled by user")
    if cancelled is None:  # pragma: no cover - render was fetched just above
        raise RenderNotFoundError(f"render not found: {render_id}")
    return cancelled


def recover_stale_running() -> int:
    return render_artifacts.recover_stale_running(stale_after_sec=STALE_RUNNING_AFTER_SEC)


def artifact_path(render: RenderArtifact) -> Path:
    if not render.artifact_key:
        raise RenderValidationError(f"render {render.id} has no artifact key")
    root = settings().store_root.resolve()
    path = (root / render.artifact_key).resolve()
    if not path.is_relative_to(root):
        raise RenderValidationError(f"render artifact escaped store root: {render.artifact_key}")
    return path


def cleanup_orphan_render_files() -> int:
    """Delete render files under .aidj/projects that no row references."""
    root = settings().projects_root
    if not root.exists():
        return 0
    live = {render.artifact_key for render in render_artifacts.list_all() if render.artifact_key}
    removed = 0
    for path in root.glob("*/renders/render-*.m4a"):
        key = str(path.relative_to(settings().store_root))
        if key in live:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            log.warning("failed to delete orphan render artifact: %s", path)
    return removed


def probe_audio(path: Path) -> AudioProbe:
    result = _run_json_probe(path)
    streams = [s for s in result.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise RenderValidationError(f"no audio stream in {path}")
    stream = streams[0]
    duration_raw = result.get("format", {}).get("duration") or stream.get("duration")
    try:
        duration = float(duration_raw)
        sample_rate = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
    except (TypeError, ValueError) as exc:
        raise RenderValidationError(f"ffprobe returned invalid metadata for {path}") from exc
    if duration <= 0 or sample_rate <= 0 or channels <= 0:
        raise RenderValidationError(f"ffprobe returned unusable metadata for {path}")
    return AudioProbe(duration_sec=duration, sample_rate=sample_rate, channels=channels)


def _execute_render(
    prepared: PreparedRender,
    render: RenderArtifact,
    output_path: Path,
) -> RenderArtifact:
    assert render.claim_token is not None
    ffmpeg_version = _ffmpeg_version()
    source_loudness = _measure_track_loudness(prepared.from_track, prepared.from_path)
    target_loudness = _measure_track_loudness(prepared.to_track, prepared.to_path)
    warnings = list(prepared.warnings)
    if source_loudness.summary is None:
        warnings.append("source LUFS measurement failed; using ffmpeg loudnorm fallback")
    if target_loudness.summary is None:
        warnings.append("target LUFS measurement failed; using ffmpeg loudnorm fallback")

    command = build_ffmpeg_command(prepared, output_path)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with _process_lock:
        _processes[render.id] = process
    try:
        _, stderr = process.communicate(timeout=MAX_EXPECTED_RENDER_TIME_SEC)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        current = render_artifacts.fail(
            render_id=render.id,
            claim_token=render.claim_token,
            error=f"ffmpeg timed out after {MAX_EXPECTED_RENDER_TIME_SEC:.0f}s",
            warnings=warnings,
        )
        return current
    finally:
        with _process_lock:
            _processes.pop(render.id, None)

    current = render_artifacts.get(render.id)
    if current is not None and current.status is RenderStatus.CANCELLED:
        return current

    if process.returncode != 0:
        message = stderr.decode(errors="replace").strip()[:500]
        return render_artifacts.fail(
            render_id=render.id,
            claim_token=render.claim_token,
            error=f"ffmpeg failed: {message}",
            warnings=warnings,
        )

    try:
        probe = probe_audio(output_path)
        rms_dbfs = decoded_middle_rms_dbfs(output_path, duration_sec=probe.duration_sec)
        if rms_dbfs <= SILENCE_THRESHOLD_DBFS:
            raise RenderValidationError(f"render output is silent: middle RMS {rms_dbfs:.1f} dBFS")
        output_loudness = _measure_loudness(output_path).summary
    except Exception as exc:
        return render_artifacts.fail(
            render_id=render.id,
            claim_token=render.claim_token,
            error=str(exc),
            warnings=warnings,
        )

    actuals = RenderActuals(
        source_lufs=source_loudness.summary.integrated_lufs
        if source_loudness.summary is not None
        else None,
        target_lufs=target_loudness.summary.integrated_lufs
        if target_loudness.summary is not None
        else None,
        ffmpeg_version=ffmpeg_version,
        source_loudness=source_loudness.summary,
        target_loudness=target_loudness.summary,
        output_loudness=output_loudness,
        source_loudness_origin=source_loudness.origin,
        target_loudness_origin=target_loudness.origin,
    )
    return render_artifacts.complete(
        render_id=render.id,
        claim_token=render.claim_token,
        duration_sec=probe.duration_sec,
        sample_rate=probe.sample_rate,
        channels=probe.channels,
        actuals=actuals,
        warnings=warnings,
    )


def build_ffmpeg_command(prepared: PreparedRender, output_path: Path) -> list[str]:
    filter_complex = _filter_complex(prepared)
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(prepared.from_path),
        "-i",
        str(prepared.to_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-ar",
        str(OUTPUT_SAMPLE_RATE),
        "-ac",
        str(OUTPUT_CHANNELS),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_path),
    ]


def decoded_middle_rms_dbfs(path: Path, *, duration_sec: float) -> float:
    start = max(0.0, duration_sec * 0.25)
    window = max(0.1, duration_sec * 0.5)
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                _sec(start),
                "-t",
                _sec(window),
                "-i",
                str(path),
                "-ac",
                "1",
                "-ar",
                "8000",
                "-f",
                "s16le",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=60.0,
        )
    except subprocess.CalledProcessError as exc:
        raise RenderValidationError(
            f"ffmpeg output decode failed: {exc.stderr.decode(errors='replace')[:300]}"
        ) from exc
    pcm = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if pcm.size == 0:
        return -math.inf
    rms = float(np.sqrt(np.mean(np.square(pcm))))
    if rms <= 0:
        return -math.inf
    return 20.0 * math.log10(rms)


def _select_technique(
    candidate: TransitionCandidate,
    requested: RenderTechnique | None,
) -> RenderTechnique:
    allowed = [RenderTechnique(tech.value) for tech in candidate.allowed_techniques]
    supported_allowed = [tech for tech in allowed if tech in SUPPORTED_TECHNIQUES]
    if requested is not None:
        if requested not in allowed:
            raise RenderValidationError(
                f"technique {requested.value!r} is not allowed for candidate {candidate.id}"
            )
        if requested not in SUPPORTED_TECHNIQUES:
            raise RenderValidationError(f"technique {requested.value!r} is not implemented")
        return requested
    if not supported_allowed:
        raise RenderValidationError(f"candidate {candidate.id} has no supported render techniques")
    return supported_allowed[0]


def _base_warnings(candidate: TransitionCandidate) -> list[str]:
    warnings: list[str] = []
    if candidate.scores.verification is not CandidateVerification.VERIFIED:
        warnings.append(f"beat-grid verification is {candidate.scores.verification.value}")
    if candidate.scores.key_compatible is None:
        warnings.append("key compatibility is unknown")
    elif candidate.scores.key_compatible is False:
        warnings.append("candidate is not harmonically compatible")
    return warnings


def _confidence_snapshot(
    candidate: TransitionCandidate,
    warnings: list[str],
) -> RenderConfidenceSnapshot:
    from_profile = track_profiles.get(candidate.from_track)
    to_profile = track_profiles.get(candidate.to_track)
    labels = _labels_for_profiles(from_profile, to_profile)
    from_labels = labels.get(_beat_run_id(from_profile), [])
    to_labels = labels.get(_beat_run_id(to_profile), [])
    if not from_labels:
        warnings.append("source beat-grid analysis has no human listening label")
    if not to_labels:
        warnings.append("target beat-grid analysis has no human listening label")

    from_tempo_conf = from_profile.tempo.confidence if from_profile and from_profile.tempo else None
    to_tempo_conf = to_profile.tempo.confidence if to_profile and to_profile.tempo else None
    from_key_conf = from_profile.key.confidence if from_profile and from_profile.key else None
    to_key_conf = to_profile.key.confidence if to_profile and to_profile.key else None

    if from_tempo_conf is None:
        warnings.append("source tempo confidence is unknown")
    if to_tempo_conf is None:
        warnings.append("target tempo confidence is unknown")
    if from_key_conf is None:
        warnings.append("source key confidence is unknown")
    if to_key_conf is None:
        warnings.append("target key confidence is unknown")
    if from_profile is None or from_profile.key is None:
        warnings.append("source key is missing")
    if to_profile is None or to_profile.key is None:
        warnings.append("target key is missing")

    return RenderConfidenceSnapshot(
        from_tempo_confidence=from_tempo_conf,
        to_tempo_confidence=to_tempo_conf,
        from_key_confidence=from_key_conf,
        to_key_confidence=to_key_conf,
        from_beat_source=_beat_source(from_profile, candidate.scores.from_source),
        to_beat_source=_beat_source(to_profile, candidate.scores.to_source),
        from_key_source=from_profile.key.provenance.source
        if from_profile and from_profile.key
        else None,
        to_key_source=to_profile.key.provenance.source if to_profile and to_profile.key else None,
        from_beat_labels=from_labels,
        to_beat_labels=to_labels,
    )


def _labels_for_profiles(
    from_profile: TrackProfile | None,
    to_profile: TrackProfile | None,
) -> dict[int | None, list[AnalysisLabelKind]]:
    run_ids = [rid for rid in (_beat_run_id(from_profile), _beat_run_id(to_profile)) if rid]
    label_map = analysis_labels.list_for_runs(run_ids)
    return {run_id: [label.kind for label in labels] for run_id, labels in label_map.items()}


def _beat_run_id(profile: TrackProfile | None) -> int | None:
    if profile is None or profile.beat_grid is None:
        return None
    return profile.beat_grid.provenance.analysis_run_id


def _beat_source(profile: TrackProfile | None, fallback: str) -> str:
    if profile is None or profile.beat_grid is None:
        return fallback
    return profile.beat_grid.provenance.source


def _derive_timing(
    *,
    technique: RenderTechnique,
    from_cue_sec: float,
    to_cue_sec: float,
    from_duration_sec: float,
    to_duration_sec: float,
    tempo_match_ratio: float,
    warnings: list[str],
) -> RenderTiming:
    if from_cue_sec < 0 or to_cue_sec < 0:
        raise RenderValidationError("cue seconds must be non-negative")
    if from_cue_sec >= from_duration_sec:
        raise RenderValidationError("source cue is outside source audio")
    if to_cue_sec >= to_duration_sec:
        raise RenderValidationError("target cue is outside target audio")
    if tempo_match_ratio <= 0:
        raise RenderValidationError("tempo_match_ratio must be positive")

    preferred_transition, min_transition, preferred_lead, preferred_tail = _timing_defaults(
        technique
    )
    source_pre = from_cue_sec
    source_post = from_duration_sec - from_cue_sec
    target_post_output = (to_duration_sec - to_cue_sec) / tempo_match_ratio

    transition = min(preferred_transition, source_post, target_post_output)
    if transition < min_transition:
        raise RenderValidationError(
            f"no usable audio window around cues (transition {transition:.2f}s)"
        )
    source_lead = min(preferred_lead, source_pre)
    target_tail = min(preferred_tail, max(0.0, target_post_output - transition))

    if transition < preferred_transition:
        warnings.append(
            f"transition shortened from {preferred_transition:.1f}s to {transition:.1f}s"
        )
    if source_lead < preferred_lead:
        warnings.append(
            f"source lead-in shortened from {preferred_lead:.1f}s to {source_lead:.1f}s"
        )
    if target_tail < preferred_tail:
        warnings.append(f"target tail shortened from {preferred_tail:.1f}s to {target_tail:.1f}s")

    target_output_duration = transition + target_tail
    return RenderTiming(
        source_start_sec=from_cue_sec - source_lead,
        source_duration_sec=source_lead + transition,
        target_start_sec=to_cue_sec,
        target_input_duration_sec=target_output_duration * tempo_match_ratio,
        target_output_duration_sec=target_output_duration,
        transition_length_sec=transition,
        source_lead_in_sec=source_lead,
        target_tail_sec=target_tail,
    )


def _timing_defaults(technique: RenderTechnique) -> tuple[float, float, float, float]:
    if technique is RenderTechnique.PHRASE_SWAP:
        return (
            _PHRASE_TRANSITION_SEC,
            _PHRASE_MIN_TRANSITION_SEC,
            _PHRASE_SOURCE_LEAD_IN_SEC,
            _PHRASE_TARGET_TAIL_SEC,
        )
    if technique is RenderTechnique.ECHO_OUT:
        return (
            _ECHO_TRANSITION_SEC,
            _MIN_TRANSITION_SEC,
            _ECHO_SOURCE_LEAD_IN_SEC,
            _ECHO_TARGET_TAIL_SEC,
        )
    return (
        _PREFERRED_TRANSITION_SEC,
        _MIN_TRANSITION_SEC,
        _PREFERRED_SOURCE_LEAD_IN_SEC,
        _PREFERRED_TARGET_TAIL_SEC,
    )


def _filter_complex(prepared: PreparedRender) -> str:
    timing = prepared.timing
    source = _source_chain(timing, prepared.request_config.loudness_target_lufs)
    target = _target_chain(
        timing,
        prepared.request_config.tempo_match_ratio,
        prepared.request_config.loudness_target_lufs,
    )
    transition = _sec(timing.transition_length_sec)
    lead = _sec(timing.source_lead_in_sec)

    if prepared.technique is RenderTechnique.FILTER_BLEND:
        delay_ms = max(0, int(round(timing.source_lead_in_sec * 1000)))
        return (
            f"{source},lowpass=f=5000,afade=t=out:st={lead}:d={transition}[s];"
            f"{target},highpass=f=80,afade=t=in:st=0:d={transition},"
            f"adelay={delay_ms}:all=1[t];"
            "[s][t]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[out]"
        )
    if prepared.technique is RenderTechnique.ECHO_OUT:
        return (
            f"{source},aecho=0.8:0.9:500|1000:0.35|0.2,"
            f"afade=t=out:st={lead}:d={transition}[s];"
            f"{target},afade=t=in:st=0:d={transition}[t];"
            f"[s][t]acrossfade=d={transition}:c1=tri:c2=tri,alimiter=limit=0.95[out]"
        )
    curve = "qsin" if prepared.technique is RenderTechnique.PHRASE_SWAP else "tri"
    return (
        f"{source}[s];"
        f"{target}[t];"
        f"[s][t]acrossfade=d={transition}:c1={curve}:c2={curve},alimiter=limit=0.95[out]"
    )


def _source_chain(timing: RenderTiming, loudness_target_lufs: float) -> str:
    return (
        f"[0:a]atrim=start={_sec(timing.source_start_sec)}:"
        f"duration={_sec(timing.source_duration_sec)},"
        f"{_normalise_chain(loudness_target_lufs)}"
    )


def _target_chain(
    timing: RenderTiming,
    tempo_match_ratio: float,
    loudness_target_lufs: float,
) -> str:
    atempo = _atempo_chain(tempo_match_ratio)
    return (
        f"[1:a]atrim=start={_sec(timing.target_start_sec)}:"
        f"duration={_sec(timing.target_input_duration_sec)},"
        f"{atempo},"
        f"{_normalise_chain(loudness_target_lufs)}"
    )


def _normalise_chain(loudness_target_lufs: float) -> str:
    return (
        "aresample=44100,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"loudnorm=I={_sec(loudness_target_lufs)}:TP=-1.5:LRA=11,"
        "asetpts=PTS-STARTPTS"
    )


def _atempo_chain(ratio: float) -> str:
    if ratio <= 0 or not math.isfinite(ratio):
        raise RenderValidationError("tempo ratio must be finite and positive")
    parts: list[float] = []
    remaining = ratio
    while remaining > 2.0:
        parts.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        parts.append(0.5)
        remaining /= 0.5
    parts.append(remaining)
    return ",".join(f"atempo={_sec(part)}" for part in parts)


def _measure_track_loudness(track: Track, path: Path) -> LoudnessMeasurement:
    try:
        stat = path.stat()
    except OSError:
        return LoudnessMeasurement(summary=None, origin="unavailable")
    key = (track.content_hash, stat.st_mtime, stat.st_size, _LOUDNESS_METHOD_VERSION)
    if key in _loudness_cache:
        return LoudnessMeasurement(summary=_loudness_cache[key], origin="cache")
    measurement = _measure_loudness(path)
    _loudness_cache[key] = measurement.summary
    return LoudnessMeasurement(
        summary=measurement.summary,
        origin="fresh" if measurement.summary is not None else "unavailable",
    )


def _measure_loudness(path: Path) -> LoudnessMeasurement:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-af",
                "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=120.0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return LoudnessMeasurement(summary=None, origin="unavailable")
    data = _extract_last_json_object(result.stderr)
    if data is None:
        return LoudnessMeasurement(summary=None, origin="unavailable")
    lufs = _float_or_none(data.get("input_i"))
    lra = _float_or_none(data.get("input_lra"))
    true_peak = _float_or_none(data.get("input_tp"))
    return LoudnessMeasurement(
        summary=RenderLoudnessSummary(
            integrated_lufs=lufs,
            loudness_range=lra,
            true_peak_dbfs=true_peak,
            clipping_detected=true_peak is not None and true_peak > -0.3,
        ),
        origin="fresh",
    )


def _run_json_probe(path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30.0,
        )
    except subprocess.CalledProcessError as exc:
        raise RenderValidationError(f"ffprobe failed: {exc.stderr.strip()[:300]}") from exc
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RenderValidationError("ffprobe returned non-JSON output") from exc


def _ffmpeg_version() -> str:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10.0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.splitlines()[0] if result.stdout else "unknown"


def _extract_last_json_object(text: str) -> dict[str, Any] | None:
    start = text.rfind("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _sec(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
