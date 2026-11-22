#!/usr/bin/env python3
"""Run automated data quality gates and generate a PR-ready release digest.

Exits with code 0 when all blocking gates pass, or code 1 if any gate fails.

Usage:
    .venv/bin/python scripts/run_quality_gates.py
    .venv/bin/python scripts/run_quality_gates.py \
        --baseline-json /tmp/baseline_dashboard_data.json \
        --use-gemini \
        --output-markdown logs/release_digest.md
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
from biomedrxiv.qa_engine import DEFAULT_GEMINI_MODEL
from biomedrxiv.quality_gates import evaluate_quality_gates
from biomedrxiv.release_agent import build_release_digest_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate automated data quality gates and generate release digest Markdown."
    )
    parser.add_argument(
        "--candidate-json",
        type=Path,
        default=PROJECT_ROOT / "web" / "data" / "dashboard_data.json",
        help="Path to candidate dashboard_data.json (default: web/data/dashboard_data.json).",
    )
    parser.add_argument(
        "--baseline-json",
        type=Path,
        default=None,
        help="Optional baseline dashboard_data.json for regression & release diffing.",
    )
    parser.add_argument(
        "--canonical-parquet",
        type=Path,
        default=PROJECT_ROOT / "data" / "processed" / "preprints_canonical.parquet",
        help="Path to canonical Parquet table (checked if present).",
    )
    parser.add_argument(
        "--min-dois",
        type=int,
        default=100_000,
        help="Minimum acceptable unique DOI count (default: 100000).",
    )
    parser.add_argument(
        "--max-shrinkage-pct",
        type=float,
        default=2.0,
        help="Maximum allowed row-count drop vs baseline in percent (default: 2.0).",
    )
    parser.add_argument(
        "--use-gemini",
        action="store_true",
        help="Append Gemini executive summary / failure triage if GEMINI_API_KEY is set.",
    )
    parser.add_argument(
        "--gemini-model",
        default=DEFAULT_GEMINI_MODEL,
        help=f"Gemini model for commentary (default: {DEFAULT_GEMINI_MODEL}).",
    )
    parser.add_argument(
        "--output-report",
        type=Path,
        default=None,
        help="Optional JSON output path for structured quality report.",
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        default=None,
        help="Optional Markdown output path for PR body / release digest.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

    paths = ProjectPaths.from_root(PROJECT_ROOT)
    paths.ensure_directories()

    report = evaluate_quality_gates(
        candidate_json_path=args.candidate_json,
        canonical_parquet_path=args.canonical_parquet,
        baseline_json_path=args.baseline_json,
        min_unique_dois=args.min_dois,
        max_shrinkage_pct=args.max_shrinkage_pct,
    )

    markdown = build_release_digest_markdown(
        report,
        use_gemini=args.use_gemini,
        gemini_model=args.gemini_model,
    )

    if args.output_report:
        args.output_report.parent.mkdir(parents=True, exist_ok=True)
        args.output_report.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")

    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(markdown, encoding="utf-8")

    print(markdown)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
