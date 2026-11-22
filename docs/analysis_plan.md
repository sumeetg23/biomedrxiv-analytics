# BioRxiv & MedRxiv Dataset — Analysis Plan, Research Questions & Visualization Blueprint

## 1. Dataset Snapshot Profile (Verified on Active Snapshot)

From the initial downloaded snapshot (`m0delcrafter/bio-and-med-dataset`, version `v20261122_183540_bb4c451d`):

| File | Size | Rows | Description |
| :--- | ---: | ---: | :--- |
| `biorxiv_medrxiv_preprints.csv` | 1.31 GB | 605,767 | Flattened preprint metadata + abstracts (15 columns) |
| `biorxiv_pages.jsonl` | 1.11 GB | 16,421 | Raw paginated bioRxiv API responses (`cursor`, `papers`) |
| `medrxiv_pages.jsonl` | 308.8 MB | 3,802 | Raw paginated medRxiv API responses (`cursor`, `papers`) |

### Coverage Summary
- **Unique Preprints (DOIs):** **449,506** across **605,767** version postings (avg. 1.35 versions/paper; max version = 26).
- **bioRxiv:** 492,516 rows / 358,194 unique DOIs (`2013-11-07` to `2026-10-31`).
- **medRxiv:** 113,251 rows / 91,312 unique DOIs (`2019-06-25` to `2026-10-31`).
- **Baseline Peer-Review Rate (Snapshot `published` column):** **49.43%** of unique DOIs (222,211 / 449,506) — vs. a **biased 52.1%** if computed naively on raw rows without deduplicating versions!

### Critical Data Quality Findings (Must Clean Before Modeling/Charting)
1. **Multi-Version Granularity Trap:** Each row is a `(doi, version)` posting event. Always deduplicate to 1 row per DOI via `biomedrxiv.preprocessing.deduplicate_by_doi()` for paper-level metrics, while keeping version-level rows for revision dynamics.
2. **Dirty Categorical Strings:**
   - `category`: **115 raw values collapse to 76 canonical categories** after stripping trailing whitespace and lowercasing (e.g., `"Cancer Biology "` vs `"cancer biology"`).
   - `type`: Mixed casing, server-specific labels (`PUBLISHAHEADOFPRINT` for medRxiv vs `new results` for bioRxiv), stray newline (`PUBLISHAHEADOFPRINT\n`), and **859 withdrawn version records** (`withdrawn` + `WITHDRAWN`).
   - `license`: `CC-BY-NC-ND` (1 row) vs `cc_by_nc_nd` (214,347 rows); 610 nulls.
   - `author_corresponding` & `authors`: Double internal spaces (`"Jacob  Scott"`) and trailing semicolons.
3. **Right-Censoring Bias in Publication Rate:** Preprints posted in late 2025–2026 have not yet completed a typical 6–18 month peer-review cycle. Comparing raw publication rates across years without **cohort-windowed rates** (e.g., published within 12/24 months) or **Kaplan–Meier survival curves** will falsely imply a recent collapse in peer-review conversion.
4. **Stale & Incomplete `published` Column:** The CSV's `published` column only records a bare DOI string frozen at scrape time (`2026-10-31`) — it omits **journal name**, **publication date**, and **newly published papers** after the scrape. Use `scripts/check_publications.py` to enrich with bioRxiv/medRxiv `pubs` API, Crossref (`is-preprint-of`), and OpenAlex.

---

## 2. Phased Analysis Plan

### Phase 1 — Data Validation, Cleaning & Parquet Materialization
- Run schema validation and nullity audit across all 15 columns.
- Clean string normalization issues (`biomedrxiv.preprocessing.clean_preprints_dataframe`).
- Materialize two fast analytical tables in `data/processed/`:
  1. `preprints_versions.parquet` (605,767 version-level events)
  2. `preprints_canonical.parquet` (449,506 unique DOI records with engineered features: `first_posted_date`, `latest_posted_date`, `num_versions`, `revision_span_days`, `author_count`, `abstract_word_count`, `title_word_count`, `ever_withdrawn`, `snapshot_is_published`).

### Phase 2 — Peer-Reviewed Publication Enrichment
- Bulk-harvest published mappings via `scripts/check_publications.py bulk-harvest` (bioRxiv & medRxiv `pubs` API) into `data/cache/publication_status.sqlite`.
- Run Crossref/OpenAlex fallback on high-priority subsets (e.g., older `NA` cohorts or specific categories) to measure false-negative rates in bioRxiv's automated matcher.
- Engineer `published_journal`, `published_date`, `days_to_publication`, and `publisher_prefix` (from DOI prefix, e.g., `10.1038` = Nature Portfolio, `10.1016` = Elsevier, `10.1371` = PLOS, `10.1126` = Science/AAAS).

