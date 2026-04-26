"""Modal worker for open-jockey: runs allin1 on a GPU container.

Deploy with:

    modal deploy plugins/allin1_remote/modal_worker/analyze.py

This file defines a single ``modal.App`` named ``aidj-analyzers`` containing one
function: ``analyze_allin1(audio_bytes, filename) -> dict``. The host-side
``allin1_remote`` plugin invokes it via ``modal.Function.from_name`` so the
deployed function can live for the long term while plugins come and go.

The returned dict matches the local ``allin1`` plugin's ``BeatGridAnalysis``
JSON shape exactly — drop-in compatible with the host's analyze pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import modal

# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------

app = modal.App("aidj-analyzers")

# Bake torch + allin1 into the image once. After deploy, cold start is just
# container boot (~5–10s) rather than pip install (would be ~3 min).
#
# Use a fully-pinned transitive lock (regenerated from the local allin1 plugin)
# so the Modal worker and the local plugin install the *same* torch / demucs /
# numpy / etc. versions. A bare ``allin1==1.1.0`` would let pip silently pick
# different transitive deps on every Modal image rebuild — that's the failure
# mode we're avoiding here. See requirements.txt for the regen command.
_REQUIREMENTS_PATH = str(Path(__file__).resolve().parent / "requirements.txt")

allin1_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install_from_requirements(_REQUIREMENTS_PATH)
)

# ---------------------------------------------------------------------------
# Schema mapping (kept in lock-step with the local allin1 plugin)
# ---------------------------------------------------------------------------

# Silence pseudo-segments allin1 emits at track boundaries — drop them so they
# don't pollute the section list with synthetic 'unknown' entries.
_BOUNDARY_LABELS: frozenset[str] = frozenset({"start", "end"})

# Normalise allin1's segment vocabulary onto open-jockey's SectionLabel enum
# values. Any label outside this map becomes 'unknown'.
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

# Microsecond rounding for downbeat/beat float-set lookup.
_TIME_PRECISION = 6


def _normalise_label(raw: str) -> str:
    return _SECTION_LABEL_MAP.get(raw.lower().strip(), "unknown")


def _convert(result: Any) -> dict[str, Any]:
    """Map an allin1 ``AnalysisResult`` onto BeatGridAnalysis JSON.

    Identical to ``plugins/allin1/allin1_plugin/__main__.py:_convert`` — the two
    are kept in sync deliberately because the worker can't import from a sibling
    plugin's package (different deployment context).
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


# ---------------------------------------------------------------------------
# The function
# ---------------------------------------------------------------------------


@app.function(
    image=allin1_image,
    gpu="T4",
    timeout=600,
)
def analyze_allin1(audio_bytes: bytes, filename: str = "input.mp3") -> dict[str, Any]:
    """Run allin1 on the supplied audio bytes; return BeatGridAnalysis JSON.

    The audio is written to a tempdir, allin1's demix/spec byproducts are
    contained there too (``keep_byproducts=False``), and the tempdir is cleaned
    up on context exit whether analysis succeeds or fails.
    """
    import tempfile
    from pathlib import Path

    import allin1  # type: ignore[import-untyped]
    import torch

    # Inferencing only. Saves memory and avoids accidental autograd graph growth.
    torch.set_grad_enabled(False)

    # ``filename`` is supplied by the caller — strip any directory components so
    # an attacker can't traverse outside the tempdir (e.g. "../../etc/foo").
    safe_name = Path(filename).name or "input.mp3"

    with tempfile.TemporaryDirectory(prefix="allin1-") as tmp:
        audio_path = Path(tmp) / safe_name
        audio_path.write_bytes(audio_bytes)
        scratch = Path(tmp) / "scratch"
        scratch.mkdir()

        results = allin1.analyze(
            [str(audio_path)],
            demix_dir=str(scratch),
            spec_dir=str(scratch),
            keep_byproducts=False,
        )
        if not results:
            raise RuntimeError("allin1 returned no results")
        return _convert(results[0])
