"""The stdio JSON-RPC server loop. One ``serve()`` call per plugin process."""
from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable, Mapping
from typing import Any

Handler = Callable[[str, dict[str, Any]], Any]


def serve(handle: Handler, *, info: Mapping[str, Any]) -> None:
    """Run the request/response loop until stdin is closed.

    Parameters
    ----------
    handle:
        Plugin-specific dispatcher. Called with ``(method, params)``; whatever
        it returns is sent back as the JSON-RPC ``result``. Raise to produce a
        JSON-RPC ``error`` response (the message and traceback are forwarded).
    info:
        The dict returned for the reserved ``info`` method. Should at least
        contain ``name`` and ``version``.
    """
    info_dict = dict(info)
    sys.stderr.write(
        f"plugin {info_dict.get('name', '?')}@{info_dict.get('version', '?')} ready\n"
    )
    sys.stderr.flush()

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit({"id": None, "error": {"code": -32700, "message": f"parse error: {exc}"}})
            continue

        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        try:
            if method == "info":
                result: Any = info_dict
            elif method == "ping":
                result = "pong"
            else:
                result = handle(method, params)
        except Exception as exc:  # noqa: BLE001 — we want to forward all exceptions
            _emit(
                {
                    "id": req_id,
                    "error": {
                        "code": -1,
                        "message": str(exc),
                        "trace": traceback.format_exc(),
                    },
                }
            )
            continue

        _emit({"id": req_id, "result": result})


def _emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()