### Phase 3 — Exploratory Data Analysis & Cohort Survival Analysis
- Decompose temporal growth (monthly/yearly volume, seasonality, day-of-week posting behavior, COVID-19 surge & post-pandemic normalization).
- Compare bioRxiv vs. medRxiv across categories, team sizes, licenses, withdrawal rates, and revision counts.
- Apply **Kaplan–Meier survival analysis** (event = peer-reviewed publication, censoring date = API verification date) to estimate unbiased 12-, 24-, and 36-month publication probabilities by server, category, and posting year.

### Phase 4 — Text & Semantic Analysis (Titles + Abstracts)
- TF-IDF / BERTopic / embedding-based topic evolution (e.g., CRISPR, single-cell RNA-seq, AlphaFold/protein folding, LLMs in medicine, mRNA vaccines, spatial transcriptomics).
- Compare abstract readability, length, and buzzword frequency between papers that reach high-impact peer-reviewed journals vs. those that remain unpublished.

### Phase 5 — Predictive Modeling (Following ML Best Practices)
- **Task:** Predict whether a preprint posted ≥ 24 months ago gets published in a peer-reviewed journal (binary classification) and/or predict `days_to_publication` (regression).
- **Strict Anti-Leakage Discipline:**
  - Use a **chronological train / validation / test split** (e.g., train ≤ 2021, val = 2022, test = 2023) so recent right-censored papers (2025–2026) do not corrupt labels and future vocabulary does not leak into TF-IDF/encoders.
  - Fit all imputers, encoders, scalers, and text vectorizers **strictly on the training split**.
  - Establish naive majority baseline + Logistic Regression baseline before gradient boosting (LightGBM/XGBoost).
  - Report **ROC-AUC, PR-AUC, F1, Brier score / calibration curves**, bootstrapped 95% CIs, and slice-based error analysis across `server` and `category`.

---

## 3. High-Impact Analytical Questions to Ask

### A. Volume, Growth & Macro Trends
1. How have monthly bioRxiv and medRxiv submissions evolved from 2013 to 2026, and what structural breaks occurred during COVID-19 (2020–2021) and the generative AI / AlphaFold boom (2022–2026)?
2. Are there strong seasonal or day-of-week effects in preprint submissions (e.g., academic grant/conference deadlines, December slumps, weekday posting patterns)?
3. Which subject categories are the fastest growing vs. plateauing in share of total preprints over the last 3 years?

### B. Peer-Reviewed Publication Outcomes (Enriched via API)
4. What is the **unbiased** 12-month and 24-month peer-reviewed publication rate by server (`bioRxiv` vs. `medRxiv`) and by subject category once right-censoring is accounted for?
5. How long does peer review take (`days_to_publication`), and has median time-to-publication gotten faster or slower between 2015 and 2024?
6. Which journals and publishers absorb the largest share of bioRxiv vs. medRxiv preprints (e.g., *PLOS ONE*, *Nature Communications*, *eLife*, *PNAS*, *BMJ*, *JAMA Network Open*)?
7. How many preprints marked `NA` in the Kaggle snapshot have **actually been published** when re-checked live via bioRxiv `pubs` + Crossref + OpenAlex (quantifying snapshot staleness and bioRxiv linker recall)?
8. Do `Confirmatory Results` and `Contradictory Results` publish at lower rates than `New Results` (testing publication bias in peer-reviewed journals)?

### C. Revision & Withdrawal Dynamics
9. Does posting a revised preprint (v2+) strongly predict eventual peer-reviewed publication (since authors often upload revised manuscripts responding to peer reviewers)?
10. What is the typical time gap between v1 and v2, and what characterizes extreme-version outliers (papers with 10–26 versions)?
11. What are the withdrawal rates across servers, categories, and years — did medRxiv experience elevated withdrawal rates during the 2020–2021 COVID-19 surge?

### D. Collaboration, Authorship & Institutions
12. How has median team size (`author_count`) changed over time and across disciplines (e.g., large consortium genomics/clinical trials vs. solo theoretical biology)?
13. Is there an optimal team size associated with higher peer-review conversion or faster time-to-publication?
14. Which corresponding institutions produce the most preprints, and do publication conversion rates vary significantly across top institutions? (Requires institutional name normalization.)

### E. Open Science & Licensing Behavior
15. How has Creative Commons license adoption (`cc_by` vs. `cc_by_nc_nd` vs. `cc_no` / copyright-retained) shifted over time, especially following funder open-access mandates (NIH, Plan S, Wellcome)?
16. Do open-license (`cc_by` / `cc0`) preprints convert to peer-reviewed publications at different rates than `cc_no` (all-rights-reserved) preprints?

