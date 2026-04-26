"""Entrypoint for the echo plugin.

Reads newline-delimited JSON-RPC requests from stdin, writes responses to stdout.
Logs/diagnostics go to stderr (the host captures it to a file).

This is the canonical pattern for every aidj plugin: implement ``handle(method,
params) -> result`` and the boilerplate stays the same.
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any

from echo_plugin import VERSION


def handle(method: str, params: dict[str, Any]) -> Any:
    if method == "info":
        return {"name": "echo", "version": VERSION}
    if method == "echo":
        return {"echo": params}
    if method == "ping":
        return "pong"
    raise ValueError(f"unknown method: {method}")


def main() -> None:
    sys.stderr.write(f"echo plugin v{VERSION} ready\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stdout.write(json.dumps({"id": None, "error": {"code": -32700, "message": f"parse error: {exc}"}}) + "\n")
            sys.stdout.flush()
            continue
        try:
            result = handle(req.get("method", ""), req.get("params") or {})
            resp = {"id": req.get("id"), "result": result}
        except Exception as exc:
            resp = {
                "id": req.get("id"),
                "error": {
                    "code": -1,
                    "message": str(exc),
                    "trace": traceback.format_exc(),
                },
            }
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
