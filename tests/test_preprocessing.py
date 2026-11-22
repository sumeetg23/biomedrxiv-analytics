"""Unit tests for data cleaning and DOI-level deduplication."""

from __future__ import annotations

import pandas as pd

from biomedrxiv.preprocessing import (
    clean_preprints_dataframe,
    count_authors,
    deduplicate_by_doi,
)


def test_count_authors() -> None:
    assert count_authors("Alice Smith;Bob Jones;") == 2
    assert count_authors("Single Author") == 1
    assert count_authors("") == 0
    assert count_authors(None) == 0


def test_clean_and_deduplicate_handles_real_dataset_quirks() -> None:
    raw = pd.DataFrame(
        {
            "doi": ["10.1101/001", "10.1101/001", "10.1101/002"],
            "date": ["2023-01-10", "2023-03-10", "2024-05-01"],
            "version": [1, 2, 1],
            "type": ["New Results", "new results", "WITHDRAWN"],
            "license": ["CC-BY-NC-ND", "cc_by_nc_nd", "cc_no"],
            "category": ["Cancer Biology ", "cancer biology", "Genomics "],
            "author_corresponding": ["Jacob  Scott", "Jacob Scott", "Jane   Doe"],
            "authors": ["Jacob Scott;Bob A;", "Jacob Scott;Bob A;Carol B;", "Jane Doe;"],
            "published": ["10.1038/abc", "10.1038/abc", None],
            "server": ["bioRxiv", "bioRxiv", "medRxiv"],
        }
    )

    cleaned = clean_preprints_dataframe(raw)
    assert cleaned["category_clean"].tolist() == ["cancer biology", "cancer biology", "genomics"]
    assert cleaned["license_clean"].tolist() == ["cc_by_nc_nd", "cc_by_nc_nd", "cc_no"]
    assert cleaned["author_corresponding_clean"].tolist() == [
        "Jacob Scott",
        "Jacob Scott",
        "Jane Doe",
    ]
    assert cleaned["is_withdrawn"].tolist() == [False, False, True]
    assert cleaned["author_count"].tolist() == [2, 3, 1]

    dedup = deduplicate_by_doi(cleaned)
    assert len(dedup) == 2

    row1 = dedup.loc[dedup["normalized_doi"] == "10.1101/001"].iloc[0]
    assert row1["num_versions"] == 2
    assert row1["revision_span_days"] == 59
    assert row1["author_count"] == 3
    assert bool(row1["snapshot_is_published"]) is True
    assert bool(row1["ever_withdrawn"]) is False

    row2 = dedup.loc[dedup["normalized_doi"] == "10.1101/002"].iloc[0]
    assert row2["num_versions"] == 1
    assert bool(row2["ever_withdrawn"]) is True
    assert bool(row2["snapshot_is_published"]) is False
