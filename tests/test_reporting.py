"""Unit tests for Parquet materialization, dashboard aggregations, and HTML output."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from biomedrxiv.config import ProjectPaths
from biomedrxiv.dashboard_template import write_static_dashboard
from biomedrxiv.reporting import (
    build_dashboard_payload,
    materialize_parquet_tables,
    wilson_ci_pct,
)


def test_wilson_ci_bounds() -> None:
    rate, lo, hi = wilson_ci_pct(50, 100)
    assert rate == 50.0
    assert 39.0 < lo < 50.0 < hi < 61.0
    assert wilson_ci_pct(0, 0) == (0.0, 0.0, 0.0)


def test_end_to_end_materialize_and_dashboard_generation(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path / "proj")
    paths.ensure_directories()

    sample_csv = tmp_path / "sample.csv"
    df = pd.DataFrame(
        {
            "title": [
                "CRISPR base editing in mice",
                "CRISPR base editing in mice (revised)",
                "AlphaFold structure study",
                "COVID-19 clinical outcomes",
            ],
            "authors": [
                "Alice A;Bob B;",
                "Alice A;Bob B;Carol C;",
                "Dan D;",
                "Eve E;Frank F;Grace G;Heidi H;",
            ],
            "author_corresponding": ["Alice  A", "Alice A", "Dan D", "Eve E"],
            "author_corresponding_institution": [
                "Stanford University",
                "Stanford University",
                "MIT",
                "Harvard Medical School",
            ],
            "doi": ["10.1101/001", "10.1101/001", "10.1101/002", "10.1101/003"],
            "date": ["2022-01-15", "2022-04-20", "2023-06-10", "2020-05-01"],
            "version": [1, 2, 1, 1],
            "type": ["New Results", "new results", "confirmatory results", "WITHDRAWN"],
            "license": ["cc_by", "cc_by", "cc_no", "CC-BY-NC-ND"],
            "category": ["Genomics ", "genomics", "Bioinformatics ", "Infectious Diseases "],
            "jatsxml": ["a", "b", "c", "d"],
            "abstract": [
                "We use CRISPR Cas9 editing.",
                "We use CRISPR Cas9 editing with more controls.",
                "AlphaFold enables protein structure prediction.",
                "SARS-CoV-2 retrospective cohort.",
            ],
            "published": ["10.1038/s41586-022-001", "10.1038/s41586-022-001", None, "10.1136/bmj.001"],
            "server": ["bioRxiv", "bioRxiv", "bioRxiv", "medRxiv"],
            "doi_link": [
                "https://doi.org/10.1101/001",
                "https://doi.org/10.1101/001",
                "https://doi.org/10.1101/002",
                "https://doi.org/10.1101/003",
            ],
        }
    )
    df.to_csv(sample_csv, index=False)

    audit = materialize_parquet_tables(paths, csv_path=sample_csv, chunksize=2)
    assert audit["raw_row_count"] == 4
    assert audit["unique_doi_count"] == 3
    assert audit["withdrawn_unique_dois"] == 1
    assert (paths.processed_dir / "preprints_versions.parquet").exists()
    assert (paths.processed_dir / "preprints_canonical.parquet").exists()

    payload = build_dashboard_payload(paths, audit=audit)
    assert payload["kpis"]["all"]["unique_dois"] == 3
    assert payload["kpis"]["biorxiv"]["unique_dois"] == 2
    assert payload["kpis"]["medrxiv"]["unique_dois"] == 1

    kw_totals = {s["key"]: s["total_papers"] for s in payload["frontier_trends"]["series"]}
    assert kw_totals["crispr"] == 1
    assert kw_totals["alphafold"] == 1
    assert kw_totals["covid_sarscov2"] == 1

    html_path, json_path = write_static_dashboard(payload, tmp_path / "docs")
    assert html_path.exists()
    assert json_path.exists()
    assert (tmp_path / "docs" / ".nojekyll").exists()
    assert "__DASHBOARD_JSON__" not in html_path.read_text(encoding="utf-8")
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["kpis"]["all"]["unique_dois"] == 3
