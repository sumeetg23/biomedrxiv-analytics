/* BioRxiv & MedRxiv Preprint Analytics — Static Web Dashboard Controller */

let DATA = null;
let currentServer = "all";
let currentTab = "overview";

const PLOT_CFG = { responsive: true, displaylogo: false };

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
      font: {
        color: c.text,
        family: "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif",
        size: 12,
      },
      margin: { l: 58, r: 24, t: 24, b: 48 },
      xaxis: { gridcolor: c.grid, zerolinecolor: c.grid },
      yaxis: { gridcolor: c.grid, zerolinecolor: c.grid },
      legend: { orientation: "h", y: 1.08, x: 0 },
      hovermode: "closest",
    },
    extra
  );
}

function renderKPIs() {
  const k = DATA.kpis[currentServer];
  const cards = [
    {
      label: "Unique Preprints (DOIs)",
      value: fmtInt(k.unique_dois),
      sub: `${fmtInt(k.version_rows)} total version postings`,
    },
    {
      label: `Mature Pub Rate (≤ ${k.mature_cohort_cutoff_year})`,
      value: fmtPct(k.mature_pub_rate_pct),
      sub: `Overall snapshot rate: ${fmtPct(k.overall_pub_rate_pct)}`,
    },
    {
      label: "Date Coverage",
      value: `${k.date_min.slice(0, 4)} – ${k.date_max.slice(0, 4)}`,
      sub: `${k.date_min} to ${k.date_max}`,
    },
    {
      label: "Avg Versions / Paper",
      value: k.avg_versions.toFixed(2),
      sub: `Max versions on a DOI: ${k.max_versions}`,
    },
    {
      label: "Median Team Size",
      value: `${k.median_authors} authors`,
      sub: `${k.unique_categories} canonical categories`,
    },
    {
      label: "Withdrawn Preprints",
      value: fmtInt(k.withdrawn_count),
      sub: `${k.withdrawn_per_1k.toFixed(2)} per 1,000 DOIs`,
    },
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
      x: mg.months,
      y: mg.biorxiv,
      name: "bioRxiv",
      type: "scatter",
      mode: "lines",
      stackgroup: currentServer === "all" ? "one" : undefined,
      line: { width: 1.5, color: c.biorxiv },
    });
  }
  if (currentServer === "all" || currentServer === "medrxiv") {
    traces.push({
      x: mg.months,
      y: mg.medrxiv,
      name: "medRxiv",
      type: "scatter",
      mode: "lines",
      stackgroup: currentServer === "all" ? "one" : undefined,
      line: { width: 1.5, color: c.medrxiv },
    });
  }
  Plotly.newPlot(
    "chart-monthly",
    traces,
    baseLayout({
      hovermode: "x unified",
      yaxis: { title: "Unique Preprints / Month", gridcolor: c.grid },
      xaxis: { title: "First Posting Month", gridcolor: c.grid },
      shapes: [
        {
          type: "line",
          x0: "2019-06",
          x1: "2019-06",
          y0: 0,
          y1: 1,
          yref: "paper",
          line: { dash: "dot", color: c.muted, width: 1.3 },
        },
        {
          type: "line",
          x0: "2020-05",
          x1: "2020-05",
          y0: 0,
          y1: 1,
          yref: "paper",
          line: { dash: "dot", color: c.danger, width: 1.3 },
        },
      ],
      annotations: [
        {
          x: "2019-06",
          y: 0.96,
          yref: "paper",
          text: "medRxiv launch",
          showarrow: false,
          xanchor: "right",
          font: { size: 11, color: c.muted },
        },
        {
          x: "2020-05",
          y: 0.88,
          yref: "paper",
          text: "COVID-19 surge",
          showarrow: false,
          xanchor: "left",
          font: { size: 11, color: c.danger },
        },
      ],
    }),
    PLOT_CFG
  );

  // 2. Top categories
  const cats = [...DATA.top_categories[currentServer]].reverse();
  Plotly.newPlot(
    "chart-categories",
    [
      {
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
        hovertemplate:
          "<b>%{y}</b><br>Unique DOIs: %{x:,}<br>Mature Pub Rate: %{customdata[0]}%<br>Median Authors: %{customdata[1]}<br>Avg Versions: %{customdata[2]}<extra></extra>",
      },
    ],
    baseLayout({
      margin: { l: 195, r: 20, t: 20, b: 48 },
      xaxis: { title: "Unique Preprints (DOIs)", gridcolor: c.grid },
      yaxis: { automargin: true },
    }),
    PLOT_CFG
  );

  // 3. Annual volume + publication rate
  const ann = DATA.annual_summary;
  const barY = currentServer === "all" ? ann.total : ann[currentServer];
  Plotly.newPlot(
    "chart-annual",
    [
      {
        x: ann.years,
        y: barY,
        name: "Unique Preprints",
        type: "bar",
        marker: { color: currentServer === "medrxiv" ? c.medrxiv : c.biorxiv, opacity: 0.82 },
      },
      {
        x: ann.years,
        y: ann.pub_rate_pct,
        name: "All-Server Snapshot Pub %",
        type: "scatter",
        mode: "lines+markers",
        yaxis: "y2",
        line: { color: c.accent3, width: 2.5 },
      },
    ],
    baseLayout({
      hovermode: "x unified",
      xaxis: { dtick: 1, gridcolor: c.grid },
      yaxis: { title: "Unique Preprints", gridcolor: c.grid },
      yaxis2: {
        title: "Snapshot Published (%)",
        overlaying: "y",
        side: "right",
        range: [0, 90],
        showgrid: false,
      },
    }),
    PLOT_CFG
  );
}

