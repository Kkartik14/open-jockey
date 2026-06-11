"""Generated audio fixtures for renderer tests."""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np


def write_sine_click_wav(
    path: Path,
    *,
    bpm: float,
    duration_sec: float = 32.0,
    frequency_hz: float = 440.0,
    sample_rate_hz: int = 44_100,
    channels: int = 2,
) -> Path:
    """Write a simple tonal WAV with short beat clicks at ``bpm``."""
    n = int(duration_sec * sample_rate_hz)
    t = np.arange(n, dtype=np.float32) / float(sample_rate_hz)
    audio = 0.18 * np.sin(2.0 * math.pi * frequency_hz * t)

    beat_interval = 60.0 / bpm
    click_len = max(1, int(0.018 * sample_rate_hz))
    decay = np.linspace(1.0, 0.0, click_len, dtype=np.float32)
    beat = 0.0
    while beat < duration_sec:
        start = int(beat * sample_rate_hz)
        end = min(n, start + click_len)
        audio[start:end] += 0.65 * decay[: end - start]
        beat += beat_interval

    audio = np.clip(audio, -0.95, 0.95)
    if channels == 2:
        pcm = np.column_stack([audio, audio])
    elif channels == 1:
        pcm = audio[:, None]
    else:
        raise ValueError("channels must be 1 or 2")
    samples = (pcm * 32767.0).astype("<i2")

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate_hz)
        handle.writeframes(samples.tobytes())
    return path


def write_silence_wav(
    path: Path,
    *,
    duration_sec: float = 8.0,
    sample_rate_hz: int = 44_100,
    channels: int = 2,
) -> Path:
    n = int(duration_sec * sample_rate_hz)
    samples = np.zeros((n, channels), dtype="<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate_hz)
        handle.writeframes(samples.tobytes())
    return path
