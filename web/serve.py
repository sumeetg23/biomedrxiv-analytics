#!/usr/bin/env python3
"""Lightweight local preview server for the static web dashboard (``web/``).

Usage:
    python3 web/serve.py
    python3 web/serve.py --port 8080
"""

from __future__ import annotations

import argparse
import functools
import http.server
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the static Bio&Med-Rxiv web dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080).")
    args = parser.parse_args()

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB_DIR))
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving web dashboard from {WEB_DIR} at http://{args.host}:{args.port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
