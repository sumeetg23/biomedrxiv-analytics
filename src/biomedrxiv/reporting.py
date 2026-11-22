"""Materialize cleaned Parquet tables and compute dashboard analytics payloads.

Designed to handle the full 1.31 GB / 605,767-row Bio&Med-Rxiv CSV safely with
bounded memory using chunked streaming into PyArrow Parquet files.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from biomedrxiv.config import ProjectPaths
from biomedrxiv.preprocessing import DEFAULT_CSV_NAME, clean_preprints_dataframe

logger = logging.getLogger(__name__)

# Curated DOI prefix -> Scholarly Publisher mapping (Crossref standard prefixes)
PUBLISHER_PREFIX_MAP: dict[str, str] = {
    "10.1038": "Nature Portfolio / Springer Nature",
    "10.1016": "Elsevier / Cell Press / Lancet",
    "10.1371": "PLOS",
    "10.1093": "Oxford University Press",
    "10.1111": "Wiley (Society Journals)",
    "10.1002": "Wiley",
    "10.1007": "Springer",
    "10.3389": "Frontiers",
    "10.7554": "eLife",
    "10.1186": "BMC / Springer Nature",
    "10.1073": "PNAS",
    "10.1126": "Science / AAAS",
    "10.1136": "BMJ",
    "10.3390": "MDPI",
    "10.1021": "American Chemical Society (ACS)",
    "10.1080": "Taylor & Francis",
    "10.1128": "American Society for Microbiology (ASM)",
    "10.1523": "Journal of Neuroscience (SfN)",
    "10.1098": "The Royal Society",
    "10.1177": "SAGE Publications",
    "10.1001": "JAMA Network (AMA)",
    "10.1056": "NEJM",
    "10.1101": "Cold Spring Harbor Laboratory Press",
    "10.1152": "American Physiological Society",
    "10.1084": "Rockefeller University Press",
    "10.1083": "Rockefeller University Press",
    "10.1085": "Rockefeller University Press",
    "10.1172": "JCI / ASCI",
    "10.1017": "Cambridge University Press",
    "10.1109": "IEEE",
    "10.1074": "ASBMB (JBC)",
    "10.15252": "EMBO Press",
    "10.1161": "American Heart Association",
    "10.1158": "AACR",
    "10.1242": "Company of Biologists",
    "10.1096": "FASEB",
    "10.1139": "Canadian Science Publishing",
    "10.1088": "IOP Publishing",
    "10.1039": "Royal Society of Chemistry",
    "10.7717": "PeerJ",
    "10.1534": "Genetics Society of America",
    "10.1091": "ASCB (MBoC)",
    "10.1200": "ASCO (JCO)",
    "10.1097": "Wolters Kluwer / Lippincott",
    "10.2196": "JMIR Publications",
    "10.1146": "Annual Reviews",
}

# Emerging biomedical frontier regexes scanned across title + abstract
FRONTIER_KEYWORDS: dict[str, tuple[str, re.Pattern[str]]] = {
    "covid_sarscov2": (
        "COVID-19 / SARS-CoV-2",
        re.compile(r"\b(?:covid-?19|sars-?cov-?2)\b", re.IGNORECASE),
    ),
    "single_cell": (
        "Single-Cell / scRNA-seq",
        re.compile(r"\b(?:single[- ]cell|scrna[- ]?seq|snrna[- ]?seq)\b", re.IGNORECASE),
    ),
    "deep_learning": (
        "Deep Learning / Neural Nets",
        re.compile(r"\b(?:deep learning|neural network|convolutional neural|transformer model)\b", re.IGNORECASE),
    ),
    "crispr": (
        "CRISPR / Gene Editing",
        re.compile(r"\b(?:crispr|cas9|cas12|cas13|base editing|prime editing)\b", re.IGNORECASE),
    ),
    "alphafold": (
        "AlphaFold / Protein Structure AI",
        re.compile(r"\b(?:alphafold|rosettafold|esmfold|protein structure prediction)\b", re.IGNORECASE),
    ),
    "llm_genai": (
        "LLMs & Generative AI",
        re.compile(
            r"\b(?:large language model|chatgpt|gpt-?4|generative ai|foundation model)\b",
            re.IGNORECASE,
        ),
    ),
    "spatial_omics": (
        "Spatial Transcriptomics",
        re.compile(r"\b(?:spatial transcriptom|spatial omics|visium|merfish|seqfish)\b", re.IGNORECASE),
    ),
    "mrna_vaccine": (
        "mRNA Vaccines / Therapeutics",
        re.compile(r"\b(?:mrna vaccine|lipid nanoparticle|bnt162b2|mrna-?1273)\b", re.IGNORECASE),
    ),
    "glp1": (
        "GLP-1 / Incretin Therapies",
        re.compile(r"\b(?:glp-?1|semaglutide|tirzepatide|ozempic|wegovy)\b", re.IGNORECASE),
    ),
}

MATURE_COHORT_MAX_YEAR = 2024


def map_publisher_from_doi(published_doi: Any) -> tuple[str | None, str | None]:
    """Extract DOI prefix and human-readable publisher name."""
    if published_doi is None or (isinstance(published_doi, float) and pd.isna(published_doi)):
        return None, None
    text = str(published_doi).strip().lower()
    if "/" not in text:
        return None, None
    prefix = text.split("/", 1)[0]
    return prefix, PUBLISHER_PREFIX_MAP.get(prefix, f"Other ({prefix})")


def wilson_ci_pct(successes: int, total: int, z: float = 1.959964) -> tuple[float, float, float]:
    """Return (rate_pct, ci_low_pct, ci_high_pct) using the Wilson score interval."""
    if total <= 0:
        return 0.0, 0.0, 0.0
    p = successes / total
    denom = 1.0 + (z * z) / total
    center = (p + (z * z) / (2.0 * total)) / denom
    margin = (z / denom) * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * total)) / total)
    low = max(0.0, center - margin) * 100.0
    high = min(1.0, center + margin) * 100.0
    return round(p * 100.0, 2), round(low, 2), round(high, 2)


def _normalize_institution(name: Any) -> str | None:
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return None
    cleaned = re.sub(r"\s+", " ", str(name)).strip().strip(",.;")
    if not cleaned or cleaned.lower() in {"na", "none", "n/a", "unknown"}:
        return None
    return cleaned


def _team_size_bucket(n: int) -> str:
    if n <= 1:
        return "1 (Solo)"
    if n <= 3:
        return "2–3"
    if n <= 6:
        return "4–6"
    if n <= 10:
        return "7–10"
    if n <= 20:
        return "11–20"
    return "21+"


def _enrich_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    """Clean a raw CSV chunk and attach engineered counts, publisher, and keyword flags."""
    cleaned = clean_preprints_dataframe(chunk)

    title_str = cleaned["title"].fillna("").astype(str) if "title" in cleaned.columns else pd.Series("", index=cleaned.index)
    abs_str = cleaned["abstract"].fillna("").astype(str) if "abstract" in cleaned.columns else pd.Series("", index=cleaned.index)
    combined_text = title_str + " " + abs_str

    cleaned["title_word_count"] = title_str.str.split().str.len().astype("int32")
    cleaned["abstract_word_count"] = abs_str.str.split().str.len().astype("int32")

    for kw_key, (_label, pattern) in FRONTIER_KEYWORDS.items():
        cleaned[f"kw_{kw_key}"] = combined_text.str.contains(pattern, regex=True).astype(bool)

    if "snapshot_published_doi" in cleaned.columns:
        prefixes = cleaned["snapshot_published_doi"].map(
            lambda d: str(d).split("/", 1)[0] if pd.notna(d) and "/" in str(d) else None
        )
        cleaned["publisher_prefix"] = prefixes.astype("string")
        cleaned["publisher_name"] = prefixes.map(
            lambda p: PUBLISHER_PREFIX_MAP.get(p, f"Other ({p})") if pd.notna(p) else None
        ).astype("string")
    else:
        cleaned["publisher_prefix"] = pd.Series(pd.NA, index=cleaned.index, dtype="string")
        cleaned["publisher_name"] = pd.Series(pd.NA, index=cleaned.index, dtype="string")

    if "author_corresponding_institution" in cleaned.columns:
        cleaned["institution_clean"] = (
            cleaned["author_corresponding_institution"]
            .map(_normalize_institution)
            .astype("string")
        )
    else:
        cleaned["institution_clean"] = pd.Series(pd.NA, index=cleaned.index, dtype="string")

    if "version" in cleaned.columns:
        cleaned["version"] = pd.to_numeric(cleaned["version"], errors="coerce").fillna(1).astype("int32")
    else:
        cleaned["version"] = np.int32(1)

    if "author_count" in cleaned.columns:
        cleaned["author_count"] = cleaned["author_count"].astype("int32")
    else:
        cleaned["author_count"] = np.int32(0)

    return cleaned


CANONICAL_COMPACT_COLUMNS = [
    "normalized_doi",
    "doi",
    "doi_link",
    "title",
    "server_clean",
    "category_clean",
    "type_clean",
    "license_clean",
    "is_withdrawn",
    "author_corresponding_clean",
    "institution_clean",
    "author_count",
    "title_word_count",
    "abstract_word_count",
    "version",
    "posted_date",
    "snapshot_is_published",
    "snapshot_published_doi",
    "publisher_prefix",
    "publisher_name",
    *[f"kw_{k}" for k in FRONTIER_KEYWORDS],
]


def materialize_parquet_tables(
    paths: ProjectPaths,
    *,
    csv_path: Path | None = None,
    chunksize: int = 75_000,
    write_versions_parquet: bool = True,
) -> dict[str, Any]:
    """Stream-clean the raw CSV into version-level and canonical DOI-level Parquet tables.

    Returns a dictionary of materialization metadata and raw-vs-clean quality audit counts.
    """
    paths.ensure_directories()
    source_csv = csv_path or (paths.current_link / DEFAULT_CSV_NAME)
    if not source_csv.exists():
        raise FileNotFoundError(
            f"Dataset CSV not found at {source_csv}. Run scripts/download_dataset.py sync first."
        )

    versions_out = paths.processed_dir / "preprints_versions.parquet"
    canonical_out = paths.processed_dir / "preprints_canonical.parquet"

    compact_chunks: list[pd.DataFrame] = []
    writer: pq.ParquetWriter | None = None
    schema_ref: pa.Schema | None = None

    raw_row_count = 0
    raw_categories: set[str] = set()
    clean_categories: set[str] = set()
    raw_types: set[str] = set()
    raw_licenses: set[str] = set()
    withdrawn_version_rows = 0
    raw_published_rows = 0

    logger.info("Reading and cleaning %s in chunks of %d rows...", source_csv, chunksize)
    try:
        for idx, chunk in enumerate(
            pd.read_csv(source_csv, chunksize=chunksize, low_memory=False), start=1
        ):
            raw_row_count += len(chunk)
            if "category" in chunk.columns:
                raw_categories.update(chunk["category"].dropna().astype(str).unique())
            if "type" in chunk.columns:
                raw_types.update(chunk["type"].dropna().astype(str).unique())
            if "license" in chunk.columns:
                raw_licenses.update(chunk["license"].dropna().astype(str).unique())

            enriched = _enrich_chunk(chunk)
            clean_categories.update(enriched["category_clean"].dropna().astype(str).unique())
            withdrawn_version_rows += int(enriched["is_withdrawn"].sum())
            raw_published_rows += int(enriched["snapshot_is_published"].sum())

            if write_versions_parquet:
                # Drop raw redundant text columns only? Keep all cleaned + raw columns for full fidelity
                table = pa.Table.from_pandas(enriched, preserve_index=False)
                if writer is None:
                    schema_ref = table.schema
                    writer = pq.ParquetWriter(versions_out, schema_ref, compression="zstd")
                else:
                    # Align schema in case a chunk had all-null column inferred as null type
                    table = table.cast(schema_ref, safe=False)
                writer.write_table(table)

            present_cols = [c for c in CANONICAL_COMPACT_COLUMNS if c in enriched.columns]
            compact_chunks.append(enriched[present_cols])
            logger.info("Processed chunk %d (%d cumulative rows)...", idx, raw_row_count)
    finally:
        if writer is not None:
            writer.close()

    logger.info("Building canonical DOI-level Parquet table...")
    all_compact = pd.concat(compact_chunks, ignore_index=True)
    del compact_chunks

    valid = all_compact.dropna(subset=["normalized_doi"]).sort_values(
        ["normalized_doi", "version", "posted_date"], kind="mergesort"
    )
    grouped = valid.groupby("normalized_doi", sort=False)

    latest = grouped.last().reset_index()
    agg_spec: dict[str, tuple[str, Any]] = {
        "num_versions": ("version", "max"),
        "version_row_count": ("version", "size"),
        "first_posted_date": ("posted_date", "min"),
        "latest_posted_date": ("posted_date", "max"),
        "ever_withdrawn": ("is_withdrawn", "any"),
        "snapshot_is_published_any": ("snapshot_is_published", "any"),
    }
    for kw_key in FRONTIER_KEYWORDS:
        agg_spec[f"kw_{kw_key}_any"] = (f"kw_{kw_key}", "any")

    agg_df = grouped.agg(**agg_spec).reset_index()
    agg_df["revision_span_days"] = (
        agg_df["latest_posted_date"] - agg_df["first_posted_date"]
    ).dt.days

    canonical = latest.merge(agg_df, on="normalized_doi", how="left")
    canonical["snapshot_is_published"] = canonical.pop("snapshot_is_published_any").astype(bool)
    for kw_key in FRONTIER_KEYWORDS:
        canonical[f"kw_{kw_key}"] = canonical.pop(f"kw_{kw_key}_any").astype(bool)

    canonical.to_parquet(canonical_out, index=False, compression="zstd")
    logger.info(
        "Materialized canonical Parquet: %d unique DOIs -> %s (%.1f MiB)",
        len(canonical),
        canonical_out,
        canonical_out.stat().st_size / (1024 * 1024),
    )

    audit = {
        "raw_row_count": int(raw_row_count),
        "unique_doi_count": int(len(canonical)),
        "duplicate_version_rows": int(raw_row_count - len(canonical)),
        "raw_unique_categories": int(len(raw_categories)),
        "clean_unique_categories": int(len(clean_categories)),
        "raw_unique_types": int(len(raw_types)),
        "raw_unique_licenses": int(len(raw_licenses)),
        "withdrawn_version_rows": int(withdrawn_version_rows),
        "withdrawn_unique_dois": int(canonical["ever_withdrawn"].sum()),
        "raw_row_publication_rate_pct": round((raw_published_rows / max(raw_row_count, 1)) * 100.0, 2),
        "dedup_doi_publication_rate_pct": round(float(canonical["snapshot_is_published"].mean() * 100.0), 2),
        "versions_parquet": (
            versions_out.relative_to(paths.root).as_posix()
            if write_versions_parquet
            else None
        ),
        "versions_parquet_mb": round(versions_out.stat().st_size / (1024 * 1024), 2)
        if write_versions_parquet and versions_out.exists()
        else None,
        "canonical_parquet": canonical_out.relative_to(paths.root).as_posix(),
        "canonical_parquet_mb": round(canonical_out.stat().st_size / (1024 * 1024), 2),
    }

    audit_path = paths.processed_dir / "materialization_audit.json"
    audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    return audit


def build_dashboard_payload(
    paths: ProjectPaths,
    *,
    canonical_df: pd.DataFrame | None = None,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute compact, publication-grade analytics JSON powering the static web dashboard."""
    canonical_path = paths.processed_dir / "preprints_canonical.parquet"
    if canonical_df is None:
        if not canonical_path.exists():
            raise FileNotFoundError(
                f"Canonical Parquet not found at {canonical_path}. Run materialize first."
            )
        df = pd.read_parquet(canonical_path)
    else:
        df = canonical_df.copy()

    if audit is None:
        audit_path = paths.processed_dir / "materialization_audit.json"
        if audit_path.exists():
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        else:
            audit = {
                "raw_row_count": int(df["version_row_count"].sum()) if "version_row_count" in df.columns else int(len(df)),
                "unique_doi_count": int(len(df)),
                "duplicate_version_rows": int(df["version_row_count"].sum() - len(df)) if "version_row_count" in df.columns else 0,
                "raw_unique_categories": int(df["category_clean"].nunique()),
                "clean_unique_categories": int(df["category_clean"].nunique()),
                "withdrawn_unique_dois": int(df["ever_withdrawn"].sum()),
                "raw_row_publication_rate_pct": round(float(df["snapshot_is_published"].mean() * 100), 2),
                "dedup_doi_publication_rate_pct": round(float(df["snapshot_is_published"].mean() * 100), 2),
            }

    registry_meta: dict[str, Any] = {}
    if paths.registry_file.exists():
        reg = json.loads(paths.registry_file.read_text(encoding="utf-8"))
        cur_vid = reg.get("current_version")
        for v in reg.get("versions", []):
            if v.get("version_id") == cur_vid:
                registry_meta = {
                    "dataset_handle": reg.get("dataset_handle"),
                    "version_id": cur_vid,
                    "created_at_utc": v.get("created_at_utc"),
                    "content_sha256": v.get("content_sha256", "")[:16],
                    "total_size_gb": round(v.get("total_size_bytes", 0) / (1024**3), 2),
                    "versions_tracked": len(reg.get("versions", [])),
                }
                break

    df["first_posted_date"] = pd.to_datetime(df["first_posted_date"], errors="coerce")
    df["year"] = df["first_posted_date"].dt.year
    df["month"] = df["first_posted_date"].dt.to_period("M").astype(str)
    df["quarter"] = df["first_posted_date"].dt.to_period("Q").astype(str)
    df["half_year"] = (
        df["year"].astype("Int64").astype(str)
        + "-H"
        + np.where(df["first_posted_date"].dt.month <= 6, "1", "2")
    )

    mature = df[df["year"] <= MATURE_COHORT_MAX_YEAR]

    # 1. KPI summary per server filter (all, biorxiv, medrxiv)
    kpis: dict[str, Any] = {}
    for key, subset in [
        ("all", df),
        ("biorxiv", df[df["server_clean"] == "biorxiv"]),
        ("medrxiv", df[df["server_clean"] == "medrxiv"]),
    ]:
        sub_mature = subset[subset["year"] <= MATURE_COHORT_MAX_YEAR]
        kpis[key] = {
            "unique_dois": int(len(subset)),
            "version_rows": int(subset["version_row_count"].sum())
            if "version_row_count" in subset.columns
            else int(len(subset)),
            "overall_pub_rate_pct": round(float(subset["snapshot_is_published"].mean() * 100.0), 2)
            if len(subset)
            else 0.0,
            "mature_pub_rate_pct": round(
                float(sub_mature["snapshot_is_published"].mean() * 100.0), 2
            )
            if len(sub_mature)
            else 0.0,
            "mature_cohort_cutoff_year": MATURE_COHORT_MAX_YEAR,
            "median_authors": float(subset["author_count"].median()) if len(subset) else 0.0,
            "avg_versions": round(float(subset["num_versions"].mean()), 2) if len(subset) else 0.0,
            "max_versions": int(subset["num_versions"].max()) if len(subset) else 0,
            "withdrawn_count": int(subset["ever_withdrawn"].sum()),
            "withdrawn_per_1k": round(
                float(subset["ever_withdrawn"].mean() * 1000.0), 2
            )
            if len(subset)
            else 0.0,
            "unique_categories": int(subset["category_clean"].nunique()),
            "date_min": str(subset["first_posted_date"].min().date()) if len(subset) else "",
            "date_max": str(subset["first_posted_date"].max().date()) if len(subset) else "",
        }

    # 2. Monthly volume (2013-11 .. 2026-10)
    monthly_piv = (
        df.groupby(["month", "server_clean"], observed=True)
        .size()
        .unstack(fill_value=0)
        .sort_index()
    )
    for col in ("biorxiv", "medrxiv"):
        if col not in monthly_piv.columns:
            monthly_piv[col] = 0
    monthly_piv["total"] = monthly_piv["biorxiv"] + monthly_piv["medrxiv"]
    monthly_piv["cumulative_total"] = monthly_piv["total"].cumsum()
    monthly_growth = {
        "months": monthly_piv.index.tolist(),
        "biorxiv": monthly_piv["biorxiv"].astype(int).tolist(),
        "medrxiv": monthly_piv["medrxiv"].astype(int).tolist(),
        "total": monthly_piv["total"].astype(int).tolist(),
        "cumulative_total": monthly_piv["cumulative_total"].astype(int).tolist(),
    }

    # Annual summary & YoY growth
    annual_piv = (
        df.groupby(["year", "server_clean"], observed=True)
        .size()
        .unstack(fill_value=0)
        .sort_index()
    )
    for col in ("biorxiv", "medrxiv"):
        if col not in annual_piv.columns:
            annual_piv[col] = 0
    annual_piv["total"] = annual_piv["biorxiv"] + annual_piv["medrxiv"]
    annual_pub = df.groupby("year")["snapshot_is_published"].mean().reindex(annual_piv.index) * 100.0
    annual_summary = {
        "years": [int(y) for y in annual_piv.index.tolist()],
        "biorxiv": annual_piv["biorxiv"].astype(int).tolist(),
        "medrxiv": annual_piv["medrxiv"].astype(int).tolist(),
        "total": annual_piv["total"].astype(int).tolist(),
        "pub_rate_pct": [round(float(x), 2) for x in annual_pub.fillna(0.0).tolist()],
    }

    # 3. Top categories per server slice (with mature-cohort publication rate)
    top_categories: dict[str, list[dict[str, Any]]] = {}
    for key, subset in [
        ("all", df),
        ("biorxiv", df[df["server_clean"] == "biorxiv"]),
        ("medrxiv", df[df["server_clean"] == "medrxiv"]),
    ]:
        sub_mat = subset[subset["year"] <= MATURE_COHORT_MAX_YEAR]
        vol = subset.groupby("category_clean").agg(
            count=("normalized_doi", "size"),
            overall_pub_pct=("snapshot_is_published", lambda s: round(float(s.mean() * 100), 1)),
            median_authors=("author_count", "median"),
            avg_versions=("num_versions", "mean"),
        )
        mat_rate = sub_mat.groupby("category_clean")["snapshot_is_published"].mean() * 100.0
        vol["mature_pub_pct"] = mat_rate.reindex(vol.index).round(1).fillna(0.0)
        top20 = vol.sort_values("count", ascending=False).head(20).reset_index()
        top_categories[key] = [
            {
                "category": str(r["category_clean"]).title(),
                "count": int(r["count"]),
                "overall_pub_pct": float(r["overall_pub_pct"]),
                "mature_pub_pct": float(r["mature_pub_pct"]),
                "median_authors": float(r["median_authors"]),
                "avg_versions": round(float(r["avg_versions"]), 2),
            }
            for _, r in top20.iterrows()
        ]

    # 4. Cohort Publication Heatmap (Posting Year 2015..2026 x Top 18 Categories)
    top18_cats = (
        df["category_clean"].value_counts().head(18).index.tolist()
    )
    heat_years = sorted(int(y) for y in df["year"].dropna().unique() if y >= 2015)
    heat_sub = df[df["category_clean"].isin(top18_cats) & df["year"].isin(heat_years)]
    heat_piv = (
        heat_sub.groupby(["category_clean", "year"])["snapshot_is_published"]
        .agg(["mean", "size"])
    )
    z_matrix: list[list[float | None]] = []
    n_matrix: list[list[int]] = []
    for cat in top18_cats:
        row_z: list[float | None] = []
        row_n: list[int] = []
        for yr in heat_years:
            if (cat, yr) in heat_piv.index:
                cnt = int(heat_piv.loc[(cat, yr), "size"])
                rate = round(float(heat_piv.loc[(cat, yr), "mean"] * 100.0), 1)
                row_z.append(rate if cnt >= 15 else None)
                row_n.append(cnt)
            else:
                row_z.append(None)
                row_n.append(0)
        z_matrix.append(row_z)
        n_matrix.append(row_n)

    cohort_heatmap = {
        "years": heat_years,
        "categories": [c.title() for c in top18_cats],
        "pub_rate_pct": z_matrix,
        "counts": n_matrix,
    }

    # 5. Revision Count vs Peer-Review Publication Rate (Mature cohorts <= 2024)
    revision_buckets = ["1", "2", "3", "4", "5+"]
    rev_col = np.where(mature["num_versions"] >= 5, "5+", mature["num_versions"].astype(str))
    mature_rev = mature.assign(rev_bucket=rev_col)
    revisions_vs_pub: dict[str, list[dict[str, Any]]] = {}
    for srv in ("biorxiv", "medrxiv"):
        sub = mature_rev[mature_rev["server_clean"] == srv]
        rows_out = []
        for b in revision_buckets:
            g = sub[sub["rev_bucket"] == b]
            n = int(len(g))
            pub_n = int(g["snapshot_is_published"].sum())
            rate, lo, hi = wilson_ci_pct(pub_n, n)
            rows_out.append(
                {
                    "versions": b,
                    "count": n,
                    "published_count": pub_n,
                    "pub_rate_pct": rate,
                    "ci_low": lo,
                    "ci_high": hi,
                }
            )
        revisions_vs_pub[srv] = rows_out

    # 6. Top Publishers Absorbing Preprints (from DOI prefix)
    pub_only = df[df["snapshot_is_published"] & df["publisher_name"].notna()]
    top_publishers: dict[str, list[dict[str, Any]]] = {}
    for key, subset in [
        ("all", pub_only),
        ("biorxiv", pub_only[pub_only["server_clean"] == "biorxiv"]),
        ("medrxiv", pub_only[pub_only["server_clean"] == "medrxiv"]),
    ]:
        # Group long-tail "Other (10.xxxx)" into a single bucket for clean chart display
        mapped = subset["publisher_name"].map(
            lambda x: "Other Publishers" if str(x).startswith("Other (") else str(x)
        )
        vc = mapped.value_counts()
        top15 = vc.drop(labels=["Other Publishers"], errors="ignore").head(15)
        total_pub = max(len(subset), 1)
        top_publishers[key] = [
            {
                "publisher": pub_name,
                "count": int(cnt),
                "share_pct": round(float(cnt / total_pub * 100.0), 2),
            }
            for pub_name, cnt in top15.items()
        ]

    # 7. Open Science & License Evolution (2014..2026, 100% stacked)
    lic_order = ["cc_by", "cc_by_nc_nd", "cc_by_nc", "cc_by_nd", "cc_no", "cc0", "other"]
    lic_labels = {
        "cc_by": "CC-BY (Most Open)",
        "cc_by_nc_nd": "CC-BY-NC-ND",
        "cc_by_nc": "CC-BY-NC",
        "cc_by_nd": "CC-BY-ND",
        "cc_no": "All Rights Reserved (cc_no)",
        "cc0": "CC0 (Public Domain)",
        "other": "Other / Unspecified",
    }
    lic_years = sorted(int(y) for y in df["year"].dropna().unique() if y >= 2014)
    license_evolution: dict[str, Any] = {"years": lic_years, "labels": lic_labels, "by_server": {}}
    for key, subset in [
        ("all", df),
        ("biorxiv", df[df["server_clean"] == "biorxiv"]),
        ("medrxiv", df[df["server_clean"] == "medrxiv"]),
    ]:
        sub = subset[subset["year"].isin(lic_years)].copy()
        sub["lic_grp"] = sub["license_clean"].map(
            lambda x: "cc0"
            if str(x).startswith("cc0")
            else (str(x) if str(x) in lic_order else "other")
        )
        piv = sub.groupby(["year", "lic_grp"]).size().unstack(fill_value=0).reindex(lic_years, fill_value=0)
        row_totals = piv.sum(axis=1).replace(0, 1)
        series_map = {}
        for lk in lic_order:
            counts = piv[lk] if lk in piv.columns else pd.Series(0, index=lic_years)
            pcts = (counts / row_totals * 100.0).round(2)
            series_map[lk] = pcts.tolist()
        license_evolution["by_server"][key] = series_map

    # 8. Emerging Research Frontier Keyword Adoption (% of quarterly abstracts, 2015Q1..2026Q4)
    q_df = df[df["year"] >= 2015]
    q_totals = q_df.groupby("quarter").size().sort_index()
    quarters = q_totals.index.tolist()
    frontier_series: list[dict[str, Any]] = []
    for kw_key, (label, _pat) in FRONTIER_KEYWORDS.items():
        col = f"kw_{kw_key}"
        if col not in q_df.columns:
            continue
        q_hits = q_df.groupby("quarter")[col].sum().reindex(quarters, fill_value=0)
        pct = (q_hits / q_totals.replace(0, 1) * 100.0).round(3)
        frontier_series.append(
            {
                "key": kw_key,
                "label": label,
                "total_papers": int(q_hits.sum()),
                "pct_by_quarter": pct.tolist(),
                "count_by_quarter": q_hits.astype(int).tolist(),
            }
        )
    frontier_trends = {"quarters": quarters, "series": frontier_series}

    # 9. Withdrawal Rate Timeline (per 1,000 preprints by half-year, 2016-H1 .. 2026-H2)
    wd_df = df[df["year"] >= 2016]
    half_years = sorted(wd_df["half_year"].dropna().unique().tolist())
    withdrawals_timeline: dict[str, Any] = {"periods": half_years}
    for srv in ("biorxiv", "medrxiv"):
        sub = wd_df[wd_df["server_clean"] == srv]
        grp = sub.groupby("half_year")["ever_withdrawn"].agg(["sum", "size"])
        rates: list[float | None] = []
        counts: list[int] = []
        for p in half_years:
            if p in grp.index and grp.loc[p, "size"] >= 100:
                cnt = int(grp.loc[p, "sum"])
                tot = int(grp.loc[p, "size"])
                rates.append(round(cnt / tot * 1000.0, 2))
                counts.append(cnt)
            else:
                rates.append(None)
                counts.append(0)
        withdrawals_timeline[srv] = {"rate_per_1k": rates, "count": counts}

    # 10. Collaboration & Team Size Trends + Publication Rate by Team Size
    team_years = sorted(int(y) for y in df["year"].dropna().unique() if y >= 2014)
    team_trends: dict[str, Any] = {"years": team_years}
    for srv in ("biorxiv", "medrxiv"):
        sub = df[(df["server_clean"] == srv) & (df["author_count"] > 0) & df["year"].isin(team_years)]
        q50 = sub.groupby("year")["author_count"].median()
        q75 = sub.groupby("year")["author_count"].quantile(0.75)
        q25 = sub.groupby("year")["author_count"].quantile(0.25)
        team_trends[srv] = {
            "median": [round(float(q50.loc[y]), 1) if y in q50.index else None for y in team_years],
            "q25": [round(float(q25.loc[y]), 1) if y in q25.index else None for y in team_years],
            "q75": [round(float(q75.loc[y]), 1) if y in q75.index else None for y in team_years],
        }

    bucket_order = ["1 (Solo)", "2–3", "4–6", "7–10", "11–20", "21+"]
    mat_team = mature[mature["author_count"] > 0].copy()
    mat_team["team_bucket"] = mat_team["author_count"].map(_team_size_bucket)
    team_vs_pub: list[dict[str, Any]] = []
    for b in bucket_order:
        g = mat_team[mat_team["team_bucket"] == b]
        n = int(len(g))
        pub_n = int(g["snapshot_is_published"].sum())
        rate, lo, hi = wilson_ci_pct(pub_n, n)
        team_vs_pub.append(
            {"bucket": b, "count": n, "pub_rate_pct": rate, "ci_low": lo, "ci_high": hi}
        )

    # 11. Preprint Type & Publication Bias (bioRxiv mature cohorts where types are recorded)
    type_labels = {
        "new results": "New Results",
        "confirmatory results": "Confirmatory Results",
        "contradictory results": "Contradictory Results",
    }
    bio_mat = mature[mature["server_clean"] == "biorxiv"]
    type_bias: list[dict[str, Any]] = []
    for tk, tlabel in type_labels.items():
        g = bio_mat[bio_mat["type_clean"] == tk]
        n = int(len(g))
        pub_n = int(g["snapshot_is_published"].sum())
        rate, lo, hi = wilson_ci_pct(pub_n, n)
        type_bias.append(
            {"type": tlabel, "count": n, "pub_rate_pct": rate, "ci_low": lo, "ci_high": hi}
        )

    # 12. Top 30 Corresponding Institutions (volume + mature-cohort publication rate)
    inst_all = df.dropna(subset=["institution_clean"])
    inst_counts = inst_all["institution_clean"].value_counts().head(30)
    inst_mat = mature.dropna(subset=["institution_clean"])
    inst_mat_grp = inst_mat.groupby("institution_clean").agg(
        mature_count=("normalized_doi", "size"),
        mature_pub=("snapshot_is_published", "mean"),
    )
    inst_med_auth = inst_all.groupby("institution_clean")["author_count"].median()
    top_institutions: list[dict[str, Any]] = []
    for inst_name, cnt in inst_counts.items():
        m_cnt = int(inst_mat_grp.loc[inst_name, "mature_count"]) if inst_name in inst_mat_grp.index else 0
        m_pub = (
            round(float(inst_mat_grp.loc[inst_name, "mature_pub"] * 100.0), 1)
            if inst_name in inst_mat_grp.index
            else 0.0
        )
        med_a = float(inst_med_auth.loc[inst_name]) if inst_name in inst_med_auth.index else 0.0
        top_institutions.append(
            {
                "institution": str(inst_name),
                "total_preprints": int(cnt),
                "mature_preprints": m_cnt,
                "mature_pub_rate_pct": m_pub,
                "median_authors": med_a,
            }
        )

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_provenance": registry_meta,
        "quality_audit": audit,
        "kpis": kpis,
        "monthly_growth": monthly_growth,
        "annual_summary": annual_summary,
        "top_categories": top_categories,
        "cohort_heatmap": cohort_heatmap,
        "revisions_vs_pub": revisions_vs_pub,
        "top_publishers": top_publishers,
        "license_evolution": license_evolution,
        "frontier_trends": frontier_trends,
        "withdrawals_timeline": withdrawals_timeline,
        "team_trends": team_trends,
        "team_vs_pub": team_vs_pub,
        "type_bias": type_bias,
        "top_institutions": top_institutions,
    }
