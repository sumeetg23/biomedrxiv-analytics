#!/usr/bin/env python3
"""Materialize cleaned Parquet tables and generate the interactive web dashboard.

Usage:
    # Full pipeline: clean CSV -> Parquet tables -> interactive dashboard in docs/
    .venv/bin/python scripts/build_report.py

    # Rebuild dashboard quickly from existing canonical Parquet (skips 1.3 GB CSV pass)
    .venv/bin/python scripts/build_report.py --skip-parquet

    # Build and launch a local preview server
    .venv/bin/python scripts/build_report.py --skip-parquet --serve --port 8080
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from biomedrxiv.config import ProjectPaths
from biomedrxiv.dashboard_template import write_static_dashboard
from biomedrxiv.reporting import build_dashboard_payload, materialize_parquet_tables


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize cleaned Parquet tables and build the interactive web dashboard."
    )
    parser.add_argument(
        "--skip-parquet",
        action="store_true",
        help="Skip CSV -> Parquet materialization and rebuild dashboard from existing Parquet.",
    )
    parser.add_argument(
        "--skip-versions-parquet",
        action="store_true",
        help="Only materialize canonical DOI-level Parquet (faster, uses less disk).",
    )
    parser.add_argument(
        "--chunksize",
        type=int,
        default=75_000,
        help="CSV chunk size for bounded-memory processing (default: 75000).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "docs",
        help="Directory for static dashboard output (default: docs/ for GitHub Pages).",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start a local HTTP preview server after building the dashboard.",
    )
    parser.add_argument("--port", type=int, default=8080, help="Local preview port (default: 8080).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    logger = logging.getLogger("biomedrxiv.build_report")

    paths = ProjectPaths.from_root(PROJECT_ROOT)
    audit = None
    if not args.skip_parquet:
        audit = materialize_parquet_tables(
            paths,
            chunksize=args.chunksize,
            write_versions_parquet=not args.skip_versions_parquet,
        )

    payload = build_dashboard_payload(paths, audit=audit)
    html_path, json_path = write_static_dashboard(payload, args.output_dir)

    summary = {
        "dashboard_html": str(html_path),
        "dashboard_json": str(json_path),
        "json_size_kb": round(json_path.stat().st_size / 1024, 1),
        "unique_dois": payload["kpis"]["all"]["unique_dois"],
        "mature_pub_rate_pct": payload["kpis"]["all"]["mature_pub_rate_pct"],
        "quality_audit": payload["quality_audit"],
    }
    print(json.dumps(summary, indent=2))

    if args.serve:
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(args.output_dir)
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
        logger.info("Serving dashboard at http://127.0.0.1:%d/ (Ctrl+C to stop)", args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
