"""ROR (Research Organization Registry) institutional deduplication engine.

Combines:
1. **Official ROR CC0 Data Dump** (downloaded from Zenodo ``ror-data`` community
   and cached locally in ``data/cache/ror/``) for blazing-fast local matching
   across all 130,000+ raw affiliation strings with zero API rate limits.
2. **Multi-tier local matcher**:
   - Tier 1: Exact normalized name / unambiguous alias / curated acronym match.
   - Tier 2: Segment-based parsing for comma/semicolon-separated affiliations
     (e.g. ``"Department of Genetics, Stanford University, Stanford, CA"``),
     testing adjacent 2-segment pairs first (so ``"University of California,
     San Francisco"`` stays intact!).
   - Tier 3: Sub-unit / trailing-school stripping (e.g.
     ``"Stanford University School of Medicine"`` -> ``"Stanford University"``
     when the full sub-unit is not its own ROR entry).
   - Tier 4 (optional): Official ROR v2 affiliation API fallback
     (``https://api.ror.org/v2/organizations?affiliation=...``) cached in SQLite
     for high-volume remaining unmatched strings.
3. **Structured output**:
   - ``data/processed/institution_ror_map.parquet`` mapping every raw
     ``institution_clean`` string to ``ror_id``, ``ror_name``, ``ror_country``,
     ``ror_country_code``, ``ror_type``, and ``match_method``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from biomedrxiv.config import ProjectPaths
from biomedrxiv.publication_checker import PoliteHttpClient

logger = logging.getLogger(__name__)

ZENODO_ROR_LATEST_URL = "https://zenodo.org/api/records/"
ROR_V2_ORGS_API = "https://api.ror.org/v2/organizations"

# Common typos and shorthand tokens in biomedical author affiliations
TYPO_REPLACEMENTS = [
    (re.compile(r"\b(?:univeristy|unviversity|unviersity|universtiy|univesity|univerity|univertisy)\b"), "university"),
    (re.compile(r"\buniv\.?\b"), "university"),
    (re.compile(r"\binst\.?\b"), "institute"),
    (re.compile(r"\bmed\.?\b"), "medical"),
    (re.compile(r"\bctr\.?\b"), "center"),
    (re.compile(r"\bhosp\.?\b"), "hospital"),
    (re.compile(r"\bdept\.?\b"), "department"),
    (re.compile(r"\bcoll\.?\b"), "college"),
    (re.compile(r"\bcentre\b"), "center"),
]

# Trailing sub-unit suffixes stripped in Tier 3 when the full phrase isn't a standalone ROR org
SUBUNIT_SUFFIX_RE = re.compile(
    r"\s+(?:school of medicine|medical school|college of medicine|school of public health|"
    r"faculty of medicine|medical center|health sciences center|health system|"
    r"cancer center|cancer institute|graduate school|school of engineering|"
    r"college of arts and sciences|medicine)$",
    re.IGNORECASE,
)

# High-frequency unambiguous biomedical acronyms / shorthands -> canonical English name
CURATED_SHORTHANDS: dict[str, str] = {
    "mit": "massachusetts institute of technology",
    "ucsf": "university of california san francisco",
    "ucla": "university of california los angeles",
    "ucsd": "university of california san diego",
    "uc berkeley": "university of california berkeley",
    "uc davis": "university of california davis",
    "uc irvine": "university of california irvine",
    "uc santa cruz": "university of california santa cruz",
    "uc santa barbara": "university of california santa barbara",
    "ucl": "university college london",
    "nih": "national institutes of health",
    "cdc": "centers for disease control and prevention",
    "caltech": "california institute of technology",
    "eth zurich": "eth zurich",
    "epfl": "ecole polytechnique federale de lausanne",
    "unc chapel hill": "university of north carolina at chapel hill",
    "ut austin": "the university of texas at austin",
    "ut southwestern": "the university of texas southwestern medical center",
    "nyu": "new york university",
    "nyu langone": "nyu langone health",
    "upenn": "university of pennsylvania",
    "umich": "university of michigan",
    "uw": "university of washington",
    "hms": "harvard medical school",
    "mgh": "massachusetts general hospital",
    "bwh": "brigham and women's hospital",
    "mskcc": "memorial sloan kettering cancer center",
    "broad institute": "broad institute",
    "stanford": "stanford university",
    "oxford": "university of oxford",
    "cambridge": "university of cambridge",
    "harvard": "harvard university",
    "yale": "yale university",
    "princeton": "princeton university",
    "columbia": "columbia university",
    "cornell": "cornell university",
    "duke": "duke university",
    "johns hopkins": "johns hopkins university",
    "mount sinai": "icahn school of medicine at mount sinai",
    "imperial college": "imperial college london",
    "king's college london": "king's college london",
    "cnrs": "centre national de la recherche scientifique",
    "inserm": "institut national de la sante et de la recherche medicale",
    "csic": "consejo superior de investigaciones cientificas",
}

ORG_TYPE_PRIORITY = {
    "education": 1,
    "healthcare": 2,
    "facility": 3,
    "government": 4,
    "nonprofit": 5,
    "company": 6,
    "funder": 7,
    "archive": 8,
    "other": 9,
}

NON_ORG_SEGMENT_RE = re.compile(
    r"^(?:department|division|center|centre|institute|laboratory|lab|program|unit|"
    r"school|faculty|college|section|group|branch|office)\s+of\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ROROrg:
    """Canonical organization record from the Research Organization Registry."""

    ror_id: str
    name: str
    country: str
    country_code: str
    org_type: str


@dataclass(frozen=True)
class RORMatch:
    """Resolution result for a single raw institutional affiliation string."""

    raw_institution: str
    ror_id: str | None
    ror_name: str
    ror_country: str | None
    ror_country_code: str | None
    ror_type: str | None
    match_method: str  # "exact", "alias", "segment", "subunit", "ror_api", "unmatched"


def normalize_org_text(text: str) -> str:
    """Normalize an organization string for deterministic matching."""
    if not text:
        return ""
    # Fold Unicode accents (e.g. École -> Ecole, Zürich -> Zurich)
    nfkd = unicodedata.normalize("NFKD", str(text))
    ascii_str = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    s = ascii_str.lower().strip()
    s = s.replace("&", " and ")
    # Replace punctuation/slashes/hyphens with spaces
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for pat, repl in TYPO_REPLACEMENTS:
        s = pat.sub(repl, s)
    s = re.sub(r"\s+", " ", s).strip()
    # Strip leading "the " for cleaner matching ("The University of Texas" == "University of Texas")
    if s.startswith("the ") and len(s) > 8:
        s = s[4:]
    return s


def parse_ror_dump_entry(entry: dict[str, Any]) -> tuple[ROROrg, list[str], list[str]] | None:
    """Parse either ROR v2 or ROR v1 JSON entry into ``(ROROrg, primary_names, aliases)``."""
    ror_id = str(entry.get("id") or "").strip()
    if not ror_id:
        return None
    if entry.get("status") not in {None, "active"}:
        return None

    types = [str(t).lower() for t in (entry.get("types") or [])]
    types_sorted = sorted(types, key=lambda t: ORG_TYPE_PRIORITY.get(t, 99))
    org_type = types_sorted[0] if types_sorted else "other"

    display_name = ""
    primary_names: list[str] = []
    aliases: list[str] = []

    if "names" in entry and isinstance(entry["names"], list):
        # ROR schema v2
        for n in entry["names"]:
            val = str(n.get("value") or "").strip()
            ntypes = set(n.get("types") or [])
            if not val:
                continue
            if "ror_display" in ntypes:
                display_name = val
                primary_names.append(val)
            elif "label" in ntypes:
                primary_names.append(val)
            elif "alias" in ntypes:
                aliases.append(val)
            elif "acronym" in ntypes and len(val) >= 4:
                aliases.append(val)
        if not display_name and primary_names:
            display_name = primary_names[0]
    else:
        # ROR schema v1 fallback
        display_name = str(entry.get("name") or "").strip()
        if display_name:
            primary_names.append(display_name)
        for lbl in entry.get("labels") or []:
            if isinstance(lbl, dict) and lbl.get("label"):
                primary_names.append(str(lbl["label"]).strip())
        for al in entry.get("aliases") or []:
            if al:
                aliases.append(str(al).strip())
        for acr in entry.get("acronyms") or []:
            if acr and len(str(acr).strip()) >= 4:
                aliases.append(str(acr).strip())

    if not display_name:
        return None

    country = ""
    country_code = ""
    if "locations" in entry and isinstance(entry["locations"], list) and entry["locations"]:
        geo = (entry["locations"][0] or {}).get("geonames_details") or {}
        country = str(geo.get("country_name") or "").strip()
        country_code = str(geo.get("country_code") or "").strip()
    elif isinstance(entry.get("country"), dict):
        country = str(entry["country"].get("country_name") or "").strip()
        country_code = str(entry["country"].get("country_code") or "").strip()

    org = ROROrg(
        ror_id=ror_id,
        name=display_name,
        country=country,
        country_code=country_code,
        org_type=org_type,
    )
    return org, primary_names, aliases


def download_ror_dump(cache_dir: Path, *, force: bool = False) -> Path:
    """Download the latest official CC0 ROR data dump from Zenodo and extract its JSON."""
    ror_dir = cache_dir / "ror"
    ror_dir.mkdir(parents=True, exist_ok=True)
    cached_json = ror_dir / "ror_data_latest.json"
    if cached_json.exists() and not force:
        logger.info("Using cached ROR data dump at %s", cached_json)
        return cached_json

    logger.info("Querying Zenodo for latest official ROR data dump...")
    resp = requests.get(
        ZENODO_ROR_LATEST_URL,
        params={"communities": "ror-data", "sort": "mostrecent", "size": 1},
        timeout=30,
    )
    resp.raise_for_status()
    hits = (resp.json().get("hits") or {}).get("hits") or []
    if not hits:
        raise RuntimeError("No ROR data dump found on Zenodo.")

    files = hits[0].get("files") or []
    zip_file = next((f for f in files if str(f.get("key", "")).endswith(".zip")), None)
    if not zip_file:
        raise RuntimeError("Could not locate .zip archive in latest Zenodo ROR record.")

    download_url = (zip_file.get("links") or {}).get("self")
    if not download_url:
        raise RuntimeError("Missing download link in Zenodo ROR file metadata.")

    zip_path = ror_dir / "ror_latest.zip"
    logger.info("Downloading ROR archive (%s)...", zip_file.get("key"))
    with requests.get(download_url, stream=True, timeout=180) as dl:
        dl.raise_for_status()
        with zip_path.open("wb") as fh:
            for chunk in dl.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)

    # Extract the v2 JSON file (preferred) or largest JSON file in the archive
    with zipfile.ZipFile(zip_path) as zf:
        json_members = [m for m in zf.namelist() if m.endswith(".json")]
        if not json_members:
            raise RuntimeError("No JSON file found inside ROR zip archive.")
        v2_members = [m for m in json_members if "schema_v2" in m]
        chosen = v2_members[0] if v2_members else sorted(
            json_members, key=lambda m: zf.getinfo(m).file_size, reverse=True
        )[0]
        logger.info("Extracting %s -> %s", chosen, cached_json)
        with zf.open(chosen) as src, cached_json.open("wb") as dst:
            dst.write(src.read())

    zip_path.unlink(missing_ok=True)
    return cached_json


class RORMatcher:
    """Fast in-memory multi-tier ROR matcher built from an official ROR JSON dump."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.exact_index: dict[str, ROROrg] = {}
        self.alias_index: dict[str, ROROrg] = {}
        self.org_count = 0
        self._build_index(entries)

    @classmethod
    def from_json_file(cls, json_path: Path) -> RORMatcher:
        logger.info("Loading ROR index from %s...", json_path)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return cls(data)

    def _build_index(self, entries: list[dict[str, Any]]) -> None:
        exact_candidates: dict[str, list[ROROrg]] = defaultdict(list)
        alias_candidates: dict[str, list[ROROrg]] = defaultdict(list)

        for entry in entries:
            parsed = parse_ror_dump_entry(entry)
            if parsed is None:
                continue
            org, primary_names, aliases = parsed
            self.org_count += 1
            for pname in primary_names:
                norm = normalize_org_text(pname)
                if len(norm) >= 3:
                    exact_candidates[norm].append(org)
            for al in aliases:
                norm = normalize_org_text(al)
                if len(norm) >= 4:
                    alias_candidates[norm].append(org)

        # Resolve exact index collisions by organization type priority (education > healthcare > ...)
        for key, orgs in exact_candidates.items():
            orgs.sort(key=lambda o: (ORG_TYPE_PRIORITY.get(o.org_type, 99), len(o.name)))
            self.exact_index[key] = orgs[0]

        # Only keep unambiguous aliases (1 unique ROR ID) to prevent acronym false positives
        for key, orgs in alias_candidates.items():
            if key in self.exact_index:
                continue
            unique_ids = {o.ror_id for o in orgs}
            if len(unique_ids) == 1:
                self.alias_index[key] = orgs[0]

        logger.info(
            "Built ROR index: %d orgs | %d exact keys | %d unambiguous aliases",
            self.org_count,
            len(self.exact_index),
            len(self.alias_index),
        )

    def _lookup_norm(self, norm: str) -> tuple[ROROrg, str] | None:
        if not norm:
            return None
        # Check curated biomedical shorthands first
        mapped = CURATED_SHORTHANDS.get(norm)
        if mapped:
            mapped_norm = normalize_org_text(mapped)
            if mapped_norm in self.exact_index:
                return self.exact_index[mapped_norm], "alias"
            if mapped_norm in self.alias_index:
                return self.alias_index[mapped_norm], "alias"

        if norm in self.exact_index:
            return self.exact_index[norm], "exact"
        if norm in self.alias_index:
            return self.alias_index[norm], "alias"
        return None

    def match(self, raw_institution: str) -> RORMatch:
        """Resolve a raw institutional affiliation string to a canonical ROR organization."""
        raw = str(raw_institution).strip()
        if not raw:
            return RORMatch(raw, None, raw, None, None, None, "unmatched")

        # Tier 1: Full normalized string match
        norm_full = normalize_org_text(raw)
        hit = self._lookup_norm(norm_full)
        if hit:
            org, method = hit
            return RORMatch(raw, org.ror_id, org.name, org.country, org.country_code, org.org_type, method)

        # Tier 2: Segment parsing (handles "Dept of X, University of Y, City, Country")
        # Split on semicolons, pipes, and commas; test adjacent 2-segment pairs BEFORE singles
        # so "University of California, San Francisco" matches as a unit!
        parts = [p.strip() for p in re.split(r"[,;|/]", raw) if p.strip()]
        if len(parts) > 1:
            candidates: list[tuple[int, ROROrg]] = []
            # Test 2-part joins first (e.g. "University of California, Berkeley")
            for idx in range(len(parts) - 1):
                pair_norm = normalize_org_text(f"{parts[idx]} {parts[idx + 1]}")
                pair_hit = self._lookup_norm(pair_norm)
                if pair_hit:
                    org = pair_hit[0]
                    candidates.append((ORG_TYPE_PRIORITY.get(org.org_type, 99), org))

            if candidates:
                candidates.sort(key=lambda x: x[0])
                best = candidates[0][1]
                return RORMatch(raw, best.ror_id, best.name, best.country, best.country_code, best.org_type, "segment")

            for seg in parts:
                if NON_ORG_SEGMENT_RE.match(seg):
                    continue
                seg_norm = normalize_org_text(seg)
                if len(seg_norm) < 4:
                    continue
                seg_hit = self._lookup_norm(seg_norm)
                if seg_hit:
                    org = seg_hit[0]
                    candidates.append((ORG_TYPE_PRIORITY.get(org.org_type, 99), org))

            if candidates:
                candidates.sort(key=lambda x: x[0])
                best = candidates[0][1]
                return RORMatch(raw, best.ror_id, best.name, best.country, best.country_code, best.org_type, "segment")

        # Tier 3: Sub-unit / school suffix stripping ("Stanford University School of Medicine" -> "Stanford University")
        stripped = SUBUNIT_SUFFIX_RE.sub("", raw).strip(" ,;-")
        if stripped and stripped.lower() != raw.lower():
            sub_hit = self._lookup_norm(normalize_org_text(stripped))
            if sub_hit:
                org = sub_hit[0]
                return RORMatch(raw, org.ror_id, org.name, org.country, org.country_code, org.org_type, "subunit")

        # Also try suffix stripping on individual comma-delimited segments
        if len(parts) > 1:
            for seg in parts:
                s_seg = SUBUNIT_SUFFIX_RE.sub("", seg).strip(" ,;-")
                if s_seg and s_seg.lower() != seg.lower():
                    sub_hit = self._lookup_norm(normalize_org_text(s_seg))
                    if sub_hit:
                        org = sub_hit[0]
                        return RORMatch(raw, org.ror_id, org.name, org.country, org.country_code, org.org_type, "subunit")

        return RORMatch(raw, None, raw, None, None, None, "unmatched")


