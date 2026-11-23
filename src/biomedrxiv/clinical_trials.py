"""Clinical trial linkage for Bio&Med-Rxiv preprints and peer-reviewed publications.

Supports two complementary clinical-trial linkage layers:
1. **Direct Trial Registry Extraction & ClinicalTrials.gov API v2 Enrichment**
   - Extracts clinical trial identifiers (``NCT``, ``ISRCTN``, ``ChiCTR``,
     ``ACTRN``, ``EudraCT``) directly from preprint titles & abstracts.
   - Batch-enriches ``NCT`` IDs via ClinicalTrials.gov API v2 (``filter.ids``,
     up to 50 NCT IDs per HTTP request) into a resumable SQLite cache.
2. **Downstream Translational Linkage (Publications Leading to Clinical Trials)**
   - Resolves peer-reviewed ``published_doi`` -> ``PMID`` in batches via
     Europe PMC REST API.
   - Queries NIH iCite (``https://icite.od.nih.gov/api/pubs``, up to 200 PMIDs
     per request) to capture ``cited_by_clin`` (clinical trials/guidelines
     citing the publication), ``apt`` (Approximate Potential to Translate), and
     ``relative_citation_ratio`` (RCR).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from biomedrxiv.config import ProjectPaths
from biomedrxiv.publication_checker import PoliteHttpClient, normalize_doi

logger = logging.getLogger(__name__)

CLINICALTRIALS_V2_STUDIES = "https://clinicaltrials.gov/api/v2/studies"
EUROPEPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
NIH_ICITE_PUBS_URL = "https://icite.od.nih.gov/api/pubs"

CT_V2_FIELDS = ",".join(
    [
        "NCTId",
        "BriefTitle",
        "OverallStatus",
        "Phase",
        "StudyType",
        "LeadSponsorName",
        "LeadSponsorClass",
        "EnrollmentCount",
        "StartDate",
        "CompletionDate",
        "Condition",
        "InterventionName",
        "HasResults",
    ]
)

# Registry ID extraction patterns
TRIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "ClinicalTrials.gov": re.compile(r"\b(NCT\d{8})\b", re.IGNORECASE),
    "ISRCTN": re.compile(r"\b(ISRCTN\d{8})\b", re.IGNORECASE),
    "ChiCTR": re.compile(r"\b(ChiCTR(?:\d{8,10}|-[A-Z]{2,5}-\d{6,10}))\b", re.IGNORECASE),
    "ANZCTR": re.compile(r"\b(ACTRN\d{14})\b", re.IGNORECASE),
    "EudraCT": re.compile(r"EudraCT[:\s#-]*(20\d{2}-\d{6}-\d{2})\b", re.IGNORECASE),
}


def extract_trial_ids_from_text(text: Any) -> list[tuple[str, str]]:
    """Extract deduplicated ``(trial_id, registry)`` pairs from text."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return []
    s = str(text)
    if not s:
        return []

    seen: set[str] = set()
    found: list[tuple[str, str]] = []
    for registry, pattern in TRIAL_PATTERNS.items():
        for match in pattern.findall(s):
            tid = match.upper() if registry != "ChiCTR" else f"ChiCTR{match[6:]}"
            if registry == "EudraCT" and not tid.startswith("EUDRACT"):
                tid = f"EudraCT:{tid}"
            if tid not in seen:
                seen.add(tid)
                found.append((tid, registry))
    return found


@dataclass
class NCTTrialRecord:
    """Enriched ClinicalTrials.gov v2 study metadata."""

    nct_id: str
    brief_title: str | None = None
    overall_status: str | None = None
    phase: str | None = None
    study_type: str | None = None
    sponsor_name: str | None = None
    sponsor_class: str | None = None
    enrollment_count: int | None = None
    start_date: str | None = None
    completion_date: str | None = None
    conditions: list[str] | None = None
    interventions: list[str] | None = None
    has_results: bool = False
    fetched_at_utc: str = ""

    def __post_init__(self) -> None:
        if not self.fetched_at_utc:
            self.fetched_at_utc = datetime.now(timezone.utc).isoformat()


