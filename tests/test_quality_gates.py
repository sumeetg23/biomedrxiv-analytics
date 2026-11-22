"""Unit tests for automated data quality gates and release-digest generation."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from biomedrxiv.quality_gates import evaluate_quality_gates
from biomedrxiv.release_agent import build_release_digest_markdown


def _sample_dashboard(
    *,
    unique_dois: int = 100,
    withdrawn: int = 2,
    pub_rate: float = 55.0,
    leak_path: bool = False,
) -> dict:
    return {
        "generated_at_utc": "2026-11-22T20:00:00+00:00",
        "dataset_provenance": {
            "version_id": "v20261122_000000_abcd1234",
            "note": "/Users/alice/secret" if leak_path else "clean",
        },
        "quality_audit": {"raw_row_count": unique_dois + 10},
        "available_years": [2023],
        "kpis": {
            "all": {
                "unique_dois": unique_dois,
                "withdrawn_count": withdrawn,
                "mature_pub_rate_pct": pub_rate,
                "date_max": "2026-10-31",
            },
            "biorxiv": {"unique_dois": unique_dois},
            "medrxiv": {"unique_dois": 0},
        },
        "by_year": {
            "2023": {
                "biorxiv": {
                    "kpi": {
                        "unique_dois": unique_dois,
                        "withdrawn_count": withdrawn,
                    }
                }
            }
        },
        "monthly_growth": {},
        "annual_summary": {},
        "cohort_heatmap": {},
        "frontier_trends": {"series": [{"label": "CRISPR", "total_papers": 12}]},
        "license_evolution": {},
        "withdrawals_timeline": {},
        "top_institutions": [],
    }


def _sample_parquet(path: Path, *, duplicate_doi: bool = False, bad_server: bool = False) -> None:
    df = pd.DataFrame(
        {
            "normalized_doi": ["10.1101/001", "10.1101/001" if duplicate_doi else "10.1101/002"],
            "server_clean": ["biorxiv", "arxiv" if bad_server else "medrxiv"],
            "category_clean": ["genomics", "epidemiology"],
            "first_posted_date": pd.to_datetime(["2023-01-01", "2023-06-01"]),
            "num_versions": [1, 2],
            "author_count": [5, 7],
            "snapshot_is_published": [True, False],
            "ever_withdrawn": [False, False],
        }
    )
    df.to_parquet(path, index=False)


def test_quality_gates_pass_and_compute_release_diff(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    pq = tmp_path / "canonical.parquet"

    base.write_text(json.dumps(_sample_dashboard(unique_dois=100, withdrawn=2)), encoding="utf-8")
    cand.write_text(json.dumps(_sample_dashboard(unique_dois=110, withdrawn=3)), encoding="utf-8")
    _sample_parquet(pq)

    report = evaluate_quality_gates(
        candidate_json_path=cand,
        canonical_parquet_path=pq,
        baseline_json_path=base,
        min_unique_dois=50,
    )
    assert report.passed is True
    assert report.fail_count == 0
    assert report.diff is not None
    assert report.diff["metrics"]["unique_dois"]["delta"] == 10


def test_quality_gates_catch_shrinkage_leaks_and_duplicates(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    pq = tmp_path / "canonical.parquet"

    base.write_text(json.dumps(_sample_dashboard(unique_dois=100)), encoding="utf-8")
    cand.write_text(json.dumps(_sample_dashboard(unique_dois=80, leak_path=True)), encoding="utf-8")
    _sample_parquet(pq, duplicate_doi=True, bad_server=True)

    report = evaluate_quality_gates(
        candidate_json_path=cand,
        canonical_parquet_path=pq,
        baseline_json_path=base,
        min_unique_dois=50,
        max_shrinkage_pct=2.0,
    )
    assert report.passed is False
    failed_names = {c.name for c in report.checks if c.status == "FAIL"}
    assert "no_local_path_leaks" in failed_names
    assert "non_shrinkage_regression" in failed_names
    assert "doi_uniqueness" in failed_names
    assert "server_domain_validity" in failed_names


def test_release_digest_with_mocked_gemini_commentary(tmp_path: Path) -> None:
    cand = tmp_path / "cand.json"
    cand.write_text(json.dumps(_sample_dashboard(unique_dois=100)), encoding="utf-8")
    report = evaluate_quality_gates(candidate_json_path=cand, min_unique_dois=50)

    fake_client = SimpleNamespace(
        interactions=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(
                output_text="- All gates passed with zero regressions."
            )
        )
    )
    md = build_release_digest_markdown(report, use_gemini=True, gemini_client=fake_client)
    assert "Automated Dataset Update & Quality Gate Report — **PASSED**" in md
    assert "### AI Agent Executive Summary" in md
    assert "All gates passed with zero regressions." in md
