#!/usr/bin/env python3
"""CLI to deduplicate raw institutional affiliation strings via the official ROR registry.

Downloads the official CC0 ROR data dump once (cached in ``data/cache/ror/``),
resolves all 130k+ unique raw affiliation strings into canonical ROR IDs,
official names, countries, and organization types, and writes
``data/processed/institution_ror_map.parquet``.

Usage:
    # Local fast ROR dump matching (~15 seconds across all 454k preprints)
    .venv/bin/python scripts/deduplicate_institutions.py

    # Also run ROR v2 API affiliation fallback on top 150 unmatched strings
    .venv/bin/python scripts/deduplicate_institutions.py --api-fallback-top-n 150
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
from biomedrxiv.ror_dedup import build_institution_ror_mapping, download_ror_dump


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deduplicate Bio&Med-Rxiv institutional affiliations using the official ROR registry."
    )
    parser.add_argument(
        "--force-download-ror",
        action="store_true",
        help="Re-download the latest ROR data dump from Zenodo even if cached.",
    )
    parser.add_argument(
        "--api-fallback-top-n",
        type=int,
        default=100,
        help="Query ROR v2 affiliation API for top N unmatched high-volume strings (default: 100; 0 disables).",
    )
    parser.add_argument("--delay", type=float, default=0.15, help="API delay in seconds (default: 0.15).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    paths = ProjectPaths.from_root(PROJECT_ROOT)
    paths.ensure_directories()

    ror_json = download_ror_dump(paths.cache_dir, force=args.force_download_ror)
    summary = build_institution_ror_mapping(
        paths,
        ror_json_path=ror_json,
        api_fallback_top_n=args.api_fallback_top_n,
        delay_seconds=args.delay,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
