# open-jockey

> A personal, local-first AI DJ — *in active development*. Goal: point it at a folder of music, get back a continuous DJ-style mix, with audio never leaving your machine. **Today the analyzer/labeling/profile layers exist; the mixing layers (Candidate Graph, Planner, Renderer) do not. The tool cannot produce a mix yet.**

The design splits the work into four layers (see [How it works](#how-it-works)). When complete, open-jockey will do the analysis, planning, and rendering locally so a folder of songs becomes a continuous set without manual beatmatching, cue points, or transition planning. The diagram below describes the *design target*; the [Status](#status) and [Roadmap](#roadmap) sections describe what's actually built today.

## Why local-first

- **No streaming integration** (Spotify, Apple Music, AudioShake, Music AI, etc.). Your library stays where it is.
- **No cloud audio processing.** Beat detection, key detection, stem separation, time-stretching, EQ, and rendering all run on your machine.
- **An LLM is used only for high-level planning** — it sees structured metadata about each track (BPM, key, energy curve, sections) and decides ordering and transitions. It never sees the audio. Provider-neutral; v1 ships with Claude.

## Status

**Phase 0 + Phase 1 done. Phase 2 partly done. Phases 3–7 not started.**

What's actually built today:

- ✅ Phase 0 foundation — project store (SQLite + content-addressed cache), plugin runtime (uv-managed subprocess per plugin), FastAPI app, React/Vite frontend, full pytest suite.
- ✅ Phase 1 analyzer pipeline — `BeatGridAnalysis` + `KeyAnalysis` schemas, atomic `analysis_runs` lifecycle (claim-token, force, stale recovery), analyze API + dual beat-grid UI with click-track verification, 8 structured failure-mode labels with per-analyzer / per-genre rollup. Plugins: `allin1` (local — currently broken on `madmom` import; quarantined behind `allin1_remote`), `allin1_remote` (Modal GPU, cloud-audio-gated), `librosa` (local baseline), `essentia` (locked, real-hardware run unverified). `madmom_msaf` quarantined.
- 🚧 Phase 2 — Canonical Track Intelligence (3 of 8 steps in): `TrackProfile` contract + provenance + readiness, `track_profiles` repository, deterministic profile builder, thin profile API (`GET/POST .../profile`, `GET /profiles/coverage`). **Still to do**: energy analyzer, lazy demucs + vocal windows, batch profile build, profile-coverage UI, label-driven analyzer selection. **No real-track listening test has been run yet** — see `private/plan.md`.

What's NOT built and what the architecture diagram below describes as *design intent* only:

- ❌ Phase 3 Transition Candidate Graph
- ❌ Phase 4 Renderer / automation envelopes (no mix has ever been produced)
- ❌ Phase 5 Planner (LLM or local heuristic)
- ❌ Phases 6–7 plan editing UI, polish

The `projects`, `candidates`, and `stems` SQLite tables exist as empty stubs to reserve the schema; no code populates them yet.

See [Roadmap](#roadmap) for the full table.

## How it works

**Design target (not all layers built — see [Status](#status)).** Four layers, with the **Transition Candidate Graph** acting as a hard contract between deterministic analysis and the non-deterministic LLM. Today only the Project Store and Analyzer (+ the in-progress canonical `TrackProfile` layer on top) exist; Candidate Graph, Planner, and Renderer are the still-to-do work:

```
                +------------------------------------+
                |  PROJECT STORE                     |
                |  SQLite + content-addressed cache  |
                |  jobs · model versions · projects  |
                +-----^---------^----------^---------+
                      |         |          |
+---------------------+--+   +--+----------+--+   +--+--------------+
|  ANALYZER              |-->| TRANSITION     |-->|  PLANNER (LLM)  |
|  - plugin per analyzer |   | CANDIDATE      |   |  - adapter      |
|  - own venv/subprocess |   | GRAPH          |   |  - picks among  |
|  - allin1, demucs, …   |   | nodes + edges  |   |    candidates   |
+------------------------+   +----------------+   +--------+--------+
                                                           |
                                                           v
                                                  +--------+--------+
                                                  |  RENDERER       |
                                                  |  automation     |
                                                  |  envelopes      |
                                                  +-----------------+
```

The LLM cannot invent cue bars or pick incompatible transitions, because the graph only contains feasible candidates the analyzer produced. The renderer compiles each chosen technique into deterministic parameter curves (gain, EQ, filter, delay, tempo) and executes them — no creative decisions in the audio path.

## Quickstart

### Prerequisites

- **Python 3.11–3.13** (uv installs it automatically on first run)
- **Node.js 20.19+ or 22.12+** recommended. Vite is pinned to v6 so older Node still works; upgrade Node to bump it to v7+.
- **[`uv`](https://docs.astral.sh/uv/)** for the Python project + per-plugin venvs.

### Install

```bash
cd backend  && uv sync     && cd ..
cd frontend && npm install && cd ..
```

### Run

In two terminals:

```bash
# terminal 1 — backend on http://127.0.0.1:8000
cd backend && uv run aidj serve --reload

# terminal 2 — frontend on http://127.0.0.1:5173 (proxies /api → :8000)
cd frontend && npm run dev
```

Open <http://127.0.0.1:5173>. The health badge goes green, discovered plugins show a `ping` button, and an ingest box accepts local file paths.

## Repo layout

```
open-jockey/
├── README.md                  # this file
├── .gitignore
├── backend/                   # Python backend  (see backend/README.md)
│   ├── pyproject.toml
│   ├── aidj/                  # the `aidj` Python package + CLI
│   └── tests/                 # pytest suite
├── frontend/                  # Vite + React + TS UI  (see frontend/README.md)
│   ├── package.json
│   └── src/
└── plugins/                   # one directory per plugin  (see plugins/README.md)
    └── echo/                  # trivial RPC smoke test
```

The Python package is named `aidj` — that's the import name and the CLI command. The repo is named `open-jockey`. They don't have to match.

Runtime data (SQLite DB, plugin logs, content-addressed cache) lives in `.aidj/` at the repo root and is gitignored. Safe to delete; it'll be recreated on next boot.

## Development

### Run the test suite

```bash
cd backend && uv run pytest -q
```

The backend suite is 160+ tests. It covers the store, migrations, API routes, plugin RPC, analyzer runs, labels, waveform peaks, and track-profile persistence.

### CLI

```bash
cd backend
uv run aidj info          # project root, store paths, registered plugins
uv run aidj serve         # FastAPI server
uv run aidj serve --reload --port 8000
```

### Layered docs

- [`backend/README.md`](backend/README.md) — Python project layout, repo functions, how to add a route or entity
- [`frontend/README.md`](frontend/README.md) — Vite/React stack, proxy setup, version pinning rationale
- [`plugins/README.md`](plugins/README.md) — manifest format, JSON-RPC contract, how to write a new analyzer

## Roadmap

| Phase | What | Status |
| --- | --- | --- |
| 0 | Project Store, plugin runtime, FastAPI app, frontend skeleton, test suite | done |
| 1 | Analyzer pipeline (schema/repo/API/plugin contract), `allin1` + `allin1_remote` + `librosa` + `essentia` plugins, dual beat-grid UI, click-track verification, structured failure-mode labels, per-analyzer + per-genre rollup | done (running the bake-off itself is user activity) |
| 2 | **Canonical Track Intelligence**: `TrackProfile` contract + persistence, deterministic profile builder, local energy analyzer, lazy demucs stem bake-off + vocal windows, batch profile build, profile-coverage UI, label-driven analyzer selection | in progress (step 1: contract + persistence) |
| 3 | Transition Candidate Graph: cue-point extraction, edge generation, scoring, pruning | |
| 4 | Renderer with automation envelopes, all transition techniques | |
| 5 | Planner: `LocalHeuristicPlanner` baseline + `AnthropicPlanner` with structured outputs + validator | |
| 6 | Plan editing UI, partial re-render | |
| 7 | LUFS normalisation, project save/load polish, GPU acceleration | |
