"""Deterministic data quality gates, contract validation, and release diffing.

Guards every automated dataset update before changes can be published to
``web/data/dashboard_data.json`` or opened as a Pull Request.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

REQUIRED_CANONICAL_COLUMNS = {
    "normalized_doi",
    "server_clean",
    "category_clean",
    "first_posted_date",
    "num_versions",
    "author_count",
    "snapshot_is_published",
    "ever_withdrawn",
}

REQUIRED_DASHBOARD_KEYS = {
    "generated_at_utc",
    "quality_audit",
    "available_years",
    "kpis",
    "by_year",
    "monthly_growth",
    "annual_summary",
    "cohort_heatmap",
    "frontier_trends",
    "license_evolution",
    "withdrawals_timeline",
    "top_institutions",
}

LOCAL_PATH_PATTERNS = [
    re.compile(r"/Users/[^/\s\"']+"),
    re.compile(r"/home/[^/\s\"']+"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+"),
]


@dataclass
class CheckResult:
    """Outcome of a single automated quality gate check."""

    name: str
    status: str  # "PASS", "WARN", "FAIL"
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityReport:
    """Aggregated result of all data quality gates and baseline diff."""

    passed: bool
    checked_at_utc: str
    checks: list[CheckResult]
    diff: dict[str, Any] | None = None

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "FAIL")

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "WARN")

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checked_at_utc": self.checked_at_utc,
            "fail_count": self.fail_count,
            "warn_count": self.warn_count,
            "checks": [asdict(c) for c in self.checks],
            "diff": self.diff,
        }


def compute_release_diff(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Compare two ``dashboard_data.json`` snapshots to summarize what changed."""
    b_all = baseline.get("kpis", {}).get("all", {})
    c_all = candidate.get("kpis", {}).get("all", {})
    b_bio = baseline.get("kpis", {}).get("biorxiv", {})
    c_bio = candidate.get("kpis", {}).get("biorxiv", {})
    b_med = baseline.get("kpis", {}).get("medrxiv", {})
    c_med = candidate.get("kpis", {}).get("medrxiv", {})

    def _delta(old: Any, new: Any) -> float | int | None:
        if old is None or new is None:
            return None
        diff = new - old
        return round(diff, 2) if isinstance(diff, float) else int(diff)

    old_dois = int(b_all.get("unique_dois") or 0)
    new_dois = int(c_all.get("unique_dois") or 0)
    pct_change = round(((new_dois - old_dois) / old_dois) * 100.0, 2) if old_dois > 0 else None

    # Compare frontier keyword totals
    b_front = {
        s["label"]: s["total_papers"]
        for s in baseline.get("frontier_trends", {}).get("series", [])
    }
    c_front = {
        s["label"]: s["total_papers"]
        for s in candidate.get("frontier_trends", {}).get("series", [])
    }
    frontier_deltas = [
        {"topic": label, "before": b_front.get(label, 0), "after": after, "delta": after - b_front.get(label, 0)}
        for label, after in c_front.items()
        if after != b_front.get(label, 0)
    ]
    frontier_deltas.sort(key=lambda x: x["delta"], reverse=True)

    # Compare clinical trial stats if present
    b_ct = (baseline.get("clinical_trials") or {}).get("cache_stats") or {}
    c_ct = (candidate.get("clinical_trials") or {}).get("cache_stats") or {}

    return {
        "baseline_version": (baseline.get("dataset_provenance") or {}).get("version_id"),
        "candidate_version": (candidate.get("dataset_provenance") or {}).get("version_id"),
        "date_max_before": b_all.get("date_max"),
        "date_max_after": c_all.get("date_max"),
        "metrics": {
            "unique_dois": {"before": old_dois, "after": new_dois, "delta": new_dois - old_dois, "pct_change": pct_change},
            "biorxiv_dois": {
                "before": b_bio.get("unique_dois"),
                "after": c_bio.get("unique_dois"),
                "delta": _delta(b_bio.get("unique_dois"), c_bio.get("unique_dois")),
            },
            "medrxiv_dois": {
                "before": b_med.get("unique_dois"),
                "after": c_med.get("unique_dois"),
                "delta": _delta(b_med.get("unique_dois"), c_med.get("unique_dois")),
            },
            "mature_pub_rate_pct": {
                "before": b_all.get("mature_pub_rate_pct"),
                "after": c_all.get("mature_pub_rate_pct"),
                "delta": _delta(b_all.get("mature_pub_rate_pct"), c_all.get("mature_pub_rate_pct")),
            },
            "withdrawn_count": {
                "before": b_all.get("withdrawn_count"),
                "after": c_all.get("withdrawn_count"),
                "delta": _delta(b_all.get("withdrawn_count"), c_all.get("withdrawn_count")),
            },
            "nct_trials_cached": {
                "before": b_ct.get("nct_trials_cached"),
                "after": c_ct.get("nct_trials_cached"),
                "delta": _delta(b_ct.get("nct_trials_cached"), c_ct.get("nct_trials_cached")),
            },
        },
        "frontier_deltas": frontier_deltas[:5],
    }


