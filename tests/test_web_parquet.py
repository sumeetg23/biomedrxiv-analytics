"""Unit tests for the compact Web Analytics Parquet exporter (Step 3 DuckDB-Wasm)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from biomedrxiv.web_parquet import export_web_parquet
from test_rag_corpus import _create_synthetic_processed_dir


def test_export_web_parquet_schema_and_ror_enrichment(tmp_path: Path) -> None:
    proc_dir = _create_synthetic_processed_dir(tmp_path / "processed")
    out_dir = tmp_path / "web_parquet"

    stats = export_web_parquet(proc_dir, out_dir)
    assert stats.preprints_rows == 3
    assert stats.clinical_trials_rows == 1
    assert stats.downstream_clinical_rows == 1
    assert (out_dir / "manifest.json").exists()

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["files"].keys()) == {"preprints", "clinical_trials", "downstream_clinical"}

    con = duckdb.connect(":memory:")
    row = con.execute(
        f"""
        SELECT doi, server, year, institution, country, is_published, kw_glp1
        FROM read_parquet('{(out_dir / "preprints.parquet").as_posix()}')
        WHERE doi = '10.1101/2021.01.01.000001'
        """
    ).fetchone()
    assert row == ("10.1101/2021.01.01.000001", "medrxiv", 2021, "Harvard University", "United States", True, True)

    trial = con.execute(
        f"""
        SELECT trial_id, phase, enrollment_count, conditions, institution
        FROM read_parquet('{(out_dir / "clinical_trials.parquet").as_posix()}')
        """
    ).fetchone()
    assert trial == ("NCT04123456", "PHASE3", 1961, "Obesity, Type 2 Diabetes", "Harvard University")

    dc = con.execute(
        f"""
        SELECT doi, pmid, cited_by_clin_count, institution
        FROM read_parquet('{(out_dir / "downstream_clinical.parquet").as_posix()}')
        """
    ).fetchone()
    assert dc == ("10.1101/2023.02.02.000002", "37000001", 4, "Broad Institute")
