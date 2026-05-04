"""librosa beat-tracking plugin — baseline for the analyzer bake-off.

Why ship a known-mediocre detector deliberately:

- ``librosa.beat.beat_track`` is the reference implementation everyone in MIR
  has compared against for a decade. Dynamic programming over an
  onset-strength envelope.
- It's known to lock onto half/double tempo on dense tracks and to drift on
  rubato (Indian classical, jazz, ballads). The ``half_time`` /
  ``double_time`` / ``unusable`` labels in ``analysis_labels`` exist exactly
  for capturing this — the failure mode is the data.
- Without a second analyzer, ``allin1`` vs ``allin1_remote`` is not a bake-off
  (same model, same weights). librosa gives the labeler something to actually
  disagree with.

Downbeats: librosa has no real downbeat detector. We assume 4/4 and mark
every 4th beat starting from index 0. Catches club music; misses waltz, 7/8,
tracks that start on the upbeat. ``wrong_downbeat_phase`` captures it.

Sections: ``librosa.segment.agglomerative`` returns boundaries from a
chroma-CQT self-similarity matrix. The cluster IDs are not musical labels,
so every emitted segment is labelled ``unknown`` (same compromise
``madmom_msaf`` made). The planner sees the boundaries without having to
trust the interpretation.

The heavy ``import librosa`` lives inside ``handle("analyze")`` so ``info``
and ``ping`` answer cleanly even when librosa or its native dependencies
fail to load — keeps plugin discovery healthy on broken installs.
"""
from __future__ import annotations

import logging
from importlib.metadata import version
from pathlib import Path
from typing import Any

from aidj_plugin_sdk import serve

INFO = {"name": "librosa", "version": version("librosa-plugin")}

# 22050 Hz is plenty for beat detection (Nyquist=11kHz covers all rhythmic
# content). ~2x faster than CD-quality 44100, no audible BPM impact.
_TARGET_SR = 22050

# Sections k. Most pop songs have ~6-10 distinguishable structural blocks
# (intro/verse/chorus variations + bridge + outro). 8 is the resolution dial;
# the segmentation is unsupervised so this isn't a strong tuning knob.
_DEFAULT_K = 8

log = logging.getLogger(__name__)


def handle(method: str, params: dict[str, Any]) -> Any:
    if method == "analyze":
        # Heavy imports deferred so info/ping survive a broken install.
        import librosa
        import numpy as np

        audio_path = params.get("audio_path")
        if not audio_path:
            raise ValueError("analyze: 'audio_path' is required")
        path = Path(audio_path)
        if not path.is_file():
            raise FileNotFoundError(audio_path)

        y, sr = librosa.load(str(path), sr=_TARGET_SR, mono=True)
        duration_sec = float(len(y)) / float(sr)

        # --- beats + tempo --------------------------------------------------
        # librosa 0.10+ returns tempo as np.ndarray (shape (1,)); pre-0.10
        # returned a scalar. atleast_1d normalises both into a 1-D array.
        tempo_raw, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(np.atleast_1d(tempo_raw)[0])
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)

        # --- naive 4/4 downbeats --------------------------------------------
        beats_out = [
            {"time_sec": float(t), "is_downbeat": (i % 4 == 0)}
            for i, t in enumerate(beat_times)
        ]

        # --- structural sections via chroma-CQT agglomerative ---------------
        sections_out = _segment(y, sr, duration_sec, k=_DEFAULT_K)

        return {
            "tempo": {"bpm": bpm},
            "beats": beats_out,
            "sections": sections_out,
            "duration_sec": duration_sec,
        }

    raise ValueError(f"unknown method: {method}")


def _segment(
    y: Any,
    sr: int,
    duration_sec: float,
    *,
    k: int,
) -> list[dict[str, Any]]:
    """Agglomerative segmentation on chroma-CQT. Boundaries only; labels are
    the unsupervised cluster IDs which carry no musical meaning, so we emit
    ``unknown`` for every band.

    Returns an empty list on failure (very short clips, numerical issues)
    rather than failing the whole analyze call — sections are optional in
    BeatGridAnalysis and the frontend treats them defensively.
    """
    import librosa

    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        # ``k`` boundaries → k+1 segments. Clamp so we never ask for more
        # boundaries than chroma frames divided by 4 (a minimum segment-size
        # heuristic) — very short clips would otherwise raise.
        k_eff = max(2, min(k, chroma.shape[1] // 4))
        boundary_frames = librosa.segment.agglomerative(chroma, k=k_eff)
        boundary_times = sorted(
            float(t) for t in librosa.frames_to_time(boundary_frames, sr=sr)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("librosa segmentation failed: %s; emitting empty sections", exc)
        return []

    # Anchor at 0 and duration so the section list spans the whole track.
    edges = [0.0, *boundary_times, duration_sec]
    out: list[dict[str, Any]] = []
    for s, e in zip(edges, edges[1:], strict=False):
        if e > s:
            out.append({"start_sec": s, "end_sec": e, "label": "unknown"})
    return out


if __name__ == "__main__":
    serve(handle, info=INFO)
