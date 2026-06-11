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


def test_concurrency_safe_defaults_to_false(tmp_aidj) -> None:
    """Existing plugins (echo, allin1) don't set concurrency_safe → False."""
    by_name = {m.name: m for m in registry().manifests()}
    assert by_name["echo"].manifest.concurrency_safe is False
    assert by_name["allin1"].manifest.concurrency_safe is False


def test_allin1_remote_manifest_declares_concurrency_safe(tmp_aidj) -> None:
    """The Modal-backed plugin opts in to concurrency_safe for the future
    parallelism push."""
    by_name = {m.name: m for m in registry().manifests()}
    assert "allin1_remote" in by_name
    assert by_name["allin1_remote"].manifest.concurrency_safe is True
    # GPU is 'none' locally — the actual GPU lives on Modal, not on this machine.
    assert by_name["allin1_remote"].manifest.hardware.gpu == "none"


def test_manifest_default_timeout_sec(tmp_aidj) -> None:
    by_name = {m.name: m for m in registry().manifests()}
    # Echo doesn't declare it → default of 60.
    assert by_name["echo"].manifest.default_timeout_sec == 60.0
    # Heavy analyzers should have generous timeouts.
    assert by_name["allin1"].manifest.default_timeout_sec == 600.0
    assert by_name["allin1_remote"].manifest.default_timeout_sec == 600.0


def test_plugin_uses_manifest_timeout_by_default(tmp_aidj) -> None:
    """``Plugin.default_timeout`` should follow the manifest's declared value."""
    assert registry().get("echo").default_timeout == 60.0
    assert registry().get("allin1").default_timeout == 600.0
    assert registry().get("allin1_remote").default_timeout == 600.0


def test_cloud_audio_field_defaults(tmp_aidj) -> None:
    by_name = {m.name: m for m in registry().manifests()}
    assert by_name["echo"].manifest.cloud_audio is False
    assert by_name["allin1"].manifest.cloud_audio is False
    assert by_name["allin1_remote"].manifest.cloud_audio is True


def test_essentia_plugin_discovered(tmp_aidj) -> None:
    """Key-detection plugin scaffolded in plugins/essentia/ is picked up."""
    by_name = {m.name: m for m in registry().manifests()}
    assert "essentia" in by_name
    assert by_name["essentia"].manifest.cloud_audio is False
    assert by_name["essentia"].manifest.default_timeout_sec == 300.0


def test_librosa_plugin_discovered(tmp_aidj) -> None:
    """Baseline beat-tracker scaffolded in plugins/librosa/ — gives the
    bake-off a real comparison candidate against allin1 (allin1_remote runs
    the same model, so it doesn't count)."""
    by_name = {m.name: m for m in registry().manifests()}
    assert "librosa" in by_name
    assert by_name["librosa"].manifest.cloud_audio is False
    assert by_name["librosa"].manifest.default_timeout_sec == 300.0
    # Local CPU plugin — declares no GPU.
    assert by_name["librosa"].manifest.hardware.gpu == "none"


def test_madmom_msaf_plugin_disabled_via_renamed_manifest(tmp_aidj) -> None:
    """madmom_msaf is intentionally quarantined (manifest renamed); discovery
    must skip it. If a future change re-enables the plugin this test will fail
    and we'll know to run the bake-off methodology against it."""
    by_name = {m.name: m for m in registry().manifests()}
    assert "madmom_msaf" not in by_name


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
