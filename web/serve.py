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
import os
import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Static handler supporting HTTP Range requests for local DuckDB-Wasm Parquet reads."""

    def send_head(self):
        range_header = self.headers.get("Range")
        if not range_header:
            return super().send_head()

        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().send_head()

        m = RANGE_RE.match(range_header.strip())
        if not m:
            return super().send_head()

        file_size = os.path.getsize(path)
        start_s, end_s = m.groups()
        if start_s == "" and end_s == "":
            return super().send_head()
        if start_s == "":
            length = int(end_s)
            start = max(0, file_size - length)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
            end = min(end, file_size - 1)

        if start > end or start >= file_size:
            self.send_error(416, "Requested Range Not Satisfiable")
            return None

        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._range_remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_range_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        self._range_remaining = None
        bufsize = 64 * 1024
        while remaining > 0:
            chunk = source.read(min(bufsize, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the static Bio&Med-Rxiv web dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080).")
    args = parser.parse_args()

    handler = functools.partial(RangeRequestHandler, directory=str(WEB_DIR))
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving web dashboard from {WEB_DIR} at http://{args.host}:{args.port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
