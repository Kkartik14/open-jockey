"""aidj-plugin-sdk — the stdio JSON-RPC loop every aidj plugin uses.

Plugins implement ``handle(method, params) -> result`` and call
``serve(handle, info=...)``. The SDK takes care of:

- request parsing
- the reserved ``info`` and ``ping`` methods
- exception → JSON-RPC error mapping (with stack trace)
- stdout flushing per response

Use stderr (or stdlib ``logging``) for diagnostics — the host captures it into
the plugin's logfile.
"""
from aidj_plugin_sdk.serve import Handler, serve

__all__ = ["Handler", "serve"]
