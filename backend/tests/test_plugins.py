"""Plugin runtime — discovery, RPC round-trip, error mapping, restart.

These tests spawn a real subprocess via uv, so they're slower than the rest of
the suite. The first run installs the plugin's tiny venv; subsequent runs reuse it.
"""
from __future__ import annotations

import pytest

from aidj.plugins.manifest import LoadedManifest
from aidj.plugins.registry import registry
from aidj.plugins.runtime import PluginError


def test_discovery_finds_echo(tmp_aidj) -> None:
    names = [m.name for m in registry().manifests()]
    assert "echo" in names


def test_loaded_manifest_carries_project_dir(tmp_aidj) -> None:
    [lm] = [m for m in registry().manifests() if m.name == "echo"]
    assert isinstance(lm, LoadedManifest)
    assert lm.project_dir.is_dir()
    assert (lm.project_dir / "manifest.yaml").is_file()


def test_echo_round_trip(tmp_aidj) -> None:
    p = registry().get("echo")
    info = p.call("info")
    assert info == {"name": "echo", "version": "0.1.0"}

    assert p.call("ping") == "pong"
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
