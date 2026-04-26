"""Plugin manifest schema.

A manifest is a YAML file at the root of each plugin describing how to launch it
and what it provides. The runtime reads manifests at discovery time and uses them
to spawn the plugin in its own uv-managed venv.

``Manifest`` is the on-disk shape (immutable). ``LoadedManifest`` pairs a manifest
with the resolved directory it lives in — this avoids mutating a Pydantic model
post-construction.
"""
from __future__ import annotations

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
    """The contents of ``manifest.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    description: str = ""
    runtime: Literal["uv"] = "uv"
    python: str = "3.11"
    entrypoint_module: str = Field(
        ...,
        description="Module to run with `python -m <entrypoint_module>`",
    )
    hardware: Hardware = Field(default_factory=Hardware)


@dataclass(frozen=True)
class LoadedManifest:
    """A discovered manifest paired with its source directory."""

    manifest: Manifest
    project_dir: Path

    @classmethod
    def load(cls, plugin_dir: Path) -> Self:
        yaml_path = plugin_dir / "manifest.yaml"
        if not yaml_path.is_file():
            raise FileNotFoundError(f"missing manifest: {yaml_path}")
        data = yaml.safe_load(yaml_path.read_text())
        return cls(
            manifest=Manifest.model_validate(data),
            project_dir=plugin_dir.resolve(),
        )

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def version(self) -> str:
        return self.manifest.version
