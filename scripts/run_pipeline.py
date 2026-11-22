#!/usr/bin/env python3
"""End-to-end automated pipeline orchestrator with fail-fast quality gates.

Steps:
1. Snapshot current ``web/data/dashboard_data.json`` as baseline (if present).
2. Sync latest Kaggle dataset (unless ``--skip-download``).
3. Run ETL (CSV -> Parquet tables -> JSON contract).
4. Extract clinical trial links (+ optional ClinicalTrials.gov API v2 enrichment).
5. Refresh ``web/data/dashboard_data.json`` with clinical trials payload.
6. Execute data quality gates & write ``logs/release_digest.md`` (+ optional Gemini summary).
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from biomedrxiv.biorxiv_sync import sync_incremental_from_biorxiv_api
from biomedrxiv.clinical_trials import (
    ClinicalTrialsCache,
    ClinicalTrialsLinker,
    extract_direct_trial_links,
)
from biomedrxiv.config import DEFAULT_KAGGLE_DATASET, ProjectPaths
from biomedrxiv.etl import (
    build_analytics_payload,
    export_dashboard_json,
    materialize_parquet_tables,
)
from biomedrxiv.quality_gates import evaluate_quality_gates
from biomedrxiv.release_agent import build_release_digest_markdown
from biomedrxiv.ror_dedup import build_institution_ror_mapping
from biomedrxiv.versioning import DatasetVersionManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run end-to-end Bio&Med-Rxiv sync, ETL, clinical trial linkage, and quality gates."
    )
    parser.add_argument("--dataset", default=DEFAULT_KAGGLE_DATASET)
    parser.add_argument(
        "--source",
        choices=["biorxiv-api", "kaggle", "both"],
        default="biorxiv-api",
        help=(
            "Data update source (default: biorxiv-api for direct incremental updates; "
            "use 'both' in clean CI runners to bootstrap from Kaggle then apply live API deltas)."
        ),
    )
    parser.add_argument("--skip-download", action="store_true", help="Reuse active data/current snapshot.")
    parser.add_argument("--skip-parquet", action="store_true", help="Reuse existing Parquet tables.")
    parser.add_argument(
        "--enrich-nct",
        action="store_true",
        help="Also query ClinicalTrials.gov API v2 for any newly discovered NCT IDs.",
    )
    parser.add_argument(
        "--dedup-ror",
        action="store_true",
        help="Run official ROR institutional deduplication (downloads CC0 ROR dump if needed).",
    )
    parser.add_argument(
        "--use-gemini",
        action="store_true",
        help="Generate Gemini executive summary / failure triage if GEMINI_API_KEY is set.",
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        default=PROJECT_ROOT / "logs" / "release_digest.md",
        help="Path to write release digest Markdown (default: logs/release_digest.md).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    logger = logging.getLogger("biomedrxiv.pipeline")
    paths = ProjectPaths.from_root(PROJECT_ROOT)
    paths.ensure_directories()

    dashboard_json = PROJECT_ROOT / "web" / "data" / "dashboard_data.json"
    baseline_copy: Path | None = None
    with tempfile.TemporaryDirectory() as tmpdir:
        if dashboard_json.exists():
            baseline_copy = Path(tmpdir) / "baseline_dashboard_data.json"
            shutil.copy2(dashboard_json, baseline_copy)

        if not args.skip_download:
            if args.source in {"kaggle", "both"} or not (paths.current_link / "biorxiv_medrxiv_preprints.csv").exists():
                manager = DatasetVersionManager(paths=paths, dataset_handle=args.dataset)
                res = manager.sync_from_kaggle(force_download=True, notes="automated pipeline Kaggle base sync")
                logger.info("Kaggle sync status=%s version=%s", res.status, res.version_id)

            if args.source in {"biorxiv-api", "both"}:
                inc = sync_incremental_from_biorxiv_api(paths)
                logger.info(
                    "Direct bioRxiv/medRxiv API sync (%s..%s): fetched=%d, changes=%d, pubs=%d, version=%s",
                    inc.start_date,
                    inc.end_date,
                    inc.fetched_records,
                    inc.new_or_updated_records,
                    inc.pubs_harvested,
                    inc.update_result.version_id if inc.update_result else "unchanged",
                )

        audit = None
        if not args.skip_parquet:
            audit = materialize_parquet_tables(paths)

        links = extract_direct_trial_links(paths)
        if args.enrich_nct and not links.empty:
            nct_ids = (
                links.loc[links["registry"] == "ClinicalTrials.gov", "trial_id"]
                .dropna()
                .unique()
                .tolist()
            )
            cache = ClinicalTrialsCache(paths.cache_dir / "clinical_trials.sqlite")
            ClinicalTrialsLinker(cache=cache).enrich_nct_ids(nct_ids)

        cached_ror = paths.cache_dir / "ror" / "ror_data_latest.json"
        if args.dedup_ror or cached_ror.exists():
            ror_summary = build_institution_ror_mapping(
                paths,
                ror_json_path=cached_ror if cached_ror.exists() else None,
                api_fallback_top_n=0,
            )
            logger.info(
                "ROR deduplication coverage: %.1f%% (%d unique ROR orgs)",
                ror_summary["coverage_pct"],
                ror_summary["unique_ror_orgs_matched"],
            )

        payload = build_analytics_payload(paths, audit=audit)
        export_dashboard_json(payload, dashboard_json)

        report = evaluate_quality_gates(
            candidate_json_path=dashboard_json,
            canonical_parquet_path=paths.processed_dir / "preprints_canonical.parquet",
            baseline_json_path=baseline_copy,
        )
        markdown = build_release_digest_markdown(report, use_gemini=args.use_gemini)

        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(markdown, encoding="utf-8")
        print(markdown)
        return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