### F. NLP & Scientific Frontier Tracking
17. What emerging methods and topics (e.g., `"AlphaFold"`, `"CRISPR"`, `"single-cell"`, `"large language model"`, `"spatial transcriptomics"`, `"GLP-1"`) show exponential adoption trajectories in titles/abstracts?
18. Do papers with shorter, clearer titles or specific structural abstract patterns correlate with higher publication rates or top-tier journal placement?
19. How much do titles or abstracts change between v1 and the final preprint version (measuring manuscript evolution during peer review)?

---

## 4. Visualization Blueprint

| # | Visualization | Chart Type | Encodings (X / Y / Hue / Facet) | Granularity | Key Insight / Pitfall Guard |
| :-: | :--- | :--- | :--- | :--- | :--- |
| 1 | **Preprint Ecosystem Growth** | Stacked area + YoY growth line (dual panel) | X: Month (`2013–2026`), Y: Unique DOIs, Hue: `server` | Unique DOI (`v1` date) | Annotate medRxiv launch (Jun 2019) & COVID-19 peak (May 2020). |
| 2 | **Category Treemap / Ranked Bar** | Horizontal sorted bar chart (small multiples by server) | X: Unique DOI count, Y: Top 20 `category_clean`, Facet: `server` | Unique DOI | Use stripped/lowercased `category_clean` (76 vs 115 raw). |
| 3 | **Category Share Shift (2018 → 2026)** | Slopegraph or Bump Chart | X: Year, Y: Rank / % share, Color: Category | Unique DOI | Highlights rise of Bioinformatics, AI/Health Informatics, Neuroscience. |
| 4 | **Cohort Publication Rate Heatmap** | 2D Heatmap | X: Posting Year, Y: Top 20 Categories, Fill: % Published | Unique DOI | Immediately exposes right-censoring in 2025–2026 vs mature cohorts. |
| 5 | **Time-to-Publication Survival Curves** | Kaplan–Meier cumulative incidence curves | X: Months since v1, Y: Cumulative % published, Hue: `server` or top categories | Unique DOI (enriched) | Gold-standard unbiased view of peer-review conversion & speed. |
| 6 | **Peer-Review Turnaround Distribution** | Ridgeline / Violin + Boxplot | X: `days_to_publication`, Y: Top 12 journals or categories | Published DOIs | Clip/annotate negative lags (journal published before preprint indexed). |
| 7 | **Preprint → Journal Flow** | Sankey / Alluvial Diagram | Left: `server` → Middle: Top Categories → Right: Top Publishers/Journals | Published DOIs | Shows exact pipelines (e.g., medRxiv → BMJ/Lancet/JAMA vs bioRxiv → Nature/PLOS/eLife). |
| 8 | **Revisions vs. Publication Rate** | Grouped bar + error bars (95% CI) | X: `num_versions` (`1, 2, 3, 4, 5+`), Y: % Published, Hue: `server` | Unique DOI (mature cohorts ≤ 2023) | Tests whether revising a preprint strongly predicts publication success. |
| 9 | **Submission Cadence Heatmap** | Calendar / Weekday × Month Heatmap | X: Month, Y: Day of week, Fill: Avg daily submissions | Version postings | Reveals weekday posting spikes and holiday troughs. |
| 10 | **License Evolution Over Time** | 100% Stacked Area Chart | X: Year, Y: Share of preprints, Hue: `license_clean` | Unique DOI | Tracks shift toward `cc_by` vs `cc_no` / `cc_by_nc_nd`. |
| 11 | **Team Size Distributions by Field** | Horizontal Boxen / Strip + Median dot plot | X: `author_count` (log scale), Y: Category, Hue: `server` | Unique DOI | Highlights consortium fields (Genomics, Oncology) vs small-team fields. |
| 12 | **Withdrawal Rate Timeline** | Annotated line chart (per 1,000 preprints) | X: Quarter, Y: Withdrawn per 1,000 DOIs, Hue: `server` | Unique DOI (`ever_withdrawn`) | Shows COVID-era medRxiv withdrawal spikes vs steady baseline. |
| 13 | **Emerging Technology Keyword Trends** | Small-multiples normalized line chart | X: Quarter, Y: % of abstracts mentioning term, Facet: Keyword | Unique DOI | Tracks CRISPR, single-cell, AlphaFold, LLMs, spatial omics over time. |
| 14 | **Top Institutions: Volume vs. Conversion** | Scatter / Bubble Plot | X: Total preprints (log), Y: Mature-cohort publication rate %, Size: Median team size | Unique DOI (≤ 2023) | Separates high-volume institutions with high vs. lower journal conversion. |
| 15 | **Publication Prediction Model Diagnostics** | 3-panel ROC/PR + Calibration + SHAP bar | Panel A: PR curves, Panel B: Calibration, Panel C: Top SHAP features | Chronological test split | Quantifies what truly drives peer-review success (revisions, field, length, team size). |
