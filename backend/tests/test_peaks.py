"""Peak-extraction unit tests + cache behaviour."""
from __future__ import annotations

import shutil
import struct
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from aidj.audio import peaks as audio_peaks

# These tests need ffmpeg/ffprobe at runtime to actually decode anything. On a
# host without them, the tests for the success path are skipped — but the
# is_ffmpeg_available + 503 fallback path is covered separately via mocking.
_FFMPEG_PRESENT = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(
    not _FFMPEG_PRESENT,
    reason="ffmpeg/ffprobe not on PATH",
)


def _make_test_wav(path: Path, *, duration_sec: float = 1.0, sample_rate: int = 8000) -> None:
    """Write a tiny PCM-WAV file deterministically (no audio libs required)."""
    n_samples = int(duration_sec * sample_rate)
    samples = []
    for i in range(n_samples):
        # Sawtooth-ish, amplitude ~ +/- 16000
        samples.append(int(16000 * ((i % 100) - 50) / 50))
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(struct.pack(f"<{n_samples}h", *samples))


def test_is_ffmpeg_available_returns_bool() -> None:
    # Just check the API contract — the value is environment-dependent.
    assert isinstance(audio_peaks.is_ffmpeg_available(), bool)


def test_extract_peaks_raises_when_ffmpeg_missing(tmp_path: Path) -> None:
    p = tmp_path / "any.wav"
    p.write_bytes(b"fake")
    with (
        patch.object(audio_peaks, "is_ffmpeg_available", return_value=False),
        pytest.raises(audio_peaks.PeaksError, match="ffmpeg"),
    ):
        audio_peaks.extract_peaks(p)


@needs_ffmpeg
def test_extract_peaks_basic(tmp_path: Path) -> None:
    p = tmp_path / "song.wav"
    _make_test_wav(p, duration_sec=2.0)

    data = audio_peaks.extract_peaks(p, samples=64)
    assert 1.9 <= data.duration_sec <= 2.1
    assert data.samples == len(data.peaks)
    assert 0 < data.samples <= 64
    # All amplitudes are normalised into [0, 1].
    assert all(0.0 <= v <= 1.0 for v in data.peaks)
    # Our sawtooth should produce a non-trivial waveform — at least one
    # sample above 0.3 magnitude.
    assert max(data.peaks) > 0.3


@needs_ffmpeg
def test_extract_peaks_respects_samples_target(tmp_path: Path) -> None:
    p = tmp_path / "song.wav"
    _make_test_wav(p, duration_sec=1.0)

    small = audio_peaks.extract_peaks(p, samples=32)
    big = audio_peaks.extract_peaks(p, samples=512)
    # We aim for at most ``samples`` buckets — fewer is fine if the file is short.
    assert small.samples <= 32
    assert big.samples > small.samples


@needs_ffmpeg
def test_get_or_compute_peaks_caches_result(tmp_aidj, tmp_path: Path) -> None:
    p = tmp_path / "song.wav"
    _make_test_wav(p, duration_sec=1.0)

    first = audio_peaks.get_or_compute_peaks("a" * 64, p, samples=64)

    # Second call should hit the cache rather than ffmpeg.
    with patch.object(audio_peaks, "extract_peaks") as extract:
        second = audio_peaks.get_or_compute_peaks("a" * 64, p, samples=64)
        assert not extract.called

    assert second.duration_sec == first.duration_sec
    assert second.peaks == first.peaks


@needs_ffmpeg
def test_get_or_compute_peaks_separates_by_samples(tmp_aidj, tmp_path: Path) -> None:
    p = tmp_path / "song.wav"
    _make_test_wav(p, duration_sec=1.0)

    a = audio_peaks.get_or_compute_peaks("b" * 64, p, samples=32)
    b = audio_peaks.get_or_compute_peaks("b" * 64, p, samples=128)
    assert a.samples != b.samples  # different buckets → independently cached
