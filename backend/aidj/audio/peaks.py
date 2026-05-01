"""Waveform peak extraction.

The frontend's WaveSurfer needs a downsampled magnitude-per-bucket array to
draw a waveform. If we let WaveSurfer fetch + decode the audio itself, every
track-detail page load downloads the entire file (multi-MB at minimum, hundreds
of MB for a long FLAC). Instead we precompute peaks server-side and let the
``<audio>`` element handle playback via HTTP Range — small JSON over the wire,
fast UI, real Range-based streaming for actual playback.

ffmpeg + ffprobe are required at runtime. They are *system* deps (``brew
install ffmpeg`` / ``apt install ffmpeg``); we don't bundle them. If they are
missing, the peaks endpoint surfaces a 503 with a clear message.
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from aidj.store import cache

log = logging.getLogger(__name__)

PEAKS_KIND = "peaks"
DEFAULT_SAMPLES = 2048
DEFAULT_SAMPLE_RATE_HZ = 8000  # downsample target — plenty for a UI waveform
FFPROBE_TIMEOUT_SEC = 30.0
FFMPEG_TIMEOUT_SEC = 120.0

# Bump whenever the math that produces the peaks array changes (bucket sizing,
# normalisation, smoothing, etc.). The cache filename includes this number so
# stale files written by an older algorithm version are simply not read —
# they sit on disk until cache GC sweeps them, but the wrong shape never
# reaches the client.
PEAKS_FORMAT_VERSION = 2


@dataclass(frozen=True)
class PeaksData:
    duration_sec: float
    samples: int
    peaks: list[float]


class PeaksError(RuntimeError):
    """Raised when peak extraction fails (ffmpeg missing, decode failure, etc.)."""


def is_ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def extract_peaks(
    path: Path,
    *,
    samples: int = DEFAULT_SAMPLES,
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
) -> PeaksData:
    """Decode ``path`` via ffmpeg and downsample to ``samples`` magnitude peaks.

    The values are absolute amplitudes in [0, 1]. Mono only — stereo is folded
    by ffmpeg before we see it (``-ac 1``).
    """
    if not is_ffmpeg_available():
        raise PeaksError("ffmpeg/ffprobe not on PATH; install with `brew install ffmpeg` (macOS) or your package manager")

    duration = _probe_duration(path)
    pcm = _decode_pcm(path, sample_rate_hz=sample_rate_hz)

    if pcm.size == 0:
        return PeaksData(duration_sec=duration, samples=0, peaks=[])

    # Use ``ceil`` instead of floor division so the resulting bucket count is
    # always ≤ ``samples`` (otherwise short audio + small ``samples`` like
    # 8000 PCM / samples=2048 yields bucket_size=3 → 2666 buckets > 2048).
    bucket_size = max(1, math.ceil(pcm.size / samples))
    n_buckets = pcm.size // bucket_size
    if n_buckets == 0:
        return PeaksData(duration_sec=duration, samples=0, peaks=[])

    # Trim to a clean multiple of bucket_size so reshape is exact, then take
    # the per-bucket magnitude. This is the standard min/max envelope reduced
    # to a single magnitude per bucket — sufficient for a 96px-tall canvas.
    trimmed = pcm[: n_buckets * bucket_size]
    buckets = trimmed.reshape(n_buckets, bucket_size)
    peaks = np.max(np.abs(buckets), axis=1).astype(np.float32)

    return PeaksData(
        duration_sec=duration,
        samples=int(peaks.size),
        peaks=[float(x) for x in peaks],
    )


def _probe_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=FFPROBE_TIMEOUT_SEC,
        )
    except subprocess.CalledProcessError as exc:
        raise PeaksError(f"ffprobe failed: {exc.stderr.strip()[:300]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PeaksError(f"ffprobe timed out after {FFPROBE_TIMEOUT_SEC}s") from exc

    raw = result.stdout.strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise PeaksError(f"ffprobe returned non-numeric duration: {raw!r}") from exc


def _decode_pcm(path: Path, *, sample_rate_hz: int) -> np.ndarray:
    """Decode any audio file to a 1-D float32 numpy array in [-1, 1]."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-i", str(path),
                "-ac", "1",                  # mono
                "-ar", str(sample_rate_hz),  # downsample
                "-f", "s16le", "-",          # raw 16-bit signed little-endian
            ],
            capture_output=True,
            check=True,
            timeout=FFMPEG_TIMEOUT_SEC,
        )
    except subprocess.CalledProcessError as exc:
        raise PeaksError(
            f"ffmpeg decode failed: {exc.stderr.decode(errors='replace').strip()[:300]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PeaksError(f"ffmpeg decode timed out after {FFMPEG_TIMEOUT_SEC}s") from exc

    return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# Cached entry point
# ---------------------------------------------------------------------------


def get_or_compute_peaks(
    track_hash: str,
    source_path: Path,
    *,
    samples: int = DEFAULT_SAMPLES,
) -> PeaksData:
    """Return cached peaks if present, otherwise compute + cache.

    The cache is keyed on (track_hash, format_version, samples) so different
    waveform sizes coexist *and* old algorithm versions are silently ignored.
    Track-hash being content-addressed means a re-ingested file with the same
    contents hits the same cache.
    """
    filename = f"peaks-v{PEAKS_FORMAT_VERSION}-{samples}.json"
    cached = cache.get_bytes(PEAKS_KIND, track_hash, filename)
    if cached is not None:
        try:
            data = json.loads(cached)
            return PeaksData(**data)
        except (ValueError, TypeError) as exc:
            log.warning("dropping malformed cached peaks for %s: %s", track_hash[:12], exc)

    peaks = extract_peaks(source_path, samples=samples)
    cache.put_bytes(
        PEAKS_KIND,
        track_hash,
        filename,
        json.dumps(asdict(peaks)).encode("utf-8"),
    )
    return peaks
