# aidj-plugin-sdk

Tiny helper for writing aidj plugins. Provides the JSON-RPC stdio loop so plugins only implement domain logic.

```python
from importlib.metadata import version
from aidj_plugin_sdk import serve

INFO = {"name": "myplugin", "version": version("myplugin")}


def handle(method: str, params: dict) -> object:
    if method == "analyze":
        return _do_work(params)
    raise ValueError(f"unknown method: {method}")


if __name__ == "__main__":
    serve(handle, info=INFO)
```

The SDK handles the reserved `info` and `ping` methods, parse errors, and exception → JSON-RPC error mapping with stack traces.

Each plugin depends on this package via a uv path source (see `plugins/README.md`).
