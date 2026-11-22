"""Peer-reviewed journal publication verification for bioRxiv & medRxiv preprints.

Architecture:
- **SQLite persistent cache** (``data/cache/publication_status.sqlite``) so runs
  are resumable, incremental, and avoid redundant API traffic. Papers already
  confirmed as published are never re-queried; unpublished papers are re-checked
  only after a configurable TTL (default: 30 days).
- **Tier 1 (Bulk & Single-DOI): bioRxiv / medRxiv Official ``pubs`` API**
  (``https://api.biorxiv.org/pubs/{server}/...``). Bulk date-window harvesting
  fetches 100 published-preprint records per HTTP request, making it practical
  to verify large datasets without hammering upstream endpoints.
- **Tier 2 (Fallback): Crossref REST API** (``relation.is-preprint-of``)
  to catch peer-reviewed links not yet indexed in bioRxiv's ``pubs`` feed.
- **Tier 3 (Batch Fallback): OpenAlex Works API** (50 DOIs per request)
  for additional journal-version discovery and venue metadata.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests

logger = logging.getLogger(__name__)

BIORXIV_PUBS_BASE = "https://api.biorxiv.org/pubs"
CROSSREF_WORKS_BASE = "https://api.crossref.org/works"
OPENALEX_WORKS_BASE = "https://api.openalex.org/works"

DOI_CLEAN_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def normalize_doi(raw_doi: Any) -> str | None:
    """Normalize a raw DOI string to lowercase canonical form (e.g. '10.1101/...')."""
    if raw_doi is None or (isinstance(raw_doi, float) and pd.isna(raw_doi)):
        return None
    text = str(raw_doi).strip()
    if not text or text.lower() in {"na", "n/a", "none", "nan", "null"}:
        return None
    cleaned = DOI_CLEAN_RE.sub("", text).strip().lower()
    # Strip bioRxiv version suffixes such as 'v1', 'v2' if accidentally appended
    cleaned = re.sub(r"v\d+$", "", cleaned)
    return cleaned if "/" in cleaned else None


@dataclass
class PublicationRecord:
    """Peer-reviewed publication status for a single preprint DOI."""

    preprint_doi: str
    is_published: bool
    published_doi: str | None = None
    published_journal: str | None = None
    published_date: str | None = None
    preprint_date: str | None = None
    server: str | None = None
    verification_source: str = "unknown"
    checked_at_utc: str = ""

    def __post_init__(self) -> None:
        if not self.checked_at_utc:
            self.checked_at_utc = datetime.now(timezone.utc).isoformat()


class PublicationCache:
    """SQLite-backed cache for preprint -> peer-reviewed publication lookups."""

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
                CREATE TABLE IF NOT EXISTS publication_status (
                    preprint_doi TEXT PRIMARY KEY,
                    is_published INTEGER NOT NULL,
                    published_doi TEXT,
                    published_journal TEXT,
                    published_date TEXT,
                    preprint_date TEXT,
                    server TEXT,
                    verification_source TEXT NOT NULL,
                    checked_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bulk_harvest_log (
                    server TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    records_ingested INTEGER NOT NULL,
                    harvested_at_utc TEXT NOT NULL,
                    PRIMARY KEY (server, start_date, end_date)
                )
                """
            )
            conn.commit()

    def upsert_many(self, records: Iterable[PublicationRecord]) -> int:
        rows = [
            (
                r.preprint_doi,
                int(r.is_published),
                r.published_doi,
                r.published_journal,
                r.published_date,
                r.preprint_date,
                r.server,
                r.verification_source,
                r.checked_at_utc,
            )
            for r in records
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO publication_status (
                    preprint_doi, is_published, published_doi, published_journal,
                    published_date, preprint_date, server, verification_source, checked_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(preprint_doi) DO UPDATE SET
                    is_published = CASE
                        WHEN excluded.is_published = 1 THEN 1
                        ELSE publication_status.is_published
                    END,
                    published_doi = COALESCE(excluded.published_doi, publication_status.published_doi),
                    published_journal = COALESCE(excluded.published_journal, publication_status.published_journal),
                    published_date = COALESCE(excluded.published_date, publication_status.published_date),
                    preprint_date = COALESCE(excluded.preprint_date, publication_status.preprint_date),
                    server = COALESCE(excluded.server, publication_status.server),
                    verification_source = CASE
                        WHEN excluded.is_published = 1 OR publication_status.is_published = 0
                        THEN excluded.verification_source
                        ELSE publication_status.verification_source
                    END,
                    checked_at_utc = excluded.checked_at_utc
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def get_many(self, dois: Iterable[str]) -> dict[str, PublicationRecord]:
        doi_list = list({d for d in dois if d})
        if not doi_list:
            return {}
        results: dict[str, PublicationRecord] = {}
        chunk_size = 900  # stay safely under SQLite variable limit
        with self._connect() as conn:
            for start in range(0, len(doi_list), chunk_size):
                chunk = doi_list[start : start + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"SELECT * FROM publication_status WHERE preprint_doi IN ({placeholders})",
                    chunk,
                )
                for row in cursor.fetchall():
                    results[row["preprint_doi"]] = PublicationRecord(
                        preprint_doi=row["preprint_doi"],
                        is_published=bool(row["is_published"]),
                        published_doi=row["published_doi"],
                        published_journal=row["published_journal"],
                        published_date=row["published_date"],
                        preprint_date=row["preprint_date"],
                        server=row["server"],
                        verification_source=row["verification_source"],
                        checked_at_utc=row["checked_at_utc"],
                    )
        return results

    def record_bulk_harvest(
        self, server: str, start_date: str, end_date: str, records_ingested: int
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO bulk_harvest_log
                    (server, start_date, end_date, records_ingested, harvested_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (server, start_date, end_date, records_ingested, now_iso),
            )
            conn.commit()

    def count_cached(self) -> dict[str, int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM publication_status").fetchone()[0]
            published = conn.execute(
                "SELECT COUNT(*) FROM publication_status WHERE is_published = 1"
            ).fetchone()[0]
        return {"total_cached": total, "published_cached": published}


class PoliteHttpClient:
    """HTTP client with rate limiting, retries, and exponential backoff."""

    def __init__(
        self,
        *,
        mailto: str | None = None,
        min_delay_seconds: float = 0.2,
        timeout_seconds: float = 30.0,
        max_retries: int = 4,
    ) -> None:
        self.min_delay_seconds = min_delay_seconds
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._last_request_ts = 0.0
        self.session = requests.Session()
        ua = "BioMedRxivAnalyzer/0.1"
        if mailto:
            ua = f"{ua} (mailto:{mailto})"
        self.session.headers.update({"User-Agent": ua, "Accept": "application/json"})

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Fetch JSON with polite throttling and exponential backoff on transient failures."""
        for attempt in range(self.max_retries):
            elapsed = time.monotonic() - self._last_request_ts
            if elapsed < self.min_delay_seconds:
                time.sleep(self.min_delay_seconds - elapsed)
            self._last_request_ts = time.monotonic()

            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_seconds)
            except requests.RequestException as exc:
                wait = (2**attempt) * 0.5
                logger.warning("Request error (%s) for %s; retrying in %.1fs", exc, url, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
            if resp.status_code in {429, 500, 502, 503, 504}:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else (2**attempt)
                logger.warning(
                    "HTTP %d for %s; backing off %.1fs (attempt %d/%d)",
                    resp.status_code,
                    url,
                    wait,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(wait)
                continue

            logger.warning("Unexpected HTTP %d for %s", resp.status_code, url)
            return None

        logger.error("Exhausted retries for %s", url)
        return None


class PublicationVerifier:
    """Verifies whether preprints have been published in peer-reviewed journals."""

    def __init__(
        self,
        cache: PublicationCache,
        http_client: PoliteHttpClient | None = None,
        mailto: str | None = None,
    ) -> None:
        self.cache = cache
        self.mailto = mailto
        self.http = http_client or PoliteHttpClient(mailto=mailto)

    # ------------------------------------------------------------------
    # Tier 1: bioRxiv / medRxiv Official Published Papers API
    # ------------------------------------------------------------------
    def harvest_biorxiv_pubs_window(
        self,
        server: str,
        start_date: str,
        end_date: str,
        *,
        max_pages: int | None = None,
    ) -> int:
        """Bulk-download published preprint mappings from bioRxiv/medRxiv ``pubs`` API.

        Each page returns up to 100 published records. Results are stored directly
        into the local SQLite cache so subsequent dataset joins are instant.
        """
        if server not in {"biorxiv", "medrxiv"}:
            raise ValueError(f"Unsupported server '{server}'. Must be 'biorxiv' or 'medrxiv'.")

        cursor = 0
        pages_fetched = 0
        total_ingested = 0

        while True:
            if max_pages is not None and pages_fetched >= max_pages:
                break

            url = f"{BIORXIV_PUBS_BASE}/{server}/{start_date}/{end_date}/{cursor}"
            payload = self.http.get_json(url)
            if not payload:
                break

            collection = payload.get("collection") or []
            if not collection:
                break

            batch: list[PublicationRecord] = []
            for item in collection:
                preprint_doi = normalize_doi(item.get("biorxiv_doi") or item.get("preprint_doi"))
                published_doi = normalize_doi(item.get("published_doi"))
                if not preprint_doi:
                    continue
                batch.append(
                    PublicationRecord(
                        preprint_doi=preprint_doi,
                        is_published=bool(published_doi),
                        published_doi=published_doi,
                        published_journal=(item.get("published_journal") or None),
                        published_date=(item.get("published_date") or None),
                        preprint_date=(item.get("preprint_date") or None),
                        server=server,
                        verification_source=f"{server}_pubs_api",
                    )
                )

            total_ingested += self.cache.upsert_many(batch)
            pages_fetched += 1

            messages = payload.get("messages") or [{}]
            msg = messages[0] if isinstance(messages, list) and messages else {}
            total_reported = int(msg.get("total", 0) or 0)
            count = int(msg.get("count", len(collection)) or len(collection))
            cursor += count

            if count == 0 or (total_reported and cursor >= total_reported):
                break

        self.cache.record_bulk_harvest(server, start_date, end_date, total_ingested)
        logger.info(
            "Harvested %d published records from %s (%s -> %s).",
            total_ingested,
            server,
            start_date,
            end_date,
        )
        return total_ingested

    def lookup_biorxiv_doi(self, doi: str, server: str = "biorxiv") -> PublicationRecord | None:
        """Query the bioRxiv/medRxiv ``pubs`` API for a single preprint DOI."""
        norm = normalize_doi(doi)
        if not norm:
            return None
        url = f"{BIORXIV_PUBS_BASE}/{server}/{norm}"
        payload = self.http.get_json(url)
        if not payload:
            return None

        collection = payload.get("collection") or []
        if not collection:
            return PublicationRecord(
                preprint_doi=norm,
                is_published=False,
                server=server,
                verification_source=f"{server}_pubs_api",
            )

        item = collection[0]
        pub_doi = normalize_doi(item.get("published_doi"))
        return PublicationRecord(
            preprint_doi=norm,
            is_published=bool(pub_doi),
            published_doi=pub_doi,
            published_journal=(item.get("published_journal") or None),
            published_date=(item.get("published_date") or None),
            preprint_date=(item.get("preprint_date") or None),
            server=server,
            verification_source=f"{server}_pubs_api",
        )

    # ------------------------------------------------------------------
    # Tier 2: Crossref REST API (relation.is-preprint-of)
    # ------------------------------------------------------------------
    def lookup_crossref_doi(self, doi: str) -> PublicationRecord | None:
        """Inspect Crossref metadata for ``relation.is-preprint-of`` journal links."""
        norm = normalize_doi(doi)
        if not norm:
            return None

        params = {"mailto": self.mailto} if self.mailto else None
        payload = self.http.get_json(f"{CROSSREF_WORKS_BASE}/{quote(norm, safe='')}", params=params)
        if not payload or "message" not in payload:
            return None

        msg = payload["message"]
        relations = msg.get("relation") or {}
        preprint_links = relations.get("is-preprint-of") or []
        pub_doi: str | None = None
        for rel in preprint_links:
            if rel.get("id-type") == "doi" and rel.get("id"):
                pub_doi = normalize_doi(rel["id"])
                if pub_doi:
                    break

        if not pub_doi:
            return PublicationRecord(
                preprint_doi=norm,
                is_published=False,
                verification_source="crossref_api",
            )

        # Resolve journal title & date for the published DOI
        pub_journal: str | None = None
        pub_date: str | None = None
        pub_payload = self.http.get_json(
            f"{CROSSREF_WORKS_BASE}/{quote(pub_doi, safe='')}", params=params
        )
        if pub_payload and "message" in pub_payload:
            pub_msg = pub_payload["message"]
            containers = pub_msg.get("container-title") or []
            pub_journal = containers[0] if containers else None
            for date_key in ("published-print", "published-online", "issued", "created"):
                parts = (pub_msg.get(date_key) or {}).get("date-parts")
                if parts and parts[0]:
                    padded = [str(p).zfill(2) for p in parts[0][:3]]
                    pub_date = "-".join(padded)
                    break

        return PublicationRecord(
            preprint_doi=norm,
            is_published=True,
            published_doi=pub_doi,
            published_journal=pub_journal,
            published_date=pub_date,
            verification_source="crossref_api",
        )

    # ------------------------------------------------------------------
    # Tier 3: OpenAlex Batch API (50 DOIs per call)
    # ------------------------------------------------------------------
    def lookup_openalex_batch(self, dois: list[str]) -> dict[str, PublicationRecord]:
        """Batch-check up to 50 preprint DOIs per request against OpenAlex."""
        normalized = [d for d in (normalize_doi(x) for x in dois) if d]
        if not normalized:
            return {}

        results: dict[str, PublicationRecord] = {}
        batch_size = 50
        for start in range(0, len(normalized), batch_size):
            chunk = normalized[start : start + batch_size]
            doi_filter = "|".join(f"https://doi.org/{d}" for d in chunk)
            params: dict[str, Any] = {
                "filter": f"doi:{doi_filter}",
                "per-page": len(chunk),
                "select": "doi,primary_location,locations,publication_date,type",
            }
            if self.mailto:
                params["mailto"] = self.mailto

            payload = self.http.get_json(OPENALEX_WORKS_BASE, params=params)
            if not payload:
                continue

            for work in payload.get("results") or []:
                work_doi = normalize_doi(work.get("doi"))
                if not work_doi:
                    continue

                pub_journal: str | None = None
                pub_doi: str | None = None
                pub_date: str | None = work.get("publication_date")

                # Check if any location is a peer-reviewed journal
                for loc in work.get("locations") or []:
                    source = loc.get("source") or {}
                    if source.get("type") == "journal" and source.get("display_name"):
                        pub_journal = source["display_name"]
                        loc_doi = normalize_doi(loc.get("landing_page_url"))
                        pub_doi = loc_doi or work_doi
                        break

                if pub_journal:
                    results[work_doi] = PublicationRecord(
                        preprint_doi=work_doi,
                        is_published=True,
                        published_doi=pub_doi,
                        published_journal=pub_journal,
                        published_date=pub_date,
                        verification_source="openalex_api",
                    )

        return results

    # ------------------------------------------------------------------
    # Orchestrator: verify a list of DOIs using cache + tiered providers
    # ------------------------------------------------------------------
    def verify_dois(
        self,
        dois: Iterable[str],
        *,
        server_map: dict[str, str] | None = None,
        recheck_days: int = 30,
        use_biorxiv_api: bool = True,
        use_crossref_fallback: bool = True,
        use_openalex_fallback: bool = False,
        max_live_queries: int | None = None,
    ) -> dict[str, PublicationRecord]:
        """Resolve publication status for ``dois``, leveraging SQLite cache first."""
        norm_dois = list(dict.fromkeys(d for d in (normalize_doi(x) for x in dois) if d))
        cached = self.cache.get_many(norm_dois)
        cutoff = datetime.now(timezone.utc) - timedelta(days=recheck_days)

        to_query: list[str] = []
        for d in norm_dois:
            rec = cached.get(d)
            if rec is None:
                to_query.append(d)
            elif not rec.is_published:
                try:
                    checked_dt = datetime.fromisoformat(rec.checked_at_utc)
                    if checked_dt.tzinfo is None:
                        checked_dt = checked_dt.replace(tzinfo=timezone.utc)
                    if checked_dt < cutoff:
                        to_query.append(d)
                except ValueError:
                    to_query.append(d)

        if max_live_queries is not None and len(to_query) > max_live_queries:
            logger.info(
                "Limiting live API queries to %d of %d uncached DOIs.",
                max_live_queries,
                len(to_query),
            )
            to_query = to_query[:max_live_queries]

        logger.info(
            "DOIs requested: %d | Cache hits: %d | Live queries scheduled: %d",
            len(norm_dois),
            len(norm_dois) - len(to_query),
            len(to_query),
        )

        # Tier 1: bioRxiv / medRxiv single-DOI lookup
        unresolved: list[str] = []
        if use_biorxiv_api and to_query:
            tier1_updates: list[PublicationRecord] = []
            for d in to_query:
                srv = (server_map or {}).get(d, "biorxiv")
                srv = srv if srv in {"biorxiv", "medrxiv"} else "biorxiv"
                rec = self.lookup_biorxiv_doi(d, server=srv)
                if rec and rec.is_published:
                    cached[d] = rec
                    tier1_updates.append(rec)
                else:
                    # Try alternate server if server was unknown
                    alt_srv = "medrxiv" if srv == "biorxiv" else "biorxiv"
                    if not server_map or d not in server_map:
                        alt_rec = self.lookup_biorxiv_doi(d, server=alt_srv)
                        if alt_rec and alt_rec.is_published:
                            cached[d] = alt_rec
                            tier1_updates.append(alt_rec)
                            continue
                    unresolved.append(d)
                    if rec:
                        cached[d] = rec
                        tier1_updates.append(rec)
            self.cache.upsert_many(tier1_updates)
        else:
            unresolved = list(to_query)

        # Tier 2: Crossref is-preprint-of fallback
        still_unresolved: list[str] = []
        if use_crossref_fallback and unresolved:
            tier2_updates: list[PublicationRecord] = []
            for d in unresolved:
                rec = self.lookup_crossref_doi(d)
                if rec:
                    cached[d] = rec
                    tier2_updates.append(rec)
                    if not rec.is_published:
                        still_unresolved.append(d)
                else:
                    still_unresolved.append(d)
            self.cache.upsert_many(tier2_updates)
        else:
            still_unresolved = unresolved

        # Tier 3: OpenAlex batch fallback
        if use_openalex_fallback and still_unresolved:
            oa_results = self.lookup_openalex_batch(still_unresolved)
            if oa_results:
                self.cache.upsert_many(oa_results.values())
                cached.update(oa_results)

        return {d: cached[d] for d in norm_dois if d in cached}


def enrich_dataframe_with_publications(
    df: pd.DataFrame,
    records: dict[str, PublicationRecord],
    *,
    doi_column: str,
    preprint_date_column: str | None = None,
) -> pd.DataFrame:
    """Attach peer-reviewed publication columns and time-to-publication metrics."""
    out = df.copy()
    norm_series = out[doi_column].map(normalize_doi)

    out["normalized_doi"] = norm_series
    out["is_published"] = norm_series.map(
        lambda d: records[d].is_published if d in records else pd.NA
    )
    out["published_doi"] = norm_series.map(
        lambda d: records[d].published_doi if d in records else None
    )
    out["published_journal"] = norm_series.map(
        lambda d: records[d].published_journal if d in records else None
    )
    out["published_date"] = norm_series.map(
        lambda d: records[d].published_date if d in records else None
    )
    out["verification_source"] = norm_series.map(
        lambda d: records[d].verification_source if d in records else None
    )
    out["verified_at_utc"] = norm_series.map(
        lambda d: records[d].checked_at_utc if d in records else None
    )

    # Compute days from preprint posting to peer-reviewed journal publication
    pub_dt = pd.to_datetime(out["published_date"], errors="coerce")
    if preprint_date_column and preprint_date_column in out.columns:
        pre_dt = pd.to_datetime(out[preprint_date_column], errors="coerce")
    else:
        pre_dt = pd.to_datetime(
            norm_series.map(lambda d: records[d].preprint_date if d in records else None),
            errors="coerce",
        )
    out["days_to_publication"] = (pub_dt - pre_dt).dt.days
    return out