@dataclass
class DownstreamClinicalRecord:
    """NIH iCite translational / clinical-citation record for a published paper."""

    published_doi: str
    pmid: str | None = None
    is_clinical: bool = False
    apt: float | None = None
    cited_by_clin_count: int = 0
    cited_by_clin_pmids: list[str] | None = None
    citation_count: int = 0
    relative_citation_ratio: float | None = None
    fetched_at_utc: str = ""

    def __post_init__(self) -> None:
        if not self.fetched_at_utc:
            self.fetched_at_utc = datetime.now(timezone.utc).isoformat()


class ClinicalTrialsCache:
    """SQLite cache for ClinicalTrials.gov studies and NIH iCite translational links."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nct_trials (
                    nct_id TEXT PRIMARY KEY,
                    brief_title TEXT,
                    overall_status TEXT,
                    phase TEXT,
                    study_type TEXT,
                    sponsor_name TEXT,
                    sponsor_class TEXT,
                    enrollment_count INTEGER,
                    start_date TEXT,
                    completion_date TEXT,
                    conditions_json TEXT,
                    interventions_json TEXT,
                    has_results INTEGER NOT NULL DEFAULT 0,
                    fetched_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downstream_clinical (
                    published_doi TEXT PRIMARY KEY,
                    pmid TEXT,
                    is_clinical INTEGER NOT NULL DEFAULT 0,
                    apt REAL,
                    cited_by_clin_count INTEGER NOT NULL DEFAULT 0,
                    cited_by_clin_pmids_json TEXT,
                    citation_count INTEGER NOT NULL DEFAULT 0,
                    relative_citation_ratio REAL,
                    fetched_at_utc TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def upsert_nct_trials(self, records: Iterable[NCTTrialRecord]) -> int:
        rows = [
            (
                r.nct_id.upper(),
                r.brief_title,
                r.overall_status,
                r.phase,
                r.study_type,
                r.sponsor_name,
                r.sponsor_class,
                r.enrollment_count,
                r.start_date,
                r.completion_date,
                json.dumps(r.conditions or []),
                json.dumps(r.interventions or []),
                int(r.has_results),
                r.fetched_at_utc,
            )
            for r in records
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO nct_trials (
                    nct_id, brief_title, overall_status, phase, study_type,
                    sponsor_name, sponsor_class, enrollment_count, start_date,
                    completion_date, conditions_json, interventions_json,
                    has_results, fetched_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def get_nct_trials(self, nct_ids: Iterable[str] | None = None) -> dict[str, NCTTrialRecord]:
        results: dict[str, NCTTrialRecord] = {}
        with self._connect() as conn:
            if nct_ids is None:
                rows = conn.execute("SELECT * FROM nct_trials").fetchall()
            else:
                id_list = list({x.upper() for x in nct_ids if x})
                rows = []
                for start in range(0, len(id_list), 900):
                    chunk = id_list[start : start + 900]
                    ph = ",".join("?" for _ in chunk)
                    rows.extend(
                        conn.execute(
                            f"SELECT * FROM nct_trials WHERE nct_id IN ({ph})", chunk
                        ).fetchall()
                    )
            for r in rows:
                results[r["nct_id"]] = NCTTrialRecord(
                    nct_id=r["nct_id"],
                    brief_title=r["brief_title"],
                    overall_status=r["overall_status"],
                    phase=r["phase"],
                    study_type=r["study_type"],
                    sponsor_name=r["sponsor_name"],
                    sponsor_class=r["sponsor_class"],
                    enrollment_count=r["enrollment_count"],
                    start_date=r["start_date"],
                    completion_date=r["completion_date"],
                    conditions=json.loads(r["conditions_json"] or "[]"),
                    interventions=json.loads(r["interventions_json"] or "[]"),
                    has_results=bool(r["has_results"]),
                    fetched_at_utc=r["fetched_at_utc"],
                )
        return results

    def upsert_downstream(self, records: Iterable[DownstreamClinicalRecord]) -> int:
        rows = [
            (
                r.published_doi,
                r.pmid,
                int(r.is_clinical),
                r.apt,
                r.cited_by_clin_count,
                json.dumps(r.cited_by_clin_pmids or []),
                r.citation_count,
                r.relative_citation_ratio,
                r.fetched_at_utc,
            )
            for r in records
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO downstream_clinical (
                    published_doi, pmid, is_clinical, apt, cited_by_clin_count,
                    cited_by_clin_pmids_json, citation_count,
                    relative_citation_ratio, fetched_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def get_downstream(
        self, published_dois: Iterable[str] | None = None
    ) -> dict[str, DownstreamClinicalRecord]:
        results: dict[str, DownstreamClinicalRecord] = {}
        with self._connect() as conn:
            if published_dois is None:
                rows = conn.execute("SELECT * FROM downstream_clinical").fetchall()
            else:
                doi_list = list({d for d in (normalize_doi(x) for x in published_dois) if d})
                rows = []
                for start in range(0, len(doi_list), 900):
                    chunk = doi_list[start : start + 900]
                    ph = ",".join("?" for _ in chunk)
                    rows.extend(
                        conn.execute(
                            f"SELECT * FROM downstream_clinical WHERE published_doi IN ({ph})",
                            chunk,
                        ).fetchall()
                    )
            for r in rows:
                results[r["published_doi"]] = DownstreamClinicalRecord(
                    published_doi=r["published_doi"],
                    pmid=r["pmid"],
                    is_clinical=bool(r["is_clinical"]),
                    apt=r["apt"],
                    cited_by_clin_count=int(r["cited_by_clin_count"]),
                    cited_by_clin_pmids=json.loads(r["cited_by_clin_pmids_json"] or "[]"),
                    citation_count=int(r["citation_count"]),
                    relative_citation_ratio=r["relative_citation_ratio"],
                    fetched_at_utc=r["fetched_at_utc"],
                )
        return results

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            nct_total = conn.execute("SELECT COUNT(*) FROM nct_trials").fetchone()[0]
            nct_results = conn.execute(
                "SELECT COUNT(*) FROM nct_trials WHERE has_results = 1"
            ).fetchone()[0]
            ds_total = conn.execute("SELECT COUNT(*) FROM downstream_clinical").fetchone()[0]
            ds_cited_by_clin = conn.execute(
                "SELECT COUNT(*) FROM downstream_clinical WHERE cited_by_clin_count > 0"
            ).fetchone()[0]
        return {
            "nct_trials_cached": int(nct_total),
            "nct_trials_with_results": int(nct_results),
            "downstream_dois_cached": int(ds_total),
            "downstream_dois_cited_by_clinical_trials": int(ds_cited_by_clin),
        }


def parse_clinicaltrials_v2_study(study: dict[str, Any]) -> NCTTrialRecord | None:
    """Parse a ClinicalTrials.gov API v2 study JSON object into an ``NCTTrialRecord``."""
    proto = study.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    nct_id = ident.get("nctId")
    if not nct_id:
        return None

    status_mod = proto.get("statusModule") or {}
    design_mod = proto.get("designModule") or {}
    sponsor_mod = proto.get("sponsorCollaboratorsModule") or {}
    lead_sponsor = sponsor_mod.get("leadSponsor") or {}
    cond_mod = proto.get("conditionsModule") or {}
    arms_mod = proto.get("armsInterventionsModule") or {}

    phases = design_mod.get("phases") or []
    phase_str = "/".join(phases) if phases else "NA"

    enrollment_info = design_mod.get("enrollmentInfo") or {}
    enroll_raw = enrollment_info.get("count")
    enrollment_count = int(enroll_raw) if enroll_raw is not None else None

    interventions = [
        iv.get("name")
        for iv in (arms_mod.get("interventions") or [])
        if isinstance(iv, dict) and iv.get("name")
    ]

    return NCTTrialRecord(
        nct_id=str(nct_id).upper(),
        brief_title=ident.get("briefTitle"),
        overall_status=status_mod.get("overallStatus"),
        phase=phase_str,
        study_type=design_mod.get("studyType"),
        sponsor_name=lead_sponsor.get("name"),
        sponsor_class=lead_sponsor.get("class"),
        enrollment_count=enrollment_count,
        start_date=(status_mod.get("startDateStruct") or {}).get("date"),
        completion_date=(status_mod.get("completionDateStruct") or {}).get("date"),
        conditions=list(cond_mod.get("conditions") or []),
        interventions=interventions[:10],
        has_results=bool(study.get("hasResults")),
    )


class ClinicalTrialsLinker:
    """Batch client for ClinicalTrials.gov API v2 and Europe PMC + NIH iCite."""

    def __init__(
        self,
        cache: ClinicalTrialsCache,
        http_client: PoliteHttpClient | None = None,
    ) -> None:
        self.cache = cache
        self.http = http_client or PoliteHttpClient(min_delay_seconds=0.25)

    def enrich_nct_ids(
        self,
        nct_ids: Iterable[str],
        *,
        batch_size: int = 50,
        force_refresh: bool = False,
        max_batches: int | None = None,
    ) -> dict[str, NCTTrialRecord]:
        """Batch-fetch ClinicalTrials.gov v2 study metadata using ``filter.ids``."""
        unique_ids = list(dict.fromkeys(x.upper() for x in nct_ids if x and x.upper().startswith("NCT")))
        cached = {} if force_refresh else self.cache.get_nct_trials(unique_ids)
        missing = [nid for nid in unique_ids if nid not in cached]

        logger.info(
            "NCT IDs requested: %d | Cache hits: %d | To fetch: %d",
            len(unique_ids),
            len(unique_ids) - len(missing),
            len(missing),
        )

        batches_run = 0
        for start in range(0, len(missing), batch_size):
            if max_batches is not None and batches_run >= max_batches:
                break
            chunk = missing[start : start + batch_size]
            params = {
                "filter.ids": ",".join(chunk),
                "fields": CT_V2_FIELDS,
                "pageSize": len(chunk),
            }
            payload = self.http.get_json(CLINICALTRIALS_V2_STUDIES, params=params)
            batches_run += 1
            if not payload:
                continue

            parsed: list[NCTTrialRecord] = []
            for study in payload.get("studies") or []:
                rec = parse_clinicaltrials_v2_study(study)
                if rec:
                    parsed.append(rec)
                    cached[rec.nct_id] = rec
            self.cache.upsert_nct_trials(parsed)
            logger.info(
                "Batch %d: enriched %d/%d NCT studies.", batches_run, len(parsed), len(chunk)
            )

        return {nid: cached[nid] for nid in unique_ids if nid in cached}

    def resolve_dois_to_pmids(
        self, published_dois: Iterable[str], *, batch_size: int = 25
    ) -> dict[str, str]:
        """Resolve published journal DOIs to PubMed IDs (PMIDs) in batches via Europe PMC."""
        norm_dois = list(dict.fromkeys(d for d in (normalize_doi(x) for x in published_dois) if d))
        doi_to_pmid: dict[str, str] = {}

        for start in range(0, len(norm_dois), batch_size):
            chunk = norm_dois[start : start + batch_size]
            query = " OR ".join(f'DOI:"{d}"' for d in chunk)
            params = {
                "query": query,
                "format": "json",
                "pageSize": len(chunk) * 2,
                "resultType": "lite",
            }
            payload = self.http.get_json(EUROPEPMC_SEARCH_URL, params=params)
            if not payload:
                continue
            for item in (payload.get("resultList") or {}).get("result") or []:
                item_doi = normalize_doi(item.get("doi"))
                pmid = item.get("pmid")
                if item_doi and pmid:
                    doi_to_pmid[item_doi] = str(pmid)
        return doi_to_pmid

    def trace_downstream_clinical_impact(
        self,
        published_dois: Iterable[str],
        *,
        force_refresh: bool = False,
        max_dois: int | None = None,
    ) -> dict[str, DownstreamClinicalRecord]:
        """Trace which peer-reviewed papers are cited by clinical trials via NIH iCite."""
        norm_dois = list(dict.fromkeys(d for d in (normalize_doi(x) for x in published_dois) if d))
        cached = {} if force_refresh else self.cache.get_downstream(norm_dois)
        missing = [d for d in norm_dois if d not in cached]
        if max_dois is not None and len(missing) > max_dois:
            missing = missing[:max_dois]

        if not missing:
            return {d: cached[d] for d in norm_dois if d in cached}

        logger.info("Resolving %d published DOIs to PMIDs via Europe PMC...", len(missing))
        doi_to_pmid = self.resolve_dois_to_pmids(missing)
        pmid_to_doi = {pmid: doi for doi, pmid in doi_to_pmid.items()}

        # Store unresolved DOIs so we don't re-query them unnecessarily
        unresolved_recs = [
            DownstreamClinicalRecord(published_doi=d, pmid=None)
            for d in missing
            if d not in doi_to_pmid
        ]
        if unresolved_recs:
            self.cache.upsert_downstream(unresolved_recs)
            for rec in unresolved_recs:
                cached[rec.published_doi] = rec

        pmids = list(pmid_to_doi.keys())
        logger.info("Querying NIH iCite for %d resolved PMIDs...", len(pmids))
        for start in range(0, len(pmids), 200):
            chunk = pmids[start : start + 200]
            payload = self.http.get_json(NIH_ICITE_PUBS_URL, params={"pmids": ",".join(chunk)})
            if not payload:
                continue

            batch_recs: list[DownstreamClinicalRecord] = []
            for item in payload.get("data") or []:
                pmid = str(item.get("pmid"))
                doi = pmid_to_doi.get(pmid)
                if not doi:
                    continue
                clin_cites = [str(x) for x in (item.get("cited_by_clin") or [])]
                apt_val = item.get("apt")
                rcr_val = item.get("relative_citation_ratio")
                rec = DownstreamClinicalRecord(
                    published_doi=doi,
                    pmid=pmid,
                    is_clinical=bool(item.get("is_clinical")),
                    apt=round(float(apt_val), 3) if apt_val is not None else None,
                    cited_by_clin_count=len(clin_cites),
                    cited_by_clin_pmids=clin_cites[:50],
                    citation_count=int(item.get("citation_count") or 0),
                    relative_citation_ratio=(
                        round(float(rcr_val), 2) if rcr_val is not None else None
                    ),
                )
                batch_recs.append(rec)
                cached[doi] = rec
            self.cache.upsert_downstream(batch_recs)

        return {d: cached[d] for d in norm_dois if d in cached}


def extract_direct_trial_links(
    paths: ProjectPaths,
    *,
    versions_parquet: Path | None = None,
    canonical_parquet: Path | None = None,
) -> pd.DataFrame:
    """Scan materialized Parquet abstracts/titles and write ``preprint_trial_links.parquet``."""
    v_path = versions_parquet or (paths.processed_dir / "preprints_versions.parquet")
    c_path = canonical_parquet or (paths.processed_dir / "preprints_canonical.parquet")
    if not v_path.exists():
        raise FileNotFoundError(f"Versions Parquet not found at {v_path}. Run scripts/run_etl.py first.")

    logger.info("Scanning %s for clinical trial registry IDs...", v_path)
    df = pd.read_parquet(
        v_path,
        columns=["normalized_doi", "version", "title", "abstract"],
    )
    # Keep latest version per DOI for clean text scan
    df = df.dropna(subset=["normalized_doi"]).sort_values(["normalized_doi", "version"]).drop_duplicates(
        "normalized_doi", keep="last"
    )

    combined = df["title"].fillna("").astype(str) + " " + df["abstract"].fillna("").astype(str)
    # Fast pre-filter before full multi-registry extraction
    prefilter = combined.str.contains(
        r"(?:NCT\d{8}|ISRCTN\d{8}|ChiCTR|ACTRN\d{14}|EudraCT)",
        case=False,
        regex=True,
    )
    candidates = df.loc[prefilter, ["normalized_doi"]].copy()
    candidates["combined"] = combined.loc[prefilter]

    link_rows: list[dict[str, Any]] = []
    for doi, text in zip(candidates["normalized_doi"], candidates["combined"], strict=False):
        for tid, reg in extract_trial_ids_from_text(text):
            link_rows.append({"normalized_doi": doi, "trial_id": tid, "registry": reg})

    links_df = pd.DataFrame(link_rows, columns=["normalized_doi", "trial_id", "registry"])
    if c_path.exists() and not links_df.empty:
        canon = pd.read_parquet(
            c_path,
            columns=[
                "normalized_doi",
                "title",
                "server_clean",
                "category_clean",
                "first_posted_date",
                "snapshot_is_published",
                "snapshot_published_doi",
                "publisher_name",
                "institution_clean",
                "doi_link",
            ],
        )
        links_df = links_df.merge(canon, on="normalized_doi", how="left")

    out_path = paths.processed_dir / "preprint_trial_links.parquet"
    links_df.to_parquet(out_path, index=False, compression="zstd")
    logger.info(
        "Extracted %d preprint-to-trial links across %d unique DOIs -> %s",
        len(links_df),
        links_df["normalized_doi"].nunique() if not links_df.empty else 0,
        out_path,
    )
    return links_df


def build_clinical_trials_payload(paths: ProjectPaths) -> dict[str, Any] | None:
    """Build year-partitioned clinical trials analytics for ``web/data/dashboard_data.json``."""
    links_path = paths.processed_dir / "preprint_trial_links.parquet"
    if not links_path.exists():
        return None

    links = pd.read_parquet(links_path)
    if links.empty:
        return None

    links["first_posted_date"] = pd.to_datetime(links["first_posted_date"], errors="coerce")
    links["year"] = links["first_posted_date"].dt.year

    cache = ClinicalTrialsCache(paths.cache_dir / "clinical_trials.sqlite")
    nct_map = cache.get_nct_trials()
    downstream_map = cache.get_downstream()

    # Attach enriched NCT fields to links dataframe
    links["phase"] = links["trial_id"].map(lambda x: nct_map[x].phase if x in nct_map else None)
    links["overall_status"] = links["trial_id"].map(
        lambda x: nct_map[x].overall_status if x in nct_map else None
    )
    links["sponsor_class"] = links["trial_id"].map(
        lambda x: nct_map[x].sponsor_class if x in nct_map else None
    )
    links["sponsor_name"] = links["trial_id"].map(
        lambda x: nct_map[x].sponsor_name if x in nct_map else None
    )
    links["enrollment_count"] = links["trial_id"].map(
        lambda x: nct_map[x].enrollment_count if x in nct_map else None
    )
    links["has_results"] = links["trial_id"].map(
        lambda x: nct_map[x].has_results if x in nct_map else False
    )
    links["trial_start_date"] = links["trial_id"].map(
        lambda x: nct_map[x].start_date if x in nct_map else None
    )
    links["conditions"] = links["trial_id"].map(
        lambda x: nct_map[x].conditions if x in nct_map else []
    )
    links["trial_brief_title"] = links["trial_id"].map(
        lambda x: nct_map[x].brief_title if x in nct_map else None
    )

    # Save enriched links Parquet for DuckDB / downstream analysis
    enriched_out = paths.processed_dir / "clinical_trials_enriched.parquet"
    links.to_parquet(enriched_out, index=False, compression="zstd")

    # Build year-partitioned additive cubes matching Milestone 1's architecture
    years = sorted(int(y) for y in links["year"].dropna().unique().tolist())
    by_year: dict[str, dict[str, Any]] = {}

    for yr in years:
        yr_str = str(yr)
        by_year[yr_str] = {}
        yr_df = links[links["year"] == yr]
        for srv in ("biorxiv", "medrxiv"):
            sub = yr_df[yr_df["server_clean"] == srv]
            if sub.empty:
                continue

            sub_dois = sub.drop_duplicates("normalized_doi")
            sub_trials = sub.drop_duplicates("trial_id")
            enriched_trials = sub_trials[sub_trials["phase"].notna()]

            # Phase distribution (normalize multi-phase nicely)
            phase_vc = (
                enriched_trials["phase"]
                .fillna("UNKNOWN")
                .map(lambda p: "Not Applicable / Non-Drug" if p == "NA" else p.replace("PHASE", "Phase "))
                .value_counts()
            )
            status_vc = enriched_trials["overall_status"].fillna("UNKNOWN").value_counts()
            sponsor_vc = enriched_trials["sponsor_class"].fillna("UNKNOWN").value_counts()
            reg_vc = sub["registry"].value_counts()
            cat_vc = sub_dois["category_clean"].dropna().map(lambda c: str(c).title()).value_counts()

            # Conditions explosion (top conditions per year-server)
            cond_counts: dict[str, int] = {}
            for cond_list in enriched_trials["conditions"]:
                if isinstance(cond_list, (list, tuple)):
                    for cond in cond_list:
                        c_str = str(cond).strip()
                        if c_str:
                            cond_counts[c_str] = cond_counts.get(c_str, 0) + 1
            top_cond_map = dict(
                sorted(cond_counts.items(), key=lambda kv: kv[1], reverse=True)[:40]
            )

            by_year[yr_str][srv] = {
                "kpi": {
                    "trial_linked_dois": int(len(sub_dois)),
                    "published_trial_dois": int(sub_dois["snapshot_is_published"].sum()),
                    "total_links": int(len(sub)),
                    "unique_trials": int(len(sub_trials)),
                    "enriched_nct_trials": int(len(enriched_trials)),
                    "trials_with_results": int(enriched_trials["has_results"].sum()),
                    "total_enrollment": int(
                        pd.to_numeric(enriched_trials["enrollment_count"], errors="coerce")
                        .fillna(0)
                        .sum()
                    ),
                },
                "registries": {str(k): int(v) for k, v in reg_vc.items()},
                "phases": {str(k): int(v) for k, v in phase_vc.items()},
                "statuses": {str(k): int(v) for k, v in status_vc.items()},
                "sponsor_classes": {str(k): int(v) for k, v in sponsor_vc.items()},
                "categories": {str(k): int(v) for k, v in cat_vc.items()},
                "conditions": top_cond_map,
            }

    # Featured / searchable table of enriched NCT trial-linked preprints (top 200 by enrollment + recency)
    nct_links = links[links["registry"] == "ClinicalTrials.gov"].copy()
    nct_links["enroll_num"] = pd.to_numeric(nct_links["enrollment_count"], errors="coerce").fillna(-1)
    nct_links["is_enriched"] = nct_links["phase"].notna()
    featured_df = (
        nct_links.sort_values(
            ["is_enriched", "has_results", "first_posted_date", "enroll_num"],
            ascending=[False, False, False, False],
        )
        .drop_duplicates(["normalized_doi", "trial_id"])
        .head(200)
    )
    featured_trials = [
        {
            "preprint_doi": str(r.normalized_doi),
            "doi_link": str(r.doi_link) if pd.notna(r.doi_link) else f"https://doi.org/{r.normalized_doi}",
            "title": str(r.title)[:140] if pd.notna(r.title) else "",
            "server": str(r.server_clean),
            "year": int(r.year) if pd.notna(r.year) else None,
            "category": str(r.category_clean).title() if pd.notna(r.category_clean) else "",
            "is_published": bool(r.snapshot_is_published),
            "published_doi": str(r.snapshot_published_doi) if pd.notna(r.snapshot_published_doi) else None,
            "trial_id": str(r.trial_id),
            "phase": (
                "Not Applicable"
                if r.phase == "NA"
                else (str(r.phase).replace("PHASE", "Phase ") if pd.notna(r.phase) else "Unenriched")
            ),
            "status": str(r.overall_status) if pd.notna(r.overall_status) else "Unenriched",
            "sponsor": str(r.sponsor_name)[:60] if pd.notna(r.sponsor_name) else "",
            "sponsor_class": str(r.sponsor_class) if pd.notna(r.sponsor_class) else "",
            "enrollment": int(r.enrollment_count) if pd.notna(r.enrollment_count) else None,
            "has_results": bool(r.has_results),
        }
        for r in featured_df.itertuples(index=False)
    ]

    # Downstream translational impact summary (from NIH iCite cache)
    ds_records = list(downstream_map.values())
    ds_cited = [r for r in ds_records if r.cited_by_clin_count > 0]
    apts = [r.apt for r in ds_records if r.apt is not None]
    top_translational: list[dict[str, Any]] = []
    if ds_cited:
        canon_path = paths.processed_dir / "preprints_canonical.parquet"
        pub_meta: dict[str, dict[str, Any]] = {}
        if canon_path.exists():
            canon_sub = pd.read_parquet(
                canon_path,
                columns=[
                    "normalized_doi",
                    "title",
                    "server_clean",
                    "category_clean",
                    "first_posted_date",
                    "snapshot_published_doi",
                    "publisher_name",
                ],
            )
            canon_sub = canon_sub[canon_sub["snapshot_published_doi"].isin({r.published_doi for r in ds_cited})]
            for row in canon_sub.itertuples(index=False):
                pub_meta[str(row.snapshot_published_doi)] = {
                    "preprint_doi": str(row.normalized_doi),
                    "title": str(row.title)[:140] if pd.notna(row.title) else "",
                    "server": str(row.server_clean),
                    "year": int(pd.to_datetime(row.first_posted_date).year)
                    if pd.notna(row.first_posted_date)
                    else None,
                    "category": str(row.category_clean).title() if pd.notna(row.category_clean) else "",
                    "publisher": str(row.publisher_name) if pd.notna(row.publisher_name) else "",
                }

        for rec in sorted(ds_cited, key=lambda x: (x.cited_by_clin_count, x.apt or 0), reverse=True)[:50]:
            meta = pub_meta.get(rec.published_doi, {})
            top_translational.append(
                {
                    "published_doi": rec.published_doi,
                    "pmid": rec.pmid,
                    "preprint_doi": meta.get("preprint_doi"),
                    "title": meta.get("title", ""),
                    "server": meta.get("server", ""),
                    "year": meta.get("year"),
                    "category": meta.get("category", ""),
                    "cited_by_clin_count": rec.cited_by_clin_count,
                    "apt": rec.apt,
                    "rcr": rec.relative_citation_ratio,
                    "citation_count": rec.citation_count,
                }
            )

    downstream_summary = {
        "evaluated_published_dois": len(ds_records),
        "resolved_pmids": sum(1 for r in ds_records if r.pmid),
        "cited_by_clinical_trials_count": len(ds_cited),
        "cited_by_clinical_trials_pct": round(
            (len(ds_cited) / max(sum(1 for r in ds_records if r.pmid), 1)) * 100.0, 2
        ),
        "median_apt": round(float(pd.Series(apts).median()), 3) if apts else None,
        "high_apt_count_ge_0_5": sum(1 for a in apts if a >= 0.5),
        "top_translational_papers": top_translational,
    }

    # Category-partitioned clinical trials cubes for top-bar Subject Category filtering
    by_category_ct: dict[str, dict[str, dict[str, Any]]] = {}
    links_cat = links.dropna(subset=["category_clean", "year"]).copy()
    links_cat["cat_title"] = links_cat["category_clean"].astype(str).str.title()
    for (cat_title, yr_val, srv_val), sub in links_cat.groupby(
        ["cat_title", "year", "server_clean"], sort=False
    ):
        yr_s = str(int(yr_val))
        srv_s = str(srv_val)
        sub_dois = sub.drop_duplicates("normalized_doi")
        sub_trials = sub.drop_duplicates("trial_id")
        enriched_trials = sub_trials[sub_trials["phase"].notna()]

        phase_vc = (
            enriched_trials["phase"]
            .fillna("UNKNOWN")
            .map(lambda p: "Not Applicable / Non-Drug" if p == "NA" else p.replace("PHASE", "Phase "))
            .value_counts()
        )
        status_vc = enriched_trials["overall_status"].fillna("UNKNOWN").value_counts()
        sponsor_vc = enriched_trials["sponsor_class"].fillna("UNKNOWN").value_counts()
        reg_vc = sub["registry"].value_counts()

        cond_counts: dict[str, int] = {}
        for cond_list in enriched_trials["conditions"]:
            if isinstance(cond_list, (list, tuple)):
                for cond in cond_list:
                    c_str = str(cond).strip()
                    if c_str:
                        cond_counts[c_str] = cond_counts.get(c_str, 0) + 1
        top_cond_map = dict(sorted(cond_counts.items(), key=lambda kv: kv[1], reverse=True)[:15])

        by_category_ct.setdefault(str(cat_title), {}).setdefault(yr_s, {})[srv_s] = {
            "kpi": {
                "trial_linked_dois": int(len(sub_dois)),
                "published_trial_dois": int(sub_dois["snapshot_is_published"].sum()),
                "total_links": int(len(sub)),
                "unique_trials": int(len(sub_trials)),
                "enriched_nct_trials": int(len(enriched_trials)),
                "trials_with_results": int(enriched_trials["has_results"].sum()),
                "total_enrollment": int(
                    pd.to_numeric(enriched_trials["enrollment_count"], errors="coerce")
                    .fillna(0)
                    .sum()
                ),
            },
            "registries": {str(k): int(v) for k, v in reg_vc.items()},
            "phases": {str(k): int(v) for k, v in phase_vc.items()},
            "statuses": {str(k): int(v) for k, v in status_vc.items()},
            "sponsor_classes": {str(k): int(v) for k, v in sponsor_vc.items()},
            "conditions": top_cond_map,
        }

    return {
        "years": years,
        "cache_stats": cache.stats(),
        "by_year": by_year,
        "by_category": by_category_ct,
        "featured_trials": featured_trials,
        "downstream_icite": downstream_summary,
    }
