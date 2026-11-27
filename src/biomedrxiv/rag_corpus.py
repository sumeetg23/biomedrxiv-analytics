"""Build a compact semantic-search corpus for Cloudflare Vectorize (Step 2 RAG).

Selects up to `max_docs` (default 12,500) high-signal records so that
`max_docs * 384 <= 4,800,000` dimensions (comfortably below Cloudflare Vectorize's
5,000,000 free stored-dimension limit with `@cf/baai/bge-small-en-v1.5`):

  1. Tier A (all rows):  Clinical trial-linked preprints (`clinical_trials_enriched.parquet`)
  2. Tier B (all rows):  NIH iCite downstream translational papers (`downstream_clinical_impact.parquet`)
  3. Tier C (fill cap):  High-signal general preprints (`preprints_canonical.parquet`)
                         stratified across all canonical categories & servers.

All metadata values are flattened to primitive JSON scalars (`str | int | float | bool`)
and vector IDs are guaranteed <= 64 ASCII bytes for Cloudflare Vectorize v2 compatibility.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

EMBEDDING_DIMENSIONS = 384
VECTORIZE_FREE_DIM_CAP = 5_000_000
DEFAULT_MAX_DOCS = 12_500  # 12,500 * 384 = 4,800,000 dims (< 5M free cap)
MAX_VECTOR_ID_BYTES = 64
MAX_METADATA_BYTES = 4096  # Vectorize allows 10 KiB; keep compact for fast responses


@dataclass(frozen=True)
class RagCorpusStats:
    total_docs: int
    clinical_trial_docs: int
    downstream_clinical_docs: int
    general_preprint_docs: int
    unique_categories: int
    estimated_tokens: int
    estimated_neurons: float
    stored_dimensions: int
    free_cap_utilization_pct: float
    output_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_docs": self.total_docs,
            "clinical_trial_docs": self.clinical_trial_docs,
            "downstream_clinical_docs": self.downstream_clinical_docs,
            "general_preprint_docs": self.general_preprint_docs,
            "unique_categories": self.unique_categories,
            "embedding_dimensions": EMBEDDING_DIMENSIONS,
            "stored_dimensions": self.stored_dimensions,
            "free_cap_utilization_pct": round(self.free_cap_utilization_pct, 2),
            "estimated_tokens": self.estimated_tokens,
            "estimated_neurons": round(self.estimated_neurons, 1),
            "output_path": self.output_path,
        }


def _clean_str(val: Any, max_len: int = 240) -> str:
    if val is None:
        return ""
    s = " ".join(str(val).split()).strip()
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


def _make_vector_id(prefix: str, key: str) -> str:
    """Create a deterministic ASCII vector ID <= 64 bytes."""
    raw = f"{prefix}:{key}"
    ascii_safe = raw.encode("ascii", errors="ignore").decode("ascii").replace(" ", "_")
    if len(ascii_safe.encode("utf-8")) <= MAX_VECTOR_ID_BYTES:
        return ascii_safe
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    keep = MAX_VECTOR_ID_BYTES - len(prefix) - len(digest) - 2
    return f"{prefix}:{ascii_safe[len(prefix) + 1 : len(prefix) + 1 + max(keep, 8)]}:{digest}"


def _sanitize_metadata(meta: dict[str, Any]) -> dict[str, str | int | float | bool]:
    """Ensure every metadata value is a Vectorize-safe scalar primitive."""
    clean: dict[str, str | int | float | bool] = {}
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, bool):
            clean[k] = v
        elif isinstance(v, int):
            clean[k] = v
        elif isinstance(v, float):
            if math.isfinite(v):
                clean[k] = round(v, 4)
        else:
            s = _clean_str(v, max_len=280)
            if s:
                clean[k] = s
    # Safety check on byte budget
    encoded = json.dumps(clean, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        for shrink_key in ("conditions", "title", "institution", "sponsor"):
            if shrink_key in clean and isinstance(clean[shrink_key], str):
                clean[shrink_key] = clean[shrink_key][:140]
    return clean


def _format_conditions(conds: Any) -> str:
    if conds is None:
        return ""
    if isinstance(conds, (list, tuple)):
        items = [_clean_str(c, 60) for c in conds if c]
        return ", ".join(items[:5])
    return _clean_str(conds, 200)


def _topic_tags(row: dict[str, Any]) -> list[str]:
    mapping = [
        ("kw_covid_sarscov2", "COVID-19/SARS-CoV-2"),
        ("kw_single_cell", "single-cell"),
        ("kw_deep_learning", "deep learning"),
        ("kw_crispr", "CRISPR"),
        ("kw_alphafold", "AlphaFold"),
        ("kw_llm_genai", "LLM/GenAI"),
        ("kw_spatial_omics", "spatial omics"),
        ("kw_mrna_vaccine", "mRNA vaccine"),
        ("kw_glp1", "GLP-1"),
    ]
    return [label for col, label in mapping if row.get(col)]


def build_rag_corpus(
    processed_dir: Path,
    output_path: Path | None = None,
    max_docs: int = DEFAULT_MAX_DOCS,
) -> RagCorpusStats:
    """Build JSONL corpus for Cloudflare Vectorize from materialized Parquet tables."""
    processed_dir = Path(processed_dir)
    if output_path is None:
        output_path = processed_dir / "rag_corpus.jsonl"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    preprints_path = processed_dir / "preprints_canonical.parquet"
    trials_path = processed_dir / "clinical_trials_enriched.parquet"
    downstream_path = processed_dir / "downstream_clinical_impact.parquet"
    ror_path = processed_dir / "institution_ror_map.parquet"

    if not preprints_path.exists():
        raise FileNotFoundError(f"Missing required Parquet file: {preprints_path}")

    con = duckdb.connect(":memory:")
    has_ror = ror_path.exists()
    ror_join = (
        f"LEFT JOIN read_parquet('{ror_path.as_posix()}') r ON p.institution_clean = r.institution_clean"
        if has_ror
        else ""
    )
    ror_cols = (
        "COALESCE(r.ror_name, p.institution_clean) AS inst_name, r.ror_country AS country"
        if has_ror
        else "p.institution_clean AS inst_name, NULL::VARCHAR AS country"
    )

    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    covered_dois: set[str] = set()

    # ------------------------------------------------------------------
    # Tier A: All clinical trial-linked preprints
    # ------------------------------------------------------------------
    ct_count = 0
    if trials_path.exists():
        ct_ror_join = (
            f"LEFT JOIN read_parquet('{ror_path.as_posix()}') r ON t.institution_clean = r.institution_clean"
            if has_ror
            else ""
        )
        ct_ror_cols = (
            "COALESCE(r.ror_name, t.institution_clean) AS inst_name, r.ror_country AS country"
            if has_ror
            else "t.institution_clean AS inst_name, NULL::VARCHAR AS country"
        )
        ct_query = f"""
            SELECT
                t.normalized_doi,
                t.trial_id,
                t.registry,
                t.title,
                t.server_clean,
                t.category_clean,
                t.year,
                t.snapshot_is_published,
                t.snapshot_published_doi,
                t.publisher_name,
                t.doi_link,
                t.phase,
                t.overall_status,
                t.sponsor_class,
                t.sponsor_name,
                t.enrollment_count,
                t.has_results,
                t.conditions,
                t.trial_brief_title,
                {ct_ror_cols}
            FROM read_parquet('{trials_path.as_posix()}') t
            {ct_ror_join}
            ORDER BY COALESCE(t.enrollment_count, -1) DESC, t.year DESC NULLS LAST, t.trial_id
        """
        cols = [d[0] for d in con.execute(ct_query).description]
        for tup in con.execute(ct_query).fetchall():
            if len(records) >= max_docs:
                break
            row = dict(zip(cols, tup))
            doi = _clean_str(row["normalized_doi"], 80)
            trial_id = _clean_str(row["trial_id"], 40)
            vec_id = _make_vector_id("ct", f"{doi}:{trial_id}")
            if vec_id in seen_ids:
                continue

            title = _clean_str(row["title"], 240)
            cond_str = _format_conditions(row["conditions"])
            enrollment = (
                int(round(row["enrollment_count"]))
                if row["enrollment_count"] is not None and math.isfinite(row["enrollment_count"])
                else None
            )
            year = int(row["year"]) if row["year"] is not None else None
            published = bool(row["snapshot_is_published"])
            pub_str = (
                f"Published in {_clean_str(row['publisher_name'], 80)}"
                if published and row["publisher_name"]
                else ("Published" if published else "Unpublished preprint")
            )
            parts = [
                f"Clinical Trial {trial_id} ({_clean_str(row['registry'], 40)}):",
                title,
                f"Server: {row['server_clean'] or 'unknown'} | Category: {row['category_clean'] or 'unknown'} | Year: {year or 'unknown'}",
            ]
            trial_bits = []
            if row["phase"]:
                trial_bits.append(f"Phase: {row['phase']}")
            if row["overall_status"]:
                trial_bits.append(f"Status: {row['overall_status']}")
            if enrollment is not None:
                trial_bits.append(f"Enrollment: {enrollment}")
            if row["sponsor_name"]:
                trial_bits.append(f"Sponsor: {_clean_str(row['sponsor_name'], 80)}")
            if trial_bits:
                parts.append(" | ".join(trial_bits))
            if cond_str:
                parts.append(f"Conditions: {cond_str}")
            if row["inst_name"]:
                loc = f"{_clean_str(row['inst_name'], 80)}" + (f" ({row['country']})" if row["country"] else "")
                parts.append(f"Institution: {loc}")
            parts.append(pub_str)

            meta = _sanitize_metadata(
                {
                    "doc_type": "clinical_trial",
                    "doi": doi,
                    "doi_link": row["doi_link"] or f"https://doi.org/{doi}",
                    "title": title,
                    "server": row["server_clean"],
                    "category": row["category_clean"],
                    "year": year,
                    "institution": row["inst_name"],
                    "country": row["country"],
                    "published": published,
                    "publisher": row["publisher_name"],
                    "published_doi": row["snapshot_published_doi"],
                    "trial_id": trial_id,
                    "registry": row["registry"],
                    "phase": row["phase"],
                    "status": row["overall_status"],
                    "sponsor": row["sponsor_name"],
                    "sponsor_class": row["sponsor_class"],
                    "enrollment": enrollment,
                    "conditions": cond_str,
                }
            )
            records.append({"id": vec_id, "text": ". ".join(parts), "metadata": meta})
            seen_ids.add(vec_id)
            covered_dois.add(doi)
            ct_count += 1

    # ------------------------------------------------------------------
    # Tier B: All NIH iCite downstream translational impact papers
    # ------------------------------------------------------------------
    dc_count = 0
    if downstream_path.exists() and len(records) < max_docs:
        dc_query = f"""
            SELECT
                p.normalized_doi,
                p.title,
                p.server_clean,
                p.category_clean,
                EXTRACT(YEAR FROM p.first_posted_date)::INTEGER AS year,
                p.snapshot_published_doi,
                p.publisher_name,
                p.doi_link,
                d.pmid,
                d.is_clinical,
                d.apt,
                d.cited_by_clin_count,
                d.citation_count,
                d.relative_citation_ratio,
                {ror_cols}
            FROM read_parquet('{downstream_path.as_posix()}') d
            INNER JOIN read_parquet('{preprints_path.as_posix()}') p
                ON LOWER(p.snapshot_published_doi) = LOWER(d.published_doi)
            {ror_join}
            WHERE d.published_doi LIKE '10.%/%'
              AND LENGTH(SPLIT_PART(d.published_doi, '/', 2)) >= 3
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY LOWER(d.published_doi)
                ORDER BY p.first_posted_date DESC NULLS LAST, p.normalized_doi
            ) = 1
            ORDER BY COALESCE(d.cited_by_clin_count, 0) DESC, COALESCE(d.apt, -1) DESC, COALESCE(d.citation_count, 0) DESC
        """
        cols = [d[0] for d in con.execute(dc_query).description]
        for tup in con.execute(dc_query).fetchall():
            if len(records) >= max_docs:
                break
            row = dict(zip(cols, tup))
            doi = _clean_str(row["normalized_doi"], 80)
            vec_id = _make_vector_id("dc", doi)
            if vec_id in seen_ids:
                continue

            title = _clean_str(row["title"], 240)
            year = int(row["year"]) if row["year"] is not None else None
            clin_cites = int(row["cited_by_clin_count"] or 0)
            tot_cites = int(row["citation_count"] or 0)
            apt = float(row["apt"]) if row["apt"] is not None and math.isfinite(row["apt"]) else None
            rcr = (
                float(row["relative_citation_ratio"])
                if row["relative_citation_ratio"] is not None and math.isfinite(row["relative_citation_ratio"])
                else None
            )

            parts = [
                f"Translational Impact Paper (NIH iCite): {title}",
                f"Server: {row['server_clean'] or 'unknown'} | Category: {row['category_clean'] or 'unknown'} | Year: {year or 'unknown'}",
                f"Cited by clinical trials/guidelines: {clin_cites} | Total citations: {tot_cites}"
                + (f" | NIH APT: {apt:.2f}" if apt is not None else "")
                + (f" | RCR: {rcr:.2f}" if rcr is not None else ""),
            ]
            if row["publisher_name"]:
                parts.append(f"Published in {_clean_str(row['publisher_name'], 80)}")
            if row["inst_name"]:
                loc = f"{_clean_str(row['inst_name'], 80)}" + (f" ({row['country']})" if row["country"] else "")
                parts.append(f"Institution: {loc}")

            meta = _sanitize_metadata(
                {
                    "doc_type": "downstream_clinical",
                    "doi": doi,
                    "doi_link": row["doi_link"] or f"https://doi.org/{doi}",
                    "title": title,
                    "server": row["server_clean"],
                    "category": row["category_clean"],
                    "year": year,
                    "institution": row["inst_name"],
                    "country": row["country"],
                    "published": True,
                    "publisher": row["publisher_name"],
                    "published_doi": row["snapshot_published_doi"],
                    "pmid": row["pmid"],
                    "cited_by_clin": clin_cites,
                    "citations": tot_cites,
                    "apt": apt,
                    "rcr": rcr,
                }
            )
            records.append({"id": vec_id, "text": ". ".join(parts), "metadata": meta})
            seen_ids.add(vec_id)
            covered_dois.add(doi)
            dc_count += 1

    # ------------------------------------------------------------------
    # Tier C: Stratified high-signal general preprints up to max_docs
    # ------------------------------------------------------------------
    remaining = max(0, max_docs - len(records))
    pp_count = 0
    if remaining > 0:
        # Stratified ranking: assign rank within each (category_clean, server_clean)
        # prioritizing flagship biomedical keywords, published status, revision depth,
        # and recency so every category is well represented.
        pp_query = f"""
            WITH scored AS (
                SELECT
                    p.normalized_doi,
                    p.title,
                    p.server_clean,
                    p.category_clean,
                    EXTRACT(YEAR FROM p.first_posted_date)::INTEGER AS year,
                    p.num_versions,
                    p.author_count,
                    p.snapshot_is_published,
                    p.snapshot_published_doi,
                    p.publisher_name,
                    p.doi_link,
                    p.kw_covid_sarscov2,
                    p.kw_single_cell,
                    p.kw_deep_learning,
                    p.kw_crispr,
                    p.kw_alphafold,
                    p.kw_llm_genai,
                    p.kw_spatial_omics,
                    p.kw_mrna_vaccine,
                    p.kw_glp1,
                    {ror_cols},
                    (
                        (CASE WHEN p.snapshot_is_published THEN 30 ELSE 0 END)
                        + LEAST(COALESCE(p.num_versions, 1), 6) * 6
                        + (CASE WHEN p.kw_glp1 THEN 28 ELSE 0 END)
                        + (CASE WHEN p.kw_alphafold THEN 24 ELSE 0 END)
                        + (CASE WHEN p.kw_llm_genai THEN 22 ELSE 0 END)
                        + (CASE WHEN p.kw_crispr THEN 16 ELSE 0 END)
                        + (CASE WHEN p.kw_mrna_vaccine THEN 18 ELSE 0 END)
                        + (CASE WHEN p.kw_spatial_omics THEN 18 ELSE 0 END)
                        + (CASE WHEN p.kw_single_cell THEN 12 ELSE 0 END)
                        + (CASE WHEN p.kw_deep_learning THEN 10 ELSE 0 END)
                        + (CASE WHEN p.kw_covid_sarscov2 THEN 8 ELSE 0 END)
                        + (CASE WHEN EXTRACT(YEAR FROM p.first_posted_date) >= 2024 THEN 12 ELSE 0 END)
                        + (CASE WHEN p.publisher_name IS NOT NULL AND p.publisher_name <> '' THEN 6 ELSE 0 END)
                    ) AS priority_score
                FROM read_parquet('{preprints_path.as_posix()}') p
                {ror_join}
                WHERE COALESCE(p.ever_withdrawn, FALSE) = FALSE
                  AND p.title IS NOT NULL
                  AND LENGTH(TRIM(p.title)) >= 12
            ),
            ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(category_clean, 'unknown'), COALESCE(server_clean, 'unknown')
                        ORDER BY priority_score DESC, year DESC NULLS LAST, normalized_doi
                    ) AS cat_rank
                FROM scored
            )
            SELECT *
            FROM ranked
            ORDER BY
                -- First guarantee top-30 per (category, server) slice, then fill by global priority
                (CASE WHEN cat_rank <= 30 THEN 0 ELSE 1 END),
                cat_rank,
                priority_score DESC,
                year DESC NULLS LAST
            LIMIT {remaining + len(covered_dois) + 500}
        """
        cur = con.execute(pp_query)
        cols = [d[0] for d in cur.description]
        for tup in cur.fetchall():
            if pp_count >= remaining:
                break
            row = dict(zip(cols, tup))
            doi = _clean_str(row["normalized_doi"], 80)
            if doi in covered_dois:
                continue
            vec_id = _make_vector_id("pp", doi)
            if vec_id in seen_ids:
                continue

            title = _clean_str(row["title"], 240)
            year = int(row["year"]) if row["year"] is not None else None
            published = bool(row["snapshot_is_published"])
            topics = _topic_tags(row)
            pub_str = (
                f"Published in {_clean_str(row['publisher_name'], 80)}"
                if published and row["publisher_name"]
                else ("Published" if published else "Unpublished preprint")
            )
            parts = [
                f"Preprint: {title}",
                f"Server: {row['server_clean'] or 'unknown'} | Category: {row['category_clean'] or 'unknown'} | Year: {year or 'unknown'} | Versions: {int(row['num_versions'] or 1)}",
            ]
            if topics:
                parts.append(f"Topics: {', '.join(topics)}")
            if row["inst_name"]:
                loc = f"{_clean_str(row['inst_name'], 80)}" + (f" ({row['country']})" if row["country"] else "")
                parts.append(f"Institution: {loc}")
            parts.append(pub_str)

            meta = _sanitize_metadata(
                {
                    "doc_type": "preprint",
                    "doi": doi,
                    "doi_link": row["doi_link"] or f"https://doi.org/{doi}",
                    "title": title,
                    "server": row["server_clean"],
                    "category": row["category_clean"],
                    "year": year,
                    "versions": int(row["num_versions"] or 1),
                    "institution": row["inst_name"],
                    "country": row["country"],
                    "published": published,
                    "publisher": row["publisher_name"],
                    "published_doi": row["snapshot_published_doi"],
                    "topics": ", ".join(topics),
                }
            )
            records.append({"id": vec_id, "text": ". ".join(parts), "metadata": meta})
            seen_ids.add(vec_id)
            covered_dois.add(doi)
            pp_count += 1

    # Write JSONL corpus
    total_chars = 0
    categories: set[str] = set()
    with output_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            total_chars += len(rec["text"])
            cat = rec["metadata"].get("category")
            if isinstance(cat, str) and cat:
                categories.add(cat)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # Conservative estimate: ~4 chars/token; @cf/baai/bge-small-en-v1.5 = 1,841 neurons / 1M tokens
    est_tokens = max(1, total_chars // 4)
    est_neurons = (est_tokens / 1_000_000.0) * 1841.0
    stored_dims = len(records) * EMBEDDING_DIMENSIONS
    util_pct = (stored_dims / VECTORIZE_FREE_DIM_CAP) * 100.0

    stats = RagCorpusStats(
        total_docs=len(records),
        clinical_trial_docs=ct_count,
        downstream_clinical_docs=dc_count,
        general_preprint_docs=pp_count,
        unique_categories=len(categories),
        estimated_tokens=est_tokens,
        estimated_neurons=est_neurons,
        stored_dimensions=stored_dims,
        free_cap_utilization_pct=util_pct,
        output_path=str(output_path),
    )

    stats_path = output_path.with_name(output_path.stem + "_stats.json")
    stats_path.write_text(json.dumps(stats.to_dict(), indent=2) + "\n", encoding="utf-8")
    return stats
