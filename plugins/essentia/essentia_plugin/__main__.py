"""essentia key-detection plugin entrypoint.

Uses ``essentia.standard.KeyExtractor`` to estimate a track's key and scale,
then maps onto the Camelot wheel notation that DJs use for harmonic mixing
(e.g. C major → 8B, A minor → 8A; songs in adjacent Camelot positions sound
musically compatible).

Output schema is ``KeyAnalysis`` (see backend/aidj/store/models.py): a single
flat dict with ``key``, ``scale``, ``camelot``, ``confidence``.

The ``import essentia.standard`` is deferred to inside ``handle("analyze")``
so the SDK loop starts cleanly even if essentia's native bindings fail to
load — ``info`` and ``ping`` remain answerable for plugin discovery.
"""
from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
from typing import Any

from aidj_plugin_sdk import serve

INFO = {"name": "essentia", "version": version("essentia-plugin")}

# Canonical key spelling. essentia returns natural-sharp ("C#") rather than
# flats; we keep that convention. We also keep an alias map so a future caller
# that hands us a flat-spelt key still resolves.
_KEY_ALIASES: dict[str, str] = {
    "Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#",
}


# Camelot wheel — major keys on the B side, minor keys on the A side. The
# numbers wrap (1B is adjacent to 12B). This is the table every DJ memorises.
_CAMELOT: dict[tuple[str, str], str] = {
    ("C",  "major"): "8B",
    ("G",  "major"): "9B",
    ("D",  "major"): "10B",
    ("A",  "major"): "11B",
    ("E",  "major"): "12B",
    ("B",  "major"): "1B",
    ("F#", "major"): "2B",
    ("C#", "major"): "3B",
    ("G#", "major"): "4B",
    ("D#", "major"): "5B",
    ("A#", "major"): "6B",
    ("F",  "major"): "7B",
    ("A",  "minor"): "8A",
    ("E",  "minor"): "9A",
    ("B",  "minor"): "10A",
    ("F#", "minor"): "11A",
    ("C#", "minor"): "12A",
    ("G#", "minor"): "1A",
    ("D#", "minor"): "2A",
    ("A#", "minor"): "3A",
    ("F",  "minor"): "4A",
    ("C",  "minor"): "5A",
    ("G",  "minor"): "6A",
    ("D",  "minor"): "7A",
}


def _camelot_for(key: str, scale: str) -> str | None:
    norm_key = _KEY_ALIASES.get(key, key)
    norm_scale = scale.lower()
    return _CAMELOT.get((norm_key, norm_scale))


def handle(method: str, params: dict[str, Any]) -> Any:
    if method == "analyze":
        # Heavy import lives here so a broken essentia install only surfaces
        # on real analyze calls — info/ping still answer at process start.
        import essentia.standard as es  # type: ignore[import-untyped]

        audio_path = params.get("audio_path")
        if not audio_path:
            raise ValueError("analyze: 'audio_path' is required")
        path = Path(audio_path)
        if not path.is_file():
            raise FileNotFoundError(audio_path)

        audio = es.MonoLoader(filename=str(path))()
        key, scale, strength = es.KeyExtractor()(audio)
        return {
            "key": key,
            "scale": scale,
            "camelot": _camelot_for(key, scale),
            "confidence": float(strength),
        }

    raise ValueError(f"unknown method: {method}")


if __name__ == "__main__":
    serve(handle, info=INFO)
