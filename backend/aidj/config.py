"""Settings for aidj. Resolves the project root and the .aidj store location.

The ``settings()`` accessor reads a module-level singleton rather than using
``lru_cache`` so tests (and future runtime contexts) can swap it out cleanly via
``set_settings``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_project_root() -> Path:
    """Resolve the project root — preferring an existing .aidj/, then a .git marker."""
    cwd = Path.cwd().resolve()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / ".aidj").is_dir():
            return candidate
    for candidate in [cwd, *cwd.parents]:
        if (candidate / ".git").exists():
            return candidate
    return cwd


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIDJ_", extra="ignore")

    project_root: Path = Field(default_factory=_default_project_root)
    store_dirname: str = ".aidj"
    plugins_dirname: str = "plugins"
    # Override the default uv cache location. None → ``store_root/uv-cache``.
    # Set via ``AIDJ_UV_CACHE_DIR`` or directly in tests.
    uv_cache_dir: Path | None = None

    @property
    def store_root(self) -> Path:
        return self.project_root / self.store_dirname

    @property
    def db_path(self) -> Path:
        return self.store_root / "aidj.db"

    @property
    def cache_root(self) -> Path:
        return self.store_root / "cache"

    @property
    def models_root(self) -> Path:
        return self.store_root / "models"

    @property
    def logs_root(self) -> Path:
        return self.store_root / "logs"

    @property
    def projects_root(self) -> Path:
        return self.store_root / "projects"

    @property
    def plugins_root(self) -> Path:
        return self.project_root / self.plugins_dirname

    @property
    def uv_cache_root(self) -> Path:
        return (
            self.uv_cache_dir if self.uv_cache_dir is not None else (self.store_root / "uv-cache")
        )

    def ensure_dirs(self) -> None:
        dirs = (
            self.store_root,
            self.cache_root,
            self.models_root,
            self.logs_root,
            self.projects_root,
            self.uv_cache_root,
        )
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def settings() -> Settings:
    """Return the active settings, constructing defaults on first use."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(new: Settings | None) -> None:
    """Override the active settings. Pass ``None`` to clear (next call rebuilds)."""
    global _settings
    _settings = new
