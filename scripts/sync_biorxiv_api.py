#!/usr/bin/env python3
"""CLI to incrementally update the dataset directly from bioRxiv & medRxiv APIs.

Fetches all new preprints posted since the active snapshot's latest date (plus
newly published journal links via the ``pubs`` API), merges them cleanly without
duplicates, and creates a new SHA-256-versioned snapshot in ``data/versions/``.

Usage:
    # Auto-detect latest date in data/current and pull everything up to today
    .venv/bin/python scripts/sync_biorxiv_api.py

    # Explicit date range
    .venv/bin/python scripts/sync_biorxiv_api.py --start-date 2026-10-30 --end-date 2026-11-22
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

from biomedrxiv.biorxiv_sync import sync_incremental_from_biorxiv_api
from biomedrxiv.config import ProjectPaths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Incrementally sync new preprints & publications directly from bioRxiv/medRxiv APIs."
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Override start date (YYYY-MM-DD). Defaults to (active max date - overlap_days).",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="Override end date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=2,
        help="Boundary overlap days to catch late-indexed postings (default: 2).",
    )
    parser.add_argument(
        "--server",
        choices=["both", "biorxiv", "medrxiv"],
        default="both",
        help="Which preprint server(s) to sync (default: both).",
    )
    parser.add_argument(
        "--skip-pubs",
        action="store_true",
        help="Skip harvesting recent peer-reviewed publication mappings from the pubs API.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.25,
        help="Minimum delay in seconds between API requests (default: 0.25).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional cap on API pages per server (100 records/page).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    servers = ("biorxiv", "medrxiv") if args.server == "both" else (args.server,)
    paths = ProjectPaths.from_root(PROJECT_ROOT)
    summary = sync_incremental_from_biorxiv_api(
        paths,
        start_date=args.start_date,
        end_date=args.end_date,
        overlap_days=args.overlap_days,
        servers=servers,
        also_harvest_pubs=not args.skip_pubs,
        delay_seconds=args.delay,
        max_pages_per_server=args.max_pages,
    )

    out = {
        "start_date": summary.start_date,
        "end_date": summary.end_date,
        "fetched_records": summary.fetched_records,
        "new_or_updated_records": summary.new_or_updated_records,
        "pubs_harvested": summary.pubs_harvested,
        "snapshot_status": summary.update_result.status if summary.update_result else "no_changes",
        "version_id": summary.update_result.version_id if summary.update_result else None,
        "diff": summary.update_result.diff if summary.update_result else None,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
