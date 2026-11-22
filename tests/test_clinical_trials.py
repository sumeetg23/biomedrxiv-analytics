"""Unit tests for clinical trial ID extraction, API v2 parsing, NIH iCite, and payload building."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from biomedrxiv.clinical_trials import (
    ClinicalTrialsCache,
    ClinicalTrialsLinker,
    build_clinical_trials_payload,
    extract_direct_trial_links,
    extract_trial_ids_from_text,
    parse_clinicaltrials_v2_study,
)
from biomedrxiv.config import ProjectPaths


class _StubHttp:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self.calls.append((url, params))
        return self.handler(url, params)


def test_extract_trial_ids_from_text() -> None:
    text = (
        "Registered at ClinicalTrials.gov (NCT04280705, nct04280705) and "
        "ISRCTN50189673, plus ChiCTR2000029308 and EudraCT 2020-001113-21. "
        "Unrelated date 2020-01-01 should not match."
    )
    found = extract_trial_ids_from_text(text)
    assert ("NCT04280705", "ClinicalTrials.gov") in found
    assert ("ISRCTN50189673", "ISRCTN") in found
    assert ("ChiCTR2000029308", "ChiCTR") in found
    assert ("EudraCT:2020-001113-21", "EudraCT") in found
    assert len(found) == 4


def test_parse_clinicaltrials_v2_study() -> None:
    study = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT04280705", "briefTitle": "Adaptive COVID-19 Trial"},
            "statusModule": {
                "overallStatus": "COMPLETED",
                "startDateStruct": {"date": "2020-02-21"},
                "completionDateStruct": {"date": "2020-05-21"},
            },
            "designModule": {
                "studyType": "INTERVENTIONAL",
                "phases": ["PHASE3"],
                "enrollmentInfo": {"count": 1062},
            },
            "sponsorCollaboratorsModule": {
                "leadSponsor": {"name": "NIAID", "class": "NIH"}
            },
            "conditionsModule": {"conditions": ["COVID-19"]},
            "armsInterventionsModule": {"interventions": [{"name": "Remdesivir"}]},
        },
        "hasResults": True,
    }
    rec = parse_clinicaltrials_v2_study(study)
    assert rec is not None
    assert rec.nct_id == "NCT04280705"
    assert rec.phase == "PHASE3"
    assert rec.sponsor_class == "NIH"
    assert rec.enrollment_count == 1062
    assert rec.has_results is True


def test_linker_enrichment_icite_and_payload(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path / "proj")
    paths.ensure_directories()

    # Create minimal versions & canonical Parquet
    versions = pd.DataFrame(
        {
            "normalized_doi": ["10.1101/001", "10.1101/002"],
            "version": [1, 1],
            "title": ["Remdesivir trial NCT04280705", "Preclinical target paper"],
            "abstract": ["Phase 3 study NCT04280705.", "Mechanistic mouse study."],
        }
    )
    canonical = pd.DataFrame(
        {
            "normalized_doi": ["10.1101/001", "10.1101/002"],
            "title": ["Remdesivir trial NCT04280705", "Preclinical target paper"],
            "server_clean": ["medrxiv", "biorxiv"],
            "category_clean": ["infectious diseases", "cancer biology"],
            "first_posted_date": pd.to_datetime(["2020-05-01", "2021-03-10"]),
            "snapshot_is_published": [True, True],
            "snapshot_published_doi": ["10.1056/nejmoa2007764", "10.1038/s41586-021-0001"],
            "publisher_name": ["NEJM", "Nature Portfolio / Springer Nature"],
            "institution_clean": ["NIH", "MIT"],
            "doi_link": ["https://doi.org/10.1101/001", "https://doi.org/10.1101/002"],
        }
    )
    versions.to_parquet(paths.processed_dir / "preprints_versions.parquet", index=False)
    canonical.to_parquet(paths.processed_dir / "preprints_canonical.parquet", index=False)

    links = extract_direct_trial_links(paths)
    assert len(links) == 1
    assert links.iloc[0]["trial_id"] == "NCT04280705"

    def _handler(url: str, params: dict[str, Any] | None):
        if "clinicaltrials.gov" in url:
            return {
                "studies": [
                    {
                        "protocolSection": {
                            "identificationModule": {
                                "nctId": "NCT04280705",
                                "briefTitle": "Adaptive COVID-19 Trial",
                            },
                            "statusModule": {"overallStatus": "COMPLETED"},
                            "designModule": {
                                "studyType": "INTERVENTIONAL",
                                "phases": ["PHASE3"],
                                "enrollmentInfo": {"count": 1062},
                            },
                            "sponsorCollaboratorsModule": {
                                "leadSponsor": {"name": "NIAID", "class": "NIH"}
                            },
                            "conditionsModule": {"conditions": ["COVID-19"]},
                        },
                        "hasResults": True,
                    }
                ]
            }
        if "ebi.ac.uk/europepmc" in url:
            return {
                "resultList": {
                    "result": [{"doi": "10.1038/s41586-021-0001", "pmid": "33700001"}]
                }
            }
        if "icite.od.nih.gov" in url:
            return {
                "data": [
                    {
                        "pmid": 33700001,
                        "is_clinical": False,
                        "apt": 0.75,
                        "cited_by_clin": [36000001, 36000002],
                        "citation_count": 42,
                        "relative_citation_ratio": 3.4,
                    }
                ]
            }
        return None

    cache = ClinicalTrialsCache(paths.cache_dir / "clinical_trials.sqlite")
    linker = ClinicalTrialsLinker(cache=cache, http_client=_StubHttp(_handler))
    nct_res = linker.enrich_nct_ids(["NCT04280705"])
    assert "NCT04280705" in nct_res

    ds_res = linker.trace_downstream_clinical_impact(["10.1038/s41586-021-0001"])
    assert ds_res["10.1038/s41586-021-0001"].cited_by_clin_count == 2
    assert ds_res["10.1038/s41586-021-0001"].apt == 0.75

    payload = build_clinical_trials_payload(paths)
    assert payload is not None
    assert payload["by_year"]["2020"]["medrxiv"]["kpi"]["trial_linked_dois"] == 1
    assert payload["by_year"]["2020"]["medrxiv"]["phases"]["Phase 3"] == 1
    assert payload["downstream_icite"]["cited_by_clinical_trials_count"] == 1
