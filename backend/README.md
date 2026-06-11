# open-jockey · backend

Python backend that owns the **Project Store**, the **plugin runtime**, and the **FastAPI app**. Imports as `aidj`. The CLI command is also `aidj`.

## Stack

- **Python 3.11–3.13** managed by [`uv`](https://docs.astral.sh/uv/)
- **FastAPI** + **uvicorn**
- **Pydantic v2** for every data shape (domain models, requests, responses)
- **SQLite** (stdlib `sqlite3`, WAL mode) for metadata
- **stdlib `subprocess`** for plugin isolation (one uv venv per plugin)

No ORM, no `aiosqlite`, no async DB — single-user app, sync is fine.

## Layout

```
backend/
├── pyproject.toml              # project + dev deps (uv-managed)
├── uv.lock                     # committed for reproducible installs
├── aidj/
│   ├── __init__.py             # exports __version__
│   ├── config.py               # Settings (env-overridable) + module-level singleton
│   ├── logging_config.py       # one place that configures stdlib logging
│   ├── cli.py                  # `aidj` CLI entry (argparse)
│   ├── api/
│   │   ├── __init__.py
│   │   └── main.py             # FastAPI app + every route
│   ├── store/                  # the Project Store
│   │   ├── __init__.py
│   │   ├── models.py           # Track, Job, AnalysisRun, JobStatus, AnalysisStatus
│   │   ├── db.py               # connection, schema bootstrap, fetch_one/fetch_all
│   │   ├── hashing.py          # streaming sha256 + derivation_key()
│   │   ├── cache.py            # content-addressed cache primitives
│   │   ├── tracks.py           # Track repository (ingest/get/list_all/delete)
│   │   └── jobs.py             # Job queue (enqueue/claim_next/complete/fail)
│   └── plugins/                # plugin runtime (host side)
│       ├── __init__.py
│       ├── manifest.py         # Manifest (frozen Pydantic) + LoadedManifest
│       ├── runtime.py          # Plugin (subprocess + JSON-RPC), discover_plugins
│       └── registry.py         # in-process registry, lazy plugin instantiation
└── tests/
    ├── conftest.py             # tmp_aidj fixture (isolated store, real plugins)
    ├── test_store.py
    ├── test_plugins.py
    └── test_api.py
```

## Install

```bash
cd backend
uv sync                # creates .venv/, installs project + deps from uv.lock
```

## Run

```bash
uv run aidj serve              # http://127.0.0.1:8000
uv run aidj serve --reload     # hot-reload during development
uv run aidj serve --port 8001  # bind to a different port
uv run aidj info               # print project root, store paths, registered plugins
```

## Tests

```bash
uv run pytest                  # full suite (~1.2s)
uv run pytest tests/test_store.py -v
uv run pytest -k "echo"        # only tests with "echo" in the name
```

The `tmp_aidj` fixture in `tests/conftest.py` builds a fully-isolated environment per test: temp project root, real `plugins/` symlinked in, fresh DB, every module-level singleton (settings, db connection, registry) reset before AND after the test.

## Configuration

`Settings` reads `AIDJ_*` env vars:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AIDJ_PROJECT_ROOT` | walks up from cwd looking for `.aidj/` then `.git`, falls back to cwd | repo root |
| `AIDJ_STORE_DIRNAME` | `.aidj` | runtime data dir under project root |
| `AIDJ_PLUGINS_DIRNAME` | `plugins` | where plugin manifests live |
| `AIDJ_UV_CACHE_DIR` | `<store_root>/uv-cache` | uv cache used when spawning plugin subprocesses (sandbox-friendly default; override to share with `~/.cache/uv`) |
| `AIDJ_LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |

## API surface

Open <http://127.0.0.1:8000/docs> while the server runs for the live OpenAPI spec. Every route declares a `response_model`; the spec is real and could feed a future codegen step.

| Method | Path | Response |
| --- | --- | --- |
| GET | `/api/health` | `HealthResponse` |
| GET | `/api/plugins` | `list[PluginInfo]` |
| POST | `/api/plugins/{name}/call` | `PluginCallResponse` |
| POST | `/api/tracks/ingest` | `Track` |
| GET | `/api/tracks` | `list[Track]` |
| POST | `/api/tracks/{hash}/analyze/{analyzer}` | `AnalysisRun` |
| GET | `/api/tracks/{hash}/analyses` | `list[AnalysisRun]` |
| GET | `/api/tracks/{hash}/analyses/{analyzer}` | `AnalysisRun` |
| GET | `/api/tracks/{hash}/profile` | `TrackProfile` |
| POST | `/api/tracks/{hash}/profile/build` | `TrackProfile` |
| GET | `/api/profiles/coverage` | `ProfileCoverageResponse` |
| POST | `/api/projects` | `Project` |
| GET | `/api/projects` | `list[Project]` |
| POST | `/api/projects/{id}/candidates/build` | `CandidateGraphBuildResult` |
| GET | `/api/projects/{id}/candidates` | `list[TransitionCandidate]` |
| POST | `/api/jobs` | `EnqueueResponse` |
| GET | `/api/jobs?status=…` | `list[Job]` |

The `analyze` endpoint:

- 404 if the track or analyzer doesn't exist
- 200 + `AnalysisRun(status="completed")` on success
- 200 + `AnalysisRun(status="failed", error=…)` on plugin error or timeout (the run is recorded; check `status`)
- `force=true` re-runs even when a completed row already exists
- `timeout` overrides the plugin default

Per-analyzer output schemas (e.g., `BeatGridAnalysis` for `allin1`) are defined in `aidj/store/models.py`. The repo stores raw JSON; the frontend interprets it based on `analyzer_name`.

`status` on `/api/jobs` is validated against the `JobStatus` enum (`queued`, `running`, `completed`, `failed`, `cancelled`). Unknown values return 422.

## Adding a new entity

The pattern is intentionally short. To add `Stem`:

1. **Model** — append to `aidj/store/models.py`:
   ```python
   class Stem(_ModelBase):
       id: int
       track_hash: str
       separator: str
       stem_name: str
       cache_key: str
       @classmethod
       def from_row(cls, row): return cls.model_validate(dict(row))
   ```
2. **Schema** — add the table to `SCHEMA_SQL` in `aidj/store/db.py`. Bump `SCHEMA_VERSION`.
3. **Repository** — new module `aidj/store/stems.py` mirroring `tracks.py`: small functions that take primitives, return `Stem` instances, use `db.execute / db.fetch_one / db.fetch_all`.
4. **Route** — in `aidj/api/main.py`, declare the route with `response_model=Stem` (or `list[Stem]`).
5. **Test** — fixture-driven test in `tests/test_store.py` and a `TestClient` test in `tests/test_api.py`.

There is no fourth copy of the wire shape anywhere.

## Linting

```bash
uv run ruff check
uv run ruff format
```

Ruff is configured in `pyproject.toml`. Line length 100, target py312, rules: pycodestyle / pyflakes / isort / bugbear / pyupgrade / simplify.
