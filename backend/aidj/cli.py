"""``aidj`` CLI — minimal entry points for ops tasks."""
from __future__ import annotations

import argparse
import logging
import sys

from aidj.logging_config import setup as setup_logging

log = logging.getLogger("aidj.cli")


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "aidj.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    from aidj.config import settings
    from aidj.plugins.registry import registry
    from aidj.store import db

    s = settings()
    s.ensure_dirs()
    db.get_conn()
    print(f"project_root: {s.project_root}")
    print(f"store_root:   {s.store_root}")
    print(f"db_path:      {s.db_path}")
    print(f"plugins_root: {s.plugins_root}")
    print("schema:       initialized")
    print("plugins:")
    for lm in registry().manifests():
        m = lm.manifest
        print(f"  - {m.name}@{m.version} ({m.entrypoint_module})")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()

    ap = argparse.ArgumentParser(prog="aidj")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="Run the FastAPI server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_info = sub.add_parser("info", help="Print store + plugin info")
    p_info.set_defaults(func=cmd_info)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
