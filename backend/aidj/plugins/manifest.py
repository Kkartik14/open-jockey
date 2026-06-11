"""Plugin manifest schema.

A manifest is a YAML file at the root of each plugin describing how to launch it
and what it provides. The runtime reads manifests at discovery time and uses them
to spawn the plugin in its own uv-managed venv.

``Manifest`` is the on-disk YAML shape (immutable). ``LoadedManifest`` pairs a
manifest with the resolved directory it lives in *and* the version read from
the plugin's own ``pyproject.toml`` — that's the single source of truth for a
plugin's version, so manifest YAML and ``__init__.py`` constants can't drift.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Hardware(BaseModel):
    model_config = ConfigDict(frozen=True)

    cpu_cores: int = 1
    ram_mb: int = 512
    gpu: Literal["required", "optional", "none"] = "none"


class Manifest(BaseModel):
    """The contents of ``manifest.yaml``. Version comes from pyproject, not here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    runtime: Literal["uv"] = "uv"
    python: str = "3.11"
    entrypoint_module: str = Field(
        ...,
        description="Module to run with `python -m <entrypoint_module>`",
    )
    hardware: Hardware = Field(default_factory=Hardware)
    concurrency_safe: bool = Field(
        default=False,
        description=(
            "True if this plugin can serve multiple concurrent calls without "
            "interfering with itself (e.g. it delegates to a remote service). "
            "Reserved for a future host-side parallelism push; declare it now "
            "so plugins don't need a manifest bump later."
        ),
    )
    default_timeout_sec: float = Field(
        default=60.0,
        gt=0,
        description=(
            "Default per-call timeout in seconds. Heavy analyzers (allin1, "
            "Demucs, Modal-backed) should declare a generous value here so "
            "long inferences don't fail at the host's stricter default."
        ),
    )
    cloud_audio: bool = Field(
        default=False,
        description=(
            "True when this plugin uploads audio bytes to a non-local service "
            "(Modal, etc.). The analyze route refuses to invoke such plugins "
            "unless AIDJ_ALLOW_CLOUD_AUDIO=1 is set in the backend's env — "
            "explicit opt-in for the local-first → user-controlled boundary."
        ),
    )


@dataclass(frozen=True)
class LoadedManifest:
    """A discovered manifest paired with its source directory and resolved version."""

    manifest: Manifest
    project_dir: Path
    version: str

    @classmethod
    def load(cls, plugin_dir: Path) -> Self:
        yaml_path = plugin_dir / "manifest.yaml"
        pyproject_path = plugin_dir / "pyproject.toml"
        if not yaml_path.is_file():
            raise FileNotFoundError(f"missing manifest: {yaml_path}")
        if not pyproject_path.is_file():
            raise FileNotFoundError(
                f"plugin needs a pyproject.toml (used as the version source): {pyproject_path}"
            )

        manifest = Manifest.model_validate(yaml.safe_load(yaml_path.read_text()))

        with pyproject_path.open("rb") as f:
            pyproject = tomllib.load(f)
        version = pyproject.get("project", {}).get("version")
        if not isinstance(version, str) or not version:
            raise ValueError(
                f"missing or invalid [project].version in {pyproject_path}; "
                "the plugin's pyproject is the single source of truth for version"
            )

        return cls(
            manifest=manifest,
            project_dir=plugin_dir.resolve(),
            version=version,
        )

    @property
    def name(self) -> str:
        return self.manifest.name
