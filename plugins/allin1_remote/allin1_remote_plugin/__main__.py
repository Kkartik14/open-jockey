"""Plugin that delegates to a deployed Modal function for allin1 inference.

Local cost is roughly the SDK loop + the modal client (~30 MB resident). All
ML happens in the Modal GPU container.

The deployed function must be at ``aidj-analyzers/analyze_allin1`` — deploy it
once with::

    modal deploy plugins/allin1_remote/modal_worker/analyze.py

If the function isn't deployed (or your Modal token is missing), the first
``analyze`` call surfaces a clear error from ``modal.Function.from_name``.

Defensive measures, in addition to the host-side ``cloud_audio`` gate:

- We re-check ``AIDJ_ALLOW_CLOUD_AUDIO`` on every call. Belt-and-braces: if a
  caller bypasses the API and pokes the plugin directly, this still blocks.
- ``AIDJ_REMOTE_MAX_BYTES`` (default 256 MB) caps the upload size. Files above
  it are rejected before any network I/O.
"""
from __future__ import annotations

import os
from importlib.metadata import version
from pathlib import Path
from typing import Any

import modal  # type: ignore[import-untyped]

from aidj_plugin_sdk import serve

INFO = {"name": "allin1_remote", "version": version("allin1-remote-plugin")}

CLOUD_AUDIO_OPT_IN_ENV = "AIDJ_ALLOW_CLOUD_AUDIO"
MAX_BYTES_ENV = "AIDJ_REMOTE_MAX_BYTES"
DEFAULT_MAX_BYTES = 256 * 1024 * 1024  # 256 MB — covers ~30 min of CD-quality FLAC

# Lazy lookup so plugin start-up doesn't fail when the worker hasn't been
# deployed yet — error surfaces on the first `analyze` call instead.
_fn: Any | None = None


def _get_fn() -> Any:
    global _fn
    if _fn is None:
        _fn = modal.Function.from_name("aidj-analyzers", "analyze_allin1")
    return _fn


def _max_upload_bytes() -> int:
    raw = os.environ.get(MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_MAX_BYTES
    return parsed if parsed > 0 else DEFAULT_MAX_BYTES


def _check_cloud_audio_opt_in() -> None:
    if os.environ.get(CLOUD_AUDIO_OPT_IN_ENV, "").strip() != "1":
        raise PermissionError(
            f"this plugin uploads audio to a remote service; set "
            f"{CLOUD_AUDIO_OPT_IN_ENV}=1 in the backend env to opt in"
        )


def handle(method: str, params: dict[str, Any]) -> Any:
    if method == "analyze":
        _check_cloud_audio_opt_in()

        audio_path = params.get("audio_path")
        if not audio_path:
            raise ValueError("analyze: 'audio_path' is required")
        path = Path(audio_path)
        if not path.is_file():
            raise FileNotFoundError(audio_path)

        size_bytes = path.stat().st_size
        cap = _max_upload_bytes()
        if size_bytes > cap:
            raise ValueError(
                f"audio file is {size_bytes} bytes (>{cap}-byte upload cap from "
                f"{MAX_BYTES_ENV}). Transcode to a smaller format or raise the cap."
            )

        # Read the bytes and ship them to Modal. The remote function does the
        # actual allin1 work and returns BeatGridAnalysis JSON — same shape as
        # the local plugin, so the host's analyze pipeline doesn't care which
        # one was used.
        audio_bytes = path.read_bytes()
        return _get_fn().remote(audio_bytes, path.name)

    raise ValueError(f"unknown method: {method}")


if __name__ == "__main__":
    serve(handle, info=INFO)