function renderPeerReview() {
  const c = themeColors();
  const h = DATA.cohort_heatmap;
  Plotly.newPlot(
    "chart-heatmap",
    [
      {
        type: "heatmap",
        x: h.years,
        y: [...h.categories].reverse(),
        z: [...h.pub_rate_pct].reverse(),
        customdata: [...h.counts].reverse(),
        colorscale: "YlGnBu",
        zmin: 0,
        zmax: 85,
        hovertemplate:
          "<b>%{y}</b> (%{x})<br>Published: %{z}%<br>Preprints: %{customdata:,}<extra></extra>",
        colorbar: { title: "Published %" },
      },
    ],
    baseLayout({
      margin: { l: 190, r: 24, t: 20, b: 48 },
      xaxis: { dtick: 1, title: "First Posting Year" },
      yaxis: { automargin: true },
    }),
    PLOT_CFG
  );

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
      hovertemplate:
        "<b>%{fullData.name} — %{x}</b><br>Published: %{y}% (95% CI: %{customdata[1]}–%{customdata[2]}%)<br>n = %{customdata[0]:,}<extra></extra>",
    };
  });
  Plotly.newPlot(
    "chart-revisions",
    revTraces,
    baseLayout({
      barmode: "group",
      yaxis: { title: "Peer-Reviewed Publication Rate (%)", range: [0, 100], gridcolor: c.grid },
      xaxis: { title: "Number of Preprint Versions Posted" },
    }),
    PLOT_CFG
  );

  // Top Publishers
  const pubs = [...DATA.top_publishers[currentServer]].reverse();
  Plotly.newPlot(
    "chart-publishers",
    [
      {
        type: "bar",
        orientation: "h",
        y: pubs.map((p) => p.publisher),
        x: pubs.map((p) => p.count),
        marker: { color: currentServer === "medrxiv" ? c.medrxiv : c.biorxiv },
        customdata: pubs.map((p) => p.share_pct),
        hovertemplate:
          "<b>%{y}</b><br>Published Papers: %{x:,} (%{customdata}% of published)<extra></extra>",
      },
    ],
    baseLayout({
      margin: { l: 220, r: 24, t: 20, b: 48 },
      xaxis: { title: "Published Preprints", gridcolor: c.grid },
      yaxis: { automargin: true },
    }),
    PLOT_CFG
  );

  // Type Bias
  const tb = DATA.type_bias;
  Plotly.newPlot(
    "chart-type-bias",
    [
      {
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
        hovertemplate:
          "<b>%{x}</b><br>Published: %{y}% (95% CI: %{customdata[1]}–%{customdata[2]}%)<br>n = %{customdata[0]:,}<extra></extra>",
      },
    ],
    baseLayout({
      yaxis: { title: "Mature Publication Rate (%)", range: [0, 80], gridcolor: c.grid },
    }),
    PLOT_CFG
  );
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
    hovertemplate:
      "<b>%{fullData.name}</b><br>%{x}: %{y:.2f}% (%{customdata:,} papers)<extra></extra>",
  }));
  Plotly.newPlot(
    "chart-frontiers",
    traces,
    baseLayout({
      hovermode: "x unified",
      xaxis: { title: "Quarter", nticks: 14, gridcolor: c.grid },
      yaxis: { title: "% of Quarterly Preprints Mentioning Topic", gridcolor: c.grid },
    }),
    PLOT_CFG
  );

  const sorted = [...ft.series].sort((a, b) => a.total_papers - b.total_papers);
  Plotly.newPlot(
    "chart-frontiers-bar",
    [
      {
        type: "bar",
        orientation: "h",
        y: sorted.map((s) => s.label),
        x: sorted.map((s) => s.total_papers),
        marker: { color: c.biorxiv },
        hovertemplate: "<b>%{y}</b><br>Total Papers (2015–2026): %{x:,}<extra></extra>",
      },
    ],
    baseLayout({
      margin: { l: 210, r: 24, t: 20, b: 48 },
      xaxis: { title: "Unique Preprints Mentioning Term", gridcolor: c.grid },
    }),
    PLOT_CFG
  );
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
  Plotly.newPlot(
    "chart-licenses",
    licTraces,
    baseLayout({
      hovermode: "x unified",
      xaxis: { dtick: 1, title: "Posting Year", gridcolor: c.grid },
      yaxis: { title: "Share of Preprints (%)", range: [0, 100], gridcolor: c.grid },
    }),
    PLOT_CFG
  );

  // Team size trend
  const tt = DATA.team_trends;
  const teamTraces = [];
  for (const srv of ["biorxiv", "medrxiv"]) {
    const col = srv === "biorxiv" ? c.biorxiv : c.medrxiv;
    teamTraces.push({
      x: tt.years,
      y: tt[srv].median,
      name: `${srv === "biorxiv" ? "bioRxiv" : "medRxiv"} Median`,
      type: "scatter",
      mode: "lines+markers",
      line: { width: 2.5, color: col },
    });
    teamTraces.push({
      x: tt.years,
      y: tt[srv].q75,
      name: `${srv === "biorxiv" ? "bioRxiv" : "medRxiv"} 75th Pct`,
      type: "scatter",
      mode: "lines",
      line: { width: 1.2, dash: "dot", color: col },
    });
  }
  Plotly.newPlot(
    "chart-team-trend",
    teamTraces,
    baseLayout({
      hovermode: "x unified",
      xaxis: { dtick: 1, gridcolor: c.grid },
      yaxis: { title: "Authors per Preprint", gridcolor: c.grid },
    }),
    PLOT_CFG
  );

  // Team size vs Pub rate
  const tvp = DATA.team_vs_pub;
  Plotly.newPlot(
    "chart-team-pub",
    [
      {
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
        hovertemplate:
          "<b>%{x} authors</b><br>Published: %{y}% (95% CI: %{customdata[1]}–%{customdata[2]}%)<br>n = %{customdata[0]:,}<extra></extra>",
      },
    ],
    baseLayout({
      xaxis: { title: "Author Team Size" },
      yaxis: { title: "Mature Publication Rate (%)", range: [0, 85], gridcolor: c.grid },
    }),
    PLOT_CFG
  );

  // Withdrawals timeline
  const wd = DATA.withdrawals_timeline;
  Plotly.newPlot(
    "chart-withdrawals",
    [
      {
        x: wd.periods,
        y: wd.biorxiv.rate_per_1k,
        name: "bioRxiv",
        type: "scatter",
        mode: "lines+markers",
        line: { color: c.biorxiv, width: 2.3 },
        customdata: wd.biorxiv.count,
        hovertemplate: "bioRxiv %{x}: %{y} / 1k (%{customdata} withdrawn)<extra></extra>",
      },
      {
        x: wd.periods,
        y: wd.medrxiv.rate_per_1k,
        name: "medRxiv",
        type: "scatter",
        mode: "lines+markers",
        line: { color: c.medrxiv, width: 2.3 },
        customdata: wd.medrxiv.count,
        hovertemplate: "medRxiv %{x}: %{y} / 1k (%{customdata} withdrawn)<extra></extra>",
      },
    ],
    baseLayout({
      hovermode: "x unified",
      xaxis: { title: "Half-Year Cohort", gridcolor: c.grid },
      yaxis: { title: "Withdrawn per 1,000 Preprints", gridcolor: c.grid },
    }),
    PLOT_CFG
  );
}

