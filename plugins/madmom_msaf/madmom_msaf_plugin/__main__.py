"""madmom + MSAF analyzer plugin entrypoint.

Beats and downbeats come from madmom's RNN-based detectors; structure
segmentation comes from MSAF. The output is mapped onto the same
``BeatGridAnalysis`` JSON shape the local ``allin1`` plugin produces — so the
host stores them in the same column and the bake-off UI can overlay either
analyzer's grid against the same waveform.

Caveats:

- madmom is unmaintained (0.16.1 is from 2018) and ships old NumPy/Cython
  pins — install on Python 3.11+ may fail. If your local lockfile build
  fails, this plugin is informally "quarantined": the manifest still
  registers but ``uv run`` will report the install error.
- MSAF returns numeric cluster IDs for sections, not labels. We emit
  ``unknown`` for every band so the planner can still see segment
  *boundaries* without trusting MSAF's interpretation.
"""
from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
from typing import Any

from aidj_plugin_sdk import serve

# All of msaf / numpy / madmom are imported inside handle("analyze") below so
# the SDK loop starts even when those installs are broken — info/ping should
# survive a quarantined plugin.

INFO = {"name": "madmom_msaf", "version": version("madmom-msaf-plugin")}

_TIME_PRECISION = 6


def handle(method: str, params: dict[str, Any]) -> Any:
    if method == "analyze":
        # Heavy imports are deferred until the user actually wants analysis.
        # Without this, every plugin start (including ping/info) crashes with
        # ImportError on a system where madmom couldn't build.
        import msaf  # type: ignore[import-untyped]
        import numpy as np
        from madmom.features.beats import (  # type: ignore[import-untyped]
            DBNBeatTrackingProcessor,
            RNNBeatProcessor,
        )
        from madmom.features.downbeats import (  # type: ignore[import-untyped]
            DBNDownBeatTrackingProcessor,
            RNNDownBeatProcessor,
        )

        audio_path = params.get("audio_path")
        if not audio_path:
            raise ValueError("analyze: 'audio_path' is required")
        path = Path(audio_path)
        if not path.is_file():
            raise FileNotFoundError(audio_path)

        # --- beats + downbeats via madmom ---
        beat_proc = DBNBeatTrackingProcessor(fps=100)
        beat_act = RNNBeatProcessor()(str(path))
        beats = [float(t) for t in beat_proc(beat_act)]

        db_proc = DBNDownBeatTrackingProcessor(beats_per_bar=[3, 4], fps=100)
        db_act = RNNDownBeatProcessor()(str(path))
        downbeats_raw = db_proc(db_act)
        downbeats = [float(t) for t, pos in downbeats_raw if int(pos) == 1]

        bpm = (
            60.0 / float(np.median(np.diff(beats)))
            if len(beats) > 1 else 0.0
        )

        # --- sections via MSAF ---
        boundaries, _labels = msaf.process(str(path), boundaries_id="sf", labels_id=None)
        sections_raw: list[tuple[float, float]] = []
        if boundaries is not None and len(boundaries) >= 2:
            for i in range(len(boundaries) - 1):
                sections_raw.append((float(boundaries[i]), float(boundaries[i + 1])))

        # --- map onto BeatGridAnalysis JSON ---
        downbeat_set = {round(t, _TIME_PRECISION) for t in downbeats}
        beats_out = [
            {"time_sec": t, "is_downbeat": round(t, _TIME_PRECISION) in downbeat_set}
            for t in beats
        ]
        sections_out = [
            {"start_sec": s, "end_sec": e, "label": "unknown"}
            for s, e in sections_raw
        ]
        duration = (
            float(sections_out[-1]["end_sec"]) if sections_out
            else (beats[-1] if beats else 0.0)
        )

        return {
            "tempo": {"bpm": bpm},
            "beats": beats_out,
            "sections": sections_out,
            "duration_sec": duration,
        }

    raise ValueError(f"unknown method: {method}")


if __name__ == "__main__":
    serve(handle, info=INFO)
