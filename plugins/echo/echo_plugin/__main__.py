"""Echo plugin entrypoint — implements only domain methods. The SDK runs the loop.

The ``analyze`` method returns canned ``BeatGridAnalysis``-shaped output so the
backend's analyzer pipeline can be tested end-to-end without depending on heavy
real analyzers (allin1, demucs, etc.).
"""
from __future__ import annotations

import time
from importlib.metadata import version
from typing import Any

from aidj_plugin_sdk import serve

INFO = {"name": "echo", "version": version("echo-plugin")}


def _canned_beat_grid() -> dict[str, Any]:
    """A small, deterministic BeatGridAnalysis-shaped payload for tests."""
    bpm = 120.0
    beat_period = 60.0 / bpm  # 0.5s
    beats = [
        {"time_sec": i * beat_period, "is_downbeat": (i % 4 == 0)}
        for i in range(16)
    ]
    sections = [
        {"start_sec": 0.0, "end_sec": 4.0, "label": "intro"},
        {"start_sec": 4.0, "end_sec": 8.0, "label": "verse"},
    ]
    return {
        "tempo": {"bpm": bpm, "confidence": 0.95},
        "beats": beats,
        "sections": sections,
        "duration_sec": 8.0,
        "confidence": 0.9,
    }


def handle(method: str, params: dict[str, Any]) -> Any:
    if method == "echo":
        return {"echo": params}
    if method == "sleep":
        seconds = float(params.get("seconds", 1))
        time.sleep(seconds)
        return {"slept": seconds}
    if method == "analyze":
        # Verify the audio_path param is present; we don't actually read the file.
        if "audio_path" not in params:
            raise ValueError("analyze: 'audio_path' is required")
        return _canned_beat_grid()
    raise ValueError(f"unknown method: {method}")


if __name__ == "__main__":
    serve(handle, info=INFO)
