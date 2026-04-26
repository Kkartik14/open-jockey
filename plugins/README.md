# open-jockey · plugins

Each analyzer (and stem separator) is a **plugin**: a separate Python project running in its own [`uv`](https://docs.astral.sh/uv/)-managed venv, talking to the host backend over **newline-delimited JSON-RPC on stdio**.

Why subprocess isolation:

- **Dependency conflicts** — `madmom` wants old NumPy, `allin1` wants newer. They cannot share a single env.
- **Native libs** — `essentia` ships with C++ bindings; isolating the install keeps the host clean.
- **Licensing** — some bundled models are non-commercial. Easier to ring-fence in a dedicated env.
- **Crash isolation** — a segfault in one analyzer doesn't take down the API.

## Directory layout

One subdirectory per plugin. `echo/` is the canonical reference:

```
plugins/
├── README.md            # this file
└── echo/
    ├── manifest.yaml    # how the host launches it
    ├── pyproject.toml   # the plugin's own Python project
    └── echo_plugin/
        ├── __init__.py
        └── __main__.py  # JSON-RPC stdio loop
```

The directory **name** under `plugins/` is incidental — what matters is `manifest.yaml`. The host's discovery walks `plugins/*/manifest.yaml`.

## Manifest

`manifest.yaml` is loaded into a frozen Pydantic `Manifest`:

| Field | Required | Default | Purpose |
| --- | --- | --- | --- |
| `name` | yes | — | unique plugin identifier (used in API paths) |
| `version` | yes | — | semver-ish |
| `description` | no | `""` | shown in the UI plugins list |
| `runtime` | no | `uv` | only `uv` is supported in v0 |
| `python` | no | `"3.11"` | Python version for the plugin's venv (uv installs it) |
| `entrypoint_module` | yes | — | host runs `python -m <entrypoint_module>` |
| `hardware.cpu_cores` | no | `1` | declared budget; not yet enforced |
| `hardware.ram_mb` | no | `512` | declared budget; not yet enforced |
| `hardware.gpu` | no | `none` | `required` / `optional` / `none` |

Example:

```yaml
name: echo
version: 0.1.0
description: Trivial echo plugin — RPC smoke test
runtime: uv
python: "3.11"
entrypoint_module: echo_plugin
hardware:
  cpu_cores: 1
  ram_mb: 64
  gpu: none
```

## JSON-RPC protocol

The host writes one JSON object per line to your stdin and expects one JSON object per line on your stdout. Anything you write to stderr is captured by the host into `.aidj/logs/plugin-<name>.log`.

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

The host raises a `PluginError(code, message, trace)` for any error response and surfaces it as HTTP 500 with the same fields.

### Reserved methods

Every plugin should implement these as a baseline so the host can poll/probe:

- `info` → `{"name": "<name>", "version": "<version>"}`
- `ping` → `"pong"`

Beyond that, methods are plugin-specific (e.g., an `allin1` plugin will expose `analyze` taking `{ "audio_path": "..." }`).

## Lifecycle

- **Lazy spawn.** The plugin process is not started when the host boots. It's spawned on the first `call()` to that plugin and reused for subsequent calls.
- **Persistent process.** A single subprocess handles many requests; you do not pay startup cost per call.
- **Restart on crash.** If your process exits (or your plugin process is `stop()`-ed), the next `call()` starts a fresh one.
- **Graceful shutdown.** When the host shuts down it closes your stdin, gives you 5 seconds to exit cleanly, then SIGKILLs.

## How the host launches you

```bash
uv run \
  --project /path/to/plugins/<your-plugin-dir> \
  --python <manifest.python> \
  python -u -m <manifest.entrypoint_module>
```

`uv` creates and caches the venv on the first run; subsequent runs reuse it.

## Add a new plugin in 5 steps

Using the `echo` plugin as the template, here's the minimum work for a new plugin called `foobar`:

1. **Scaffold**

   ```bash
   mkdir -p plugins/foobar/foobar_plugin
   touch plugins/foobar/{manifest.yaml,pyproject.toml}
   touch plugins/foobar/foobar_plugin/{__init__.py,__main__.py}
   ```

2. **`plugins/foobar/manifest.yaml`**

   ```yaml
   name: foobar
   version: 0.1.0
   description: Does the foobar thing
   python: "3.11"
   entrypoint_module: foobar_plugin
   hardware: { cpu_cores: 2, ram_mb: 1024, gpu: optional }
   ```

3. **`plugins/foobar/pyproject.toml`**

   ```toml
   [project]
   name = "foobar-plugin"
   version = "0.1.0"
   requires-python = ">=3.11"
   dependencies = ["numpy>=2.0"]   # whatever you actually need

   [build-system]
   requires = ["hatchling"]
   build-backend = "hatchling.build"

   [tool.hatch.build.targets.wheel]
   packages = ["foobar_plugin"]
   ```

4. **`plugins/foobar/foobar_plugin/__main__.py`** — the stdio loop. Steal `plugins/echo/echo_plugin/__main__.py` and replace `handle()`:

   ```python
   def handle(method, params):
       if method == "info":   return {"name": "foobar", "version": "0.1.0"}
       if method == "ping":   return "pong"
       if method == "analyze": return _real_work(params["audio_path"])
       raise ValueError(f"unknown method: {method}")
   ```

5. **First call.** Restart the backend (or hit any plugin route — the registry is lazy). The host will discover `foobar` from its manifest, spin up its venv on first use, and route `POST /api/plugins/foobar/call` to it.

## Don't

- **Don't print to stdout.** Stdout is the RPC channel. Use stderr (or `logging`) for diagnostics — the host captures it.
- **Don't catch and silence exceptions.** Let them bubble to the stdio loop so the host gets a JSON-RPC error response with the trace.
- **Don't share state across requests via globals expecting concurrency.** The host serialises calls per plugin; you'll get one request at a time, but the process is long-lived.
- **Don't write to `.aidj/`.** That belongs to the host. Use the host's cache by returning paths or accepting them as `params`.
