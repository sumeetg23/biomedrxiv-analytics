#!/usr/bin/env python3
"""CLI entrypoint to build the compact Cloudflare Vectorize RAG corpus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from biomedrxiv.rag_corpus import DEFAULT_MAX_DOCS, build_rag_corpus


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build data/processed/rag_corpus.jsonl for Cloudflare Vectorize RAG."
    )
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "processed",
        help="Directory containing materialized Parquet files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSONL path (default: <processed-dir>/rag_corpus.jsonl).",
    )
    parser.add_argument(
        "--max-docs",
        type=int,
        default=DEFAULT_MAX_DOCS,
        help=f"Maximum documents to include (default: {DEFAULT_MAX_DOCS}; 12,500 * 384 = 4.8M dims < 5M free cap).",
    )
    args = parser.parse_args()

    stats = build_rag_corpus(
        processed_dir=args.processed_dir,
        output_path=args.output,
        max_docs=args.max_docs,
    )
    print(json.dumps(stats.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
