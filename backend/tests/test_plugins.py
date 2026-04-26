"""Plugin runtime — discovery, RPC round-trip, error mapping, restart, timeout.

These tests spawn a real subprocess via uv, so they're slower than the rest of
the suite. The first run installs the plugin's tiny venv; subsequent runs reuse
it via the session-scoped ``shared_uv_cache``.
"""
from __future__ import annotations

import pytest

from aidj.plugins.manifest import LoadedManifest
from aidj.plugins.registry import registry
from aidj.plugins.runtime import PluginError


def test_discovery_finds_echo(tmp_aidj) -> None:
    names = [m.name for m in registry().manifests()]
    assert "echo" in names


def test_loaded_manifest_carries_project_dir_and_pyproject_version(tmp_aidj) -> None:
    [lm] = [m for m in registry().manifests() if m.name == "echo"]
    assert isinstance(lm, LoadedManifest)
    assert lm.project_dir.is_dir()
    assert (lm.project_dir / "manifest.yaml").is_file()
    # version comes from pyproject.toml, not the manifest.yaml
    assert lm.version == "0.1.0"


def test_info_method_served_by_sdk(tmp_aidj) -> None:
    p = registry().get("echo")
    assert p.call("info") == {"name": "echo", "version": "0.1.0"}


def test_ping_method_served_by_sdk(tmp_aidj) -> None:
    p = registry().get("echo")
    assert p.call("ping") == "pong"


def test_echo_method_round_trips_params(tmp_aidj) -> None:
    p = registry().get("echo")
    assert p.call("echo", {"hi": "there", "n": 7}) == {"echo": {"hi": "there", "n": 7}}


def test_unknown_method_raises_plugin_error(tmp_aidj) -> None:
    p = registry().get("echo")
    with pytest.raises(PluginError) as excinfo:
        p.call("does_not_exist")
    assert "unknown method" in str(excinfo.value)


def test_unknown_plugin_raises_keyerror(tmp_aidj) -> None:
    with pytest.raises(KeyError):
        registry().get("nonexistent")


def test_plugin_restarts_after_stop(tmp_aidj) -> None:
    p = registry().get("echo")
    assert p.call("ping") == "pong"
    assert p.is_alive

    p.stop()
    assert not p.is_alive

    # next call boots it again
    assert p.call("ping") == "pong"
    assert p.is_alive


def test_call_timeout_kills_plugin_and_next_call_restarts(tmp_aidj) -> None:
    p = registry().get("echo")
    # Boot it once so the slow path is just the sleep, not also venv setup.
    assert p.call("ping") == "pong"

    with pytest.raises(PluginError) as excinfo:
        p.call("sleep", {"seconds": 5}, timeout=0.5)
    assert excinfo.value.code == -32001
    assert "timed out" in str(excinfo.value).lower()
    assert not p.is_alive  # killed

    # next call gets a fresh process
    assert p.call("ping") == "pong"
    assert p.is_alive


def test_per_call_timeout_overrides_default(tmp_aidj) -> None:
    p = registry().get("echo")
    p.default_timeout = 30.0
    assert p.call("ping") == "pong"  # warm up

    with pytest.raises(PluginError) as excinfo:
        p.call("sleep", {"seconds": 2}, timeout=0.2)
    assert excinfo.value.code == -32001
