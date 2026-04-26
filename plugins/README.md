# open-jockey · plugins

Each analyzer (and stem separator) is a **plugin**: a separate Python project running in its own [`uv`](https://docs.astral.sh/uv/)-managed venv, talking to the host backend over **newline-delimited JSON-RPC on stdio**.

Why subprocess isolation:

- **Dependency conflicts** — `madmom` wants old NumPy, `allin1` wants newer. They cannot share a single env.
- **Native libs** — `essentia` ships with C++ bindings; isolating the install keeps the host clean.
- **Licensing** — some bundled models are non-commercial. Easier to ring-fence in a dedicated env.
- **Crash isolation** — a segfault in one analyzer doesn't take down the API.

Plugins do **not** implement the JSON-RPC stdio loop themselves. They depend on `aidj-plugin-sdk` (a tiny package at `../plugin_sdk/`) and call `serve(handle, info=...)`. The SDK handles parsing, the reserved `info`/`ping` methods, and exception → JSON-RPC error mapping with stack traces.

## Directory layout

One subdirectory per plugin. `echo/` is the canonical reference:

```
plugins/
├── README.md            # this file
└── echo/
    ├── manifest.yaml    # how the host launches it
    ├── pyproject.toml   # the plugin's own Python project (single source of truth for version)
    ├── uv.lock
    └── echo_plugin/
        ├── __init__.py
        └── __main__.py  # 15 lines — implements only handle()
```

The directory **name** under `plugins/` is incidental — what matters is `manifest.yaml`. The host's discovery walks `plugins/*/manifest.yaml`.

## Manifest

`manifest.yaml` is loaded into a frozen Pydantic `Manifest`:

| Field | Required | Default | Purpose |
| --- | --- | --- | --- |
| `name` | yes | — | unique plugin identifier (used in API paths) |
| `description` | no | `""` | shown in the UI plugins list |
| `runtime` | no | `uv` | only `uv` is supported in v0 |
| `python` | no | `"3.11"` | Python version for the plugin's venv (uv installs it) |
| `entrypoint_module` | yes | — | host runs `python -m <entrypoint_module>` |
| `hardware.cpu_cores` | no | `1` | declared budget; not yet enforced |
| `hardware.ram_mb` | no | `512` | declared budget; not yet enforced |
| `hardware.gpu` | no | `none` | `required` / `optional` / `none` |

**There is no `version` field on the manifest.** The plugin's version is read from its own `pyproject.toml` `[project].version` at discovery time — the pyproject is the single source of truth. This eliminates drift between manifest YAML, pyproject, and any constant in `__init__.py`.

Example:

```yaml
name: echo
description: Trivial echo plugin — RPC smoke test
runtime: uv
python: "3.11"
entrypoint_module: echo_plugin
hardware:
  cpu_cores: 1
  ram_mb: 64
  gpu: none
```

## Reserved methods (provided by the SDK)

Every plugin gets these for free without writing a line:

- `info` → returns the dict you passed to `serve(..., info=...)` (must contain at least `name` and `version`)
- `ping` → returns `"pong"`

The host calls them for health/probe purposes.

## Domain methods

Whatever you implement in `handle(method, params)`. Example for an analyzer plugin:

```python
def handle(method, params):
    if method == "analyze":
        return analyze_track(params["audio_path"])
    raise ValueError(f"unknown method: {method}")
```

Raising any exception produces a JSON-RPC error response with the message and full traceback.

## Per-call timeouts (host side)

The host enforces per-call timeouts. If your plugin hangs (e.g., a slow Demucs run that exceeds budget), the host **kills the subprocess** and raises `PluginError(code=-32001, message="…timed out after …s on '<method>'")`. The next `call()` boots a fresh process.

The HTTP layer maps this to `504 Gateway Timeout`. Override the default with the `timeout` field in `POST /api/plugins/{name}/call`:

```json
{ "method": "analyze", "params": {...}, "timeout": 600.0 }
```

`null`/omitted → use the plugin's default (60s).

## Lifecycle

- **Lazy spawn.** The plugin process is not started when the host boots. It's spawned on the first `call()` and reused for subsequent calls.
- **Persistent process.** A single subprocess handles many requests; you do not pay startup cost per call.
- **Restart on crash or timeout.** If your process exits or is force-killed, the next `call()` starts a fresh one.
- **Graceful shutdown.** When the host shuts down it closes your stdin, gives you 5 seconds to exit cleanly, then SIGKILLs.

## Sandbox-friendly uv cache

By default the host points each plugin's subprocess at a project-local uv cache (`<project_root>/.aidj/uv-cache`) via `UV_CACHE_DIR`. This means plugins do not require access to the user's `~/.cache/uv` — important in CI / sandboxed environments. Override per-process via `AIDJ_UV_CACHE_DIR=...`.

## How the host launches you

```bash
UV_CACHE_DIR=<project_root>/.aidj/uv-cache uv run \
  --project /path/to/plugins/<your-plugin-dir> \
  --python <manifest.python> \
  python -u -m <manifest.entrypoint_module>
```

`uv` creates and caches the venv on the first run; subsequent runs reuse it.

## Add a new plugin in 5 steps

Using `echo` as the template, here's the minimum work for a new plugin called `foobar`:

1. **Scaffold**

   ```bash
   mkdir -p plugins/foobar/foobar_plugin
   touch plugins/foobar/{manifest.yaml,pyproject.toml}
   touch plugins/foobar/foobar_plugin/{__init__.py,__main__.py}
   ```

2. **`plugins/foobar/manifest.yaml`** — note: no `version` field.

   ```yaml
   name: foobar
   description: Does the foobar thing
   python: "3.11"
   entrypoint_module: foobar_plugin
   hardware: { cpu_cores: 2, ram_mb: 1024, gpu: optional }
   ```

3. **`plugins/foobar/pyproject.toml`** — pyproject is the version source.

   ```toml
   [project]
   name = "foobar-plugin"
   version = "0.1.0"
   requires-python = ">=3.11"
   dependencies = [
       "aidj-plugin-sdk",
       "numpy>=2.0",          # whatever your analyzer needs
   ]

   [build-system]
   requires = ["hatchling"]
   build-backend = "hatchling.build"

   [tool.hatch.build.targets.wheel]
   packages = ["foobar_plugin"]

   [tool.uv.sources]
   aidj-plugin-sdk = { path = "../../plugin_sdk", editable = true }
   ```

4. **`plugins/foobar/foobar_plugin/__main__.py`** — only the dispatcher, no stdio loop:

   ```python
   from importlib.metadata import version
   from aidj_plugin_sdk import serve

   INFO = {"name": "foobar", "version": version("foobar-plugin")}

   def handle(method, params):
       if method == "analyze":
           return _real_work(params["audio_path"])
       raise ValueError(f"unknown method: {method}")

   if __name__ == "__main__":
       serve(handle, info=INFO)
   ```

5. **First call.** Restart the backend (or hit any plugin route — the registry is lazy). The host will discover `foobar` from its manifest, spin up its venv on first use, and route `POST /api/plugins/foobar/call` to it.

## Don't

- **Don't print to stdout.** Stdout is the RPC channel. Use stderr (or stdlib `logging`) for diagnostics — the host captures stderr to `<project_root>/.aidj/logs/plugin-<name>.log` and includes the tail in startup-failure error messages.
- **Don't catch and silence exceptions.** Let them bubble — the SDK turns them into JSON-RPC error responses with the stack trace.
- **Don't share state across requests assuming concurrency.** The host serialises calls per plugin; you'll get one request at a time, but the process is long-lived (so process-local caches do help across calls).
- **Don't write to `.aidj/`.** That belongs to the host. Use the host's cache by returning paths or accepting them as `params`.
- **Don't put `version` in `manifest.yaml`.** The host reads it from your `pyproject.toml`. Keeping it in only one place is the whole point.

## Wire-level protocol (appendix — only relevant if you're not using the SDK)

The SDK encapsulates the protocol below. Documented here for transparency.

### Request (host → plugin)

```json
{"id": 17, "method": "echo", "params": {"hi": "there"}}
```

### Response (plugin → host)

Success:

```json
{"id": 17, "result": {"echo": {"hi": "there"}}}
```

Failure:

```json
{
  "id": 17,
  "error": {
    "code": -1,
    "message": "unknown method: foobar",
    "trace": "Traceback (most recent call last)..."
  }
}
```

Error codes used by the host:

| Code | Meaning |
| --- | --- |
| `-32700` | parse error (plugin received malformed JSON) |
| `-32001` | timeout (host-side; plugin process force-killed) |
| `-32002` | plugin failed to start (uv install error etc.) |
| `-32099` | plugin exited or stdin closed before responding |
| `-1` | application error from `handle()` |
