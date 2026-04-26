"""Logging configuration. Stdlib ``logging`` is enough for a single-user app —
no structured-logging library, no JSON formatter, just consistent setup so every
module uses the same handlers and levels.
"""
from __future__ import annotations

import logging
import os
import sys

DEFAULT_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
DEFAULT_DATEFMT = "%H:%M:%S"


def setup(level: str | int | None = None) -> None:
    """Configure the root logger. Idempotent.

    Level resolves from (in order): explicit argument, ``AIDJ_LOG_LEVEL``, INFO.
    """
    resolved = level or os.environ.get("AIDJ_LOG_LEVEL") or "INFO"
    if isinstance(resolved, str):
        resolved = resolved.upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATEFMT))
    root = logging.getLogger()
    # Replace any existing handlers so reload doesn't stack them.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(resolved)
