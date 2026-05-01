# `madmom_msaf` — quarantined

This plugin is **disabled**. The manifest has been renamed to
`manifest.yaml.disabled` so the host's discovery walks past it.

## Why

`madmom` (last release 0.16.1, January 2018) depends on Cython at *build* time
but doesn't declare it as a build-system requirement. On modern Python /
modern uv:

```
ModuleNotFoundError: No module named 'Cython'
  in madmom/setup.py:12
```

Workarounds attempted:

1. Add `Cython` + `numpy<2` to runtime deps and set
   `tool.uv.no-build-isolation-package = ["madmom"]`. Doesn't help because uv
   can't pre-install Cython into the build env during the *resolve* phase —
   madmom's setup.py runs before the resolver finishes.
2. (Not yet attempted) Build madmom from source against a pre-prepared
   Cython+numpy<2 venv, vendor the wheel locally, point uv at it. Possible
   but involves baking platform-specific wheels per developer machine.
3. (Not yet attempted) Patch a fork of madmom with a real `pyproject.toml`.

Per PLAN.md's flag — *"madmom may not survive contact with modern Python"* —
this is the expected outcome. The empirical bake-off principle applies:
the failure itself is data.

## Consequence for the bake-off

We don't currently have a working second beat-detection plugin. Phase 1's
analyzer bake-off needs at least two real candidates; today we have only
`allin1_remote` (Modal-backed). Candidates worth trying instead:

- [`beat_this`](https://github.com/CPJKU/beat_this) — modern, Python 3.11+ friendly
- [`BeatNet`](https://github.com/mjhydri/BeatNet) — stack of 4 RNNs; no madmom dep
- `librosa.beat.beat_track` — slowest path; baseline only
- A maintained fork of madmom (none on PyPI as of 2026-04)

## To revive this plugin

1. Get `madmom` to import without errors in a Python 3.11 venv (likely
   requires patching its setup.py and pinning numpy<2).
2. `mv manifest.yaml.disabled manifest.yaml` so discovery picks it up.
3. `uv lock` and confirm it resolves.