def evaluate_quality_gates(
    *,
    candidate_json_path: Path,
    canonical_parquet_path: Path | None = None,
    baseline_json_path: Path | None = None,
    min_unique_dois: int = 100_000,
    max_shrinkage_pct: float = 2.0,
    max_json_mb: float = 5.0,
) -> QualityReport:
    """Run all deterministic schema, integrity, privacy, and regression gates."""
    checks: list[CheckResult] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Candidate dashboard JSON exists & parses
    if not candidate_json_path.exists():
        checks.append(
            CheckResult(
                name="dashboard_json_exists",
                status="FAIL",
                message=f"Dashboard JSON missing at {candidate_json_path}",
            )
        )
        return QualityReport(passed=False, checked_at_utc=now_iso, checks=checks)

    raw_json_text = candidate_json_path.read_text(encoding="utf-8")
    size_mb = len(raw_json_text.encode("utf-8")) / (1024 * 1024)
    if size_mb > max_json_mb:
        checks.append(
            CheckResult(
                name="dashboard_json_size",
                status="FAIL",
                message=f"Dashboard JSON is {size_mb:.2f} MiB (limit: {max_json_mb:.2f} MiB).",
            )
        )
    else:
        checks.append(
            CheckResult(
                name="dashboard_json_size",
                status="PASS",
                message=f"Dashboard JSON size is {size_mb * 1024:.1f} KiB (within {max_json_mb:.1f} MiB limit).",
            )
        )

    # 2. Privacy / local machine path leak guard
    leaks = [pat.search(raw_json_text).group(0) for pat in LOCAL_PATH_PATTERNS if pat.search(raw_json_text)]
    if leaks:
        checks.append(
            CheckResult(
                name="no_local_path_leaks",
                status="FAIL",
                message=f"Detected local filesystem path leak(s) in dashboard JSON: {leaks[:3]}",
            )
        )
    else:
        checks.append(
            CheckResult(
                name="no_local_path_leaks",
                status="PASS",
                message="Zero local filesystem paths detected in dashboard JSON.",
            )
        )

    candidate = json.loads(raw_json_text)

    # 3. Required JSON contract keys
    missing_keys = sorted(REQUIRED_DASHBOARD_KEYS - set(candidate.keys()))
    if missing_keys:
        checks.append(
            CheckResult(
                name="dashboard_contract_keys",
                status="FAIL",
                message=f"Missing required dashboard JSON keys: {missing_keys}",
            )
        )
    else:
        checks.append(
            CheckResult(
                name="dashboard_contract_keys",
                status="PASS",
                message=f"All {len(REQUIRED_DASHBOARD_KEYS)} required JSON contract keys present.",
            )
        )

    # 4. Minimum DOI floor & by_year cube reconciliation
    kpis_all = candidate.get("kpis", {}).get("all", {})
    total_dois = int(kpis_all.get("unique_dois") or 0)
    if total_dois < min_unique_dois:
        checks.append(
            CheckResult(
                name="minimum_doi_floor",
                status="FAIL",
                message=f"Unique DOI count ({total_dois:,}) is below minimum threshold ({min_unique_dois:,}).",
            )
        )
    else:
        checks.append(
            CheckResult(
                name="minimum_doi_floor",
                status="PASS",
                message=f"Unique DOI count ({total_dois:,}) exceeds minimum threshold ({min_unique_dois:,}).",
            )
        )

    by_year = candidate.get("by_year") or {}
    sum_dois = sum(s["kpi"]["unique_dois"] for y in by_year.values() for s in y.values())
    sum_wd = sum(s["kpi"]["withdrawn_count"] for y in by_year.values() for s in y.values())
    expected_wd = int(kpis_all.get("withdrawn_count") or 0)
    if sum_dois != total_dois or sum_wd != expected_wd:
        checks.append(
            CheckResult(
                name="year_cube_reconciliation",
                status="FAIL",
                message=(
                    f"by_year cube mismatch: sum_dois={sum_dois:,} vs {total_dois:,}; "
                    f"sum_withdrawn={sum_wd:,} vs {expected_wd:,}"
                ),
            )
        )
    else:
        checks.append(
            CheckResult(
                name="year_cube_reconciliation",
                status="PASS",
                message=f"by_year additive cubes reconcile exactly ({sum_dois:,} DOIs, {sum_wd:,} withdrawn).",
            )
        )

    # 5. Canonical Parquet checks (when materialized Parquet is present)
    if canonical_parquet_path is not None and canonical_parquet_path.exists():
        df = pd.read_parquet(canonical_parquet_path)
        missing_cols = sorted(REQUIRED_CANONICAL_COLUMNS - set(df.columns))
        if missing_cols:
            checks.append(
                CheckResult(
                    name="parquet_required_schema",
                    status="FAIL",
                    message=f"Canonical Parquet missing columns: {missing_cols}",
                )
            )
        else:
            checks.append(
                CheckResult(
                    name="parquet_required_schema",
                    status="PASS",
                    message=f"Canonical Parquet has all required columns ({len(df.columns)} total).",
                )
            )

            # DOI uniqueness & non-nullity
            doi_nulls = int(df["normalized_doi"].isna().sum())
            doi_dups = int(len(df) - df["normalized_doi"].nunique())
            if doi_nulls > 0 or doi_dups > 0:
                checks.append(
                    CheckResult(
                        name="doi_uniqueness",
                        status="FAIL",
                        message=f"DOI integrity violated: {doi_nulls} nulls, {doi_dups} duplicate DOIs.",
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        name="doi_uniqueness",
                        status="PASS",
                        message=f"All {len(df):,} canonical DOIs are non-null and unique.",
                    )
                )

            # Server domain check
            invalid_servers = sorted(
                set(df["server_clean"].dropna().astype(str).unique()) - {"biorxiv", "medrxiv"}
            )
            if invalid_servers:
                checks.append(
                    CheckResult(
                        name="server_domain_validity",
                        status="FAIL",
                        message=f"Unexpected server_clean values detected: {invalid_servers}",
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        name="server_domain_validity",
                        status="PASS",
                        message="All server_clean values belong strictly to {biorxiv, medrxiv}.",
                    )
                )

            # Date bounds & nullity
            dates = pd.to_datetime(df["first_posted_date"], errors="coerce")
            date_null_pct = float(dates.isna().mean() * 100.0)
            max_allowed_year = datetime.now(timezone.utc).year + 1
            out_of_bounds = int(((dates.dt.year < 2013) | (dates.dt.year > max_allowed_year)).sum())
            if date_null_pct > 0.5 or out_of_bounds > 0:
                checks.append(
                    CheckResult(
                        name="date_validity",
                        status="FAIL",
                        message=f"Date check failed: {date_null_pct:.2f}% null, {out_of_bounds} out-of-bounds dates.",
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        name="date_validity",
                        status="PASS",
                        message=f"Dates valid ({dates.min().date()} to {dates.max().date()}, {date_null_pct:.3f}% null).",
                    )
                )

            # Category nullity & cardinality
            cat_null_pct = float(df["category_clean"].isna().mean() * 100.0)
            cat_nunique = int(df["category_clean"].nunique())
            if cat_null_pct > 2.0:
                checks.append(
                    CheckResult(
                        name="category_completeness",
                        status="WARN",
                        message=f"Elevated category null rate: {cat_null_pct:.2f}% ({cat_nunique} unique).",
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        name="category_completeness",
                        status="PASS",
                        message=f"Category null rate is {cat_null_pct:.2f}% across {cat_nunique} canonical categories.",
                    )
                )

    # 6. Baseline regression / shrinkage guard
    diff: dict[str, Any] | None = None
    if baseline_json_path is not None and baseline_json_path.exists():
        baseline = json.loads(baseline_json_path.read_text(encoding="utf-8"))
        diff = compute_release_diff(baseline, candidate)
        pct_change = diff["metrics"]["unique_dois"]["pct_change"]
        if pct_change is not None and pct_change < -max_shrinkage_pct:
            checks.append(
                CheckResult(
                    name="non_shrinkage_regression",
                    status="FAIL",
                    message=(
                        f"Dataset shrank by {pct_change:.2f}% vs baseline "
                        f"(allowed drop <= {max_shrinkage_pct:.1f}%)."
                    ),
                )
            )
        else:
            delta_n = diff["metrics"]["unique_dois"]["delta"]
            checks.append(
                CheckResult(
                    name="non_shrinkage_regression",
                    status="PASS",
                    message=f"Row count delta vs baseline: {delta_n:+,} DOIs ({pct_change:+.2f}%)."
                    if pct_change is not None
                    else f"Row count delta vs baseline: {delta_n:+,} DOIs.",
                )
            )

        pub_delta = diff["metrics"]["mature_pub_rate_pct"]["delta"]
        if pub_delta is not None and abs(pub_delta) > 5.0:
            checks.append(
                CheckResult(
                    name="publication_rate_stability",
                    status="WARN",
                    message=f"Large mature publication rate shift vs baseline: {pub_delta:+.2f} pp.",
                )
            )

    passed = all(c.status != "FAIL" for c in checks)
    return QualityReport(passed=passed, checked_at_utc=now_iso, checks=checks, diff=diff)
