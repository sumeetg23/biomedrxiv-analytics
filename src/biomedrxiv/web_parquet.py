"""Export compact, sorted Web Analytics Parquet tables for browser DuckDB-Wasm (Step 3).

Produces three self-contained, ZSTD-compressed Parquet files with ROR institutional
normalization pre-joined so browser DuckDB-Wasm queries run fast over HTTP Range
requests with minimal joins and zero credit-card hosting (GitHub Pages or Hugging Face):

  1. `preprints.parquet`           (~24 MiB, 454,051 canonical unique DOIs, sorted by server/year/category)
  2. `clinical_trials.parquet`     (~0.4 MiB, 3,034 trial-linked preprints enriched via ClinicalTrials.gov)
  3. `downstream_clinical.parquet` (~0.02 MiB, ~399 valid NIH iCite translational impact papers)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

WEB_PARQUET_ROW_GROUP_SIZE = 65_536


@dataclass(frozen=True)
class WebParquetStats:
    output_dir: str
    preprints_rows: int
    preprints_mib: float
    clinical_trials_rows: int
    clinical_trials_mib: float
    downstream_clinical_rows: int
    downstream_clinical_mib: float
    total_mib: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "files": {
                "preprints.parquet": {"rows": self.preprints_rows, "mib": round(self.preprints_mib, 2)},
                "clinical_trials.parquet": {
                    "rows": self.clinical_trials_rows,
                    "mib": round(self.clinical_trials_mib, 3),
                },
                "downstream_clinical.parquet": {
                    "rows": self.downstream_clinical_rows,
                    "mib": round(self.downstream_clinical_mib, 3),
                },
            },
            "total_mib": round(self.total_mib, 2),
        }


def _file_mib(path: Path) -> float:
    return path.stat().st_size / (1024.0 * 1024.0) if path.exists() else 0.0


def export_web_parquet(
    processed_dir: Path,
    output_dir: Path,
    row_group_size: int = WEB_PARQUET_ROW_GROUP_SIZE,
) -> WebParquetStats:
    """Materialize sorted, compact Parquet files for client-side DuckDB-Wasm."""
    processed_dir = Path(processed_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    preprints_src = processed_dir / "preprints_canonical.parquet"
    trials_src = processed_dir / "clinical_trials_enriched.parquet"
    downstream_src = processed_dir / "downstream_clinical_impact.parquet"
    ror_src = processed_dir / "institution_ror_map.parquet"

    if not preprints_src.exists():
        raise FileNotFoundError(f"Missing required Parquet source: {preprints_src}")

    con = duckdb.connect(":memory:")
    has_ror = ror_src.exists()
    ror_join_p = (
        f"LEFT JOIN read_parquet('{ror_src.as_posix()}') r ON p.institution_clean = r.institution_clean"
        if has_ror
        else ""
    )
    ror_cols_p = (
        "COALESCE(r.ror_name, p.institution_clean) AS institution, "
        "r.ror_country AS country, "
        "r.ror_type AS institution_type"
        if has_ror
        else "p.institution_clean AS institution, NULL::VARCHAR AS country, NULL::VARCHAR AS institution_type"
    )

    # 1. preprints.parquet (all canonical unique DOIs, sorted for row-group pruning)
    preprints_out = output_dir / "preprints.parquet"
    con.execute(
        f"""
        COPY (
            SELECT
                p.normalized_doi AS doi,
                p.title,
                p.server_clean AS server,
                p.category_clean AS category,
                EXTRACT(YEAR FROM p.first_posted_date)::SMALLINT AS year,
                CAST(p.first_posted_date AS DATE) AS first_posted_date,
                CAST(p.latest_posted_date AS DATE) AS latest_posted_date,
                p.num_versions::SMALLINT AS num_versions,
                p.author_count::SMALLINT AS author_count,
                p.revision_span_days::INTEGER AS revision_span_days,
                p.snapshot_is_published AS is_published,
                p.snapshot_published_doi AS published_doi,
                p.publisher_name,
                {ror_cols_p},
                p.ever_withdrawn,
                p.kw_covid_sarscov2,
                p.kw_single_cell,
                p.kw_deep_learning,
                p.kw_crispr,
                p.kw_alphafold,
                p.kw_llm_genai,
                p.kw_spatial_omics,
                p.kw_mrna_vaccine,
                p.kw_glp1
            FROM read_parquet('{preprints_src.as_posix()}') p
            {ror_join_p}
            ORDER BY p.server_clean, EXTRACT(YEAR FROM p.first_posted_date), p.category_clean, p.normalized_doi
        ) TO '{preprints_out.as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 9, ROW_GROUP_SIZE {int(row_group_size)})
        """
    )
    preprints_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{preprints_out.as_posix()}')").fetchone()[0]

    # 2. clinical_trials.parquet (all trial-linked preprints)
    trials_out = output_dir / "clinical_trials.parquet"
    if trials_src.exists():
        ror_join_t = (
            f"LEFT JOIN read_parquet('{ror_src.as_posix()}') r ON t.institution_clean = r.institution_clean"
            if has_ror
            else ""
        )
        ror_cols_t = (
            "COALESCE(r.ror_name, t.institution_clean) AS institution, r.ror_country AS country"
            if has_ror
            else "t.institution_clean AS institution, NULL::VARCHAR AS country"
        )
        con.execute(
            f"""
            COPY (
                SELECT
                    t.normalized_doi AS doi,
                    t.trial_id,
                    t.registry,
                    t.title,
                    t.server_clean AS server,
                    t.category_clean AS category,
                    t.year::SMALLINT AS year,
                    t.snapshot_is_published AS is_published,
                    t.snapshot_published_doi AS published_doi,
                    t.publisher_name,
                    {ror_cols_t},
                    t.phase,
                    t.overall_status,
                    t.sponsor_class,
                    t.sponsor_name,
                    CAST(ROUND(t.enrollment_count) AS INTEGER) AS enrollment_count,
                    t.has_results,
                    CASE
                        WHEN t.conditions IS NOT NULL THEN array_to_string(t.conditions, ', ')
                        ELSE NULL
                    END AS conditions
                FROM read_parquet('{trials_src.as_posix()}') t
                {ror_join_t}
                ORDER BY t.server_clean, t.phase NULLS LAST, t.enrollment_count DESC NULLS LAST, t.trial_id
            ) TO '{trials_out.as_posix()}'
            (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 9)
            """
        )
    else:
        con.execute(
            f"""
            COPY (
                SELECT
                    NULL::VARCHAR AS doi, NULL::VARCHAR AS trial_id, NULL::VARCHAR AS registry,
                    NULL::VARCHAR AS title, NULL::VARCHAR AS server, NULL::VARCHAR AS category,
                    NULL::SMALLINT AS year, NULL::BOOLEAN AS is_published, NULL::VARCHAR AS published_doi,
                    NULL::VARCHAR AS publisher_name, NULL::VARCHAR AS institution, NULL::VARCHAR AS country,
                    NULL::VARCHAR AS phase, NULL::VARCHAR AS overall_status, NULL::VARCHAR AS sponsor_class,
                    NULL::VARCHAR AS sponsor_name, NULL::INTEGER AS enrollment_count, NULL::BOOLEAN AS has_results,
                    NULL::VARCHAR AS conditions
                WHERE FALSE
            ) TO '{trials_out.as_posix()}' (FORMAT PARQUET)
            """
        )
    trials_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{trials_out.as_posix()}')").fetchone()[0]

    # 3. downstream_clinical.parquet (valid 1-to-1 NIH iCite matches)
    downstream_out = output_dir / "downstream_clinical.parquet"
    if downstream_src.exists():
        con.execute(
            f"""
            COPY (
                SELECT
                    p.normalized_doi AS doi,
                    d.published_doi,
                    d.pmid,
                    p.title,
                    p.server_clean AS server,
                    p.category_clean AS category,
                    EXTRACT(YEAR FROM p.first_posted_date)::SMALLINT AS year,
                    p.publisher_name,
                    {ror_cols_p},
                    d.is_clinical,
                    d.apt,
                    d.cited_by_clin_count::INTEGER AS cited_by_clin_count,
                    d.citation_count::INTEGER AS citation_count,
                    d.relative_citation_ratio
                FROM read_parquet('{downstream_src.as_posix()}') d
                INNER JOIN read_parquet('{preprints_src.as_posix()}') p
                    ON LOWER(p.snapshot_published_doi) = LOWER(d.published_doi)
                {ror_join_p}
                WHERE d.published_doi LIKE '10.%/%'
                  AND LENGTH(SPLIT_PART(d.published_doi, '/', 2)) >= 3
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY LOWER(d.published_doi)
                    ORDER BY p.first_posted_date DESC NULLS LAST, p.normalized_doi
                ) = 1
                ORDER BY COALESCE(d.cited_by_clin_count, 0) DESC, COALESCE(d.apt, -1) DESC
            ) TO '{downstream_out.as_posix()}'
            (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 9)
            """
        )
    else:
        con.execute(
            f"""
            COPY (
                SELECT
                    NULL::VARCHAR AS doi, NULL::VARCHAR AS published_doi, NULL::VARCHAR AS pmid,
                    NULL::VARCHAR AS title, NULL::VARCHAR AS server, NULL::VARCHAR AS category,
                    NULL::SMALLINT AS year, NULL::VARCHAR AS publisher_name, NULL::VARCHAR AS institution,
                    NULL::VARCHAR AS country, NULL::VARCHAR AS institution_type, NULL::BOOLEAN AS is_clinical,
                    NULL::DOUBLE AS apt, NULL::INTEGER AS cited_by_clin_count, NULL::INTEGER AS citation_count,
                    NULL::DOUBLE AS relative_citation_ratio
                WHERE FALSE
            ) TO '{downstream_out.as_posix()}' (FORMAT PARQUET)
            """
        )
    downstream_rows = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{downstream_out.as_posix()}')"
    ).fetchone()[0]

    p_mib = _file_mib(preprints_out)
    t_mib = _file_mib(trials_out)
    d_mib = _file_mib(downstream_out)

    stats = WebParquetStats(
        output_dir=str(output_dir),
        preprints_rows=int(preprints_rows),
        preprints_mib=p_mib,
        clinical_trials_rows=int(trials_rows),
        clinical_trials_mib=t_mib,
        downstream_clinical_rows=int(downstream_rows),
        downstream_clinical_mib=d_mib,
        total_mib=p_mib + t_mib + d_mib,
    )

    manifest = {
        "files": {
            "preprints": "preprints.parquet",
            "clinical_trials": "clinical_trials.parquet",
            "downstream_clinical": "downstream_clinical.parquet",
        },
        "stats": stats.to_dict()["files"],
        "total_mib": round(stats.total_mib, 2),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return stats
