#!/usr/bin/env python3
"""Pure ETL entrypoint: clean CSV -> Parquet tables -> compact analytics JSON for web/.

This script handles ONLY data engineering (extract, clean, deduplicate, aggregate,
and write the JSON data contract). Web presentation files live separately under ``web/``.

Usage:
    # Full ETL: raw CSV -> Parquet tables -> web/data/dashboard_data.json
    .venv/bin/python scripts/run_etl.py

    # Re-aggregate JSON from existing canonical Parquet (~2 seconds)
    .venv/bin/python scripts/run_etl.py --skip-parquet
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from biomedrxiv.config import ProjectPaths
from biomedrxiv.etl import (
    build_analytics_payload,
    export_dashboard_json,
    materialize_parquet_tables,
)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Bio&Med-Rxiv ETL: materialize cleaned Parquet tables and export dashboard JSON."
    )
    parser.add_argument(
        "--skip-parquet",
        action="store_true",
        help="Skip CSV -> Parquet pass and rebuild aggregated JSON from existing canonical Parquet.",
    )
    parser.add_argument(
        "--skip-versions-parquet",
        action="store_true",
        help="Only write canonical DOI-level Parquet (skip full version-level Parquet).",
    )
    parser.add_argument(
        "--chunksize",
        type=int,
        default=75_000,
        help="CSV chunk size for bounded-memory processing (default: 75000).",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=PROJECT_ROOT / "web" / "data" / "dashboard_data.json",
        help="Destination path for aggregated JSON data contract (default: web/data/dashboard_data.json).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    paths = ProjectPaths.from_root(PROJECT_ROOT)
    audit = None
    if not args.skip_parquet:
        audit = materialize_parquet_tables(
            paths,
            chunksize=args.chunksize,
            write_versions_parquet=not args.skip_versions_parquet,
        )

    payload = build_analytics_payload(paths, audit=audit)
    json_path = export_dashboard_json(payload, args.output_json)

    print(
        json.dumps(
            {
                "output_json": str(json_path),
                "json_size_kb": round(json_path.stat().st_size / 1024, 1),
                "unique_dois": payload["kpis"]["all"]["unique_dois"],
                "mature_pub_rate_pct": payload["kpis"]["all"]["mature_pub_rate_pct"],
                "quality_audit": payload["quality_audit"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
