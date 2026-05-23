# open-jockey

> A personal, local-first AI DJ. Point it at a folder of music, get back a continuous DJ-style mix. Audio never leaves your machine.

Most people have music they love but don't have the time, ear, or DJ training to turn it into a proper mix. open-jockey does the analysis, planning, and rendering locally so a folder of songs becomes a continuous set without manual beatmatching, cue points, or transition planning.

## Why local-first

- **No streaming integration** (Spotify, Apple Music, AudioShake, Music AI, etc.). Your library stays where it is.
- **No cloud audio processing.** Beat detection, key detection, stem separation, time-stretching, EQ, and rendering all run on your machine.
- **An LLM is used only for high-level planning** — it sees structured metadata about each track (BPM, key, energy curve, sections) and decides ordering and transitions. It never sees the audio. Provider-neutral; v1 ships with Claude.

## Status

**Phase 1 done, Phase 2 in progress.**

- ✅ Phase 0 foundation (project store, plugin runtime, FastAPI app, frontend).
- ✅ Phase 1: `BeatGridAnalysis` + `KeyAnalysis` schemas, `analysis_runs` repo, analyze API routes, plugins (`allin1`, `allin1_remote`, `librosa`, `essentia`; `madmom_msaf` quarantined), dual beat-grid UI with click-track verification, structured failure-mode labels, per-analyzer + per-genre rollup table. Running the bake-off on real tracks is user activity.
- 🚧 Phase 2 — Canonical Track Intelligence: contract + `track_profiles` persistence landing now (step 1 of 8). Profile builder, energy analyzer, lazy demucs + vocal windows, batch profile build, and profile-coverage UI follow.

See [Roadmap](#roadmap) for the full table.

## How it works

Four layers, with the **Transition Candidate Graph** acting as a hard contract between deterministic analysis and the non-deterministic LLM:

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
