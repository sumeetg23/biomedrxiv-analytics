"""Unit tests for ROR institutional deduplication and multi-tier affiliation matching."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from biomedrxiv.config import ProjectPaths
from biomedrxiv.ror_dedup import (
    RORMatcher,
    build_institution_ror_mapping,
    normalize_org_text,
)

SAMPLE_ROR_ENTRIES = [
    {
        "id": "https://ror.org/00f54p054",
        "status": "active",
        "types": ["education"],
        "names": [
            {"value": "Stanford University", "types": ["ror_display"]},
            {"value": "Stanford", "types": ["alias"]},
        ],
        "locations": [{"geonames_details": {"country_name": "United States", "country_code": "US"}}],
    },
    {
        "id": "https://ror.org/043mz5j54",
        "status": "active",
        "types": ["education"],
        "names": [
            {"value": "University of California, San Francisco", "types": ["ror_display"]},
            {"value": "UCSF", "types": ["acronym"]},
        ],
        "locations": [{"geonames_details": {"country_name": "United States", "country_code": "US"}}],
    },
    {
        "id": "https://ror.org/052gg0110",
        "status": "active",
        "types": ["education"],
        "names": [
            {"value": "University of Oxford", "types": ["ror_display"]},
        ],
        "locations": [{"geonames_details": {"country_name": "United Kingdom", "country_code": "GB"}}],
    },
]


def test_normalize_org_text_fixes_typos_and_accents() -> None:
    assert normalize_org_text("Stanford Univeristy") == "stanford university"
    assert normalize_org_text("The University of Oxford") == "university of oxford"
    assert normalize_org_text("École Polytechnique") == "ecole polytechnique"


def test_ror_matcher_handles_typos_departments_and_subunits() -> None:
    matcher = RORMatcher(SAMPLE_ROR_ENTRIES)

    # Exact + typo
    m1 = matcher.match("Stanford Univeristy")
    assert m1.ror_id == "https://ror.org/00f54p054"
    assert m1.ror_name == "Stanford University"

    # Subunit stripping
    m2 = matcher.match("Stanford University School of Medicine")
    assert m2.ror_id == "https://ror.org/00f54p054"
    assert m2.match_method == "subunit"

    # Comma-separated department prefix + 2-segment UC name
    m3 = matcher.match("Department of Bioengineering, University of California, San Francisco, CA")
    assert m3.ror_id == "https://ror.org/043mz5j54"
    assert m3.ror_name == "University of California, San Francisco"
    assert m3.match_method == "segment"

    # Curated shorthand
    m4 = matcher.match("UCSF")
    assert m4.ror_id == "https://ror.org/043mz5j54"

    # Unmatched stays intact
    m5 = matcher.match("Independent Citizen Science Lab XYZ")
    assert m5.ror_id is None
    assert m5.match_method == "unmatched"


def test_build_institution_ror_mapping_end_to_end(tmp_path: Path) -> None:
    paths = ProjectPaths.from_root(tmp_path / "proj")
    paths.ensure_directories()

    ror_json = tmp_path / "ror.json"
    ror_json.write_text(json.dumps(SAMPLE_ROR_ENTRIES), encoding="utf-8")

    canon = pd.DataFrame(
        {
            "institution_clean": [
                "Stanford University",
                "Stanford University School of Medicine",
                "Department of Genetics, Stanford University",
                "University of Oxford",
                "Unknown Boutique Lab",
            ]
        }
    )
    canon.to_parquet(paths.processed_dir / "preprints_canonical.parquet", index=False)

    summary = build_institution_ror_mapping(paths, ror_json_path=ror_json, api_fallback_top_n=0)
    assert summary["unique_raw_strings"] == 5
    assert summary["unique_ror_orgs_matched"] == 2
    assert summary["matched_preprints"] == 4
    assert summary["coverage_pct"] == 80.0

    mapped = pd.read_parquet(paths.processed_dir / "institution_ror_map.parquet")
    stanford_rows = mapped[mapped["ror_id"] == "https://ror.org/00f54p054"]
    assert len(stanford_rows) == 3
