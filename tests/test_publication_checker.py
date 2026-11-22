"""Unit tests for peer-reviewed publication verification and caching."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from biomedrxiv.publication_checker import (
    PublicationCache,
    PublicationRecord,
    PublicationVerifier,
    enrich_dataframe_with_publications,
    normalize_doi,
)


class _StubHttpClient:
    """Deterministic fake HTTP client for testing multi-tier resolution."""

    def __init__(self, responses: dict[str, dict | None]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get_json(self, url: str, params: dict | None = None) -> dict | None:
        self.calls.append(url)
        return self.responses.get(url)


def test_normalize_doi_variants() -> None:
    assert normalize_doi("https://doi.org/10.1101/2023.01.01.522001") == "10.1101/2023.01.01.522001"
    assert normalize_doi("DOI: 10.1101/2023.01.01.522001v2") == "10.1101/2023.01.01.522001"
    assert normalize_doi("NA") is None
    assert normalize_doi(None) is None


def test_cache_persists_and_does_not_overwrite_published_with_false(tmp_path: Path) -> None:
    cache = PublicationCache(tmp_path / "pub_cache.sqlite")
    cache.upsert_many(
        [
            PublicationRecord(
                preprint_doi="10.1101/001",
                is_published=True,
                published_doi="10.1038/s41586-024-00001-x",
                published_journal="Nature",
                published_date="2024-06-01",
                verification_source="biorxiv_pubs_api",
            )
        ]
    )
    # Subsequent negative check should not erase confirmed published status
    cache.upsert_many(
        [
            PublicationRecord(
                preprint_doi="10.1101/001",
                is_published=False,
                verification_source="crossref_api",
            )
        ]
    )
    loaded = cache.get_many(["10.1101/001"])
    assert loaded["10.1101/001"].is_published is True
    assert loaded["10.1101/001"].published_journal == "Nature"


def test_tiered_verification_uses_biorxiv_then_crossref_fallback(tmp_path: Path) -> None:
    cache = PublicationCache(tmp_path / "pub_cache.sqlite")
    doi_biorxiv = "10.1101/2023.01.01.100001"
    doi_crossref = "10.1101/2023.01.01.100002"
    pub_doi_cr = "10.1016/j.cell.2024.01.001"

    http = _StubHttpClient(
        {
            f"https://api.biorxiv.org/pubs/biorxiv/{doi_biorxiv}": {
                "collection": [
                    {
                        "biorxiv_doi": doi_biorxiv,
                        "published_doi": "10.1126/science.abc1234",
                        "published_journal": "Science",
                        "published_date": "2024-02-15",
                        "preprint_date": "2023-01-01",
                    }
                ]
            },
            f"https://api.biorxiv.org/pubs/biorxiv/{doi_crossref}": {"collection": []},
            f"https://api.crossref.org/works/10.1101%2F2023.01.01.100002": {
                "message": {
                    "relation": {
                        "is-preprint-of": [{"id-type": "doi", "id": pub_doi_cr}]
                    }
                }
            },
            f"https://api.crossref.org/works/10.1016%2Fj.cell.2024.01.001": {
                "message": {
                    "container-title": ["Cell"],
                    "published-online": {"date-parts": [[2024, 3, 10]]},
                }
            },
        }
    )

    verifier = PublicationVerifier(cache=cache, http_client=http)
    results = verifier.verify_dois(
        [doi_biorxiv, doi_crossref],
        server_map={doi_biorxiv: "biorxiv", doi_crossref: "biorxiv"},
        use_biorxiv_api=True,
        use_crossref_fallback=True,
    )

    assert results[doi_biorxiv].is_published is True
    assert results[doi_biorxiv].published_journal == "Science"
    assert results[doi_crossref].is_published is True
    assert results[doi_crossref].published_journal == "Cell"
    assert results[doi_crossref].published_date == "2024-03-10"

    # Enrich dataframe and verify days_to_publication calculation
    df = pd.DataFrame(
        {
            "doi": [doi_biorxiv, doi_crossref],
            "date": ["2023-01-01", "2023-01-01"],
        }
    )
    enriched = enrich_dataframe_with_publications(
        df, results, doi_column="doi", preprint_date_column="date"
    )
    assert enriched["is_published"].tolist() == [True, True]
    assert enriched["days_to_publication"].tolist() == [410, 434]
