"""Echo plugin entrypoint — implements only domain methods. The SDK runs the loop."""
from __future__ import annotations

import time
from importlib.metadata import version
from typing import Any

from aidj_plugin_sdk import serve

INFO = {"name": "echo", "version": version("echo-plugin")}


def handle(method: str, params: dict[str, Any]) -> Any:
    if method == "echo":
        return {"echo": params}
    if method == "sleep":
        seconds = float(params.get("seconds", 1))
        time.sleep(seconds)
        return {"slept": seconds}
    raise ValueError(f"unknown method: {method}")


if __name__ == "__main__":
    serve(handle, info=INFO)
