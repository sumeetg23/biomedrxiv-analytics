#!/usr/bin/env python3
"""CLI for extracting and enriching clinical trial links from Bio&Med-Rxiv preprints.

Usage examples:
    # 1. Extract all clinical trial IDs (NCT, ISRCTN, ChiCTR, ACTRN, EudraCT) offline
    .venv/bin/python scripts/check_clinical_trials.py extract

    # 2. Batch-enrich extracted NCT IDs via ClinicalTrials.gov API v2 (50 NCTs/request)
    .venv/bin/python scripts/check_clinical_trials.py enrich-nct

    # 3. Trace downstream clinical-trial citations via Europe PMC + NIH iCite
    .venv/bin/python scripts/check_clinical_trials.py trace-downstream --server both --limit 500

    # 4. Inspect cache & link statistics
    .venv/bin/python scripts/check_clinical_trials.py stats
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from biomedrxiv.clinical_trials import (
    ClinicalTrialsCache,
    ClinicalTrialsLinker,
    extract_direct_trial_links,
)
from biomedrxiv.config import ProjectPaths
from biomedrxiv.publication_checker import PoliteHttpClient


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def _cmd_extract(paths: ProjectPaths, _args: argparse.Namespace) -> int:
    links = extract_direct_trial_links(paths)
    summary = {
        "total_links": int(len(links)),
        "unique_preprints": int(links["normalized_doi"].nunique()) if not links.empty else 0,
        "unique_trials": int(links["trial_id"].nunique()) if not links.empty else 0,
        "by_registry": links["registry"].value_counts().to_dict() if not links.empty else {},
        "by_server": (
            links.drop_duplicates("normalized_doi")["server_clean"].value_counts().to_dict()
            if not links.empty and "server_clean" in links.columns
            else {}
        ),
        "output_parquet": str(paths.processed_dir / "preprint_trial_links.parquet"),
    }
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_enrich_nct(paths: ProjectPaths, args: argparse.Namespace) -> int:
    links_path = paths.processed_dir / "preprint_trial_links.parquet"
    if not links_path.exists():
        extract_direct_trial_links(paths)

    links = pd.read_parquet(links_path)
    nct_ids = (
        links.loc[links["registry"] == "ClinicalTrials.gov", "trial_id"]
        .dropna()
        .unique()
        .tolist()
    )
    if args.limit is not None:
        nct_ids = nct_ids[: args.limit]

    cache = ClinicalTrialsCache(paths.cache_dir / "clinical_trials.sqlite")
    linker = ClinicalTrialsLinker(
        cache=cache,
        http_client=PoliteHttpClient(min_delay_seconds=args.delay),
    )
    enriched = linker.enrich_nct_ids(
        nct_ids,
        batch_size=args.batch_size,
        force_refresh=args.force_refresh,
        max_batches=args.max_batches,
    )
    print(
        json.dumps(
            {
                "nct_ids_requested": len(nct_ids),
                "nct_ids_resolved": len(enriched),
                "cache_stats": cache.stats(),
            },
            indent=2,
        )
    )
    return 0


def _cmd_trace_downstream(paths: ProjectPaths, args: argparse.Namespace) -> int:
    canon_path = paths.processed_dir / "preprints_canonical.parquet"
    if not canon_path.exists():
        raise FileNotFoundError(f"Canonical Parquet not found at {canon_path}. Run scripts/run_etl.py first.")

    df = pd.read_parquet(
        canon_path,
        columns=[
            "normalized_doi",
            "server_clean",
            "category_clean",
            "first_posted_date",
            "snapshot_is_published",
            "snapshot_published_doi",
        ],
    )
    df = df[df["snapshot_is_published"] & df["snapshot_published_doi"].notna()].copy()
    df["first_posted_date"] = pd.to_datetime(df["first_posted_date"], errors="coerce")

    if args.server != "both":
        df = df[df["server_clean"] == args.server]
    if args.category:
        df = df[df["category_clean"] == args.category.strip().lower()]
    if args.since:
        df = df[df["first_posted_date"] >= pd.to_datetime(args.since)]
    if args.until:
        df = df[df["first_posted_date"] <= pd.to_datetime(args.until)]

    # Prioritize older mature publications that have had time to influence clinical trials
    df = df.sort_values("first_posted_date", ascending=True)
    pub_dois = df["snapshot_published_doi"].dropna().unique().tolist()
    if args.limit is not None:
        pub_dois = pub_dois[: args.limit]

    cache = ClinicalTrialsCache(paths.cache_dir / "clinical_trials.sqlite")
    linker = ClinicalTrialsLinker(
        cache=cache,
        http_client=PoliteHttpClient(min_delay_seconds=args.delay),
    )
    results = linker.trace_downstream_clinical_impact(
        pub_dois,
        force_refresh=args.force_refresh,
        max_dois=args.limit,
    )

    out_rows = [
        {
            "published_doi": r.published_doi,
            "pmid": r.pmid,
            "is_clinical": r.is_clinical,
            "apt": r.apt,
            "cited_by_clin_count": r.cited_by_clin_count,
            "citation_count": r.citation_count,
            "relative_citation_ratio": r.relative_citation_ratio,
        }
        for r in results.values()
    ]
    out_path = paths.processed_dir / "downstream_clinical_impact.parquet"
    pd.DataFrame(out_rows).to_parquet(out_path, index=False, compression="zstd")

    cited = [r for r in results.values() if r.cited_by_clin_count > 0]
    print(
        json.dumps(
            {
                "published_dois_evaluated": len(results),
                "resolved_pmids": sum(1 for r in results.values() if r.pmid),
                "cited_by_clinical_trials": len(cited),
                "output_parquet": str(out_path),
                "cache_stats": cache.stats(),
            },
            indent=2,
        )
    )
    return 0


def _cmd_stats(paths: ProjectPaths, _args: argparse.Namespace) -> int:
    cache = ClinicalTrialsCache(paths.cache_dir / "clinical_trials.sqlite")
    payload: dict[str, object] = {"cache_stats": cache.stats()}
    links_path = paths.processed_dir / "preprint_trial_links.parquet"
    if links_path.exists():
        links = pd.read_parquet(links_path)
        payload["extracted_links"] = {
            "total_links": int(len(links)),
            "unique_preprints": int(links["normalized_doi"].nunique()),
            "unique_trials": int(links["trial_id"].nunique()),
            "by_registry": links["registry"].value_counts().to_dict(),
        }
    print(json.dumps(payload, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract direct clinical trial links and trace downstream clinical-trial citations."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.25,
        help="Minimum delay in seconds between API requests (default: 0.25).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")

    sub = parser.add_subparsers(dest="command", required=True)

    p_ext = sub.add_parser("extract", help="Extract registry IDs (NCT, ISRCTN, etc.) from Parquet.")
    p_ext.set_defaults(func=_cmd_extract)

    p_nct = sub.add_parser(
        "enrich-nct",
        help="Batch-enrich extracted NCT IDs via ClinicalTrials.gov API v2.",
    )
    p_nct.add_argument("--batch-size", type=int, default=50, help="NCT IDs per request (default: 50).")
    p_nct.add_argument("--limit", type=int, default=None, help="Optional max NCT IDs to enrich.")
    p_nct.add_argument("--max-batches", type=int, default=None, help="Optional cap on API batches.")
    p_nct.add_argument("--force-refresh", action="store_true")
    p_nct.set_defaults(func=_cmd_enrich_nct)

    p_ds = sub.add_parser(
        "trace-downstream",
        help="Trace publications cited by clinical trials via Europe PMC + NIH iCite.",
    )
    p_ds.add_argument("--server", choices=["biorxiv", "medrxiv", "both"], default="both")
    p_ds.add_argument("--category", default=None, help="Optional category filter.")
    p_ds.add_argument("--since", default=None, help="Filter first_posted_date >= YYYY-MM-DD.")
    p_ds.add_argument("--until", default=None, help="Filter first_posted_date <= YYYY-MM-DD.")
    p_ds.add_argument("--limit", type=int, default=500, help="Max published DOIs to evaluate (default: 500).")
    p_ds.add_argument("--force-refresh", action="store_true")
    p_ds.set_defaults(func=_cmd_trace_downstream)

    p_stats = sub.add_parser("stats", help="Show clinical trials cache and extraction statistics.")
    p_stats.set_defaults(func=_cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    paths = ProjectPaths.from_root(PROJECT_ROOT)
    paths.ensure_directories()
    return args.func(paths, args)


if __name__ == "__main__":
    raise SystemExit(main())
