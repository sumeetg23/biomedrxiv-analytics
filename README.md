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
│   ├── biorxiv_sync.py            # Direct bioRxiv/medRxiv API incremental updater
│   ├── preprocessing.py           # Data cleaning & DOI-level deduplication
│   ├── etl.py                     # Chunked Parquet materialization & JSON contract export
│   ├── ror_dedup.py               # Official ROR institutional deduplication & country geocoding
│   ├── publication_checker.py     # bioRxiv/medRxiv pubs + Crossref + OpenAlex + SQLite cache
│   ├── clinical_trials.py         # ClinicalTrials.gov v2 + Europe PMC + NIH iCite linkage
│   ├── qa_engine.py               # Read-only DuckDB SQL + Gemini/Ollama tool-calling engine
│   ├── qa_server.py               # Local FastAPI server exposing Q&A + SQL + search APIs
│   ├── quality_gates.py           # Automated schema, reconciliation, privacy & drift gates
│   ├── release_agent.py           # Deterministic + Gemini PR release digest & failure triage
│   └── scheduler.py               # macOS LaunchAgent & cron generator
├── scripts/                       # Thin CLI entrypoints
│   ├── download_dataset.py        # CLI: sync, list, diff, and switch dataset versions
│   ├── sync_biorxiv_api.py        # Direct bioRxiv/medRxiv API incremental updater CLI
│   ├── update_dataset.py          # Automated updater + macOS launchd / cron setup
│   ├── run_etl.py                 # Pure ETL: CSV -> Parquet -> web/data/dashboard_data.json
│   ├── deduplicate_institutions.py # Official ROR institutional deduplication CLI
│   ├── check_publications.py      # Multi-API peer-reviewed journal verification CLI
│   ├── check_clinical_trials.py   # Direct trial registry + downstream NIH iCite CLI
│   ├── run_qa_app.py              # Local FastAPI + DuckDB + Gemini/Ollama Q&A launcher
│   ├── run_quality_gates.py       # Standalone data quality & contract validation CLI
│   └── run_pipeline.py            # End-to-end sync -> ETL -> trials -> ROR -> gates orchestrator
├── app/
│   └── index.html                 # Local AI Analyst Chat + Direct DuckDB SQL UI
├── web/                           # Decoupled static web dashboard (GitHub Pages artifact)
│   ├── index.html                 # Dashboard markup (6 tabs + year/server filters)
│   ├── css/styles.css             # Responsive light/dark styling
│   ├── js/dashboard.js            # Plotly.js visualization controller
│   ├── data/dashboard_data.json   # Data contract produced by scripts/run_etl.py
│   ├── serve.py                   # Lightweight local HTTP preview server
│   └── .nojekyll
├── docs/
│   └── analysis_plan.md           # Full analysis plan, research questions & viz blueprint
├── tests/                         # Unit tests (pytest)
├── .github/workflows/
│   ├── ci.yml                     # Runs pytest + dashboard JSON contract gate on push/PR
│   ├── deploy-pages.yml           # Auto-deploys web/ to GitHub Pages on push to main
│   └── update-dataset.yml         # Weekly Kaggle sync -> gates -> AI digest -> auto-PR
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

## 1. Downloading & Updating Dataset Versions (Kaggle Base + Direct bioRxiv/medRxiv API)

Every snapshot is stored immutably in `data/versions/<version_id>/` with a `manifest.json`, and `data/current` points atomically to the active version. You can bootstrap from Kaggle once and then **stay ahead of Kaggle** by pulling incremental updates directly from the official bioRxiv & medRxiv REST APIs:

```bash
# Bootstrap or sync from Kaggle
.venv/bin/python scripts/download_dataset.py sync --notes "Kaggle base"

# Pull new preprints + newly published journal links directly from bioRxiv/medRxiv APIs
# (auto-detects active max posting date -> today, deduplicates, and creates a new version!)
.venv/bin/python scripts/sync_biorxiv_api.py

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

---

## 7. Local DuckDB + LLM Analyst App (`app/` + `scripts/run_qa_app.py`)

Ask natural-language analytical questions backed by deterministic, read-only **DuckDB SQL** over `preprints`, `clinical_trials`, and `downstream_clinical` Parquet views — keeping `web/` 100% static and secret-free for GitHub Pages.

- **Primary LLM:** Gemini API via `google-genai` Interactions API (default `gemini-4-hydrogen`, set `GEMINI_API_KEY`).
- **Offline LLM Fallback:** Local Ollama (`http://127.0.0.1:11434`, e.g., `llama3.2`).
- **Zero-LLM Mode:** Includes a **Direct DuckDB SQL Playground** and **Instant Paper Search** that work completely offline even without an API key, plus a local mirror of the visual dashboard at `/dashboard/`.

```bash
# Option A: With Gemini API
export GEMINI_API_KEY="your-api-key"
.venv/bin/python scripts/run_qa_app.py --port 8000

# Option B: With local Ollama (or offline SQL/search mode)
.venv/bin/python scripts/run_qa_app.py --ollama-model llama3.2 --port 8000
# Open http://127.0.0.1:8000/ (Q&A + SQL) or http://127.0.0.1:8000/dashboard/
```

---

## 8. Automated Data Quality Gates & Release-Digest PR Workflow

Every automated update passes through deterministic data-quality gates before touching `main`:

- **Gates checked (`scripts/run_quality_gates.py`):** required Parquet schema, 100% DOI uniqueness & non-nullity, server domain validity (`{biorxiv, medrxiv}`), date bounds, `by_year` cube reconciliation, zero local filesystem path leaks, JSON size guard, and non-shrinkage regression check (`<= 2%` drop vs. baseline).
- **Agent-Assisted PR Workflow (`.github/workflows/update-dataset.yml`):** Weekly (or via `workflow_dispatch`), runs `pytest` → syncs Kaggle → runs ETL + ClinicalTrials.gov enrichment → runs quality gates → generates a deterministic + optional Gemini (`GEMINI_API_KEY`) executive changelog → opens a **Pull Request** updating `web/data/dashboard_data.json` for your review. Merging the PR automatically triggers `deploy-pages.yml`.
- **CI Validation (`.github/workflows/ci.yml`):** Runs `pytest` + JSON contract validation on every push and PR.

```bash
# Run quality gates directly against active Parquet & web/data/dashboard_data.json
.venv/bin/python scripts/run_quality_gates.py --output-markdown logs/release_digest.md

# Run the full local end-to-end orchestrator (skip Kaggle download to re-validate locally)
.venv/bin/python scripts/run_pipeline.py --skip-download --skip-parquet --use-gemini
```

### GitHub Repository Secrets (for Automated Weekly PRs)
Configure these in **Settings → Secrets and variables → Actions** on GitHub:
- `KAGGLE_USERNAME` & `KAGGLE_KEY` — required for weekly Kaggle syncs.
- `GEMINI_API_KEY` *(optional)* — enables AI executive release summaries and automated failure triage in PR descriptions (falls back gracefully to deterministic scorecards if unset).