class RORApiCache:
    """SQLite cache for optional ROR v2 affiliation API fallback queries."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ror_api_matches (
                    raw_institution TEXT PRIMARY KEY,
                    ror_id TEXT,
                    ror_name TEXT,
                    ror_country TEXT,
                    ror_country_code TEXT,
                    ror_type TEXT,
                    score REAL,
                    fetched_at_utc TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, raw: str) -> RORMatch | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ror_api_matches WHERE raw_institution = ?", (raw,)
            ).fetchone()
            if not row:
                return None
            if not row["ror_id"]:
                return RORMatch(raw, None, raw, None, None, None, "unmatched")
            return RORMatch(
                raw_institution=raw,
                ror_id=row["ror_id"],
                ror_name=row["ror_name"],
                ror_country=row["ror_country"],
                ror_country_code=row["ror_country_code"],
                ror_type=row["ror_type"],
                match_method="ror_api",
            )

    def put(self, match: RORMatch, score: float = 0.0) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO ror_api_matches
                    (raw_institution, ror_id, ror_name, ror_country, ror_country_code, ror_type, score, fetched_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match.raw_institution,
                    match.ror_id,
                    match.ror_name,
                    match.ror_country,
                    match.ror_country_code,
                    match.ror_type,
                    score,
                    now_iso,
                ),
            )
            conn.commit()


def query_ror_affiliation_api(
    raw_institution: str,
    http: PoliteHttpClient,
    cache: RORApiCache,
) -> RORMatch:
    """Query official ROR v2 affiliation matcher for a single unmatched string (cached)."""
    cached = cache.get(raw_institution)
    if cached is not None:
        return cached

    payload = http.get_json(ROR_V2_ORGS_API, params={"affiliation": raw_institution})
    if payload:
        for item in payload.get("items") or []:
            if item.get("chosen"):
                parsed = parse_ror_dump_entry(item.get("organization") or {})
                if parsed:
                    org = parsed[0]
                    m = RORMatch(
                        raw_institution=raw_institution,
                        ror_id=org.ror_id,
                        ror_name=org.name,
                        ror_country=org.country,
                        ror_country_code=org.country_code,
                        ror_type=org.org_type,
                        match_method="ror_api",
                    )
                    cache.put(m, score=float(item.get("score") or 1.0))
                    return m

    miss = RORMatch(raw_institution, None, raw_institution, None, None, None, "unmatched")
    cache.put(miss, score=0.0)
    return miss


def build_institution_ror_mapping(
    paths: ProjectPaths,
    *,
    ror_json_path: Path | None = None,
    api_fallback_top_n: int = 0,
    delay_seconds: float = 0.15,
) -> dict[str, Any]:
    """Deduplicate all unique ``institution_clean`` strings in ``preprints_canonical.parquet`` via ROR."""
    canon_path = paths.processed_dir / "preprints_canonical.parquet"
    if not canon_path.exists():
        raise FileNotFoundError(f"Canonical Parquet not found at {canon_path}. Run scripts/run_etl.py first.")

    json_file = ror_json_path or download_ror_dump(paths.cache_dir)
    matcher = RORMatcher.from_json_file(json_file)

    df = pd.read_parquet(canon_path, columns=["institution_clean"])
    vc = df["institution_clean"].dropna().value_counts()
    total_non_null = int(vc.sum())
    unique_raw = vc.index.tolist()
    logger.info("Matching %d unique raw institution strings against ROR...", len(unique_raw))

    matches: list[RORMatch] = [matcher.match(raw) for raw in unique_raw]

    # Optional Tier 4: ROR v2 API fallback for highest-volume remaining unmatched strings
    if api_fallback_top_n > 0:
        unmatched_indices = [
            i for i, m in enumerate(matches) if m.ror_id is None
        ][:api_fallback_top_n]
        if unmatched_indices:
            logger.info(
                "Running ROR v2 affiliation API fallback on top %d unmatched strings...",
                len(unmatched_indices),
            )
            http = PoliteHttpClient(min_delay_seconds=delay_seconds)
            api_cache = RORApiCache(paths.cache_dir / "ror_api_cache.sqlite")
            for idx in unmatched_indices:
                matches[idx] = query_ror_affiliation_api(unique_raw[idx], http, api_cache)

    rows = [
        {
            "institution_clean": raw_orig,
            "preprint_count": int(vc[raw_orig]),
            "ror_id": m.ror_id,
            "ror_name": m.ror_name,
            "ror_country": m.ror_country,
            "ror_country_code": m.ror_country_code,
            "ror_type": m.ror_type,
            "match_method": m.match_method,
        }
        for raw_orig, m in zip(unique_raw, matches, strict=False)
    ]
    map_df = pd.DataFrame(rows)
    out_path = paths.processed_dir / "institution_ror_map.parquet"
    map_df.to_parquet(out_path, index=False, compression="zstd")

    matched_df = map_df[map_df["ror_id"].notna()]
    matched_preprints = int(matched_df["preprint_count"].sum())
    method_counts = map_df.groupby("match_method")["preprint_count"].sum().to_dict()

    summary = {
        "total_non_null_preprints": total_non_null,
        "unique_raw_strings": len(unique_raw),
        "matched_raw_strings": int(len(matched_df)),
        "unique_ror_orgs_matched": int(matched_df["ror_id"].nunique()),
        "matched_preprints": matched_preprints,
        "coverage_pct": round((matched_preprints / max(total_non_null, 1)) * 100.0, 2),
        "unique_countries": int(matched_df["ror_country"].dropna().nunique()),
        "by_method_preprints": {str(k): int(v) for k, v in method_counts.items()},
        "output_parquet": out_path.relative_to(paths.root).as_posix(),
    }
    audit_path = paths.processed_dir / "ror_dedup_audit.json"
    audit_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "ROR deduplication complete: %.1f%% preprint coverage (%d raw strings -> %d unique ROR orgs).",
        summary["coverage_pct"],
        summary["unique_raw_strings"],
        summary["unique_ror_orgs_matched"],
    )
    return summary
