"""Direct incremental updater from the official bioRxiv & medRxiv REST APIs.

Allows the local dataset to stay continuously up to date (ahead of Kaggle) while
preserving immutable, SHA-256-verified historical snapshots in ``data/versions/``
and ``data/registry.json``.

API reference: https://api.biorxiv.org/
- Details endpoint: ``https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}``
- Pubs endpoint:    ``https://api.biorxiv.org/pubs/{server}/{start}/{end}/{cursor}``
"""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from biomedrxiv.config import ProjectPaths
from biomedrxiv.preprocessing import DEFAULT_CSV_NAME
from biomedrxiv.publication_checker import (
    PoliteHttpClient,
    PublicationCache,
    PublicationVerifier,
    normalize_doi,
)
from biomedrxiv.versioning import DatasetVersionManager, UpdateResult

logger = logging.getLogger(__name__)

BIORXIV_DETAILS_BASE = "https://api.biorxiv.org/details"

CSV_COLUMNS = [
    "title",
    "authors",
    "author_corresponding",
    "author_corresponding_institution",
    "doi",
    "date",
    "version",
    "type",
    "license",
    "category",
    "jatsxml",
    "abstract",
    "published",
    "server",
    "doi_link",
]


@dataclass
class IncrementalSyncSummary:
    """Structured outcome of a direct bioRxiv/medRxiv incremental API sync."""

    start_date: str
    end_date: str
    fetched_records: int
    new_or_updated_records: int
    pubs_harvested: int
    update_result: UpdateResult | None


def detect_latest_dataset_date(paths: ProjectPaths) -> str:
    """Find the maximum posting date (YYYY-MM-DD) in the active dataset snapshot."""
    canonical_pq = paths.processed_dir / "preprints_canonical.parquet"
    if canonical_pq.exists():
        dates = pd.to_datetime(
            pd.read_parquet(canonical_pq, columns=["latest_posted_date"])["latest_posted_date"],
            errors="coerce",
        )
        max_dt = dates.max()
        if pd.notna(max_dt):
            return str(max_dt.date())

    active_csv = paths.current_link / DEFAULT_CSV_NAME
    if not active_csv.exists():
        raise FileNotFoundError(
            f"Active dataset CSV not found at {active_csv}. Run scripts/download_dataset.py sync first."
        )

    max_str = ""
    for chunk in pd.read_csv(active_csv, usecols=["date"], chunksize=100_000, low_memory=False):
        chunk_max = chunk["date"].dropna().astype(str).max()
        if isinstance(chunk_max, str) and chunk_max > max_str:
            max_str = chunk_max
    if not max_str:
        raise ValueError(f"Could not determine max posting date from {active_csv}")
    return max_str[:10]


def normalize_api_record_to_csv_row(item: dict[str, Any], server: str) -> dict[str, Any] | None:
    """Map a bioRxiv/medRxiv ``details`` API JSON object to the 15-column CSV schema."""
    raw_doi = str(item.get("doi") or "").strip()
    if not raw_doi:
        return None

    pub_raw = str(item.get("published") or "").strip()
    published = "" if not pub_raw or pub_raw.upper() == "NA" else pub_raw

    ver_raw = item.get("version", 1)
    try:
        version_int = int(ver_raw)
    except (TypeError, ValueError):
        version_int = 1

    canonical_server = "bioRxiv" if server.lower() == "biorxiv" else "medRxiv"
    return {
        "title": str(item.get("title") or "").strip(),
        "authors": str(item.get("authors") or "").strip(),
        "author_corresponding": str(item.get("author_corresponding") or "").strip(),
        "author_corresponding_institution": str(
            item.get("author_corresponding_institution") or ""
        ).strip(),
        "doi": raw_doi,
        "date": str(item.get("date") or "").strip()[:10],
        "version": version_int,
        "type": str(item.get("type") or "").strip(),
        "license": str(item.get("license") or "").strip(),
        "category": str(item.get("category") or "").strip(),
        "jatsxml": str(item.get("jatsxml") or "").strip(),
        "abstract": str(item.get("abstract") or "").strip(),
        "published": published,
        "server": canonical_server,
        "doi_link": f"https://doi.org/{raw_doi}",
    }


