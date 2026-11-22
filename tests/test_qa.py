"""Unit & FastAPI integration tests for the DuckDB + Gemini/Ollama Q&A engine."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from biomedrxiv.config import ProjectPaths
from biomedrxiv.qa_engine import (
    BioMedRxivAssistant,
    DuckDBQueryEngine,
    validate_readonly_sql,
)
from biomedrxiv.qa_server import create_app


def _seed_parquet_workspace(tmp_path: Path) -> ProjectPaths:
    paths = ProjectPaths.from_root(tmp_path / "proj")
    paths.ensure_directories()

    preprints = pd.DataFrame(
        {
            "normalized_doi": ["10.1101/001", "10.1101/002"],
            "title": ["CRISPR therapy for oncology", "Semaglutide cardiovascular trial"],
            "server_clean": ["biorxiv", "medrxiv"],
            "category_clean": ["cancer biology", "cardiovascular medicine"],
            "first_posted_date": pd.to_datetime(["2022-05-01", "2023-08-15"]),
            "num_versions": [2, 1],
            "author_count": [6, 12],
            "snapshot_is_published": [True, True],
            "snapshot_published_doi": ["10.1038/abc", "10.1056/def"],
            "publisher_name": ["Nature Portfolio / Springer Nature", "NEJM"],
            "institution_clean": ["Stanford University", "Harvard Medical School"],
            "doi_link": ["https://doi.org/10.1101/001", "https://doi.org/10.1101/002"],
        }
    )
    trials = pd.DataFrame(
        {
            "normalized_doi": ["10.1101/002"],
            "trial_id": ["NCT05000001"],
            "registry": ["ClinicalTrials.gov"],
            "phase": ["PHASE3"],
            "overall_status": ["COMPLETED"],
            "enrollment_count": [5000],
        }
    )
    preprints.to_parquet(paths.processed_dir / "preprints_canonical.parquet", index=False)
    trials.to_parquet(paths.processed_dir / "clinical_trials_enriched.parquet", index=False)
    return paths


def test_validate_readonly_sql_blocks_mutations_and_file_reads() -> None:
    assert validate_readonly_sql("SELECT COUNT(*) FROM preprints;") == "SELECT COUNT(*) FROM preprints"
    assert validate_readonly_sql("```sql\nWITH c AS (SELECT 1) SELECT * FROM c\n```").startswith("WITH")

    for dangerous in [
        "DROP TABLE preprints",
        "DELETE FROM preprints",
        "SELECT 1; DROP TABLE preprints",
        "SELECT * FROM read_csv('/etc/passwd')",
        "COPY (SELECT 1) TO '/tmp/out.csv'",
        "PRAGMA version",
    ]:
        with pytest.raises(ValueError):
            validate_readonly_sql(dangerous)


def test_duckdb_engine_queries_and_joins_parquet(tmp_path: Path) -> None:
    paths = _seed_parquet_workspace(tmp_path)
    engine = DuckDBQueryEngine(paths=paths)

    schema = engine.get_schema_summary()
    assert schema["preprints"]["row_count"] == 2
    assert schema["clinical_trials"]["row_count"] == 1

    res = engine.run_sql(
        "SELECT p.server_clean, t.trial_id, t.enrollment_count "
        "FROM preprints p JOIN clinical_trials t ON p.normalized_doi = t.normalized_doi"
    )
    assert res["row_count"] == 1
    assert res["rows"][0]["trial_id"] == "NCT05000001"
    assert res["rows"][0]["enrollment_count"] == 5000

    search = engine.search_papers("semaglutide", server="medrxiv", published_only=True)
    assert search["row_count"] == 1
    assert search["rows"][0]["normalized_doi"] == "10.1101/002"


def test_gemini_interactions_tool_calling_and_fastapi_endpoints(tmp_path: Path) -> None:
    paths = _seed_parquet_workspace(tmp_path)
    engine = DuckDBQueryEngine(paths=paths)

    class _FakeInteractions:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    id="int_1",
                    steps=[
                        SimpleNamespace(
                            type="function_call",
                            id="call_1",
                            name="run_sql_query",
                            arguments={"sql": "SELECT COUNT(*) AS n FROM preprints"},
                        )
                    ],
                    output_text="",
                )
            return SimpleNamespace(
                id="int_2",
                steps=[],
                output_text="There are 2 unique preprints in the dataset.",
            )

    fake_client = SimpleNamespace(interactions=_FakeInteractions())
    assistant = BioMedRxivAssistant(engine=engine, gemini_client=fake_client)
    app = create_app(paths=paths, assistant=assistant)
    client = TestClient(app)

    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["tables"]["preprints"] == 2
    assert health["providers"]["gemini"]["configured"] is True

    sql_resp = client.post("/api/sql", json={"sql": "SELECT COUNT(*) AS n FROM preprints"})
    assert sql_resp.status_code == 200
    assert sql_resp.json()["rows"] == [{"n": 2}]

    blocked = client.post("/api/sql", json={"sql": "DROP TABLE preprints"})
    assert blocked.status_code == 400

    ask_resp = client.post("/api/ask", json={"question": "How many preprints?", "provider": "gemini"})
    assert ask_resp.status_code == 200
    body = ask_resp.json()
    assert body["provider"] == "gemini"
    assert "2 unique preprints" in body["answer"]
    assert body["sql_queries"] == ["SELECT COUNT(*) AS n FROM preprints"]
    assert body["tables"][0]["rows"] == [{"n": 2}]
