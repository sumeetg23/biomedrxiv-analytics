/* Dual-mode AI Analyst (Quick Side-Drawer + Full-Page Chat LLM Tab) */

(function () {
  const STORAGE_KEY = "biomedrxiv_worker_url_override";

  const MODEL_DISPLAY_MAP = {
    "gemini-4-hydrogen": "Gemini 4 Hydrogen",
    "gemini-4-argon": "Gemini 4 Argon",
    "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite",
    "openai/gpt-oss-120b": "GPT-OSS 120B",
    "qwen/qwen3.8-27b": "Qwen 3.8 27B",
    "@cf/meta/llama-3.1-8b-instruct-fast": "Llama 3.1 8B",
    "@cf/meta/llama-3.1-8b-instruct-fp8": "Llama 3.1 8B",
  };

  function formatModelName(rawModel, rawProvider) {
    if (rawModel && MODEL_DISPLAY_MAP[rawModel]) {
      return MODEL_DISPLAY_MAP[rawModel];
    }
    if (rawModel && typeof rawModel === "string") {
      const base = rawModel.split("/").pop();
      if (/llama-3\.1-8b/i.test(base)) return "Llama 3.1 8B";
      if (/gpt-oss-120b/i.test(base)) return "GPT-OSS 120B";
      if (/qwen3\.8-27b/i.test(base)) return "Qwen 3.8 27B";
      return base;
    }
    if (rawProvider === "gemini") return "Gemini 4";
    if (rawProvider === "groq") return "GPT-OSS 120B";
    if (rawProvider === "workers-ai" || rawProvider === "workers_ai") return "Llama 3.1 8B";
    return "Gemini 4";
  }

  function getWorkerUrl() {
    const cfg = (window.BIOMEDRXIV_CHAT_CONFIG && window.BIOMEDRXIV_CHAT_CONFIG.workerUrl) || "";
    const override = sessionStorage.getItem(STORAGE_KEY) || "";
    return (override || cfg).trim().replace(/\/+$/, "");
  }

  function esc(str) {
    return String(str ?? "").replace(/[&<>"']/g, (m) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]
    ));
  }

  function renderMarkdownLite(text) {
    // Minimal safe formatting for bold, inline code, and line breaks
    let safe = esc(text);
    safe = safe.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    safe = safe.replace(/`([^`]+)`/g, "<code>$1</code>");
    safe = safe.replace(/\n/g, "<br>");
    return safe;
  }

  function updateContextBanner() {
    const pill = document.getElementById("chat-scope-pill");
    if (!pill || typeof window.getDashboardChatContext !== "function") return;
    const ctx = window.getDashboardChatContext();
    if (!ctx) return;
    pill.textContent = `Active Slice: ${ctx.scope.label}`;
  }

  let lastHealthUrl = "";
  let currentMode = "drawer"; // "drawer" | "page"

  function updateSetupVisibility() {
    const setupBox = document.getElementById("chat-setup-box");
    const form = document.getElementById("chat-drawer-form");
    const welcome = document.getElementById("chat-welcome");
    const providerBar = document.getElementById("chat-provider-bar");
    const log = document.getElementById("chat-drawer-log");
    const url = getWorkerUrl();
    if (url) {
      setupBox.style.display = "none";
      form.style.display = "flex";
      if (providerBar) providerBar.style.display = "flex";
      if (welcome && log) {
        welcome.style.display = log.children.length === 0 ? "flex" : "none";
      }
      refreshProviderHealth(url);
    } else {
      setupBox.style.display = "block";
      form.style.display = "none";
      if (welcome) welcome.style.display = "none";
      if (providerBar) providerBar.style.display = "none";
    }
  }

  async function refreshProviderHealth(workerUrl) {
    if (!workerUrl || workerUrl === lastHealthUrl) return;
    lastHealthUrl = workerUrl;
    const select = document.getElementById("chat-provider-select");
    if (!select) return;
    try {
      const resp = await fetch(`${workerUrl}/health`, { cache: "no-store" });
      if (!resp.ok) return;
      const info = await resp.json();
      const providers = info && info.providers;
      if (!providers) return;

      const labels = {
        gemini: "Gemini 4",
        groq: formatModelName(providers.groq.models && providers.groq.models[0], "groq"),
        workers_ai: formatModelName(providers.workers_ai.models && providers.workers_ai.models[0], "workers_ai"),
      };
      for (const opt of select.options) {
        const key = opt.value;
        if (key === "auto" || !providers[key]) continue;
        const ok = Boolean(providers[key].configured);
        opt.disabled = !ok;
        opt.textContent = ok ? labels[key] : `${labels[key]} (unavailable)`;
      }

      const ragBadge = document.getElementById("chat-rag-badge");
      if (ragBadge) {
        const ragOn = Boolean(info && info.rag && info.rag.configured);
        ragBadge.textContent = ragOn ? "RAG: ON" : "RAG: OFF";
        ragBadge.classList.toggle("rag-on", ragOn);
        ragBadge.title = ragOn
          ? "Vectorize semantic search active (3,034 clinical trials + NIH iCite + top preprints)"
          : "Vectorize index not bound yet — answering from dashboard aggregate slice";
      }
    } catch {
      // Leave default options intact if /health is unreachable
    }
  }

  function getSelectedProvider() {
    const select = document.getElementById("chat-provider-select");
    return select ? select.value : "auto";
  }

  function renderSourcesHtml(sources) {
    if (!Array.isArray(sources) || !sources.length) return "";
    const items = sources
      .map((s) => {
        const link = s.doi_link || (s.doi ? `https://doi.org/${s.doi}` : "");
        const titleHtml = link
          ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.doi)}</a>`
          : esc(s.title || s.doi || "Untitled record");
        const bits = [];
        if (s.trial_id) {
          const ctLink = /^NCT\d+$/i.test(s.trial_id)
            ? `<a href="https://clinicaltrials.gov/study/${esc(s.trial_id)}" target="_blank" rel="noopener noreferrer" class="chat-source-tag">${esc(s.trial_id)}</a>`
            : `<span class="chat-source-tag">${esc(s.trial_id)}</span>`;
          bits.push(ctLink);
        } else if (s.doc_type === "downstream_clinical") {
          bits.push(`<span class="chat-source-tag">NIH iCite</span>`);
        }
        if (s.server) bits.push(esc(s.server));
        if (s.year) bits.push(esc(s.year));
        if (s.phase) bits.push(esc(s.phase));
        if (typeof s.enrollment === "number") bits.push(`n=${esc(s.enrollment.toLocaleString())}`);
        if (typeof s.cited_by_clin === "number" && s.cited_by_clin > 0) {
          bits.push(`${esc(s.cited_by_clin)} clin. cites`);
        }
        if (s.publisher) bits.push(esc(s.publisher));
        return (
          `<li class="chat-source-item">` +
          `<div class="chat-source-title">${titleHtml}</div>` +
          `<div class="chat-source-meta">${bits.join(" &middot; ")}</div>` +
          `</li>`
        );
      })
      .join("");
    return (
      `<details class="chat-sources">` +
      `<summary>Retrieved Evidence (${sources.length} paper${sources.length === 1 ? "" : "s"}/trial${sources.length === 1 ? "" : "s"})</summary>` +
      `<ul class="chat-sources-list">${items}</ul>` +
      `</details>`
    );
  }

  function setStatusLine(msg) {
    const el = document.getElementById("chat-status-line");
    if (!el) return;
    if (msg) {
      el.textContent = msg;
      el.style.display = "block";
    } else {
      el.textContent = "";
      el.style.display = "none";
    }
  }

  function isSqlEnabledForProvider(provider) {
    const toggle = document.getElementById("chat-sql-toggle");
    if (!toggle || !toggle.checked) return false;
    if (provider === "workers_ai") return false;
    return Boolean(window.BioMedRxivDuckDB);
  }

  function syncSqlToggleState() {
    const provider = getSelectedProvider();
    const toggle = document.getElementById("chat-sql-toggle");
    const label = document.getElementById("chat-sql-toggle-label");
    if (!toggle || !label) return;
    const blocked = provider === "workers_ai";
    toggle.disabled = blocked;
    label.classList.toggle("disabled", blocked);
    label.title = blocked
      ? "DuckDB-Wasm SQL is disabled on Llama 3.1 8B (uses Vectorize RAG + Dashboard JSON)"
      : "Auto-run browser DuckDB-Wasm SQL across all 454,051 preprints when needed (Auto, Gemini 4, GPT-OSS 120B)";
  }

  function formatSqlCell(col, val) {
    if (val === null || val === undefined || val === "") return "—";
    const s = String(val);
    if (/^(doi|published_doi)$/i.test(col) && /^10\.\d{4,9}\//.test(s)) {
      return `<a href="https://doi.org/${esc(s)}" target="_blank" rel="noopener noreferrer">${esc(s)}</a>`;
    }
    if (/^trial_id$/i.test(col) && /^NCT\d+$/i.test(s)) {
      return `<a href="https://clinicaltrials.gov/study/${esc(s)}" target="_blank" rel="noopener noreferrer">${esc(s)}</a>`;
    }
    if (typeof val === "number") {
      return Number.isInteger(val) ? esc(val.toLocaleString()) : esc(val.toFixed(2));
    }
    return esc(s.length > 140 ? `${s.slice(0, 139)}…` : s);
  }

  function renderSqlResultHtml(sqlResult) {
    if (!sqlResult || !sqlResult.sql) return "";
    const { sql, columns, rows, totalRows, elapsedMs } = sqlResult;
    let tableHtml = "";
    if (Array.isArray(columns) && columns.length && Array.isArray(rows) && rows.length) {
      const displayRows = rows.slice(0, 30);
      const thead = `<tr>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>`;
      const tbody = displayRows
        .map(
          (r) =>
            `<tr>${columns
              .map((c) => {
                const v = r[c];
                const cls = typeof v === "number" ? ' class="num"' : "";
                return `<td${cls}>${formatSqlCell(c, v)}</td>`;
              })
              .join("")}</tr>`
        )
        .join("");
      tableHtml = `<div class="chat-sql-table-wrap"><table class="chat-sql-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table></div>`;
    }
    const rowLabel =
      totalRows > 30
        ? `${totalRows.toLocaleString()} rows (showing 30)`
        : `${totalRows.toLocaleString()} row${totalRows === 1 ? "" : "s"}`;
    return (
      tableHtml +
      `<details class="chat-sql-details">` +
      `<summary>View DuckDB-Wasm SQL (${esc(rowLabel)} &middot; ${esc(elapsedMs)}ms across 454K rows)</summary>` +
      `<pre class="chat-sql-code">${esc(sql)}</pre>` +
      `</details>`
    );
  }

  function appendMessage(role, meta, text, sources, sqlResult) {
    const log = document.getElementById("chat-drawer-log");
    const stage = document.getElementById("chat-messages-stage");
    const welcome = document.getElementById("chat-welcome");
    if (welcome) welcome.style.display = "none";

    const div = document.createElement("div");
    div.className = `chat-msg ${role === "user" ? "chat-msg-user" : "chat-msg-bot"}`;
    div.innerHTML =
      `<div class="chat-msg-meta">${esc(meta)}</div>` +
      `<div class="chat-msg-body">${role === "user" ? esc(text) : renderMarkdownLite(text)}</div>` +
      (role === "bot" ? renderSqlResultHtml(sqlResult) : "") +
      (role === "bot" ? renderSourcesHtml(sources) : "");
    log.appendChild(div);
    if (stage) stage.scrollTop = stage.scrollHeight;
  }

  function clearConversation() {
    const log = document.getElementById("chat-drawer-log");
    const welcome = document.getElementById("chat-welcome");
    if (log) log.innerHTML = "";
    if (welcome && getWorkerUrl()) welcome.style.display = "flex";
    const input = document.getElementById("chat-drawer-input");
    if (input) input.focus();
  }

  function autoResizeInput(textarea) {
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(132, Math.max(38, textarea.scrollHeight))}px`;
  }

  async function tryBrowserSqlWorkflow(workerUrl, question, ctx, provider) {
    setStatusLine("Checking if exact 454K-row DuckDB SQL is needed…");
    const planResp = await fetch(`${workerUrl}/sql-plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, context: ctx, provider }),
    });
    if (!planResp.ok) return null; // Older Worker or transient error -> fall back cleanly to /ask
    const plan = await planResp.json();
    if (!plan || !plan.needs_sql || !plan.sql) return null;

    let sqlResult = null;
    try {
      sqlResult = await window.BioMedRxivDuckDB.runQuery(plan.sql, setStatusLine);
    } catch (firstErr) {
      // One automatic self-repair attempt with DuckDB error feedback
      setStatusLine("Refining DuckDB SQL query…");
      const retryResp = await fetch(`${workerUrl}/sql-plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          context: ctx,
          provider,
          previous_sql: plan.sql,
          previous_error: firstErr && firstErr.message ? firstErr.message : String(firstErr),
        }),
      });
      if (retryResp.ok) {
        const retryPlan = await retryResp.json();
        if (retryPlan && retryPlan.needs_sql && retryPlan.sql) {
          sqlResult = await window.BioMedRxivDuckDB.runQuery(retryPlan.sql, setStatusLine);
        }
      }
      if (!sqlResult) throw firstErr;
    }

    setStatusLine("Summarizing DuckDB SQL results…");
    const sumResp = await fetch(`${workerUrl}/sql-summarize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        sql: sqlResult.sql,
        columns: sqlResult.columns,
        rows: sqlResult.rows.slice(0, 40),
        total_rows: sqlResult.totalRows,
        context: ctx,
        provider,
      }),
    });
    const sumData = await sumResp.json();
    if (!sumResp.ok) {
      return {
        answer: `Executed DuckDB-Wasm query directly in your browser (${sqlResult.totalRows.toLocaleString()} rows in ${sqlResult.elapsedMs}ms), though AI summary was unavailable: ${sumData.error || sumResp.status}.`,
        provider: plan.provider || provider,
        model: plan.model || "duckdb-wasm",
        sqlResult,
      };
    }
    return {
      answer: sumData.answer,
      provider: sumData.provider,
      model: sumData.model,
      sqlResult,
    };
  }

  async function sendQuestion(question) {
    const workerUrl = getWorkerUrl();
    if (!workerUrl) {
      updateSetupVisibility();
      return;
    }

    const ctx =
      typeof window.getDashboardChatContext === "function"
        ? window.getDashboardChatContext()
        : {};

    appendMessage("user", "You", question);
    const btn = document.getElementById("chat-drawer-send");
    const input = document.getElementById("chat-drawer-input");
    btn.disabled = true;
    btn.textContent = "…";

    const provider = getSelectedProvider();
    try {
      // Tier 2: Browser DuckDB-Wasm SQL over all 454,051 rows (Gemini & Groq only)
      if (isSqlEnabledForProvider(provider)) {
        try {
          const sqlOut = await tryBrowserSqlWorkflow(workerUrl, question, ctx, provider);
          if (sqlOut) {
            const label = formatModelName(sqlOut.model, sqlOut.provider);
            appendMessage(
              "bot",
              `${label} • SQL 454K • ${sqlOut.sqlResult.elapsedMs}ms`,
              sqlOut.answer,
              null,
              sqlOut.sqlResult
            );
            if (typeof window.trackDashboardEvent === "function") {
              window.trackDashboardEvent("ai_query", {
                provider: sqlOut.provider || provider,
                model: sqlOut.model || "unknown",
                tier: "sql_454k",
                sql_elapsed_ms: sqlOut.sqlResult.elapsedMs,
              });
            }
            return;
          }
        } catch (sqlErr) {
          console.warn("DuckDB-Wasm SQL path fell back to Tier 0/1 RAG:", sqlErr);
        }
      }

      // Tier 0 (Dashboard JSON) + Tier 1 (Vectorize RAG)
      setStatusLine("Searching Vectorize RAG & generating answer…");
      const resp = await fetch(`${workerUrl}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, context: ctx, provider }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        appendMessage("bot", `Service Notice (HTTP ${resp.status})`, data.error || `HTTP ${resp.status}`);
        return;
      }
      const modelLabel = formatModelName(data.model, data.provider);
      const ragTag = data.rag_used ? " • RAG" : "";
      appendMessage(
        "bot",
        `${modelLabel}${ragTag}`,
        data.answer,
        data.sources
      );
      if (typeof window.trackDashboardEvent === "function") {
        window.trackDashboardEvent("ai_query", {
          provider: data.provider || provider,
          model: data.model || "unknown",
          tier: data.rag_used ? "rag" : "dashboard_json",
        });
      }
    } catch (err) {
      appendMessage(
        "bot",
        "Connection Error",
        `Could not reach Worker at ${workerUrl}/ask: ${err.message}`
      );
    } finally {
      setStatusLine("");
      btn.disabled = false;
      btn.textContent = "Send";
      autoResizeInput(input);
      input.focus();
    }
  }

  function mountChatInPage() {
    const shell = document.getElementById("chat-shell");
    const host = document.getElementById("chat-page-host");
    if (!shell || !host) return;
    closeDrawer();
    if (shell.parentElement !== host) {
      host.appendChild(shell);
    }
    shell.classList.remove("mode-drawer");
    shell.classList.add("mode-page");
    currentMode = "page";
    updateContextBanner();
    updateSetupVisibility();
    const input = document.getElementById("chat-drawer-input");
    if (input) input.focus();
  }

  function mountChatInDrawer() {
    const shell = document.getElementById("chat-shell");
    const drawer = document.getElementById("chat-drawer");
    if (!shell || !drawer) return;
    if (shell.parentElement !== drawer) {
      drawer.appendChild(shell);
    }
    shell.classList.remove("mode-page");
    shell.classList.add("mode-drawer");
    currentMode = "drawer";
  }

  function openDrawer() {
    if (currentMode === "page") {
      const input = document.getElementById("chat-drawer-input");
      if (input) input.focus();
      return;
    }
    mountChatInDrawer();
    document.getElementById("chat-drawer").classList.add("open");
    document.getElementById("chat-backdrop").classList.add("open");
    updateContextBanner();
    updateSetupVisibility();
    const input = document.getElementById("chat-drawer-input");
    if (input) input.focus();
    if (typeof window.trackDashboardEvent === "function") {
      window.trackDashboardEvent("ai_drawer_open", {});
    }
  }

  function closeDrawer() {
    document.getElementById("chat-drawer").classList.remove("open");
    document.getElementById("chat-backdrop").classList.remove("open");
  }

  function initChatDrawer() {
    const openBtn = document.getElementById("open-chat-btn");
    const closeBtn = document.getElementById("close-chat-btn");
    const expandBtn = document.getElementById("expand-chat-btn");
    const clearBtn = document.getElementById("chat-clear-btn");
    const backdrop = document.getElementById("chat-backdrop");
    const form = document.getElementById("chat-drawer-form");
    const input = document.getElementById("chat-drawer-input");
    const saveUrlBtn = document.getElementById("chat-save-url-btn");
    const urlInput = document.getElementById("chat-worker-url-input");

    if (!openBtn) return;

    openBtn.addEventListener("click", openDrawer);
    if (closeBtn) closeBtn.addEventListener("click", closeDrawer);
    if (backdrop) backdrop.addEventListener("click", closeDrawer);
    if (clearBtn) clearBtn.addEventListener("click", clearConversation);
    if (expandBtn) {
      expandBtn.addEventListener("click", () => {
        closeDrawer();
        if (typeof window.switchDashboardTab === "function") {
          window.switchDashboardTab("ask-ai");
        }
      });
    }

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawer();
    });

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (!q) return;
      input.value = "";
      autoResizeInput(input);
      sendQuestion(q);
    });

    input.addEventListener("input", () => autoResizeInput(input));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        form.requestSubmit();
      }
    });

    document.querySelectorAll(".chat-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const textEl = chip.querySelector(".chat-chip-text");
        const q = (textEl ? textEl.textContent : chip.textContent).trim();
        if (q) sendQuestion(q);
      });
    });

    if (saveUrlBtn && urlInput) {
      saveUrlBtn.addEventListener("click", () => {
        const val = urlInput.value.trim().replace(/\/+$/, "");
        if (!val.startsWith("http://") && !val.startsWith("https://")) {
          urlInput.focus();
          return;
        }
        sessionStorage.setItem(STORAGE_KEY, val);
        updateSetupVisibility();
        appendMessage("bot", "Connected", `Using Cloudflare Worker proxy at ${val} for this browser session.`);
      });
    }

    const providerSelect = document.getElementById("chat-provider-select");
    if (providerSelect) {
      providerSelect.addEventListener("change", syncSqlToggleState);
      syncSqlToggleState();
    }

    // Keep context pill fresh whenever dashboard filters change
    window.addEventListener("biomedrxiv:filterchange", updateContextBanner);

    // Mount full-page Chat LLM workspace when switching to the Ask AI tab
    window.addEventListener("biomedrxiv:tabchange", (e) => {
      const tab = e && e.detail && e.detail.tab;
      if (tab === "ask-ai") {
        mountChatInPage();
      } else {
        mountChatInDrawer();
      }
    });
  }

  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", initChatDrawer);
  } else {
    initChatDrawer();
  }
})();