def fetch_details_window(
    http: PoliteHttpClient,
    *,
    server: str,
    start_date: str,
    end_date: str,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch all preprint version postings for ``server`` in ``[start_date, end_date]``."""
    if server not in {"biorxiv", "medrxiv"}:
        raise ValueError(f"Unsupported server '{server}'. Must be 'biorxiv' or 'medrxiv'.")

    cursor = 0
    pages = 0
    rows: list[dict[str, Any]] = []

    while True:
        if max_pages is not None and pages >= max_pages:
            break

        url = f"{BIORXIV_DETAILS_BASE}/{server}/{start_date}/{end_date}/{cursor}"
        payload = http.get_json(url)
        if not payload:
            break

        collection = payload.get("collection") or []
        if not collection:
            break

        for item in collection:
            mapped = normalize_api_record_to_csv_row(item, server=server)
            if mapped is not None:
                rows.append(mapped)

        pages += 1
        messages = payload.get("messages") or [{}]
        msg = messages[0] if isinstance(messages, list) and messages else {}
        total_reported = int(msg.get("total", 0) or 0)
        count = int(msg.get("count", len(collection)) or len(collection))
        cursor += count

        if pages % 10 == 0 or (total_reported and cursor >= total_reported):
            logger.info(
                "[%s details %s..%s] Fetched %d / %s records (page %d)...",
                server,
                start_date,
                end_date,
                len(rows),
                total_reported or "?",
                pages,
            )

        if count == 0 or (total_reported and cursor >= total_reported):
            break

    return rows


def _row_key(doi: Any, version: Any) -> tuple[str, int] | None:
    norm = normalize_doi(doi)
    if not norm:
        return None
    try:
        ver = int(float(version))
    except (TypeError, ValueError):
        ver = 1
    return (norm, ver)


def merge_incremental_into_snapshot(
    paths: ProjectPaths,
    delta_rows: list[dict[str, Any]],
    *,
    published_updates: dict[str, str] | None = None,
    notes: str = "",
    chunksize: int = 100_000,
) -> tuple[int, UpdateResult | None]:
    """Stream-merge ``delta_rows`` (and optional new ``published`` DOIs) into a new versioned snapshot.

    Deduplicates strictly on ``(normalized_doi, version)`` and ingests via
    ``DatasetVersionManager`` so ``data/versions/``, ``data/current``, and
    ``data/registry.json`` track the new version with full SHA-256 provenance.
    """
    active_csv = paths.current_link / DEFAULT_CSV_NAME
    if not active_csv.exists():
        raise FileNotFoundError(f"Active CSV not found at {active_csv}.")

    # Deduplicate delta rows by (normalized_doi, version), keeping last occurrence
    delta_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for row in delta_rows:
        key = _row_key(row.get("doi"), row.get("version"))
        if key is not None:
            delta_by_key[key] = row

    pub_map = {
        k: v
        for k, v in (published_updates or {}).items()
        if k and v
    }

    if not delta_by_key and not pub_map:
        logger.info("No delta records or publication updates to merge.")
        return 0, None

    staging_parent = Path(tempfile.mkdtemp(prefix="biomedrxiv_merge_"))
    merged_csv = staging_parent / DEFAULT_CSV_NAME
    delta_log = staging_parent / "incremental_delta.jsonl"

    existing_keys_in_delta: set[tuple[str, int]] = set()
    updated_pub_count = 0

    try:
        with merged_csv.open("w", encoding="utf-8", newline="") as out_fh:
            writer = csv.DictWriter(out_fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()

            for chunk in pd.read_csv(
                active_csv,
                chunksize=chunksize,
                dtype=str,
                keep_default_na=False,
            ):
                # Align columns defensively
                for col in CSV_COLUMNS:
                    if col not in chunk.columns:
                        chunk[col] = ""

                records_out: list[dict[str, Any]] = []
                for row in chunk[CSV_COLUMNS].to_dict(orient="records"):
                    key = _row_key(row["doi"], row["version"])
                    if key is not None and key in delta_by_key:
                        existing_keys_in_delta.add(key)
                        # Replace with latest metadata from API
                        replacement = dict(delta_by_key[key])
                        if not replacement["published"] and row["published"]:
                            replacement["published"] = row["published"]
                        if not replacement["published"] and key[0] in pub_map:
                            replacement["published"] = pub_map[key[0]]
                            updated_pub_count += 1
                        records_out.append(replacement)
                        continue

                    # Apply freshly harvested published DOI to older preprints if missing
                    if key is not None and not str(row["published"]).strip() and key[0] in pub_map:
                        row["published"] = pub_map[key[0]]
                        updated_pub_count += 1

                    records_out.append(row)

                writer.writerows(records_out)

            # Append strictly brand-new (doi, version) rows
            new_rows = []
            for key, row in delta_by_key.items():
                if key not in existing_keys_in_delta:
                    enriched_new = dict(row)
                    if not enriched_new["published"] and key[0] in pub_map:
                        enriched_new["published"] = pub_map[key[0]]
                    new_rows.append(enriched_new)
            # Sort newly appended rows chronologically
            new_rows.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("doi") or "")))
            writer.writerows(new_rows)

        total_changes = len(new_rows) + updated_pub_count
        if total_changes == 0 and not existing_keys_in_delta:
            return 0, None

        # Save lightweight JSONL audit of newly added rows inside the snapshot
        with delta_log.open("w", encoding="utf-8") as dfh:
            for r in new_rows:
                dfh.write(json.dumps(r) + "\n")

        logger.info(
            "Merged incremental update: %d brand-new (doi, version) rows, %d refreshed boundary rows, %d newly linked published DOIs.",
            len(new_rows),
            len(existing_keys_in_delta),
            updated_pub_count,
        )

        manager = DatasetVersionManager(paths=paths)
        result = manager.ingest_directory(
            staging_parent,
            notes=notes
            or f"Direct bioRxiv/medRxiv API incremental sync (+{len(new_rows)} rows, +{updated_pub_count} pubs)",
        )
        return total_changes, result
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


def sync_incremental_from_biorxiv_api(
    paths: ProjectPaths,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    overlap_days: int = 2,
    servers: tuple[str, ...] = ("biorxiv", "medrxiv"),
    also_harvest_pubs: bool = True,
    delay_seconds: float = 0.25,
    max_pages_per_server: int | None = None,
    http_client: PoliteHttpClient | None = None,
) -> IncrementalSyncSummary:
    """Fetch new preprints (+ recent publications) directly from bioRxiv/medRxiv APIs and snapshot."""
    paths.ensure_directories()
    if end_date is None:
        end_date = date.today().isoformat()

    if start_date is None:
        latest = detect_latest_dataset_date(paths)
        latest_dt = datetime.strptime(latest, "%Y-%m-%d").date()
        start_dt = latest_dt - timedelta(days=max(0, overlap_days))
        start_date = start_dt.isoformat()

    if start_date > end_date:
        start_date = end_date

    http = http_client or PoliteHttpClient(min_delay_seconds=delay_seconds)
    logger.info(
        "Starting direct bioRxiv/medRxiv incremental sync for window %s -> %s...",
        start_date,
        end_date,
    )

    fetched_rows: list[dict[str, Any]] = []
    for srv in servers:
        srv_rows = fetch_details_window(
            http,
            server=srv,
            start_date=start_date,
            end_date=end_date,
            max_pages=max_pages_per_server,
        )
        fetched_rows.extend(srv_rows)

    pubs_harvested = 0
    published_updates: dict[str, str] = {}
    if also_harvest_pubs:
        cache = PublicationCache(paths.cache_dir / "publication_status.sqlite")
        verifier = PublicationVerifier(cache=cache, http_client=http)
        for srv in servers:
            pubs_harvested += verifier.harvest_biorxiv_pubs_window(
                server=srv,
                start_date=start_date,
                end_date=end_date,
                max_pages=max_pages_per_server,
            )
        # Load newly harvested published DOI mappings so older rows in the CSV get updated too
        with cache._connect() as conn:
            for row in conn.execute(
                "SELECT preprint_doi, published_doi FROM publication_status "
                "WHERE is_published = 1 AND published_doi IS NOT NULL"
            ).fetchall():
                published_updates[row["preprint_doi"]] = row["published_doi"]

    changes, update_result = merge_incremental_into_snapshot(
        paths,
        fetched_rows,
        published_updates=published_updates,
        notes=f"Direct bioRxiv/medRxiv API sync ({start_date}..{end_date})",
    )

    return IncrementalSyncSummary(
        start_date=start_date,
        end_date=end_date,
        fetched_records=len(fetched_rows),
        new_or_updated_records=changes,
        pubs_harvested=pubs_harvested,
        update_result=update_result,
    )
