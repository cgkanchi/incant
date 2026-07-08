"""Incant CLI: init, seed, serve."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="incant", description="Incant prompt platform")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="initialize the repo + database")
    sub.add_parser("seed", help="seed the example dataset")
    serve = sub.add_parser("serve", help="run the API + UI server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "init":
        from .service import AppContext
        AppContext().initialize()
        print("Initialized repo + database.")
        return 0

    if args.cmd == "seed":
        from .seed import seed
        key = seed()
        print("Seeded example dataset.")
        print("Renderer service key (support/prod):", key)
        return 0

    if args.cmd == "serve":
        import uvicorn
        from .config import get_settings
        s = get_settings()
        uvicorn.run(
            "incant.server:app",
            host=args.host or s.host,
            port=args.port or s.port,
            reload=args.reload,
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
