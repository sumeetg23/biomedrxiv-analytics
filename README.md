# BioRxiv & MedRxiv Preprint Dataset Toolkit

A modular, version-controlled data engineering and analytics project for the Kaggle dataset [`m0delcrafter/bio-and-med-dataset`](https://www.kaggle.com/datasets/m0delcrafter/bio-and-med-dataset) (605,767 version rows / 449,506 unique bioRxiv & medRxiv preprints spanning Nov 2013 – Oct 2026).

**[View Interactive Web Dashboard (GitHub Pages)](https://sumeetg23.github.io/biomedrxiv-analytics/)**

## Architecture: Clean Separation of ETL vs. Web

The project intentionally decouples **data engineering (ETL)** from **web visualization**:

- **ETL & Data Engineering (`src/biomedrxiv/` + `scripts/`)** — Pure Python data processing: downloads versioned Kaggle snapshots, cleans & deduplicates into zstd Parquet tables, verifies peer-reviewed publications, and exports a single JSON data contract (`web/data/dashboard_data.json`). Contains zero HTML/CSS/JS.
- **Web Dashboard (`web/`)** — Pure static frontend (`index.html`, `css/styles.css`, `js/dashboard.js`, `serve.py`) that consumes `web/data/dashboard_data.json` and deploys directly to GitHub Pages via [`.github/workflows/deploy-pages.yml`](.github/workflows/deploy-pages.yml).
- **Documentation (`docs/`)** — Research plan and analytical specifications ([`docs/analysis_plan.md`](docs/analysis_plan.md)).

```text
Bio&Med-RxivDataset/
├── data/                          # Local data store (gitignored)
│   ├── versions/                  # Immutable, SHA-256-verified dataset snapshots
│   ├── current -> versions/...    # Atomic symlink to active dataset version
│   ├── processed/                 # Materialized zstd Parquet tables
│   │   ├── preprints_versions.parquet
│   │   └── preprints_canonical.parquet
│   ├── cache/                     # SQLite cache for peer-reviewed publication checks
│   └── registry.json              # Local version history & update check audit log
├── src/biomedrxiv/                # Pure Python ETL & data-engineering package
│   ├── config.py                  # Canonical project path configuration
│   ├── versioning.py              # Kaggle download, hashing, snapshotting & diffing
│   ├── preprocessing.py           # Data cleaning & DOI-level deduplication
│   ├── etl.py                     # Chunked Parquet materialization & JSON contract export
│   ├── publication_checker.py     # bioRxiv/medRxiv pubs + Crossref + OpenAlex + SQLite cache
│   ├── clinical_trials.py         # ClinicalTrials.gov v2 + Europe PMC + NIH iCite linkage
│   └── scheduler.py               # macOS LaunchAgent & cron generator
├── scripts/                       # Thin ETL CLI entrypoints
│   ├── download_dataset.py        # CLI: sync, list, diff, and switch dataset versions
│   ├── update_dataset.py          # Automated updater + macOS launchd / cron setup
│   ├── run_etl.py                 # Pure ETL: CSV -> Parquet -> web/data/dashboard_data.json
│   ├── check_publications.py      # Multi-API peer-reviewed journal verification CLI
│   └── check_clinical_trials.py   # Direct trial registry + downstream NIH iCite CLI
├── web/                           # Decoupled static web dashboard (GitHub Pages artifact)
│   ├── index.html                 # Dashboard markup
│   ├── css/styles.css             # Responsive light/dark styling
│   ├── js/dashboard.js            # Plotly.js visualization controller
│   ├── data/dashboard_data.json   # Data contract produced by scripts/run_etl.py
│   ├── serve.py                   # Lightweight local HTTP preview server
│   └── .nojekyll
├── docs/
│   └── analysis_plan.md           # Full analysis plan, research questions & viz blueprint
├── tests/                         # Unit tests (pytest)
├── .github/workflows/
│   ├── deploy-pages.yml           # Auto-deploys web/ to GitHub Pages on push to main
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

Every snapshot is stored immutably in `data/versions/<version_id>/` with a `manifest.json`, and `data/current` points atomically to the active version. Identical re-downloads do not duplicate disk space — they log a check timestamp in `data/registry.json`.

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
```bash
# Install and activate the weekly LaunchAgent (Sundays at 03:00 local time)
.venv/bin/python scripts/update_dataset.py --install-launchd --weekday 0 --hour 3 --minute 0

# Or daily at 03:00 (--weekday -1)
.venv/bin/python scripts/update_dataset.py --install-launchd --weekday -1 --hour 3 --minute 0

# Uninstall later if desired
.venv/bin/python scripts/update_dataset.py --uninstall-launchd
```

### Option B: `cron`
```bash
.venv/bin/python scripts/update_dataset.py --print-cron --weekday 0 --hour 3 --minute 0
```

### Option C: GitHub Actions
See [`.github/workflows/update-dataset.yml`](.github/workflows/update-dataset.yml) for a weekly scheduled CI check.

---

## 3. Running the ETL Pipeline

`scripts/run_etl.py` streams the 1.31 GB CSV in memory-bounded chunks, writes zstd Parquet tables to `data/processed/`, and exports the compact JSON contract consumed by `web/`:

```bash
# Full ETL: raw CSV -> Parquet tables -> web/data/dashboard_data.json
.venv/bin/python scripts/run_etl.py

# Fast JSON re-aggregation from existing canonical Parquet (~2 seconds)
.venv/bin/python scripts/run_etl.py --skip-parquet
```

---

## 4. Viewing & Deploying the Web Dashboard (`web/`)

### Preview Locally
```bash
python3 web/serve.py --port 8080
# Open http://127.0.0.1:8080/
```

### Deploy on GitHub Pages
[`.github/workflows/deploy-pages.yml`](.github/workflows/deploy-pages.yml) automatically publishes `web/` to GitHub Pages whenever `web/` changes on `main`. After pushing your repository to GitHub:
1. Open **Settings → Pages** on GitHub (or run the `gh api` command below).
2. Set **Build and deployment → Source** to **GitHub Actions**.

```bash
# Enable GitHub Actions-based Pages deployment for your repo
gh api -X POST repos/<your-username>/<your-repo>/pages -f build_type=workflow
```

---

## 5. Checking Peer-Reviewed Journal Publication Status

`scripts/check_publications.py` resolves `is_published`, `published_doi`, `published_journal`, `published_date`, and `days_to_publication` using a 3-tier verification pipeline backed by a resumable SQLite cache (`data/cache/publication_status.sqlite`):

1. **bioRxiv / medRxiv Official `pubs` API** (bulk date-range harvesting at 100 records/request, or single-DOI lookup).
2. **Crossref REST API fallback** (`relation.is-preprint-of`).
3. **OpenAlex Batch API fallback** (50 DOIs per request).

> **API Terms & Licensing Note:** Review [api.biorxiv.org](https://api.biorxiv.org/) and [about-biorxiv](https://www.biorxiv.org/content/about-biorxiv), and respect individual paper Creative Commons licenses.

```bash
# Check individual DOIs immediately
.venv/bin/python scripts/check_publications.py doi 10.1101/000141 10.1101/000042

# Bulk-harvest newly published preprints (100 records per request)
.venv/bin/python scripts/check_publications.py bulk-harvest \
  --server both --start-date 2025-01-01 --end-date 2026-11-22

# Verify a filtered slice and export enriched Parquet + summary JSON
.venv/bin/python scripts/check_publications.py verify \
  --server medrxiv --since 2024-01-01 --limit 200 \
  --crossref-fallback --mailto your.email@example.com
```

---

## 6. Linking Clinical Trials & Downstream Translational Impact

`scripts/check_clinical_trials.py` links preprints and publications to clinical trials in two ways (cached in `data/cache/clinical_trials.sqlite`):

1. **Direct Trial Registry Extraction & ClinicalTrials.gov API v2 Enrichment** — extracts `NCT`, `ISRCTN`, `ChiCTR`, `ACTRN`, and `EudraCT` IDs from titles/abstracts and batch-enriches `NCT` studies (phase, status, sponsor, enrollment, conditions, posted results).
2. **Downstream Translational Linkage (NIH iCite)** — resolves peer-reviewed DOIs to PubMed PMIDs via Europe PMC and queries NIH iCite (`cited_by_clin`, Approximate Potential to Translate `apt`, and `rcr`) to identify papers that informed subsequent clinical trials.

> **API Terms Note:** Please review the terms at [clinicaltrials.gov](https://clinicaltrials.gov/) and [NIH iCite](https://icite.od.nih.gov/).

```bash
# 1. Extract all clinical trial IDs offline into data/processed/preprint_trial_links.parquet
.venv/bin/python scripts/check_clinical_trials.py extract

# 2. Batch-enrich NCT trials via ClinicalTrials.gov API v2 (50 NCTs per request)
.venv/bin/python scripts/check_clinical_trials.py enrich-nct

# 3. Trace downstream clinical-trial citations via Europe PMC + NIH iCite
.venv/bin/python scripts/check_clinical_trials.py trace-downstream --limit 500

# 4. Refresh the web dashboard JSON contract with Clinical Trials Tab 6
.venv/bin/python scripts/run_etl.py --skip-parquet
```
