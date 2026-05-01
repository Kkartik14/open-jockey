"""allin1 analyzer plugin entrypoint.

Wraps the all-in-one Music Structure Analysis library
(https://github.com/mir-aidj/all-in-one). On first run, uv installs torch +
demucs + allin1; downloading the model weights takes another few hundred MB.

The ``analyze`` method maps allin1's ``AnalysisResult`` onto open-jockey's
``BeatGridAnalysis`` schema. Section labels are normalised to
``SectionLabel`` values.

Heavy import (``allin1`` itself) is deferred until ``analyze`` is actually
called. That way the SDK loop starts cleanly and the reserved ``info`` /
``ping`` methods stay answerable even when the analyzer's deps are broken
(e.g. allin1's transitive ``madmom`` dependency failing to install) — the
host can still discover the plugin and probe its health.
"""
from __future__ import annotations

import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import Any

from aidj_plugin_sdk import serve

INFO = {"name": "allin1", "version": version("allin1-plugin")}

# allin1 has its own segment label set; we map onto open-jockey's normalised
# vocabulary so the host doesn't have to know per-analyzer dialects.
_SECTION_LABEL_MAP: dict[str, str] = {
    "intro": "intro",
    "verse": "verse",
    "chorus": "chorus",
    "bridge": "bridge",
    "inst": "instrumental",
    "instrumental": "instrumental",
    "solo": "instrumental",
    "outro": "outro",
    "break": "breakdown",
    "breakdown": "breakdown",
    "drop": "drop",
    "build": "drop",
}

# allin1 emits 'start' and 'end' pseudo-segments at silent track boundaries.
# They have no musical meaning — drop them so the planner doesn't see them as
# unknown sections at the head and tail of every track.
_BOUNDARY_LABELS: frozenset[str] = frozenset({"start", "end"})

# Tolerance for matching downbeats against beats. allin1 returns both arrays
# from the same source so they should be identical, but float equality across
# a list comparison is risky; round to microseconds.
_TIME_PRECISION = 6


def _normalise_label(raw: str) -> str:
    return _SECTION_LABEL_MAP.get(raw.lower().strip(), "unknown")


def _convert(result: Any) -> dict[str, Any]:
    """Map an allin1 ``AnalysisResult`` onto BeatGridAnalysis JSON.

    allin1's silence boundary segments (labels 'start' / 'end') are dropped so
    they don't pollute the section list with synthetic 'unknown' entries.
    """
    beats = [float(t) for t in result.beats]
    downbeat_set = {round(float(t), _TIME_PRECISION) for t in result.downbeats}

    beats_out = [
        {"time_sec": t, "is_downbeat": round(t, _TIME_PRECISION) in downbeat_set}
        for t in beats
    ]
    sections_out = [
        {
            "start_sec": float(seg.start),
            "end_sec": float(seg.end),
            "label": _normalise_label(seg.label),
        }
        for seg in result.segments
        if seg.label.lower().strip() not in _BOUNDARY_LABELS
    ]
    duration = float(sections_out[-1]["end_sec"]) if sections_out else (beats[-1] if beats else 0.0)

    return {
        "tempo": {"bpm": float(result.bpm)},
        "beats": beats_out,
        "sections": sections_out,
        "duration_sec": duration,
    }


def handle(method: str, params: dict[str, Any]) -> Any:
    if method == "analyze":
        # Heavy import lives here so a broken allin1/madmom install only
        # surfaces on real analyze calls, not at process start (where it
        # would also kill ping/info).
        import allin1  # type: ignore[import-untyped]

        audio_path = params.get("audio_path")
        if not audio_path:
            raise ValueError("analyze: 'audio_path' is required")
        path = Path(audio_path)
        if not path.is_file():
            raise FileNotFoundError(audio_path)

        # Run allin1 with a fresh per-call scratch dir so its demix/spec
        # byproducts can't land in the host cwd or collide across runs. The
        # tempdir is removed after analyze() returns whether it succeeded or
        # raised — the with-block cleans up on the way out.
        with tempfile.TemporaryDirectory(prefix="allin1-") as scratch:
            results = allin1.analyze(
                [str(path)],
                demix_dir=scratch,
                spec_dir=scratch,
                keep_byproducts=False,
            )
            if not results:
                raise RuntimeError("allin1 returned no results")
            return _convert(results[0])

    raise ValueError(f"unknown method: {method}")


if __name__ == "__main__":
    serve(handle, info=INFO)
