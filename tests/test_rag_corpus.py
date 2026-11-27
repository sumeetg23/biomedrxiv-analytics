"""Unit tests for the Cloudflare Vectorize RAG corpus builder."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from biomedrxiv.rag_corpus import (
    EMBEDDING_DIMENSIONS,
    MAX_METADATA_BYTES,
    MAX_VECTOR_ID_BYTES,
    VECTORIZE_FREE_DIM_CAP,
    _make_vector_id,
    build_rag_corpus,
)


def _create_synthetic_processed_dir(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute(
        """
        COPY (
            SELECT
                '10.1101/2021.01.01.000001' AS normalized_doi,
                'Phase 3 randomized trial of GLP-1 agonist in obesity' AS title,
                'medrxiv' AS server_clean,
                'endocrinology' AS category_clean,
                TIMESTAMP '2021-06-15' AS first_posted_date,
                TIMESTAMP '2021-08-20' AS latest_posted_date,
                3::INTEGER AS num_versions,
                8::INTEGER AS author_count,
                66::BIGINT AS revision_span_days,
                TRUE AS snapshot_is_published,
                '10.1056/NEJMoa12345' AS snapshot_published_doi,
                'New England Journal of Medicine' AS publisher_name,
                'Harvard Medical School' AS institution_clean,
                'https://doi.org/10.1101/2021.01.01.000001' AS doi_link,
                FALSE AS ever_withdrawn,
                FALSE AS kw_covid_sarscov2,
                FALSE AS kw_single_cell,
                FALSE AS kw_deep_learning,
                FALSE AS kw_crispr,
                FALSE AS kw_alphafold,
                FALSE AS kw_llm_genai,
                FALSE AS kw_spatial_omics,
                FALSE AS kw_mrna_vaccine,
                TRUE AS kw_glp1
            UNION ALL
            SELECT
                '10.1101/2023.02.02.000002',
                'AlphaFold structure prediction of novel CRISPR-Cas13 complexes',
                'biorxiv',
                'bioinformatics',
                TIMESTAMP '2023-03-10',
                TIMESTAMP '2023-04-01',
                2::INTEGER,
                5::INTEGER,
                22::BIGINT,
                TRUE,
                '10.1038/s41586-023-00002',
                'Nature',
                'Broad Institute',
                'https://doi.org/10.1101/2023.02.02.000002',
                FALSE,
                FALSE, FALSE, TRUE, TRUE, TRUE, FALSE, FALSE, FALSE, FALSE
            UNION ALL
            SELECT
                '10.1101/2024.05.05.000003',
                'Single-cell spatial transcriptomics atlas of human cortex development',
                'biorxiv',
                'neuroscience',
                TIMESTAMP '2024-05-05',
                TIMESTAMP '2024-05-05',
                1::INTEGER,
                12::INTEGER,
                0::BIGINT,
                FALSE,
                NULL,
                NULL,
                'Stanford University',
                'https://doi.org/10.1101/2024.05.05.000003',
                FALSE,
                FALSE, TRUE, FALSE, FALSE, FALSE, FALSE, TRUE, FALSE, FALSE
        ) TO '"""
        + (tmp_path / "preprints_canonical.parquet").as_posix()
        + "' (FORMAT PARQUET)"
    )

    con.execute(
        """
        COPY (
            SELECT
                '10.1101/2021.01.01.000001' AS normalized_doi,
                'NCT04123456' AS trial_id,
                'ClinicalTrials.gov' AS registry,
                'Phase 3 randomized trial of GLP-1 agonist in obesity' AS title,
                'medrxiv' AS server_clean,
                'endocrinology' AS category_clean,
                2021::INTEGER AS year,
                TRUE AS snapshot_is_published,
                '10.1056/NEJMoa12345' AS snapshot_published_doi,
                'New England Journal of Medicine' AS publisher_name,
                'Harvard Medical School' AS institution_clean,
                'https://doi.org/10.1101/2021.01.01.000001' AS doi_link,
                'PHASE3' AS phase,
                'COMPLETED' AS overall_status,
                'INDUSTRY' AS sponsor_class,
                'Novo Nordisk' AS sponsor_name,
                1961.0::DOUBLE AS enrollment_count,
                TRUE AS has_results,
                ['Obesity', 'Type 2 Diabetes'] AS conditions,
                'GLP-1 Trial Brief' AS trial_brief_title
        ) TO '"""
        + (tmp_path / "clinical_trials_enriched.parquet").as_posix()
        + "' (FORMAT PARQUET)"
    )

    con.execute(
        """
        COPY (
            SELECT
                '10.1038/s41586-023-00002' AS published_doi,
                '37000001' AS pmid,
                FALSE AS is_clinical,
                0.82::DOUBLE AS apt,
                4::BIGINT AS cited_by_clin_count,
                120::BIGINT AS citation_count,
                3.45::DOUBLE AS relative_citation_ratio
        ) TO '"""
        + (tmp_path / "downstream_clinical_impact.parquet").as_posix()
        + "' (FORMAT PARQUET)"
    )

    con.execute(
        """
        COPY (
            SELECT 'Harvard Medical School' AS institution_clean, 100::BIGINT AS preprint_count,
                   'https://ror.org/03vek6s52' AS ror_id, 'Harvard University' AS ror_name,
                   'United States' AS ror_country, 'US' AS ror_country_code, 'education' AS ror_type, 'exact' AS match_method
            UNION ALL
            SELECT 'Broad Institute', 80::BIGINT, 'https://ror.org/05a0ya142', 'Broad Institute', 'United States', 'US', 'nonprofit', 'exact'
            UNION ALL
            SELECT 'Stanford University', 90::BIGINT, 'https://ror.org/00f54p054', 'Stanford University', 'United States', 'US', 'education', 'exact'
        ) TO '"""
        + (tmp_path / "institution_ror_map.parquet").as_posix()
        + "' (FORMAT PARQUET)"
    )
    return tmp_path


