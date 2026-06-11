"""CLI smoke tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_aidj_info_smoke(tmp_aidj) -> None:
    """Entry-point smoke for the plugin manifest/version path.

    This catches regressions where ``aidj info`` imports cleanly but crashes
    while rendering discovered plugin metadata.
    """
    env = {
        **os.environ,
        "AIDJ_PROJECT_ROOT": str(tmp_aidj.project_root),
        "AIDJ_UV_CACHE_DIR": str(tmp_aidj.uv_cache_dir),
    }

    result = subprocess.run(
        ["uv", "run", "--frozen", "aidj", "info"],
        cwd=BACKEND_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )

    assert "schema:       initialized" in result.stdout
    assert "plugins:" in result.stdout
    assert "echo@0.1.0" in result.stdout
