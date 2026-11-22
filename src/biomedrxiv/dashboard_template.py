"""Static interactive web dashboard generator (GitHub Pages ready).

Generates ``docs/index.html`` + ``docs/data/dashboard_data.json`` using Plotly.js.
Data is both exported to ``docs/data/dashboard_data.json`` (clean API artifact)
and embedded as a JSON script fallback so opening ``docs/index.html`` directly via
``file://`` or hosting on GitHub Pages works seamlessly without CORS issues.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DASHBOARD_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>BioRxiv &amp; MedRxiv Preprint Analytics Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
  <style>
    :root {
      --bg: #f6f8fb;
      --surface: #ffffff;
      --surface-alt: #f0f4f9;
      --border: #dfe5ef;
      --text: #0f172a;
      --text-muted: #475569;
      --accent: #0f766e;
      --accent-soft: #ccfbf1;
      --biorxiv: #0f766e;
      --medrxiv: #2563eb;
      --warn: #b45309;
      --warn-bg: #fef3c7;
      --shadow: 0 1px 3px rgba(15, 23, 42, 0.06), 0 4px 14px rgba(15, 23, 42, 0.04);
      --radius: 12px;
    }
    [data-theme="dark"] {
      --bg: #0b1120;
      --surface: #111827;
      --surface-alt: #1e293b;
      --border: #273549;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --accent: #2dd4bf;
      --accent-soft: #134e4a;
      --biorxiv: #2dd4bf;
      --medrxiv: #60a5fa;
      --warn: #fbbf24;
      --warn-bg: #451a03;
      --shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }

    /* Header */
    .topbar {
      position: sticky;
      top: 0;
      z-index: 30;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0.85rem 1.5rem;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      box-shadow: var(--shadow);
    }
    .brand h1 {
      margin: 0;
      font-size: 1.2rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .brand p {
      margin: 0.1rem 0 0;
      font-size: 0.82rem;
      color: var(--text-muted);
    }
    .controls {
      display: flex;
      align-items: center;
      gap: 0.65rem;
      flex-wrap: wrap;
    }
    .segmented {
      display: inline-flex;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 3px;
    }
    .segmented button {
      border: none;
      background: transparent;
      color: var(--text-muted);
      font-size: 0.82rem;
      font-weight: 600;
      padding: 0.35rem 0.85rem;
      border-radius: 999px;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .segmented button.active {
      background: var(--accent);
      color: #ffffff;
    }
    [data-theme="dark"] .segmented button.active {
      color: #0b1120;
    }
    .icon-btn {
      border: 1px solid var(--border);
      background: var(--surface-alt);
      color: var(--text);
      border-radius: 8px;
      padding: 0.42rem 0.75rem;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
    }
    .icon-btn:hover { border-color: var(--accent); }

    /* Tabs */
    .nav-tabs {
      max-width: 1380px;
      margin: 1rem auto 0;
      padding: 0 1.25rem;
      display: flex;
      gap: 0.4rem;
      overflow-x: auto;
      border-bottom: 1px solid var(--border);
    }
    .tab-btn {
      border: none;
      background: transparent;
      color: var(--text-muted);
      font-size: 0.9rem;
      font-weight: 600;
      padding: 0.7rem 1.05rem;
      cursor: pointer;
      border-bottom: 3px solid transparent;
      white-space: nowrap;
    }
    .tab-btn.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
    }

    /* Main Layout */
    .container {
      max-width: 1380px;
      margin: 1.25rem auto 3rem;
      padding: 0 1.25rem;
    }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(195px, 1fr));
      gap: 0.9rem;
      margin-bottom: 1.25rem;
    }
    .kpi-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1rem 1.15rem;
      box-shadow: var(--shadow);
    }
    .kpi-label {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      font-weight: 600;
    }
    .kpi-value {
      font-size: 1.65rem;
      font-weight: 750;
      margin-top: 0.25rem;
      letter-spacing: -0.02em;
    }
    .kpi-sub {
      font-size: 0.78rem;
      color: var(--text-muted);
      margin-top: 0.2rem;
    }

    .tab-panel { display: none; }
    .tab-panel.active { display: block; }

    .grid-2 {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 1.15rem;
      margin-bottom: 1.15rem;
    }
    @media (max-width: 980px) {
      .grid-2 { grid-template-columns: 1fr; }
    }

    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.15rem 1.25rem;
      box-shadow: var(--shadow);
      margin-bottom: 1.15rem;
    }
    .grid-2 .card { margin-bottom: 0; }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 0.75rem;
      margin-bottom: 0.5rem;
    }
    .card h2 {
      margin: 0;
      font-size: 1.05rem;
      font-weight: 700;
    }
    .card .subtitle {
      margin: 0.2rem 0 0;
      font-size: 0.83rem;
      color: var(--text-muted);
    }
    .plot {
      width: 100%;
      min-height: 390px;
    }
    .plot-tall { min-height: 520px; }

    .callout {
      background: var(--surface-alt);
      border-left: 4px solid var(--accent);
      border-radius: 8px;
      padding: 0.85rem 1rem;
      font-size: 0.86rem;
      margin-bottom: 1.15rem;
    }
    .callout-warn {
      border-left-color: var(--warn);
      background: var(--warn-bg);
    }
    .callout strong { color: var(--text); }

    /* Table */
    .search-input {
      width: 100%;
      max-width: 340px;
      padding: 0.5rem 0.75rem;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--surface-alt);
      color: var(--text);
      font-size: 0.85rem;
    }
    .table-wrap {
      overflow-x: auto;
      margin-top: 0.75rem;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
    }
    th, td {
      text-align: left;
      padding: 0.6rem 0.75rem;
      border-bottom: 1px solid var(--border);
    }
    th {
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--text-muted);
      background: var(--surface-alt);
    }
    td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }

    footer {
      max-width: 1380px;
      margin: 2rem auto;
      padding: 1rem 1.25rem;
      border-top: 1px solid var(--border);
      font-size: 0.8rem;
      color: var(--text-muted);
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 0.5rem;
    }
    code {
      background: var(--surface-alt);
      padding: 0.1rem 0.35rem;
      border-radius: 4px;
      font-size: 0.82em;
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <h1>BioRxiv &amp; MedRxiv Preprint Analytics</h1>
      <p id="header-subtitle">Loading dataset metadata...</p>
    </div>
    <div class="controls">
      <div class="segmented" role="group" aria-label="Server filter">
        <button class="active" data-server="all">All Preprints</button>
        <button data-server="biorxiv">bioRxiv</button>
        <button data-server="medrxiv">medRxiv</button>
      </div>
      <button id="theme-toggle" class="icon-btn" title="Toggle dark/light mode">Dark Mode</button>
    </div>
  </header>

  <nav class="nav-tabs" role="tablist">
    <button class="tab-btn active" data-tab="overview">1. Overview &amp; Growth</button>
    <button class="tab-btn" data-tab="peer-review">2. Peer Review &amp; Journals</button>
    <button class="tab-btn" data-tab="frontiers">3. Research Frontiers (NLP)</button>
    <button class="tab-btn" data-tab="open-science">4. Open Science &amp; Teams</button>
    <button class="tab-btn" data-tab="institutions">5. Institutions &amp; Audit</button>
  </nav>

  <main class="container">
    <section class="kpi-grid" id="kpi-grid"></section>

    <!-- TAB 1: OVERVIEW -->
    <section class="tab-panel active" id="tab-overview">
      <div class="callout" id="audit-callout"></div>

      <div class="card">
        <div class="card-header">
          <div>
            <h2>Monthly Preprint Submissions (2013 – 2026)</h2>
            <p class="subtitle">Deduplicated unique DOIs by first posting date (v1), annotated with medRxiv launch and COVID-19 surge.</p>
          </div>
        </div>
        <div id="chart-monthly" class="plot"></div>
      </div>

      <div class="grid-2">
        <div class="card">
          <div class="card-header">
            <div>
              <h2>Top 20 Subject Categories</h2>
              <p class="subtitle">Cleaned &amp; normalized categories (115 raw strings &rarr; 76 canonical). Hover for mature-cohort publication rate.</p>
            </div>
          </div>
          <div id="chart-categories" class="plot plot-tall"></div>
        </div>

        <div class="card">
          <div class="card-header">
            <div>
              <h2>Annual Volume &amp; Snapshot Publication Rate</h2>
              <p class="subtitle">Notice right-censoring in 2025–2026: recent preprints are still undergoing peer review.</p>
            </div>
          </div>
          <div id="chart-annual" class="plot plot-tall"></div>
        </div>
      </div>
    </section>

    <!-- TAB 2: PEER REVIEW -->
    <section class="tab-panel" id="tab-peer-review">
      <div class="callout callout-warn">
        <strong>Methodological Guardrail (Right-Censoring):</strong> Peer review typically takes 6–18+ months.
        Aggregate comparisons below restrict to <strong>mature cohorts (first posted &le; 2024)</strong> so recent 2025–2026 submissions do not artificially depress conversion rates.
      </div>

      <div class="card">
        <div class="card-header">
          <div>
            <h2>Cohort Publication Rate Heatmap (Posting Year &times; Top 18 Categories)</h2>
            <p class="subtitle">Percentage of unique DOIs linked to a peer-reviewed journal DOI. Exposes both disciplinary differences and right-censoring.</p>
          </div>
        </div>
        <div id="chart-heatmap" class="plot plot-tall"></div>
      </div>

      <div class="grid-2">
        <div class="card">
          <div class="card-header">
            <div>
              <h2>Do Revisions Predict Peer-Reviewed Publication?</h2>
              <p class="subtitle">Mature cohorts (&le; 2024) with Wilson 95% confidence intervals. Revising a preprint strongly signals active peer review.</p>
            </div>
          </div>
          <div id="chart-revisions" class="plot"></div>
        </div>

        <div class="card">
          <div class="card-header">
            <div>
              <h2>Top Publishers Absorbing Preprints</h2>
              <p class="subtitle">Mapped via Crossref DOI prefix of published journal articles (updates with top-bar server filter).</p>
            </div>
          </div>
          <div id="chart-publishers" class="plot"></div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <div>
            <h2>Publication Bias Check: New vs. Confirmatory vs. Contradictory Results</h2>
            <p class="subtitle">bioRxiv mature cohorts (&le; 2024) with Wilson 95% confidence intervals.</p>
          </div>
        </div>
        <div id="chart-type-bias" class="plot" style="min-height:320px;"></div>
      </div>
    </section>

    <!-- TAB 3: RESEARCH FRONTIERS -->
    <section class="tab-panel" id="tab-frontiers">
      <div class="card">
        <div class="card-header">
          <div>
            <h2>Emerging Biomedical Research Frontiers (2015 – 2026)</h2>
            <p class="subtitle">Percentage of quarterly unique preprints mentioning each technology or topic in title/abstract. Click legend items to isolate curves.</p>
          </div>
        </div>
        <div id="chart-frontiers" class="plot plot-tall"></div>
      </div>

      <div class="card">
        <div class="card-header">
          <div>
            <h2>Total Preprints by Tracked Frontier Topic</h2>
            <p class="subtitle">Cumulative unique DOI mentions since 2015 across bioRxiv &amp; medRxiv.</p>
          </div>
        </div>
        <div id="chart-frontiers-bar" class="plot" style="min-height:340px;"></div>
      </div>
    </section>

    <!-- TAB 4: OPEN SCIENCE & TEAMS -->
    <section class="tab-panel" id="tab-open-science">
      <div class="card">
        <div class="card-header">
          <div>
            <h2>Open Science &amp; Creative Commons License Evolution (2014 – 2026)</h2>
            <p class="subtitle">Share of unique preprints by normalized license type (updates with top-bar server filter).</p>
          </div>
        </div>
        <div id="chart-licenses" class="plot"></div>
      </div>

      <div class="grid-2">
        <div class="card">
          <div class="card-header">
            <div>
              <h2>Collaboration &amp; Team Size Over Time</h2>
              <p class="subtitle">Median author count per preprint with interquartile range (25th–75th percentile).</p>
            </div>
          </div>
          <div id="chart-team-trend" class="plot"></div>
        </div>

        <div class="card">
          <div class="card-header">
            <div>
              <h2>Team Size vs. Peer-Reviewed Publication Rate</h2>
              <p class="subtitle">Mature cohorts (&le; 2024) with Wilson 95% confidence intervals.</p>
            </div>
          </div>
          <div id="chart-team-pub" class="plot"></div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <div>
            <h2>Preprint Withdrawal Rate Timeline (Per 1,000 Preprints)</h2>
            <p class="subtitle">Half-year withdrawal rate comparing bioRxiv vs. medRxiv (highlights 2020–2021 COVID-19 surge).</p>
          </div>
        </div>
        <div id="chart-withdrawals" class="plot" style="min-height:340px;"></div>
      </div>
    </section>

    <!-- TAB 5: INSTITUTIONS & AUDIT -->
    <section class="tab-panel" id="tab-institutions">
      <div class="card">
        <div class="card-header">
          <div>
            <h2>Top 30 Corresponding Institutions: Volume vs. Mature-Cohort Publication Rate</h2>
            <p class="subtitle">Bubble size encodes median team size. Raw institutional strings (not yet ROR-deduplicated).</p>
          </div>
        </div>
        <div id="chart-institutions" class="plot"></div>
      </div>

      <div class="card">
        <div class="card-header">
          <div>
            <h2>Top 30 Corresponding Institutions Table</h2>
            <p class="subtitle">Filter by institution name.</p>
          </div>
          <input type="search" id="inst-search" class="search-input" placeholder="Search institutions..." />
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Corresponding Institution</th>
                <th class="num">Total Preprints</th>
                <th class="num">Mature (&le; 2024)</th>
                <th class="num">Mature Pub Rate</th>
                <th class="num">Median Authors</th>
              </tr>
            </thead>
            <tbody id="inst-tbody"></tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <h2>Data Quality &amp; Reproducibility Provenance</h2>
        <div id="provenance-grid" class="kpi-grid" style="margin-top:0.85rem;margin-bottom:0;"></div>
      </div>
    </section>
  </main>

  <footer>
    <div id="footer-provenance">Dataset: m0delcrafter/bio-and-med-dataset</div>
    <div>Generated by <code>scripts/build_report.py</code> &bull; Interactive Plotly.js Dashboard</div>
  </footer>

  <script id="dashboard-data" type="application/json">__DASHBOARD_JSON__</script>
  <script>
    let DATA = null;
    let currentServer = "all";
    let currentTab = "overview";

    const fmtInt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
    const fmtPct = (n) => (n == null ? "—" : `${Number(n).toFixed(1)}%`);

    function themeColors() {
      const dark = document.documentElement.getAttribute("data-theme") === "dark";
      return {
        bg: "rgba(0,0,0,0)",
        text: dark ? "#e2e8f0" : "#0f172a",
        muted: dark ? "#94a3b8" : "#475569",
        grid: dark ? "#243247" : "#e2e8f0",
        biorxiv: dark ? "#2dd4bf" : "#0f766e",
        medrxiv: dark ? "#60a5fa" : "#2563eb",
        accent3: dark ? "#fbbf24" : "#d97706",
        danger: dark ? "#f87171" : "#dc2626",
      };
    }

    function baseLayout(extra = {}) {
      const c = themeColors();
      return Object.assign(
        {
          paper_bgcolor: c.bg,
          plot_bgcolor: c.bg,
          font: { color: c.text, family: "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif", size: 12 },
          margin: { l: 58, r: 24, t: 24, b: 48 },
          xaxis: { gridcolor: c.grid, zerolinecolor: c.grid },
          yaxis: { gridcolor: c.grid, zerolinecolor: c.grid },
          legend: { orientation: "h", y: 1.08, x: 0 },
          hovermode: "closest",
        },
        extra
      );
    }

    const PLOT_CFG = { responsive: true, displaylogo: false };

    function renderKPIs() {
      const k = DATA.kpis[currentServer];
      const cards = [
        { label: "Unique Preprints (DOIs)", value: fmtInt(k.unique_dois), sub: `${fmtInt(k.version_rows)} total version postings` },
        { label: `Mature Pub Rate (≤ ${k.mature_cohort_cutoff_year})`, value: fmtPct(k.mature_pub_rate_pct), sub: `Overall snapshot rate: ${fmtPct(k.overall_pub_rate_pct)}` },
        { label: "Date Coverage", value: `${k.date_min.slice(0, 4)} – ${k.date_max.slice(0, 4)}`, sub: `${k.date_min} to ${k.date_max}` },
        { label: "Avg Versions / Paper", value: k.avg_versions.toFixed(2), sub: `Max versions on a DOI: ${k.max_versions}` },
        { label: "Median Team Size", value: `${k.median_authors} authors`, sub: `${k.unique_categories} canonical categories` },
        { label: "Withdrawn Preprints", value: fmtInt(k.withdrawn_count), sub: `${k.withdrawn_per_1k.toFixed(2)} per 1,000 DOIs` },
      ];
      document.getElementById("kpi-grid").innerHTML = cards
        .map(
          (c) => `<div class="kpi-card">
            <div class="kpi-label">${c.label}</div>
            <div class="kpi-value">${c.value}</div>
            <div class="kpi-sub">${c.sub}</div>
          </div>`
        )
        .join("");
    }

    function renderOverview() {
      const c = themeColors();
      const a = DATA.quality_audit;
      const biasPp = (a.raw_row_publication_rate_pct - a.dedup_doi_publication_rate_pct).toFixed(2);
      document.getElementById("audit-callout").innerHTML =
        `<strong>Data Quality Guardrails Applied:</strong> Deduplicated <strong>${fmtInt(a.raw_row_count)}</strong> version rows into <strong>${fmtInt(a.unique_doi_count)}</strong> unique DOIs (preventing a <strong>+${biasPp} pp</strong> upward bias in publication rate), normalized <strong>${a.raw_unique_categories}</strong> messy category strings down to <strong>${a.clean_unique_categories}</strong> canonical fields, and flagged <strong>${fmtInt(a.withdrawn_unique_dois)}</strong> withdrawn papers.`;

      // 1. Monthly growth
      const mg = DATA.monthly_growth;
      const traces = [];
      if (currentServer === "all" || currentServer === "biorxiv") {
        traces.push({
          x: mg.months, y: mg.biorxiv, name: "bioRxiv", type: "scatter", mode: "lines",
          stackgroup: currentServer === "all" ? "one" : undefined,
          line: { width: 1.5, color: c.biorxiv },
        });
      }
      if (currentServer === "all" || currentServer === "medrxiv") {
        traces.push({
          x: mg.months, y: mg.medrxiv, name: "medRxiv", type: "scatter", mode: "lines",
          stackgroup: currentServer === "all" ? "one" : undefined,
          line: { width: 1.5, color: c.medrxiv },
        });
      }
      Plotly.newPlot("chart-monthly", traces, baseLayout({
        hovermode: "x unified",
        yaxis: { title: "Unique Preprints / Month", gridcolor: c.grid },
        xaxis: { title: "First Posting Month", gridcolor: c.grid },
        shapes: [
          { type: "line", x0: "2019-06", x1: "2019-06", y0: 0, y1: 1, yref: "paper", line: { dash: "dot", color: c.muted, width: 1.3 } },
          { type: "line", x0: "2020-05", x1: "2020-05", y0: 0, y1: 1, yref: "paper", line: { dash: "dot", color: c.danger, width: 1.3 } },
        ],
        annotations: [
          { x: "2019-06", y: 0.96, yref: "paper", text: "medRxiv launch", showarrow: false, xanchor: "right", font: { size: 11, color: c.muted } },
          { x: "2020-05", y: 0.88, yref: "paper", text: "COVID-19 surge", showarrow: false, xanchor: "left", font: { size: 11, color: c.danger } },
        ],
      }), PLOT_CFG);

      // 2. Top categories
      const cats = [...DATA.top_categories[currentServer]].reverse();
      Plotly.newPlot("chart-categories", [{
        type: "bar",
        orientation: "h",
        y: cats.map((d) => d.category),
        x: cats.map((d) => d.count),
        marker: {
          color: cats.map((d) => d.mature_pub_pct),
          colorscale: "Teal",
          colorbar: { title: "Mature Pub %", thickness: 12 },
        },
        customdata: cats.map((d) => [d.mature_pub_pct, d.median_authors, d.avg_versions]),
        hovertemplate: "<b>%{y}</b><br>Unique DOIs: %{x:,}<br>Mature Pub Rate: %{customdata[0]}%<br>Median Authors: %{customdata[1]}<br>Avg Versions: %{customdata[2]}<extra></extra>",
      }], baseLayout({
        margin: { l: 195, r: 20, t: 20, b: 48 },
        xaxis: { title: "Unique Preprints (DOIs)", gridcolor: c.grid },
        yaxis: { automargin: true },
      }), PLOT_CFG);

      // 3. Annual volume + publication rate
      const ann = DATA.annual_summary;
      const barY = currentServer === "all" ? ann.total : ann[currentServer];
      Plotly.newPlot("chart-annual", [
        { x: ann.years, y: barY, name: "Unique Preprints", type: "bar", marker: { color: currentServer === "medrxiv" ? c.medrxiv : c.biorxiv, opacity: 0.82 } },
        { x: ann.years, y: ann.pub_rate_pct, name: "All-Server Snapshot Pub %", type: "scatter", mode: "lines+markers", yaxis: "y2", line: { color: c.accent3, width: 2.5 } },
      ], baseLayout({
        hovermode: "x unified",
        xaxis: { dtick: 1, gridcolor: c.grid },
        yaxis: { title: "Unique Preprints", gridcolor: c.grid },
        yaxis2: { title: "Snapshot Published (%)", overlaying: "y", side: "right", range: [0, 90], showgrid: false },
      }), PLOT_CFG);
    }

    function renderPeerReview() {
      const c = themeColors();
      const h = DATA.cohort_heatmap;
      Plotly.newPlot("chart-heatmap", [{
        type: "heatmap",
        x: h.years,
        y: [...h.categories].reverse(),
        z: [...h.pub_rate_pct].reverse(),
        customdata: [...h.counts].reverse(),
        colorscale: "YlGnBu",
        zmin: 0,
        zmax: 85,
        hovertemplate: "<b>%{y}</b> (%{x})<br>Published: %{z}%<br>Preprints: %{customdata:,}<extra></extra>",
        colorbar: { title: "Published %" },
      }], baseLayout({
        margin: { l: 190, r: 24, t: 20, b: 48 },
        xaxis: { dtick: 1, title: "First Posting Year" },
        yaxis: { automargin: true },
      }), PLOT_CFG);

      // Revisions vs Pub rate
      const revTraces = ["biorxiv", "medrxiv"].map((srv) => {
        const rows = DATA.revisions_vs_pub[srv];
        return {
          type: "bar",
          name: srv === "biorxiv" ? "bioRxiv" : "medRxiv",
          x: rows.map((r) => `${r.versions} ver`),
          y: rows.map((r) => r.pub_rate_pct),
          error_y: {
            type: "data",
            symmetric: false,
            array: rows.map((r) => +(r.ci_high - r.pub_rate_pct).toFixed(2)),
            arrayminus: rows.map((r) => +(r.pub_rate_pct - r.ci_low).toFixed(2)),
          },
          marker: { color: srv === "biorxiv" ? c.biorxiv : c.medrxiv },
          customdata: rows.map((r) => [r.count, r.ci_low, r.ci_high]),
          hovertemplate: "<b>%{fullData.name} — %{x}</b><br>Published: %{y}% (95% CI: %{customdata[1]}–%{customdata[2]}%)<br>n = %{customdata[0]:,}<extra></extra>",
        };
      });
      Plotly.newPlot("chart-revisions", revTraces, baseLayout({
        barmode: "group",
        yaxis: { title: "Peer-Reviewed Publication Rate (%)", range: [0, 100], gridcolor: c.grid },
        xaxis: { title: "Number of Preprint Versions Posted" },
      }), PLOT_CFG);

      // Top Publishers
      const pubs = [...DATA.top_publishers[currentServer]].reverse();
      Plotly.newPlot("chart-publishers", [{
        type: "bar",
        orientation: "h",
        y: pubs.map((p) => p.publisher),
        x: pubs.map((p) => p.count),
        marker: { color: currentServer === "medrxiv" ? c.medrxiv : c.biorxiv },
        customdata: pubs.map((p) => p.share_pct),
        hovertemplate: "<b>%{y}</b><br>Published Papers: %{x:,} (%{customdata}% of published)<extra></extra>",
      }], baseLayout({
        margin: { l: 220, r: 24, t: 20, b: 48 },
        xaxis: { title: "Published Preprints", gridcolor: c.grid },
        yaxis: { automargin: true },
      }), PLOT_CFG);

      // Type Bias
      const tb = DATA.type_bias;
      Plotly.newPlot("chart-type-bias", [{
        type: "bar",
        x: tb.map((r) => r.type),
        y: tb.map((r) => r.pub_rate_pct),
        error_y: {
          type: "data",
          symmetric: false,
          array: tb.map((r) => +(r.ci_high - r.pub_rate_pct).toFixed(2)),
          arrayminus: tb.map((r) => +(r.pub_rate_pct - r.ci_low).toFixed(2)),
        },
        marker: { color: [c.biorxiv, c.medrxiv, c.accent3] },
        customdata: tb.map((r) => [r.count, r.ci_low, r.ci_high]),
        hovertemplate: "<b>%{x}</b><br>Published: %{y}% (95% CI: %{customdata[1]}–%{customdata[2]}%)<br>n = %{customdata[0]:,}<extra></extra>",
      }], baseLayout({
        yaxis: { title: "Mature Publication Rate (%)", range: [0, 80], gridcolor: c.grid },
      }), PLOT_CFG);
    }

    function renderFrontiers() {
      const c = themeColors();
      const ft = DATA.frontier_trends;
      const traces = ft.series.map((s) => ({
        x: ft.quarters,
        y: s.pct_by_quarter,
        name: s.label,
        type: "scatter",
        mode: "lines",
        line: { width: 2.3 },
        customdata: s.count_by_quarter,
        hovertemplate: "<b>%{fullData.name}</b><br>%{x}: %{y:.2f}% (%{customdata:,} papers)<extra></extra>",
      }));
      Plotly.newPlot("chart-frontiers", traces, baseLayout({
        hovermode: "x unified",
        xaxis: { title: "Quarter", nticks: 14, gridcolor: c.grid },
        yaxis: { title: "% of Quarterly Preprints Mentioning Topic", gridcolor: c.grid },
      }), PLOT_CFG);

      const sorted = [...ft.series].sort((a, b) => a.total_papers - b.total_papers);
      Plotly.newPlot("chart-frontiers-bar", [{
        type: "bar",
        orientation: "h",
        y: sorted.map((s) => s.label),
        x: sorted.map((s) => s.total_papers),
        marker: { color: c.biorxiv },
        hovertemplate: "<b>%{y}</b><br>Total Papers (2015–2026): %{x:,}<extra></extra>",
      }], baseLayout({
        margin: { l: 210, r: 24, t: 20, b: 48 },
        xaxis: { title: "Unique Preprints Mentioning Term", gridcolor: c.grid },
      }), PLOT_CFG);
    }

    function renderOpenScience() {
      const c = themeColors();
      const lic = DATA.license_evolution;
      const palette = {
        cc_by: "#0f766e",
        cc0: "#14b8a6",
        cc_by_nc: "#3b82f6",
        cc_by_nd: "#8b5cf6",
        cc_by_nc_nd: "#f59e0b",
        cc_no: "#64748b",
        other: "#cbd5e1",
      };
      const bySrv = lic.by_server[currentServer];
      const licTraces = Object.keys(bySrv).map((k) => ({
        x: lic.years,
        y: bySrv[k],
        name: lic.labels[k] || k,
        type: "scatter",
        mode: "lines",
        stackgroup: "one",
        line: { width: 0.8, color: palette[k] || "#94a3b8" },
      }));
      Plotly.newPlot("chart-licenses", licTraces, baseLayout({
        hovermode: "x unified",
        xaxis: { dtick: 1, title: "Posting Year", gridcolor: c.grid },
        yaxis: { title: "Share of Preprints (%)", range: [0, 100], gridcolor: c.grid },
      }), PLOT_CFG);

      // Team size trend
      const tt = DATA.team_trends;
      const teamTraces = [];
      for (const srv of ["biorxiv", "medrxiv"]) {
        const col = srv === "biorxiv" ? c.biorxiv : c.medrxiv;
        teamTraces.push({
          x: tt.years, y: tt[srv].median, name: `${srv === "biorxiv" ? "bioRxiv" : "medRxiv"} Median`,
          type: "scatter", mode: "lines+markers", line: { width: 2.5, color: col },
        });
        teamTraces.push({
          x: tt.years, y: tt[srv].q75, name: `${srv === "biorxiv" ? "bioRxiv" : "medRxiv"} 75th Pct`,
          type: "scatter", mode: "lines", line: { width: 1.2, dash: "dot", color: col },
        });
      }
      Plotly.newPlot("chart-team-trend", teamTraces, baseLayout({
        hovermode: "x unified",
        xaxis: { dtick: 1, gridcolor: c.grid },
        yaxis: { title: "Authors per Preprint", gridcolor: c.grid },
      }), PLOT_CFG);

      // Team size vs Pub rate
      const tvp = DATA.team_vs_pub;
      Plotly.newPlot("chart-team-pub", [{
        type: "bar",
        x: tvp.map((r) => r.bucket),
        y: tvp.map((r) => r.pub_rate_pct),
        error_y: {
          type: "data",
          symmetric: false,
          array: tvp.map((r) => +(r.ci_high - r.pub_rate_pct).toFixed(2)),
          arrayminus: tvp.map((r) => +(r.pub_rate_pct - r.ci_low).toFixed(2)),
        },
        marker: { color: c.medrxiv },
        customdata: tvp.map((r) => [r.count, r.ci_low, r.ci_high]),
        hovertemplate: "<b>%{x} authors</b><br>Published: %{y}% (95% CI: %{customdata[1]}–%{customdata[2]}%)<br>n = %{customdata[0]:,}<extra></extra>",
      }], baseLayout({
        xaxis: { title: "Author Team Size" },
        yaxis: { title: "Mature Publication Rate (%)", range: [0, 85], gridcolor: c.grid },
      }), PLOT_CFG);

      // Withdrawals timeline
      const wd = DATA.withdrawals_timeline;
      Plotly.newPlot("chart-withdrawals", [
        { x: wd.periods, y: wd.biorxiv.rate_per_1k, name: "bioRxiv", type: "scatter", mode: "lines+markers", line: { color: c.biorxiv, width: 2.3 }, customdata: wd.biorxiv.count, hovertemplate: "bioRxiv %{x}: %{y} / 1k (%{customdata} withdrawn)<extra></extra>" },
        { x: wd.periods, y: wd.medrxiv.rate_per_1k, name: "medRxiv", type: "scatter", mode: "lines+markers", line: { color: c.medrxiv, width: 2.3 }, customdata: wd.medrxiv.count, hovertemplate: "medRxiv %{x}: %{y} / 1k (%{customdata} withdrawn)<extra></extra>" },
      ], baseLayout({
        hovermode: "x unified",
        xaxis: { title: "Half-Year Cohort", gridcolor: c.grid },
        yaxis: { title: "Withdrawn per 1,000 Preprints", gridcolor: c.grid },
      }), PLOT_CFG);
    }

    function renderInstitutions() {
      const c = themeColors();
      const inst = DATA.top_institutions;
      Plotly.newPlot("chart-institutions", [{
        type: "scatter",
        mode: "markers+text",
        x: inst.map((d) => d.total_preprints),
        y: inst.map((d) => d.mature_pub_rate_pct),
        text: inst.map((d, i) => (i < 10 ? d.institution.slice(0, 22) : "")),
        textposition: "top center",
        textfont: { size: 10 },
        marker: {
          size: inst.map((d) => Math.max(10, Math.min(34, d.median_authors * 2.5))),
          color: inst.map((d) => d.mature_pub_rate_pct),
          colorscale: "Viridis",
          opacity: 0.85,
          line: { width: 1, color: "#ffffff" },
        },
        customdata: inst.map((d) => [d.institution, d.mature_preprints, d.median_authors]),
        hovertemplate: "<b>%{customdata[0]}</b><br>Total Preprints: %{x:,}<br>Mature Pub Rate: %{y}% (n=%{customdata[1]:,})<br>Median Authors: %{customdata[2]}<extra></extra>",
      }], baseLayout({
        xaxis: { title: "Total Preprints (log scale)", type: "log", gridcolor: c.grid },
        yaxis: { title: "Mature Cohort Publication Rate (%)", gridcolor: c.grid },
      }), PLOT_CFG);

      renderInstTable("");

      const p = DATA.dataset_provenance || {};
      const a = DATA.quality_audit || {};
      const provCards = [
        { label: "Active Snapshot ID", value: p.version_id || "Local", sub: `SHA-256: ${p.content_sha256 || "—"}` },
        { label: "Raw CSV Rows", value: fmtInt(a.raw_row_count), sub: `${fmtInt(a.duplicate_version_rows)} multi-version rows` },
        { label: "Canonical Parquet", value: `${a.canonical_parquet_mb ?? "—"} MiB`, sub: "449k unique DOIs (zstd)" },
        { label: "Versions Parquet", value: `${a.versions_parquet_mb ?? "—"} MiB`, sub: "Full cleaned postings (zstd)" },
      ];
      document.getElementById("provenance-grid").innerHTML = provCards
        .map((x) => `<div class="kpi-card"><div class="kpi-label">${x.label}</div><div class="kpi-value" style="font-size:1.15rem">${x.value}</div><div class="kpi-sub">${x.sub}</div></div>`)
        .join("");
    }

    function renderInstTable(query) {
      const q = (query || "").trim().toLowerCase();
      const rows = DATA.top_institutions.filter((d) => !q || d.institution.toLowerCase().includes(q));
      document.getElementById("inst-tbody").innerHTML = rows
        .map(
          (d, idx) => `<tr>
            <td>${idx + 1}</td>
            <td>${d.institution}</td>
            <td class="num">${fmtInt(d.total_preprints)}</td>
            <td class="num">${fmtInt(d.mature_preprints)}</td>
            <td class="num">${fmtPct(d.mature_pub_rate_pct)}</td>
            <td class="num">${d.median_authors}</td>
          </tr>`
        )
        .join("");
    }

    function renderActiveTab() {
      renderKPIs();
      if (currentTab === "overview") renderOverview();
      else if (currentTab === "peer-review") renderPeerReview();
      else if (currentTab === "frontiers") renderFrontiers();
      else if (currentTab === "open-science") renderOpenScience();
      else if (currentTab === "institutions") renderInstitutions();
    }

    async function init() {
      const inlineEl = document.getElementById("dashboard-data");
      if (inlineEl && inlineEl.textContent && !inlineEl.textContent.includes("__DASHBOARD_JSON__")) {
        DATA = JSON.parse(inlineEl.textContent);
      } else {
        const resp = await fetch("./data/dashboard_data.json");
        DATA = await resp.json();
      }

      const p = DATA.dataset_provenance || {};
      const kAll = DATA.kpis.all;
      document.getElementById("header-subtitle").textContent =
        `${fmtInt(kAll.unique_dois)} unique preprints (${kAll.date_min} to ${kAll.date_max}) • Snapshot ${p.version_id || "local"}`;
      document.getElementById("footer-provenance").textContent =
        `Dataset: ${p.dataset_handle || "m0delcrafter/bio-and-med-dataset"} • Snapshot: ${p.version_id || "local"} • Built: ${DATA.generated_at_utc.slice(0, 19).replace("T", " ")} UTC`;

      document.querySelectorAll(".segmented button").forEach((btn) => {
        btn.addEventListener("click", () => {
          document.querySelectorAll(".segmented button").forEach((b) => b.classList.remove("active"));
          btn.classList.add("active");
          currentServer = btn.dataset.server;
          renderActiveTab();
        });
      });

      document.querySelectorAll(".tab-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
          document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
          btn.classList.add("active");
          currentTab = btn.dataset.tab;
          document.getElementById(`tab-${currentTab}`).classList.add("active");
          renderActiveTab();
        });
      });

      document.getElementById("inst-search").addEventListener("input", (e) => {
        renderInstTable(e.target.value);
      });

      document.getElementById("theme-toggle").addEventListener("click", (e) => {
        const html = document.documentElement;
        const next = html.getAttribute("data-theme") === "dark" ? "light" : "dark";
        html.setAttribute("data-theme", next);
        e.target.textContent = next === "dark" ? "Light Mode" : "Dark Mode";
        renderActiveTab();
      });

      renderActiveTab();
    }

    init();
  </script>
</body>
</html>
"""


def write_static_dashboard(
    payload: dict[str, Any],
    output_dir: Path,
    *,
    inline_data: bool = True,
) -> tuple[Path, Path]:
    """Write ``docs/data/dashboard_data.json`` and ``docs/index.html``.

    Inlining the compact JSON into ``index.html`` guarantees the dashboard works
    both on GitHub Pages (HTTPS) and when opened directly from disk (``file://``).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    json_path = data_dir / "dashboard_data.json"
    json_text = json.dumps(payload, indent=2)
    json_path.write_text(json_text + "\n", encoding="utf-8")

    # Escape </script> defensively if it ever appears inside an institution name
    compact_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html_text = DASHBOARD_HTML_TEMPLATE.replace(
        "__DASHBOARD_JSON__", compact_json if inline_data else ""
    )

    html_path = output_dir / "index.html"
    html_path.write_text(html_text, encoding="utf-8")

    # Prevent GitHub Pages Jekyll processing from interfering with files
    (output_dir / ".nojekyll").touch(exist_ok=True)
    return html_path, json_path