function renderInstitutions() {
  const c = themeColors();
  const inst = DATA.top_institutions;
  Plotly.newPlot(
    "chart-institutions",
    [
      {
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
        hovertemplate:
          "<b>%{customdata[0]}</b><br>Total Preprints: %{x:,}<br>Mature Pub Rate: %{y}% (n=%{customdata[1]:,})<br>Median Authors: %{customdata[2]}<extra></extra>",
      },
    ],
    baseLayout({
      xaxis: { title: "Total Preprints (log scale)", type: "log", gridcolor: c.grid },
      yaxis: { title: "Mature Cohort Publication Rate (%)", gridcolor: c.grid },
    }),
    PLOT_CFG
  );

  renderInstTable("");

  const p = DATA.dataset_provenance || {};
  const a = DATA.quality_audit || {};
  const provCards = [
    {
      label: "Active Snapshot ID",
      value: p.version_id || "Local",
      sub: `SHA-256: ${p.content_sha256 || "—"}`,
    },
    {
      label: "Raw CSV Rows",
      value: fmtInt(a.raw_row_count),
      sub: `${fmtInt(a.duplicate_version_rows)} multi-version rows`,
    },
    {
      label: "Canonical Parquet",
      value: `${a.canonical_parquet_mb ?? "—"} MiB`,
      sub: "449k unique DOIs (zstd)",
    },
    {
      label: "Versions Parquet",
      value: `${a.versions_parquet_mb ?? "—"} MiB`,
      sub: "Full cleaned postings (zstd)",
    },
  ];
  document.getElementById("provenance-grid").innerHTML = provCards
    .map(
      (x) => `<div class="kpi-card">
        <div class="kpi-label">${x.label}</div>
        <div class="kpi-value kpi-value-sm">${x.value}</div>
        <div class="kpi-sub">${x.sub}</div>
      </div>`
    )
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
  try {
    const resp = await fetch("./data/dashboard_data.json");
    if (!resp.ok) throw new Error(`HTTP ${resp.status} loading ./data/dashboard_data.json`);
    DATA = await resp.json();
  } catch (err) {
    const errBox = document.getElementById("load-error");
    errBox.style.display = "block";
    errBox.innerHTML =
      `<strong>Could not fetch <code>./data/dashboard_data.json</code>.</strong> ` +
      `If viewing locally, start the static server with <code>python3 web/serve.py</code> ` +
      `(browsers block <code>fetch()</code> over <code>file://</code>). Error: ${err.message}`;
    return;
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

window.addEventListener("DOMContentLoaded", init);
