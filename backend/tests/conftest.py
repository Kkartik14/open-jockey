"""Shared pytest fixtures.

``tmp_aidj`` builds a fully-isolated aidj environment per test:

- a fresh ``project_root`` on tmp_path
- the real ``plugins/`` directory symlinked in so plugin RPC tests run against
  real plugin code
- a session-shared uv cache so tests don't re-resolve the plugin SDK on every
  call (otherwise each test would pay the install cost)
- the SQLite DB recreated empty
- all module-level singletons (settings, db connection, registry) reset before
  AND after the test, so ordering between tests cannot leak state
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aidj.config import Settings, set_settings
from aidj.plugins.registry import reset_registry
from aidj.store import db

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PLUGINS = REPO_ROOT / "plugins"


def _reset_singletons() -> None:
    reset_registry()
    db.close()
    set_settings(None)


@pytest.fixture(scope="session")
def shared_uv_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One uv cache shared across the whole test session.

    Per-test caches would force uv to re-resolve and re-install the plugin SDK
    every time, which makes the suite painfully slow.
    """
    return tmp_path_factory.mktemp("aidj-uv-cache")


@pytest.fixture
def tmp_aidj(tmp_path: Path, shared_uv_cache: Path) -> Settings:
    _reset_singletons()
    (tmp_path / "plugins").symlink_to(REAL_PLUGINS)
    s = Settings(project_root=tmp_path, uv_cache_dir=shared_uv_cache)
    s.ensure_dirs()
    set_settings(s)
    db.reset_for_tests(s.db_path)
    yield s
    _reset_singletons()


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    """A small file we can ingest as a Track."""
    p = tmp_path / "sample.bin"
    p.write_bytes(b"aidj test content " * 16)
    return p
