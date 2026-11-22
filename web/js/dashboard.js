/* BioRxiv & MedRxiv Preprint Analytics — Static Web Dashboard Controller */

let DATA = null;
let currentServer = "all";
let currentTab = "overview";
let startYear = null;
let endYear = null;

const PLOT_CFG = { responsive: true, displaylogo: false };
const REVISION_ORDER = ["1", "2", "3", "4", "5+"];
const TEAM_BUCKET_ORDER = ["1 (Solo)", "2–3", "4–6", "7–10", "11–20", "21+"];
const TYPE_ORDER = ["New Results", "Confirmatory Results", "Contradictory Results"];

const fmtInt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
const fmtPct = (n) => (n == null ? "—" : `${Number(n).toFixed(1)}%`);

function wilsonCiPct(successes, total, z = 1.959964) {
  if (!total || total <= 0) return { rate: 0, low: 0, high: 0 };
  const p = successes / total;
  const denom = 1 + (z * z) / total;
  const center = (p + (z * z) / (2 * total)) / denom;
  const margin = (z / denom) * Math.sqrt((p * (1 - p) + (z * z) / (4 * total)) / total);
  return {
    rate: +(p * 100).toFixed(2),
    low: +(Math.max(0, center - margin) * 100).toFixed(2),
    high: +(Math.min(1, center + margin) * 100).toFixed(2),
  };
}

function medianFromHist(hist) {
  const entries = Object.entries(hist)
    .map(([k, v]) => [Number(k), Number(v)])
    .filter(([, v]) => v > 0)
    .sort((a, b) => a[0] - b[0]);
  const total = entries.reduce((acc, [, v]) => acc + v, 0);
  if (total === 0) return 0;
  const target1 = Math.floor((total - 1) / 2);
  const target2 = Math.floor(total / 2);
  let cum = 0;
  let v1 = null;
  let v2 = null;
  for (const [val, cnt] of entries) {
    cum += cnt;
    if (v1 === null && cum > target1) v1 = val;
    if (v2 === null && cum > target2) {
      v2 = val;
      break;
    }
  }
  return +(((v1 ?? 0) + (v2 ?? 0)) / 2).toFixed(1);
}

function selectedServers() {
  return currentServer === "all" ? ["biorxiv", "medrxiv"] : [currentServer];
}

function selectedYears() {
  return DATA.available_years.filter((y) => y >= startYear && y <= endYear);
}

function yearRangeLabel() {
  return startYear === endYear ? `${startYear}` : `${startYear} – ${endYear}`;
}

function isFullRange() {
  const yrs = DATA.available_years;
  return startYear === yrs[0] && endYear === yrs[yrs.length - 1];
}

/**
 * Aggregate additive year-partitioned slices for [startYear, endYear] & server(s).
 */
function aggregateWindow(servers = selectedServers(), yrMin = startYear, yrMax = endYear) {
  const matureCutoff = DATA.mature_cohort_cutoff_year ?? 2024;
  let uniqueDois = 0;
  let versionRows = 0;
  let publishedCount = 0;
  let matureDois = 0;
  let maturePublished = 0;
  let withdrawnCount = 0;
  let versionSum = 0;
  let maxVersions = 0;
  let dateMin = "";
  let dateMax = "";
  const authorHist = {};

  const categories = {}; // cat -> [cnt, pub, vsum, asum, matCnt, matPub]
  const publishers = {}; // pubName -> cnt
  const revisions = {}; // bucket -> [cnt, pub]
  const teamBuckets = {}; // bucket -> [cnt, pub]
  const types = {}; // type -> [cnt, pub]
  const institutions = {}; // inst -> [cnt, pub, asum, matCnt, matPub]
  const countries = {}; // country -> cnt

  for (const y of DATA.available_years) {
    if (y < yrMin || y > yrMax) continue;
    const yrBlock = DATA.by_year[String(y)];
    if (!yrBlock) continue;
    const isMature = y <= matureCutoff;

    for (const srv of servers) {
      const s = yrBlock[srv];
      if (!s) continue;
      const k = s.kpi;

      uniqueDois += k.unique_dois;
      versionRows += k.version_rows;
      publishedCount += k.published_count;
      withdrawnCount += k.withdrawn_count;
      versionSum += k.version_sum;
      if (k.max_versions > maxVersions) maxVersions = k.max_versions;
      if (k.date_min && (!dateMin || k.date_min < dateMin)) dateMin = k.date_min;
      if (k.date_max && (!dateMax || k.date_max > dateMax)) dateMax = k.date_max;

      if (isMature) {
        matureDois += k.unique_dois;
        maturePublished += k.published_count;
      }

      for (const [ak, av] of Object.entries(k.author_hist || {})) {
        authorHist[ak] = (authorHist[ak] || 0) + av;
      }

      for (const [cat, arr] of Object.entries(s.categories || {})) {
        const cur = categories[cat] || [0, 0, 0, 0, 0, 0];
        cur[0] += arr[0];
        cur[1] += arr[1];
        cur[2] += arr[2];
        cur[3] += arr[3];
        if (isMature) {
          cur[4] += arr[0];
          cur[5] += arr[1];
        }
        categories[cat] = cur;
      }

      for (const [pubName, cnt] of Object.entries(s.publishers || {})) {
        publishers[pubName] = (publishers[pubName] || 0) + cnt;
      }

      for (const [b, arr] of Object.entries(s.revisions || {})) {
        const cur = revisions[b] || [0, 0];
        cur[0] += arr[0];
        cur[1] += arr[1];
        revisions[b] = cur;
      }

      for (const [b, arr] of Object.entries(s.team_buckets || {})) {
        const cur = teamBuckets[b] || [0, 0];
        cur[0] += arr[0];
        cur[1] += arr[1];
        teamBuckets[b] = cur;
      }

      for (const [t, arr] of Object.entries(s.types || {})) {
        const cur = types[t] || [0, 0];
        cur[0] += arr[0];
        cur[1] += arr[1];
        types[t] = cur;
      }

      for (const [inst, arr] of Object.entries(s.institutions || {})) {
        const cur = institutions[inst] || [0, 0, 0, 0, 0];
        cur[0] += arr[0];
        cur[1] += arr[1];
        cur[2] += arr[2];
        if (isMature) {
          cur[3] += arr[0];
          cur[4] += arr[1];
        }
        institutions[inst] = cur;
      }

      for (const [country, cnt] of Object.entries(s.countries || {})) {
        countries[country] = (countries[country] || 0) + cnt;
      }
    }
  }

  return {
    kpi: {
      unique_dois: uniqueDois,
      version_rows: versionRows,
      published_count: publishedCount,
      pub_rate_pct: uniqueDois > 0 ? +((publishedCount / uniqueDois) * 100).toFixed(2) : 0,
      mature_dois: matureDois,
      mature_pub_rate_pct: matureDois > 0 ? +((maturePublished / matureDois) * 100).toFixed(2) : null,
      mature_cutoff: matureCutoff,
      median_authors: medianFromHist(authorHist),
      avg_versions: uniqueDois > 0 ? +(versionSum / uniqueDois).toFixed(2) : 0,
      max_versions: maxVersions,
      withdrawn_count: withdrawnCount,
      withdrawn_per_1k: uniqueDois > 0 ? +((withdrawnCount / uniqueDois) * 1000).toFixed(2) : 0,
      unique_categories: Object.keys(categories).length,
      date_min: dateMin || `${yrMin}`,
      date_max: dateMax || `${yrMax}`,
    },
    categories,
    publishers,
    revisions,
    teamBuckets,
    types,
    institutions,
    countries,
  };
}

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

