"""Unit tests for direct bioRxiv/medRxiv API incremental sync & versioned merging."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from biomedrxiv.biorxiv_sync import (
    CSV_COLUMNS,
    detect_latest_dataset_date,
    sync_incremental_from_biorxiv_api,
)
from biomedrxiv.config import ProjectPaths
from biomedrxiv.versioning import DatasetVersionManager


class _StubBiorxivHttp:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        return self.responses.get(url)


def test_incremental_api_sync_merges_deduplicates_and_creates_new_version(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path / "proj")
    manager = DatasetVersionManager(paths=paths)

    # Seed v1 snapshot ending 2026-10-31
    seed_dir = tmp_path / "seed_v1"
    seed_dir.mkdir(parents=True)
    seed_df = pd.DataFrame(
        [
            {
                "title": "Existing paper v1",
                "authors": "Alice A;",
                "author_corresponding": "Alice A",
                "author_corresponding_institution": "MIT",
                "doi": "10.1101/2026.10.31.000001",
                "date": "2026-10-31",
                "version": 1,
                "type": "new results",
                "license": "cc_by",
                "category": "genomics",
                "jatsxml": "https://example.org/1.xml",
                "abstract": "Initial abstract.",
                "published": "",
                "server": "bioRxiv",
                "doi_link": "https://doi.org/10.1101/2026.10.31.000001",
            }
        ],
        columns=CSV_COLUMNS,
    )
    seed_df.to_csv(seed_dir / "biorxiv_medrxiv_preprints.csv", index=False)
    v1 = manager.ingest_directory(seed_dir, notes="seed v1")

    assert detect_latest_dataset_date(paths) == "2026-10-31"

    stub = _StubBiorxivHttp(
        {
            "https://api.biorxiv.org/details/biorxiv/2026-10-30/2026-11-05/0": {
                "messages": [{"count": 2, "total": 2}],
                "collection": [
                    # Boundary duplicate (same doi + version=1) -> should replace, not duplicate
                    {
                        "doi": "10.1101/2026.10.31.000001",
                        "title": "Existing paper v1",
                        "authors": "Alice A;",
                        "author_corresponding": "Alice A",
                        "author_corresponding_institution": "MIT",
                        "date": "2026-10-31",
                        "version": "1",
                        "type": "new results",
                        "license": "cc_by",
                        "category": "genomics",
                        "jatsxml": "https://example.org/1.xml",
                        "abstract": "Initial abstract.",
                        "published": "NA",
                    },
                    # Brand-new preprint on 2026-11-03
                    {
                        "doi": "10.1101/2026.11.03.000002",
                        "title": "Brand new bioRxiv paper",
                        "authors": "Bob B;Carol C;",
                        "author_corresponding": "Bob B",
                        "author_corresponding_institution": "Stanford",
                        "date": "2026-11-03",
                        "version": "1",
                        "type": "new results",
                        "license": "cc_by",
                        "category": "neuroscience",
                        "jatsxml": "https://example.org/2.xml",
                        "abstract": "New findings.",
                        "published": "NA",
                    },
                ],
            },
            "https://api.biorxiv.org/details/medrxiv/2026-10-30/2026-11-05/0": {
                "messages": [{"count": 0, "total": 0}],
                "collection": [],
            },
            "https://api.biorxiv.org/pubs/biorxiv/2026-10-30/2026-11-05/0": {
                "messages": [{"count": 1, "total": 1}],
                "collection": [
                    {
                        "biorxiv_doi": "10.1101/2026.10.31.000001",
                        "published_doi": "10.1038/s41586-026-99999-x",
                        "published_journal": "Nature",
                        "published_date": "2026-11-04",
                    }
                ],
            },
            "https://api.biorxiv.org/pubs/medrxiv/2026-10-30/2026-11-05/0": {
                "messages": [{"count": 0, "total": 0}],
                "collection": [],
            },
        }
    )

    summary = sync_incremental_from_biorxiv_api(
        paths,
        start_date="2026-10-30",
        end_date="2026-11-05",
        http_client=stub,
    )

    assert summary.fetched_records == 2
    assert summary.pubs_harvested == 1
    assert summary.update_result is not None
    assert summary.update_result.status == "created"
    assert summary.update_result.version_id != v1.version_id
    assert len(manager.list_versions()) == 2

    # Verify merged CSV has exactly 2 rows (no duplicate on boundary!) and patched published DOI
    merged = pd.read_csv(paths.current_link / "biorxiv_medrxiv_preprints.csv")
    assert len(merged) == 2
    assert set(merged["doi"]) == {
        "10.1101/2026.10.31.000001",
        "10.1101/2026.11.03.000002",
    }
    row_pub = merged.loc[merged["doi"] == "10.1101/2026.10.31.000001", "published"].iloc[0]
    assert row_pub == "10.1038/s41586-026-99999-x"
    assert (paths.current_link / "incremental_delta.jsonl").exists()
