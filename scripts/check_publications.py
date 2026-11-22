#!/usr/bin/env python3
"""CLI for verifying which bioRxiv/medRxiv preprints were published in peer-reviewed journals.

Examples:
    # 1. Check one or more specific DOIs immediately
    .venv/bin/python scripts/check_publications.py doi 10.1101/000141 10.1101/2023.08.15.551388

    # 2. Fast bulk-harvest of published records from bioRxiv/medRxiv pubs API (100/request)
    .venv/bin/python scripts/check_publications.py bulk-harvest \
        --server both --start-date 2025-01-01 --end-date 2026-11-22

    # 3. Verify a filtered slice of the active dataset and write enriched Parquet + summary JSON
    .venv/bin/python scripts/check_publications.py verify \
        --server medrxiv --since 2024-01-01 --limit 100 \
        --crossref-fallback --mailto your.email@example.com

    # 4. Inspect local SQLite cache statistics
    .venv/bin/python scripts/check_publications.py stats
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

from biomedrxiv.config import ProjectPaths
from biomedrxiv.preprocessing import load_current_preprints
from biomedrxiv.publication_checker import (
    PoliteHttpClient,
    PublicationCache,
    PublicationVerifier,
    enrich_dataframe_with_publications,
    normalize_doi,
)

DEFAULT_VERIFY_COLUMNS = [
    "doi",
    "title",
    "date",
    "version",
    "type",
    "license",
    "category",
    "published",
    "server",
    "author_corresponding",
    "author_corresponding_institution",
]


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def _build_verifier(paths: ProjectPaths, args: argparse.Namespace) -> PublicationVerifier:
    cache = PublicationCache(paths.cache_dir / "publication_status.sqlite")
    client = PoliteHttpClient(
        mailto=getattr(args, "mailto", None),
        min_delay_seconds=getattr(args, "delay", 0.25),
    )
    return PublicationVerifier(cache=cache, http_client=client, mailto=getattr(args, "mailto", None))


def _cmd_doi(paths: ProjectPaths, args: argparse.Namespace) -> int:
    verifier = _build_verifier(paths, args)
    results = verifier.verify_dois(
        args.dois,
        recheck_days=0 if args.force_refresh else args.recheck_days,
        use_biorxiv_api=True,
        use_crossref_fallback=args.crossref_fallback,
        use_openalex_fallback=args.openalex_fallback,
    )
    payload = []
    for raw in args.dois:
        norm = normalize_doi(raw)
        rec = results.get(norm) if norm else None
        payload.append(
            {
                "input_doi": raw,
                "normalized_doi": norm,
                "is_published": rec.is_published if rec else None,
                "published_doi": rec.published_doi if rec else None,
                "published_journal": rec.published_journal if rec else None,
                "published_date": rec.published_date if rec else None,
                "verification_source": rec.verification_source if rec else None,
            }
        )
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_bulk_harvest(paths: ProjectPaths, args: argparse.Namespace) -> int:
    verifier = _build_verifier(paths, args)
    servers = ["biorxiv", "medrxiv"] if args.server == "both" else [args.server]
    totals = {}
    for srv in servers:
        totals[srv] = verifier.harvest_biorxiv_pubs_window(
            server=srv,
            start_date=args.start_date,
            end_date=args.end_date,
            max_pages=args.max_pages,
        )
    print(
        json.dumps(
            {
                "harvested": totals,
                "cache_stats": verifier.cache.count_cached(),
            },
            indent=2,
        )
    )
    return 0


def _cmd_verify(paths: ProjectPaths, args: argparse.Namespace) -> int:
    verifier = _build_verifier(paths, args)
    df = load_current_preprints(
        paths.current_link,
        usecols=DEFAULT_VERIFY_COLUMNS,
        deduplicate=True,
    )

    if args.server != "both":
        df = df[df["server_clean"] == args.server.lower()]
    if args.category:
        df = df[df["category_clean"] == args.category.strip().lower()]
    if args.since:
        df = df[df["first_posted_date"] >= pd.to_datetime(args.since)]
    if args.until:
        df = df[df["first_posted_date"] <= pd.to_datetime(args.until)]
    if args.only_unpublished_in_snapshot:
        df = df[~df["snapshot_is_published"]]

    df = df.reset_index(drop=True)
    if args.limit is not None:
        df = df.head(args.limit).copy()

    if df.empty:
        print("No rows matched the specified filters.")
        return 0

    server_map = dict(zip(df["normalized_doi"], df["server_clean"], strict=False))
    records = verifier.verify_dois(
        df["normalized_doi"].tolist(),
        server_map=server_map,
        recheck_days=args.recheck_days,
        use_biorxiv_api=not args.cache_only,
        use_crossref_fallback=(not args.cache_only) and args.crossref_fallback,
        use_openalex_fallback=(not args.cache_only) and args.openalex_fallback,
        max_live_queries=0 if args.cache_only else args.max_live_queries,
    )

    enriched = enrich_dataframe_with_publications(
        df,
        records,
        doi_column="normalized_doi",
        preprint_date_column="first_posted_date",
    )

    # Combine snapshot signal + live verification into a unified peer-reviewed flag
    enriched["final_is_published"] = (
        enriched["is_published"].fillna(False).astype(bool)
        | enriched["snapshot_is_published"].fillna(False).astype(bool)
    )
    enriched["final_published_doi"] = enriched["published_doi"].fillna(
        enriched["snapshot_published_doi"]
    )
    enriched["newly_published_since_snapshot"] = (
        enriched["is_published"].fillna(False).astype(bool)
        & (~enriched["snapshot_is_published"].fillna(False).astype(bool))
    )

    out_path: Path = args.output or (paths.processed_dir / "publication_status.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".csv":
        enriched.to_csv(out_path, index=False)
    else:
        enriched.to_parquet(out_path, index=False)

    valid_days = enriched.loc[
        enriched["days_to_publication"].notna() & (enriched["days_to_publication"] >= 0),
        "days_to_publication",
    ]
    summary = {
        "rows_evaluated_unique_dois": int(len(enriched)),
        "snapshot_published_count": int(enriched["snapshot_is_published"].sum()),
        "verified_published_count": int(enriched["final_is_published"].sum()),
        "verified_publication_rate_pct": round(
            float(enriched["final_is_published"].mean() * 100), 2
        ),
        "newly_published_since_snapshot": int(enriched["newly_published_since_snapshot"].sum()),
        "median_days_to_publication": (
            round(float(valid_days.median()), 1) if not valid_days.empty else None
        ),
        "top_journals": (
            enriched["published_journal"].dropna().value_counts().head(10).to_dict()
        ),
        "output_file": str(out_path),
    }

    summary_path = out_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_stats(paths: ProjectPaths, _args: argparse.Namespace) -> int:
    cache = PublicationCache(paths.cache_dir / "publication_status.sqlite")
    print(json.dumps(cache.count_cached(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify peer-reviewed journal publication status for Bio&Med-Rxiv preprints."
    )
    parser.add_argument(
        "--mailto",
        default=None,
        help="Optional contact email for Crossref/OpenAlex polite pools (faster rate limits).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.25,
        help="Minimum delay in seconds between API requests (default: 0.25).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")

    sub = parser.add_subparsers(dest="command", required=True)

    p_doi = sub.add_parser("doi", help="Check one or more specific preprint DOIs.")
    p_doi.add_argument("dois", nargs="+", help="Preprint DOIs (e.g. 10.1101/000141).")
    p_doi.add_argument("--crossref-fallback", action="store_true", default=True)
    p_doi.add_argument("--openalex-fallback", action="store_true", default=False)
    p_doi.add_argument("--recheck-days", type=int, default=30)
    p_doi.add_argument("--force-refresh", action="store_true")
    p_doi.set_defaults(func=_cmd_doi)

    p_bulk = sub.add_parser(
        "bulk-harvest",
        help="Fast bulk-download of published preprint records from bioRxiv/medRxiv pubs API.",
    )
    p_bulk.add_argument("--server", choices=["biorxiv", "medrxiv", "both"], default="both")
    p_bulk.add_argument("--start-date", required=True, help="Start date (YYYY-MM-DD).")
    p_bulk.add_argument("--end-date", required=True, help="End date (YYYY-MM-DD).")
    p_bulk.add_argument("--max-pages", type=int, default=None, help="Optional page cap (100/page).")
    p_bulk.set_defaults(func=_cmd_bulk_harvest)

    p_verify = sub.add_parser(
        "verify",
        help="Enrich deduplicated dataset with live + cached peer-reviewed publication status.",
    )
    p_verify.add_argument("--server", choices=["biorxiv", "medrxiv", "both"], default="both")
    p_verify.add_argument("--category", default=None, help="Filter by subject category.")
    p_verify.add_argument("--since", default=None, help="Filter first_posted_date >= YYYY-MM-DD.")
    p_verify.add_argument("--until", default=None, help="Filter first_posted_date <= YYYY-MM-DD.")
    p_verify.add_argument(
        "--only-unpublished-in-snapshot",
        action="store_true",
        help="Only check DOIs marked unpublished in the Kaggle CSV snapshot.",
    )
    p_verify.add_argument("--limit", type=int, default=None, help="Max unique DOIs to evaluate.")
    p_verify.add_argument(
        "--max-live-queries",
        type=int,
        default=None,
        help="Cap live API lookups for uncached DOIs (useful for incremental batches).",
    )
    p_verify.add_argument(
        "--cache-only",
        action="store_true",
        help="Join only against already-cached SQLite records (zero network calls).",
    )
    p_verify.add_argument("--crossref-fallback", action="store_true", default=False)
    p_verify.add_argument("--openalex-fallback", action="store_true", default=False)
    p_verify.add_argument("--recheck-days", type=int, default=30)
    p_verify.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output .parquet or .csv path (default: data/processed/publication_status.parquet).",
    )
    p_verify.set_defaults(func=_cmd_verify)

    p_stats = sub.add_parser("stats", help="Show local publication cache statistics.")
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