function renderKPIs(agg) {
  const k = agg.kpi;
  const pubCard =
    k.mature_pub_rate_pct !== null && endYear > k.mature_cutoff
      ? {
          label: `Pub Rate (${yearRangeLabel()})`,
          value: fmtPct(k.pub_rate_pct),
          sub: `Mature (≤${k.mature_cutoff}) in window: ${fmtPct(k.mature_pub_rate_pct)}`,
        }
      : {
          label: `Peer-Review Pub Rate (${yearRangeLabel()})`,
          value: fmtPct(k.pub_rate_pct),
          sub: `${fmtInt(k.published_count)} published DOIs`,
        };

  const cards = [
    {
      label: `Unique Preprints (${yearRangeLabel()})`,
      value: fmtInt(k.unique_dois),
      sub: `${fmtInt(k.version_rows)} total version postings`,
    },
    pubCard,
    {
      label: "Active Date Window",
      value: yearRangeLabel(),
      sub: k.unique_dois > 0 ? `${k.date_min} to ${k.date_max}` : "No preprints in range",
    },
    {
      label: "Avg Versions / Paper",
      value: k.avg_versions.toFixed(2),
      sub: `Max versions on a DOI: ${k.max_versions}`,
    },
    {
      label: "Median Team Size",
      value: k.median_authors >= 31 ? "31+ authors" : `${k.median_authors} authors`,
      sub: `${k.unique_categories} active categories`,
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

function renderOverview(agg) {
  const c = themeColors();
  const a = DATA.quality_audit;
  const biasPp = (a.raw_row_publication_rate_pct - a.dedup_doi_publication_rate_pct).toFixed(2);
  const filterNote = isFullRange()
    ? ""
    : ` Currently viewing <strong>${yearRangeLabel()}</strong> (${fmtInt(agg.kpi.unique_dois)} unique DOIs).`;
  document.getElementById("audit-callout").innerHTML =
    `<strong>Data Quality Guardrails Applied:</strong> Deduplicated <strong>${fmtInt(a.raw_row_count)}</strong> version rows into <strong>${fmtInt(a.unique_doi_count)}</strong> unique DOIs (preventing a <strong>+${biasPp} pp</strong> upward bias in publication rate), normalized <strong>${a.raw_unique_categories}</strong> messy category strings down to <strong>${a.clean_unique_categories}</strong> canonical fields, and flagged <strong>${fmtInt(a.withdrawn_unique_dois)}</strong> withdrawn papers.${filterNote}`;

  // 1. Monthly growth filtered by [startYear, endYear]
  const mg = DATA.monthly_growth;
  const mIdx = [];
  for (let i = 0; i < mg.months.length; i++) {
    const yr = Number(mg.months[i].slice(0, 4));
    if (yr >= startYear && yr <= endYear) mIdx.push(i);
  }
  const fMonths = mIdx.map((i) => mg.months[i]);
  const fBio = mIdx.map((i) => mg.biorxiv[i]);
  const fMed = mIdx.map((i) => mg.medrxiv[i]);

  const traces = [];
  if (currentServer === "all" || currentServer === "biorxiv") {
    traces.push({
      x: fMonths,
      y: fBio,
      name: "bioRxiv",
      type: "scatter",
      mode: "lines",
      stackgroup: currentServer === "all" ? "one" : undefined,
      line: { width: 1.5, color: c.biorxiv },
    });
  }
  if (currentServer === "all" || currentServer === "medrxiv") {
    traces.push({
      x: fMonths,
      y: fMed,
      name: "medRxiv",
      type: "scatter",
      mode: "lines",
      stackgroup: currentServer === "all" ? "one" : undefined,
      line: { width: 1.5, color: c.medrxiv },
    });
  }

  const shapes = [];
  const annotations = [];
  if (startYear <= 2019 && endYear >= 2019) {
    shapes.push({
      type: "line",
      x0: "2019-06",
      x1: "2019-06",
      y0: 0,
      y1: 1,
      yref: "paper",
      line: { dash: "dot", color: c.muted, width: 1.3 },
    });
    annotations.push({
      x: "2019-06",
      y: 0.96,
      yref: "paper",
      text: "medRxiv launch",
      showarrow: false,
      xanchor: "right",
      font: { size: 11, color: c.muted },
    });
  }
  if (startYear <= 2020 && endYear >= 2020) {
    shapes.push({
      type: "line",
      x0: "2020-05",
      x1: "2020-05",
      y0: 0,
      y1: 1,
      yref: "paper",
      line: { dash: "dot", color: c.danger, width: 1.3 },
    });
    annotations.push({
      x: "2020-05",
      y: 0.88,
      yref: "paper",
      text: "COVID-19 surge",
      showarrow: false,
      xanchor: "left",
      font: { size: 11, color: c.danger },
    });
  }

  Plotly.newPlot(
    "chart-monthly",
    traces,
    baseLayout({
      hovermode: "x unified",
      yaxis: { title: "Unique Preprints / Month", gridcolor: c.grid },
      xaxis: { title: `First Posting Month (${yearRangeLabel()})`, gridcolor: c.grid },
      shapes,
      annotations,
    }),
    PLOT_CFG
  );

  // 2. Top 20 categories dynamically aggregated for [startYear, endYear]
  const cats = Object.entries(agg.categories)
    .map(([category, [cnt, pub, vsum, asum]]) => ({
      category,
      count: cnt,
      pub_pct: cnt > 0 ? +((pub / cnt) * 100).toFixed(1) : 0,
      avg_versions: cnt > 0 ? +(vsum / cnt).toFixed(2) : 0,
      avg_authors: cnt > 0 ? +(asum / cnt).toFixed(1) : 0,
    }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 20)
    .reverse();

  Plotly.newPlot(
    "chart-categories",
    [
      {
        type: "bar",
        orientation: "h",
        y: cats.map((d) => d.category),
        x: cats.map((d) => d.count),
        marker: {
          color: cats.map((d) => d.pub_pct),
          colorscale: "Teal",
          cmin: 0,
          cmax: 80,
          colorbar: { title: "Pub %", thickness: 12 },
        },
        customdata: cats.map((d) => [d.pub_pct, d.avg_authors, d.avg_versions]),
        hovertemplate:
          "<b>%{y}</b><br>Unique DOIs: %{x:,}<br>Pub Rate (window): %{customdata[0]}%<br>Avg Authors: %{customdata[1]}<br>Avg Versions: %{customdata[2]}<extra></extra>",
      },
    ],
    baseLayout({
      margin: { l: 195, r: 20, t: 20, b: 48 },
      xaxis: { title: `Unique Preprints (${yearRangeLabel()})`, gridcolor: c.grid },
      yaxis: { automargin: true },
    }),
    PLOT_CFG
  );

  // 3. Annual volume + server-matched publication rate
  const ann = DATA.annual_summary;
  const aIdx = [];
  for (let i = 0; i < ann.years.length; i++) {
    if (ann.years[i] >= startYear && ann.years[i] <= endYear) aIdx.push(i);
  }
  const fYears = aIdx.map((i) => ann.years[i]);
  const barY = aIdx.map((i) => (currentServer === "all" ? ann.total[i] : ann[currentServer][i]));
  const pubSeries =
    currentServer === "biorxiv"
      ? ann.biorxiv_pub_rate_pct
      : currentServer === "medrxiv"
      ? ann.medrxiv_pub_rate_pct
      : ann.pub_rate_pct;
  const lineY = aIdx.map((i) => pubSeries[i]);

  Plotly.newPlot(
    "chart-annual",
    [
      {
        x: fYears,
        y: barY,
        name: "Unique Preprints",
        type: "bar",
        marker: { color: currentServer === "medrxiv" ? c.medrxiv : c.biorxiv, opacity: 0.82 },
      },
      {
        x: fYears,
        y: lineY,
        name: "Snapshot Pub %",
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

function renderPeerReview(agg) {
  const c = themeColors();
  const h = DATA.cohort_heatmap;
  const yIndices = [];
  for (let i = 0; i < h.years.length; i++) {
    if (h.years[i] >= startYear && h.years[i] <= endYear) yIndices.push(i);
  }
  const fYears = yIndices.map((i) => h.years[i]);
  const fZ = [...h.pub_rate_pct].reverse().map((row) => yIndices.map((i) => row[i]));
  const fCounts = [...h.counts].reverse().map((row) => yIndices.map((i) => row[i]));

  Plotly.newPlot(
    "chart-heatmap",
    [
      {
        type: "heatmap",
        x: fYears,
        y: [...h.categories].reverse(),
        z: fZ,
        customdata: fCounts,
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
      xaxis: { dtick: 1, title: `First Posting Year (${yearRangeLabel()})` },
      yaxis: { automargin: true },
    }),
    PLOT_CFG
  );

  // Revisions vs Pub rate (dynamically aggregated for [startYear, endYear] per server)
  const bioAgg = aggregateWindow(["biorxiv"]);
  const medAgg = aggregateWindow(["medrxiv"]);
  const revServers = currentServer === "all" ? ["biorxiv", "medrxiv"] : [currentServer];
  const revTraces = revServers.map((srv) => {
    const src = srv === "biorxiv" ? bioAgg.revisions : medAgg.revisions;
    const rows = REVISION_ORDER.map((b) => {
      const [cnt, pub] = src[b] || [0, 0];
      const ci = wilsonCiPct(pub, cnt);
      return { versions: b, count: cnt, rate: ci.rate, low: ci.low, high: ci.high };
    });
    return {
      type: "bar",
      name: srv === "biorxiv" ? "bioRxiv" : "medRxiv",
      x: rows.map((r) => `${r.versions} ver`),
      y: rows.map((r) => r.rate),
      error_y: {
        type: "data",
        symmetric: false,
        array: rows.map((r) => +(r.high - r.rate).toFixed(2)),
        arrayminus: rows.map((r) => +(r.rate - r.low).toFixed(2)),
      },
      marker: { color: srv === "biorxiv" ? c.biorxiv : c.medrxiv },
      customdata: rows.map((r) => [r.count, r.low, r.high]),
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
      xaxis: { title: `Number of Preprint Versions (${yearRangeLabel()})` },
    }),
    PLOT_CFG
  );

  // Top Publishers dynamically aggregated
  const totalPub = Math.max(agg.kpi.published_count, 1);
  const pubs = Object.entries(agg.publishers)
    .filter(([k]) => k !== "Other Publishers")
    .map(([publisher, count]) => ({
      publisher,
      count,
      share_pct: +((count / totalPub) * 100).toFixed(2),
    }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 15)
    .reverse();

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
          "<b>%{y}</b><br>Published Papers: %{x:,} (%{customdata}% of published in window)<extra></extra>",
      },
    ],
    baseLayout({
      margin: { l: 220, r: 24, t: 20, b: 48 },
      xaxis: { title: `Published Preprints (${yearRangeLabel()})`, gridcolor: c.grid },
      yaxis: { automargin: true },
    }),
    PLOT_CFG
  );

  // Type Bias (bioRxiv types in selected year window)
  const tb = TYPE_ORDER.map((t) => {
    const [cnt, pub] = bioAgg.types[t] || [0, 0];
    const ci = wilsonCiPct(pub, cnt);
    return { type: t, count: cnt, rate: ci.rate, low: ci.low, high: ci.high };
  });
  Plotly.newPlot(
    "chart-type-bias",
    [
      {
        type: "bar",
        x: tb.map((r) => r.type),
        y: tb.map((r) => r.rate),
        error_y: {
          type: "data",
          symmetric: false,
          array: tb.map((r) => +(r.high - r.rate).toFixed(2)),
          arrayminus: tb.map((r) => +(r.rate - r.low).toFixed(2)),
        },
        marker: { color: [c.biorxiv, c.medrxiv, c.accent3] },
        customdata: tb.map((r) => [r.count, r.low, r.high]),
        hovertemplate:
          "<b>%{x}</b><br>Published: %{y}% (95% CI: %{customdata[1]}–%{customdata[2]}%)<br>n = %{customdata[0]:,}<extra></extra>",
      },
    ],
    baseLayout({
      yaxis: { title: `Publication Rate (${yearRangeLabel()}, %)`, range: [0, 85], gridcolor: c.grid },
    }),
    PLOT_CFG
  );
}

function renderFrontiers() {
  const c = themeColors();
  const ft = DATA.frontier_trends;
  const qIdx = [];
  for (let i = 0; i < ft.quarters.length; i++) {
    const yr = Number(ft.quarters[i].slice(0, 4));
    if (yr >= startYear && yr <= endYear) qIdx.push(i);
  }
  const fQuarters = qIdx.map((i) => ft.quarters[i]);

  const traces = ft.series.map((s) => ({
    x: fQuarters,
    y: qIdx.map((i) => s.pct_by_quarter[i]),
    name: s.label,
    type: "scatter",
    mode: "lines",
    line: { width: 2.3 },
    customdata: qIdx.map((i) => s.count_by_quarter[i]),
    hovertemplate:
      "<b>%{fullData.name}</b><br>%{x}: %{y:.2f}% (%{customdata:,} papers)<extra></extra>",
  }));
  Plotly.newPlot(
    "chart-frontiers",
    traces,
    baseLayout({
      hovermode: "x unified",
      xaxis: { title: `Quarter (${yearRangeLabel()})`, nticks: 14, gridcolor: c.grid },
      yaxis: { title: "% of Quarterly Preprints Mentioning Topic", gridcolor: c.grid },
    }),
    PLOT_CFG
  );

  // Recompute bar chart totals for selected [startYear, endYear]
  const sorted = ft.series
    .map((s) => ({
      label: s.label,
      total: qIdx.reduce((acc, i) => acc + (s.count_by_quarter[i] || 0), 0),
    }))
    .sort((a, b) => a.total - b.total);

  Plotly.newPlot(
    "chart-frontiers-bar",
    [
      {
        type: "bar",
        orientation: "h",
        y: sorted.map((s) => s.label),
        x: sorted.map((s) => s.total),
        marker: { color: c.biorxiv },
        hovertemplate: `<b>%{y}</b><br>Papers (${yearRangeLabel()}): %{x:,}<extra></extra>`,
      },
    ],
    baseLayout({
      margin: { l: 210, r: 24, t: 20, b: 48 },
      xaxis: { title: `Unique Preprints Mentioning Term (${yearRangeLabel()})`, gridcolor: c.grid },
    }),
    PLOT_CFG
  );
}

function renderOpenScience(agg) {
  const c = themeColors();
  const lic = DATA.license_evolution;
  const lIdx = [];
  for (let i = 0; i < lic.years.length; i++) {
    if (lic.years[i] >= startYear && lic.years[i] <= endYear) lIdx.push(i);
  }
  const fLicYears = lIdx.map((i) => lic.years[i]);
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
    x: fLicYears,
    y: lIdx.map((i) => bySrv[k][i]),
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
      xaxis: { dtick: 1, title: `Posting Year (${yearRangeLabel()})`, gridcolor: c.grid },
      yaxis: { title: "Share of Preprints (%)", range: [0, 100], gridcolor: c.grid },
    }),
    PLOT_CFG
  );

  // Team size trend filtered by year
  const tt = DATA.team_trends;
  const tIdx = [];
  for (let i = 0; i < tt.years.length; i++) {
    if (tt.years[i] >= startYear && tt.years[i] <= endYear) tIdx.push(i);
  }
  const fTeamYears = tIdx.map((i) => tt.years[i]);
  const teamTraces = [];
  for (const srv of selectedServers()) {
    const col = srv === "biorxiv" ? c.biorxiv : c.medrxiv;
    teamTraces.push({
      x: fTeamYears,
      y: tIdx.map((i) => tt[srv].median[i]),
      name: `${srv === "biorxiv" ? "bioRxiv" : "medRxiv"} Median`,
      type: "scatter",
      mode: "lines+markers",
      line: { width: 2.5, color: col },
    });
    teamTraces.push({
      x: fTeamYears,
      y: tIdx.map((i) => tt[srv].q75[i]),
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

  // Team size vs Pub rate dynamically aggregated
  const tvp = TEAM_BUCKET_ORDER.map((b) => {
    const [cnt, pub] = agg.teamBuckets[b] || [0, 0];
    const ci = wilsonCiPct(pub, cnt);
    return { bucket: b, count: cnt, rate: ci.rate, low: ci.low, high: ci.high };
  });
  Plotly.newPlot(
    "chart-team-pub",
    [
      {
        type: "bar",
        x: tvp.map((r) => r.bucket),
        y: tvp.map((r) => r.rate),
        error_y: {
          type: "data",
          symmetric: false,
          array: tvp.map((r) => +(r.high - r.rate).toFixed(2)),
          arrayminus: tvp.map((r) => +(r.rate - r.low).toFixed(2)),
        },
        marker: { color: c.medrxiv },
        customdata: tvp.map((r) => [r.count, r.low, r.high]),
        hovertemplate:
          "<b>%{x} authors</b><br>Published: %{y}% (95% CI: %{customdata[1]}–%{customdata[2]}%)<br>n = %{customdata[0]:,}<extra></extra>",
      },
    ],
    baseLayout({
      xaxis: { title: `Author Team Size (${yearRangeLabel()})` },
      yaxis: { title: "Publication Rate (%)", range: [0, 85], gridcolor: c.grid },
    }),
    PLOT_CFG
  );

  // Withdrawals timeline filtered by year
  const wd = DATA.withdrawals_timeline;
  const wIdx = [];
  for (let i = 0; i < wd.periods.length; i++) {
    const yr = Number(wd.periods[i].slice(0, 4));
    if (yr >= startYear && yr <= endYear) wIdx.push(i);
  }
  const fPeriods = wIdx.map((i) => wd.periods[i]);
  const wdTraces = selectedServers().map((srv) => ({
    x: fPeriods,
    y: wIdx.map((i) => wd[srv].rate_per_1k[i]),
    name: srv === "biorxiv" ? "bioRxiv" : "medRxiv",
    type: "scatter",
    mode: "lines+markers",
    line: { color: srv === "biorxiv" ? c.biorxiv : c.medrxiv, width: 2.3 },
    customdata: wIdx.map((i) => wd[srv].count[i]),
    hovertemplate: `${srv === "biorxiv" ? "bioRxiv" : "medRxiv"} %{x}: %{y} / 1k (%{customdata} withdrawn)<extra></extra>`,
  }));
  Plotly.newPlot(
    "chart-withdrawals",
    wdTraces,
    baseLayout({
      hovermode: "x unified",
      xaxis: { title: `Half-Year Cohort (${yearRangeLabel()})`, gridcolor: c.grid },
      yaxis: { title: "Withdrawn per 1,000 Preprints", gridcolor: c.grid },
    }),
    PLOT_CFG
  );
}

let currentInstList = [];
let currentTrialRows = [];

function renderClinicalTrials() {
  const ct = DATA.clinical_trials;
  const callout = document.getElementById("ct-callout");
  if (!ct || !ct.by_year) {
    callout.innerHTML =
      "<strong>Clinical trials data not yet extracted.</strong> Run <code>.venv/bin/python scripts/check_clinical_trials.py extract</code> followed by <code>scripts/run_etl.py --skip-parquet</code>.";
    document.getElementById("ct-kpi-grid").innerHTML = "";
    return;
  }

  const c = themeColors();
  const servers = selectedServers();
  let linkedDois = 0;
  let pubTrialDois = 0;
  let uniqueTrials = 0;
  let enrichedNct = 0;
  let withResults = 0;
  let totalEnroll = 0;
  const phases = {};
  const statuses = {};
  const sponsors = {};
  const conditions = {};
  const registries = {};

  const ctYears = (ct.years || []).filter((y) => y >= startYear && y <= endYear);
  const yrBio = [];
  const yrMed = [];

  for (const yr of ctYears) {
    const blk = ct.by_year[String(yr)] || {};
    yrBio.push(blk.biorxiv ? blk.biorxiv.kpi.trial_linked_dois : 0);
    yrMed.push(blk.medrxiv ? blk.medrxiv.kpi.trial_linked_dois : 0);

    for (const srv of servers) {
      const s = blk[srv];
      if (!s) continue;
      linkedDois += s.kpi.trial_linked_dois;
      pubTrialDois += s.kpi.published_trial_dois;
      uniqueTrials += s.kpi.unique_trials;
      enrichedNct += s.kpi.enriched_nct_trials;
      withResults += s.kpi.trials_with_results;
      totalEnroll += s.kpi.total_enrollment;

      for (const [k, v] of Object.entries(s.phases || {})) phases[k] = (phases[k] || 0) + v;
      for (const [k, v] of Object.entries(s.statuses || {})) statuses[k] = (statuses[k] || 0) + v;
      for (const [k, v] of Object.entries(s.sponsor_classes || {})) sponsors[k] = (sponsors[k] || 0) + v;
      for (const [k, v] of Object.entries(s.conditions || {})) conditions[k] = (conditions[k] || 0) + v;
      for (const [k, v] of Object.entries(s.registries || {})) registries[k] = (registries[k] || 0) + v;
    }
  }

  const pubRate = linkedDois > 0 ? +((pubTrialDois / linkedDois) * 100).toFixed(1) : 0;
  const regSummary = Object.entries(registries)
    .sort((a, b) => b[1] - a[1])
    .map(([r, n]) => `${r}: <strong>${fmtInt(n)}</strong>`)
    .join(" &bull; ");
  callout.innerHTML =
    `<strong>Clinical Trial Registry Links (${yearRangeLabel()}):</strong> ${regSummary || "No trial links in selected range."} ` +
    `&nbsp;|&nbsp; Enriched via ClinicalTrials.gov API v2 &amp; NIH iCite.`;

  const industryShare =
    enrichedNct > 0 ? +(((sponsors.INDUSTRY || 0) / enrichedNct) * 100).toFixed(1) : 0;
  const ctCards = [
    { label: `Trial-Linked Preprints (${yearRangeLabel()})`, value: fmtInt(linkedDois), sub: `${fmtInt(uniqueTrials)} trial mentions` },
    { label: "Peer-Reviewed Rate", value: fmtPct(pubRate), sub: `${fmtInt(pubTrialDois)} published DOIs` },
    { label: "Enriched NCT Trials", value: fmtInt(enrichedNct), sub: `${fmtInt(withResults)} with posted results` },
    { label: "Industry-Sponsored", value: fmtPct(industryShare), sub: `${fmtInt(sponsors.INDUSTRY || 0)} INDUSTRY vs ${fmtInt(sponsors.OTHER || 0)} Academic/Other` },
    { label: "Combined Enrollment", value: fmtInt(totalEnroll), sub: "Across enriched NCT trials" },
  ];
  document.getElementById("ct-kpi-grid").innerHTML = ctCards
    .map(
      (x) => `<div class="kpi-card">
        <div class="kpi-label">${x.label}</div>
        <div class="kpi-value">${x.value}</div>
        <div class="kpi-sub">${x.sub}</div>
      </div>`
    )
    .join("");

  // 1. Trial-linked preprints by year
  const yrTraces = [];
  if (currentServer === "all" || currentServer === "biorxiv") {
    yrTraces.push({ x: ctYears, y: yrBio, name: "bioRxiv", type: "bar", marker: { color: c.biorxiv } });
  }
  if (currentServer === "all" || currentServer === "medrxiv") {
    yrTraces.push({ x: ctYears, y: yrMed, name: "medRxiv", type: "bar", marker: { color: c.medrxiv } });
  }
  Plotly.newPlot(
    "chart-ct-yearly",
    yrTraces,
    baseLayout({
      barmode: "stack",
      xaxis: { dtick: 1, title: `Posting Year (${yearRangeLabel()})`, gridcolor: c.grid },
      yaxis: { title: "Trial-Linked Preprints", gridcolor: c.grid },
    }),
    PLOT_CFG
  );

  // 2. Phases bar chart
  const phaseEntries = Object.entries(phases).sort((a, b) => a[1] - b[1]);
  Plotly.newPlot(
    "chart-ct-phases",
    [
      {
        type: "bar",
        orientation: "h",
        y: phaseEntries.map(([k]) => k),
        x: phaseEntries.map(([, v]) => v),
        marker: { color: c.medrxiv },
        hovertemplate: "<b>%{y}</b><br>Trials: %{x:,}<extra></extra>",
      },
    ],
    baseLayout({
      margin: { l: 175, r: 20, t: 20, b: 48 },
      xaxis: { title: "Enriched NCT Studies", gridcolor: c.grid },
      yaxis: { automargin: true },
    }),
    PLOT_CFG
  );

  // 3. Trial Status bar chart
  const statusEntries = Object.entries(statuses).sort((a, b) => a[1] - b[1]).slice(-10);
  Plotly.newPlot(
    "chart-ct-status",
    [
      {
        type: "bar",
        orientation: "h",
        y: statusEntries.map(([k]) => k.replace(/_/g, " ")),
        x: statusEntries.map(([, v]) => v),
        marker: { color: c.biorxiv },
        hovertemplate: "<b>%{y}</b><br>Trials: %{x:,}<extra></extra>",
      },
    ],
    baseLayout({
      margin: { l: 185, r: 20, t: 20, b: 48 },
      xaxis: { title: "Enriched NCT Studies", gridcolor: c.grid },
      yaxis: { automargin: true },
    }),
    PLOT_CFG
  );

  // 4. Top Clinical Conditions
  const condEntries = Object.entries(conditions).sort((a, b) => b[1] - a[1]).slice(0, 15).reverse();
  Plotly.newPlot(
    "chart-ct-conditions",
    [
      {
        type: "bar",
        orientation: "h",
        y: condEntries.map(([k]) => (k.length > 36 ? k.slice(0, 34) + "…" : k)),
        x: condEntries.map(([, v]) => v),
        marker: { color: c.accent3 },
        customdata: condEntries.map(([k]) => k),
        hovertemplate: "<b>%{customdata}</b><br>Linked Trials: %{x:,}<extra></extra>",
      },
    ],
    baseLayout({
      margin: { l: 210, r: 20, t: 20, b: 48 },
      xaxis: { title: "Linked Trials", gridcolor: c.grid },
      yaxis: { automargin: true },
    }),
    PLOT_CFG
  );

  // 5. Downstream NIH iCite translational impact
  const ds = ct.downstream_icite || {};
  const dsBox = document.getElementById("ct-downstream-summary");
  const dsTbody = document.getElementById("ct-downstream-tbody");
  if (!ds.evaluated_published_dois) {
    dsBox.innerHTML =
      "No NIH iCite downstream records cached yet. Run <code>.venv/bin/python scripts/check_clinical_trials.py trace-downstream --limit 500</code> then <code>scripts/run_etl.py --skip-parquet</code> to trace basic-science papers cited by later clinical trials.";
    dsTbody.innerHTML = "";
  } else {
    dsBox.innerHTML =
      `Evaluated <strong>${fmtInt(ds.evaluated_published_dois)}</strong> published papers (<strong>${fmtInt(ds.resolved_pmids)}</strong> PubMed PMIDs resolved): ` +
      `<strong>${fmtInt(ds.cited_by_clinical_trials_count)}</strong> (<strong>${ds.cited_by_clinical_trials_pct}%</strong>) have already been cited by clinical trials/guidelines! ` +
      `Median NIH APT (Approximate Potential to Translate): <strong>${ds.median_apt ?? "—"}</strong> (${fmtInt(ds.high_apt_count_ge_0_5)} papers with APT &ge; 0.50).`;

    const dsRows = (ds.top_translational_papers || []).filter((r) => {
      const okSrv = currentServer === "all" || r.server === currentServer;
      const okYr = !r.year || (r.year >= startYear && r.year <= endYear);
      return okSrv && okYr;
    });
    dsTbody.innerHTML = dsRows
      .slice(0, 25)
      .map(
        (r) => `<tr>
          <td><a href="https://doi.org/${r.published_doi}" target="_blank" rel="noopener">${r.title || r.published_doi}</a></td>
          <td>${r.category || "—"}</td>
          <td class="num">${r.year ?? "—"}</td>
          <td class="num"><strong>${fmtInt(r.cited_by_clin_count)}</strong></td>
          <td class="num">${r.apt != null ? r.apt.toFixed(2) : "—"}</td>
          <td class="num">${r.rcr != null ? r.rcr.toFixed(2) : "—"}</td>
        </tr>`
      )
      .join("");
  }

  // 6. Featured trials table (filtered by year & server)
  currentTrialRows = (ct.featured_trials || []).filter((r) => {
    const okSrv = currentServer === "all" || r.server === currentServer;
    const okYr = !r.year || (r.year >= startYear && r.year <= endYear);
    return okSrv && okYr;
  });
  renderTrialsTable(document.getElementById("ct-search").value);
}

function renderTrialsTable(query) {
  const q = (query || "").trim().toLowerCase();
  const rows = currentTrialRows.filter(
    (r) =>
      !q ||
      r.trial_id.toLowerCase().includes(q) ||
      r.title.toLowerCase().includes(q) ||
      r.sponsor.toLowerCase().includes(q) ||
      r.phase.toLowerCase().includes(q) ||
      r.status.toLowerCase().includes(q)
  );
  document.getElementById("ct-tbody").innerHTML = rows
    .slice(0, 60)
    .map(
      (r) => `<tr>
        <td><a href="https://clinicaltrials.gov/study/${r.trial_id}" target="_blank" rel="noopener"><strong>${r.trial_id}</strong></a></td>
        <td><a href="${r.doi_link}" target="_blank" rel="noopener">${r.title}</a></td>
        <td>${r.phase}</td>
        <td>${r.status.replace(/_/g, " ")}</td>
        <td>${r.sponsor || "—"}</td>
        <td class="num">${r.enrollment != null ? fmtInt(r.enrollment) : "—"}</td>
        <td>${
          r.is_published
            ? `<a class="badge badge-ok" href="https://doi.org/${r.published_doi}" target="_blank" rel="noopener">Published</a>`
            : `<span class="badge badge-muted">Preprint</span>`
        }</td>
      </tr>`
    )
    .join("");
}

function renderInstitutions(agg) {
  const c = themeColors();
  const rorAudit = DATA.ror_dedup_audit;
  const rorBox = document.getElementById("ror-audit-callout");
  if (rorAudit) {
    rorBox.innerHTML =
      `<strong>ROR Institutional Deduplication Active:</strong> Resolved <strong>${rorAudit.coverage_pct}%</strong> of preprints (` +
      `<strong>${fmtInt(rorAudit.matched_preprints)}</strong> / ${fmtInt(rorAudit.total_non_null_preprints)}) by collapsing ` +
      `<strong>${fmtInt(rorAudit.unique_raw_strings)}</strong> raw affiliation strings into ` +
      `<strong>${fmtInt(rorAudit.unique_ror_orgs_matched)}</strong> canonical <a href="https://ror.org/" target="_blank" rel="noopener">ROR</a> organizations across ` +
      `<strong>${fmtInt(rorAudit.unique_countries)}</strong> countries.`;
  } else {
    rorBox.innerHTML =
      "<strong>ROR Deduplication Not Yet Run:</strong> Run <code>.venv/bin/python scripts/deduplicate_institutions.py</code> then <code>scripts/run_etl.py --skip-parquet</code>.";
  }

  const metaMap = DATA.institution_metadata || {};
  currentInstList = Object.entries(agg.institutions)
    .map(([institution, [cnt, pub, asum, matCnt, matPub]]) => {
      const m = metaMap[institution] || {};
      return {
        institution,
        ror_id: m.ror_id || null,
        country: m.country || "",
        org_type: m.org_type || "",
        total_preprints: cnt,
        mature_preprints: matCnt,
        pub_rate_pct: cnt > 0 ? +((pub / cnt) * 100).toFixed(1) : 0,
        mature_pub_rate_pct: matCnt > 0 ? +((matPub / matCnt) * 100).toFixed(1) : null,
        avg_authors: cnt > 0 ? +(asum / cnt).toFixed(1) : 0,
      };
    })
    .filter((d) => d.total_preprints > 0)
    .sort((a, b) => b.total_preprints - a.total_preprints)
    .slice(0, 30);

  Plotly.newPlot(
    "chart-institutions",
    [
      {
        type: "scatter",
        mode: "markers+text",
        x: currentInstList.map((d) => d.total_preprints),
        y: currentInstList.map((d) => d.pub_rate_pct),
        text: currentInstList.map((d, i) => (i < 10 ? d.institution.slice(0, 22) : "")),
        textposition: "top center",
        textfont: { size: 10 },
        marker: {
          size: currentInstList.map((d) => Math.max(10, Math.min(34, d.avg_authors * 2.2))),
          color: currentInstList.map((d) => d.pub_rate_pct),
          colorscale: "Viridis",
          opacity: 0.85,
          line: { width: 1, color: "#ffffff" },
        },
        customdata: currentInstList.map((d) => [d.institution, d.country || "—", d.avg_authors, d.mature_pub_rate_pct ?? "—"]),
        hovertemplate:
          "<b>%{customdata[0]}</b> (%{customdata[1]})<br>Preprints (window): %{x:,}<br>Pub Rate (window): %{y}%<br>Mature Pub Rate: %{customdata[3]}%<br>Avg Authors: %{customdata[2]}<extra></extra>",
      },
    ],
    baseLayout({
      xaxis: { title: `Preprints in ${yearRangeLabel()} (log scale)`, type: "log", gridcolor: c.grid },
      yaxis: { title: `Publication Rate in ${yearRangeLabel()} (%)`, gridcolor: c.grid },
    }),
    PLOT_CFG
  );

  // Top 15 Countries chart
  const topCountries = Object.entries(agg.countries || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 15)
    .reverse();
  Plotly.newPlot(
    "chart-countries",
    [
      {
        type: "bar",
        orientation: "h",
        y: topCountries.map(([k]) => k),
        x: topCountries.map(([, v]) => v),
        marker: { color: currentServer === "medrxiv" ? c.medrxiv : c.biorxiv },
        hovertemplate: `<b>%{y}</b><br>Preprints (${yearRangeLabel()}): %{x:,}<extra></extra>`,
      },
    ],
    baseLayout({
      margin: { l: 150, r: 20, t: 20, b: 48 },
      xaxis: { title: `ROR-Matched Preprints (${yearRangeLabel()})`, gridcolor: c.grid },
      yaxis: { automargin: true },
    }),
    PLOT_CFG
  );

  renderInstTable(document.getElementById("inst-search").value);

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
      sub: "454k unique DOIs (zstd)",
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
  const rows = currentInstList.filter(
    (d) =>
      !q ||
      d.institution.toLowerCase().includes(q) ||
      (d.country && d.country.toLowerCase().includes(q)) ||
      (d.org_type && d.org_type.toLowerCase().includes(q))
  );
  document.getElementById("inst-tbody").innerHTML = rows
    .map(
      (d, idx) => `<tr>
        <td>${idx + 1}</td>
        <td>${
          d.ror_id
            ? `<a href="${d.ror_id}" target="_blank" rel="noopener"><strong>${d.institution}</strong></a>`
            : `<strong>${d.institution}</strong>`
        }</td>
        <td>${d.country || "—"}</td>
        <td><span class="badge badge-muted">${d.org_type || "raw"}</span></td>
        <td class="num">${fmtInt(d.total_preprints)}</td>
        <td class="num">${fmtInt(d.mature_preprints)}</td>
        <td class="num">${fmtPct(d.pub_rate_pct)}</td>
        <td class="num">${d.avg_authors}</td>
      </tr>`
    )
    .join("");
}

function updatePresetHighlight() {
  const yrs = DATA.available_years;
  const minY = yrs[0];
  const maxY = yrs[yrs.length - 1];
  const matureY = DATA.mature_cohort_cutoff_year ?? 2024;
  document.querySelectorAll(".preset-btn").forEach((btn) => {
    const p = btn.dataset.preset;
    const isMatch =
      (p === "all" && startYear === minY && endYear === maxY) ||
      (p === "mature" && startYear === minY && endYear === Math.min(matureY, maxY)) ||
      (p === "last5" && startYear === Math.max(minY, maxY - 4) && endYear === maxY) ||
      (p === "covid" && startYear === 2020 && endYear === 2021);
    btn.classList.toggle("active", isMatch);
  });
}

function setYearRange(newStart, newEnd) {
  startYear = Number(newStart);
  endYear = Number(newEnd);
  if (startYear > endYear) {
    endYear = startYear;
  }
  document.getElementById("year-start").value = String(startYear);
  document.getElementById("year-end").value = String(endYear);
  updatePresetHighlight();
  renderActiveTab();
}

function renderActiveTab() {
  const agg = aggregateWindow();
  renderKPIs(agg);
  if (currentTab === "overview") renderOverview(agg);
  else if (currentTab === "peer-review") renderPeerReview(agg);
  else if (currentTab === "frontiers") renderFrontiers();
  else if (currentTab === "open-science") renderOpenScience(agg);
  else if (currentTab === "institutions") renderInstitutions(agg);
  else if (currentTab === "clinical-trials") renderClinicalTrials();
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

  const yrs = DATA.available_years;
  startYear = yrs[0];
  endYear = yrs[yrs.length - 1];

  const startSel = document.getElementById("year-start");
  const endSel = document.getElementById("year-end");
  const optsHtml = yrs.map((y) => `<option value="${y}">${y}</option>`).join("");
  startSel.innerHTML = optsHtml;
  endSel.innerHTML = optsHtml;
  startSel.value = String(startYear);
  endSel.value = String(endYear);

  startSel.addEventListener("change", () => {
    const s = Number(startSel.value);
    const e = Math.max(s, Number(endSel.value));
    setYearRange(s, e);
  });
  endSel.addEventListener("change", () => {
    const e = Number(endSel.value);
    const s = Math.min(Number(startSel.value), e);
    setYearRange(s, e);
  });

  document.querySelectorAll(".preset-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const minY = yrs[0];
      const maxY = yrs[yrs.length - 1];
      const matureY = DATA.mature_cohort_cutoff_year ?? 2024;
      const p = btn.dataset.preset;
      if (p === "all") setYearRange(minY, maxY);
      else if (p === "mature") setYearRange(minY, Math.min(matureY, maxY));
      else if (p === "last5") setYearRange(Math.max(minY, maxY - 4), maxY);
      else if (p === "covid") setYearRange(Math.max(minY, 2020), Math.min(maxY, 2021));
    });
  });

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
      document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.remove("active"));
      btn.classList.add("active");
      currentTab = btn.dataset.tab;
      document.getElementById(`tab-${currentTab}`).classList.add("active");
      renderActiveTab();
    });
  });

  document.getElementById("inst-search").addEventListener("input", (e) => {
    renderInstTable(e.target.value);
  });

  document.getElementById("ct-search").addEventListener("input", (e) => {
    renderTrialsTable(e.target.value);
  });

  document.getElementById("theme-toggle").addEventListener("click", (e) => {
    const html = document.documentElement;
    const next = html.getAttribute("data-theme") === "dark" ? "light" : "dark";
    html.setAttribute("data-theme", next);
    e.target.textContent = next === "dark" ? "Light Mode" : "Dark Mode";
    renderActiveTab();
  });

  updatePresetHighlight();
  renderActiveTab();
}

window.addEventListener("DOMContentLoaded", init);
