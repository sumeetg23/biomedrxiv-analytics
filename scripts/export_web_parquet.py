#!/usr/bin/env python3
"""Export compact, sorted Parquet tables into web/data/parquet/ for browser DuckDB-Wasm."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from biomedrxiv.web_parquet import export_web_parquet


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export sorted, ZSTD-compressed Web Parquet tables for browser DuckDB-Wasm."
    )
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "processed",
        help="Directory containing ETL Parquet outputs.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "web" / "data" / "parquet",
        help="Destination directory for browser DuckDB-Wasm Parquet files.",
    )
    args = parser.parse_args()

    stats = export_web_parquet(processed_dir=args.processed_dir, output_dir=args.output_dir)
    print(json.dumps(stats.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
