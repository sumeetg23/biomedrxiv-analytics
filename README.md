# BioRxiv & MedRxiv Preprint Dataset Toolkit

A modular, version-controlled data engineering and analysis workspace for the Kaggle dataset [`m0delcrafter/bio-and-med-dataset`](https://www.kaggle.com/datasets/m0delcrafter/bio-and-med-dataset) (605,767 version rows / 449,506 unique bioRxiv & medRxiv preprints spanning Nov 2013 – Oct 2026).

**[View Interactive Web Dashboard (GitHub Pages)](https://sumeetg23.github.io/biomedrxiv-analytics/)**

## Project Structure

```text
Bio&Med-RxivDataset/
├── data/
│   ├── versions/                  # Immutable, SHA-256-verified dataset snapshots (gitignored)
│   │   └── vYYYYMMDD_HHMMSS_<hash>/
│   │       ├── biorxiv_medrxiv_preprints.csv
│   │       ├── biorxiv_pages.jsonl
│   │       ├── medrxiv_pages.jsonl
│   │       └── manifest.json      # Per-file checksums, sizes, row counts, diff
│   ├── current -> versions/...    # Atomic symlink to active dataset version
│   ├── processed/                 # Materialized zstd Parquet tables (gitignored)
│   │   ├── preprints_versions.parquet
│   │   └── preprints_canonical.parquet
│   ├── cache/                     # SQLite cache for peer-reviewed publication checks
│   └── registry.json              # Local version history & update check audit log
├── docs/                          # GitHub Pages root
│   ├── index.html                 # Interactive Plotly.js web dashboard
│   ├── data/dashboard_data.json   # Compact pre-aggregated analytics JSON
│   └── analysis_plan.md           # Full analysis plan, research questions & viz blueprint
├── scripts/
│   ├── download_dataset.py        # CLI: sync, list, diff, and switch dataset versions
│   ├── update_dataset.py          # Automated updater + macOS launchd / cron setup
│   ├── check_publications.py      # Multi-API peer-reviewed journal verification CLI
│   └── build_report.py            # Materialize Parquet tables & build web dashboard
├── src/biomedrxiv/
│   ├── config.py                  # Canonical project path configuration
│   ├── versioning.py              # Kaggle download, hashing, snapshotting & diffing
│   ├── preprocessing.py           # Data cleaning & DOI-level deduplication
│   ├── publication_checker.py     # bioRxiv/medRxiv pubs + Crossref + OpenAlex + SQLite cache
│   ├── reporting.py               # Chunked Parquet materialization & analytics aggregations
│   ├── dashboard_template.py      # Static interactive HTML dashboard renderer
│   └── scheduler.py               # macOS LaunchAgent & cron generator
├── tests/                         # Unit tests (pytest)
├── .github/workflows/
│   └── update-dataset.yml         # Optional weekly GitHub Actions update check
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest
```

---

## 1. Downloading & Managing Dataset Versions

Every snapshot is stored immutably in `data/versions/<version_id>/` with a `manifest.json`, and `data/current` points atomically to the active version. If upstream Kaggle content has not changed (identical SHA-256 digest), no duplicate snapshot is created — only a check timestamp is appended to `data/registry.json`.

```bash
# Download latest from Kaggle (creates a new version only if content changed)
.venv/bin/python scripts/download_dataset.py sync --notes "Weekly refresh"

# List all stored versions and row counts
.venv/bin/python scripts/download_dataset.py list

# Compare two versions (added/removed files, row deltas, schema changes)
.venv/bin/python scripts/download_dataset.py diff <from_version_id> <to_version_id>

# Roll back or pin data/current to an older historical version
.venv/bin/python scripts/download_dataset.py switch <version_id>
```

---

## 2. Automatic Dataset Updates

### Option A: macOS Native `launchd` (Recommended on macOS)
Unlike `cron`, macOS `launchd` handles sleeping laptops gracefully and logs directly into `logs/`.

```bash
# Preview/write the plist locally without loading it
.venv/bin/python scripts/update_dataset.py \
  --write-launchd-plist ./config/com.biomedrxiv.dataset.updater.plist \
  --weekday 0 --hour 3 --minute 0

# Install and activate the weekly LaunchAgent (Sundays at 03:00 local time)
.venv/bin/python scripts/update_dataset.py --install-launchd --weekday 0 --hour 3 --minute 0

# Or daily at 03:00 (--weekday -1)
.venv/bin/python scripts/update_dataset.py --install-launchd --weekday -1 --hour 3 --minute 0

# Uninstall later if desired
.venv/bin/python scripts/update_dataset.py --uninstall-launchd
```

### Option B: `cron`
```bash
# Generate a ready-to-paste crontab entry
.venv/bin/python scripts/update_dataset.py --print-cron --weekday 0 --hour 3 --minute 0
```

### Option C: GitHub Actions
See [`.github/workflows/update-dataset.yml`](.github/workflows/update-dataset.yml) for a weekly scheduled CI check (configure `KAGGLE_USERNAME` and `KAGGLE_KEY` secrets).

---

## 3. Checking Peer-Reviewed Journal Publication Status

Although the Kaggle CSV includes a `published` column, it is **frozen at scrape time** and omits **journal names** and **publication dates**. `scripts/check_publications.py` solves this with a 3-tier verification pipeline backed by a resumable SQLite cache (`data/cache/publication_status.sqlite`):

1. **bioRxiv / medRxiv Official `pubs` API** (bulk date-range harvesting at 100 records/request, or single-DOI lookup) — returns `published_doi`, `published_journal`, and `published_date`.
2. **Crossref REST API fallback** (`relation.is-preprint-of`) — catches peer-reviewed links not yet indexed by bioRxiv.
3. **OpenAlex Batch API fallback** (50 DOIs per request) — additional journal venue discovery.

> **API Terms & Licensing Note:** Before bulk-querying bioRxiv/medRxiv endpoints, review the terms at [api.biorxiv.org](https://api.biorxiv.org/) and [about-biorxiv](https://www.biorxiv.org/content/about-biorxiv), and respect individual paper Creative Commons licenses. Pass `--mailto your@email.com` to use Crossref and OpenAlex polite pools.

```bash
# Check individual DOIs immediately
.venv/bin/python scripts/check_publications.py doi 10.1101/000141 10.1101/000042

# Fast bulk-harvest of newly published preprints (100 records per request)
.venv/bin/python scripts/check_publications.py bulk-harvest \
  --server both --start-date 2025-01-01 --end-date 2026-11-22

# Verify a slice of the dataset and export enriched Parquet + summary JSON
.venv/bin/python scripts/check_publications.py verify \
  --server medrxiv --since 2024-01-01 --limit 200 \
  --crossref-fallback --mailto your.email@example.com

# Inspect SQLite cache coverage
.venv/bin/python scripts/check_publications.py stats
```

---

## 4. Materializing Cleaned Parquet Tables & Building the Web Dashboard

`scripts/build_report.py` streams the 1.31 GB CSV in memory-bounded chunks, writes compressed zstd Parquet tables to `data/processed/`, and builds the static interactive dashboard in `docs/index.html` (ready for GitHub Pages):

```bash
# Full build: clean CSV -> Parquet tables -> interactive dashboard in docs/
.venv/bin/python scripts/build_report.py

# Fast rebuild of the dashboard from existing canonical Parquet
.venv/bin/python scripts/build_report.py --skip-parquet

# Preview locally at http://127.0.0.1:8080/
.venv/bin/python scripts/build_report.py --skip-parquet --serve --port 8080
```

---

## 5. Analysis Plan

See [`docs/analysis_plan.md`](docs/analysis_plan.md) for the full schema audit, data quality findings, phased analysis plan, 19 prioritized research questions, and 15 visualization blueprints.