def test_build_rag_corpus_tiers_and_vectorize_constraints(tmp_path: Path) -> None:
    proc_dir = _create_synthetic_processed_dir(tmp_path)
    out = tmp_path / "rag_corpus.jsonl"
    stats = build_rag_corpus(proc_dir, output_path=out, max_docs=10)

    assert stats.total_docs == 3
    assert stats.clinical_trial_docs == 1
    assert stats.downstream_clinical_docs == 1
    assert stats.general_preprint_docs == 1
    assert stats.stored_dimensions == 3 * EMBEDDING_DIMENSIONS
    assert stats.stored_dimensions < VECTORIZE_FREE_DIM_CAP

    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3

    by_type = {r["metadata"]["doc_type"]: r for r in lines}
    assert set(by_type.keys()) == {"clinical_trial", "downstream_clinical", "preprint"}

    # Check Vectorize compatibility on every record
    for rec in lines:
        assert len(rec["id"].encode("utf-8")) <= MAX_VECTOR_ID_BYTES
        assert isinstance(rec["text"], str) and len(rec["text"]) > 20
        meta = rec["metadata"]
        assert len(json.dumps(meta).encode("utf-8")) <= MAX_METADATA_BYTES
        for v in meta.values():
            assert isinstance(v, (str, int, float, bool)), f"Non-primitive metadata value: {v!r}"

    # Verify trial enrichment & ROR normalization flowed through
    ct = by_type["clinical_trial"]
    assert ct["metadata"]["trial_id"] == "NCT04123456"
    assert ct["metadata"]["phase"] == "PHASE3"
    assert ct["metadata"]["enrollment"] == 1961
    assert ct["metadata"]["institution"] == "Harvard University"
    assert ct["metadata"]["country"] == "United States"

    # Verify NIH iCite metrics flowed through
    dc = by_type["downstream_clinical"]
    assert dc["metadata"]["cited_by_clin"] == 4
    assert dc["metadata"]["apt"] == pytest.approx(0.82)


def test_make_vector_id_truncates_long_ids_deterministically() -> None:
    long_id = _make_vector_id("ct", "10.1101/2025.12.31.12345678901234567890:" + "X" * 60)
    assert len(long_id.encode("utf-8")) <= MAX_VECTOR_ID_BYTES
    assert long_id == _make_vector_id("ct", "10.1101/2025.12.31.12345678901234567890:" + "X" * 60)
