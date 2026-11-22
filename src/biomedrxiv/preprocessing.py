"""Reusable cleaning and DOI-level deduplication utilities for Bio&Med-Rxiv data.

Addresses real data quality issues observed in ``biorxiv_medrxiv_preprints.csv``:
- Multiple rows per DOI (one per preprint version, up to v26): provides both
  cleaned version-level data and canonical DOI-level deduplication.
- Trailing whitespace and mixed case in ``category`` (115 raw -> 76 canonical).
- Mixed case and trailing newlines in ``type`` (e.g. ``PUBLISHAHEADOFPRINT\\n``,
  ``withdrawn`` vs ``WITHDRAWN``).
- Mixed hyphen/underscore and casing in ``license`` (e.g. ``CC-BY-NC-ND`` vs
  ``cc_by_nc_nd``).
- Double whitespace in ``author_corresponding`` and trailing semicolons in ``authors``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from biomedrxiv.publication_checker import normalize_doi

DEFAULT_CSV_NAME = "biorxiv_medrxiv_preprints.csv"
MULTI_SPACE_RE = re.compile(r"\s+")


def count_authors(authors_raw: object) -> int:
    """Count non-empty authors in a semicolon-delimited string."""
    if authors_raw is None or (isinstance(authors_raw, float) and pd.isna(authors_raw)):
        return 0
    parts = [p.strip() for p in str(authors_raw).split(";") if p.strip()]
    return len(parts)


def clean_preprints_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Standardize string fields, dates, licenses, types, and publication flags."""
    out = df.copy()

    if "doi" in out.columns:
        out["normalized_doi"] = out["doi"].map(normalize_doi)

    if "server" in out.columns:
        out["server_clean"] = out["server"].astype("string").str.strip().str.lower()

    if "category" in out.columns:
        out["category_clean"] = out["category"].astype("string").str.strip().str.lower()

    if "type" in out.columns:
        type_clean = (
            out["type"]
            .astype("string")
            .str.replace(r"\s+", " ", regex=True)
            .str.strip()
            .str.lower()
        )
        out["type_clean"] = type_clean
        out["is_withdrawn"] = type_clean.eq("withdrawn").fillna(False)

    if "license" in out.columns:
        out["license_clean"] = (
            out["license"]
            .astype("string")
            .str.strip()
            .str.lower()
            .str.replace("-", "_", regex=False)
        )

    if "author_corresponding" in out.columns:
        out["author_corresponding_clean"] = (
            out["author_corresponding"]
            .astype("string")
            .str.strip()
            .map(lambda s: MULTI_SPACE_RE.sub(" ", s) if pd.notna(s) else pd.NA)
        )

    if "authors" in out.columns:
        out["author_count"] = out["authors"].map(count_authors)

    if "date" in out.columns:
        out["posted_date"] = pd.to_datetime(out["date"], errors="coerce")

    if "published" in out.columns:
        pub_norm = out["published"].map(normalize_doi)
        out["snapshot_published_doi"] = pub_norm
        out["snapshot_is_published"] = pub_norm.notna()

    return out


def deduplicate_by_doi(cleaned_df: pd.DataFrame) -> pd.DataFrame:
    """Collapse version-level rows into one canonical record per unique preprint DOI.

    Preserves the latest version's metadata while adding longitudinal version
    features: ``first_posted_date``, ``latest_posted_date``, ``num_versions``,
    ``revision_span_days``, and ``ever_withdrawn``.
    """
    if "normalized_doi" not in cleaned_df.columns:
        cleaned_df = clean_preprints_dataframe(cleaned_df)

    valid = cleaned_df.dropna(subset=["normalized_doi"]).copy()
    sort_cols = ["normalized_doi"]
    if "version" in valid.columns:
        sort_cols.append("version")
    if "posted_date" in valid.columns:
        sort_cols.append("posted_date")
    valid = valid.sort_values(sort_cols, kind="mergesort")

    grouped = valid.groupby("normalized_doi", sort=False)
    latest = grouped.last().reset_index()

    agg_stats = grouped.agg(
        num_versions=("version", "max") if "version" in valid.columns else ("normalized_doi", "size"),
        version_row_count=("normalized_doi", "size"),
        first_posted_date=("posted_date", "min"),
        latest_posted_date=("posted_date", "max"),
        ever_withdrawn=("is_withdrawn", "any")
        if "is_withdrawn" in valid.columns
        else ("normalized_doi", lambda s: False),
    ).reset_index()
    agg_stats["revision_span_days"] = (
        agg_stats["latest_posted_date"] - agg_stats["first_posted_date"]
    ).dt.days

    merged = latest.merge(agg_stats, on="normalized_doi", how="left")
    return merged


def load_current_preprints(
    current_dir: Path,
    *,
    filename: str = DEFAULT_CSV_NAME,
    usecols: list[str] | None = None,
    nrows: int | None = None,
    deduplicate: bool = False,
) -> pd.DataFrame:
    """Load and clean the active dataset snapshot from ``data/current``."""
    csv_path = current_dir / filename
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Active dataset CSV not found at {csv_path}. "
            "Run: .venv/bin/python scripts/download_dataset.py sync"
        )
    df = pd.read_csv(csv_path, usecols=usecols, nrows=nrows, low_memory=False)
    cleaned = clean_preprints_dataframe(df)
    return deduplicate_by_doi(cleaned) if deduplicate else cleaned
