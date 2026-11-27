/* Dual-mode AI Analyst (Quick Side-Drawer + Full-Page Chat LLM Tab) */

(function () {
  const STORAGE_KEY = "biomedrxiv_worker_url_override";
  const BYOK_KEY_STORAGE = "biomedrxiv_byok_api_key";
  const BYOK_PROVIDER_STORAGE = "biomedrxiv_byok_provider";
  const FREE_QUOTA_STORAGE = "biomedrxiv_free_quota_v1";
  let freeQuestionLimit = 3;

  const MODEL_DISPLAY_MAP = {
    "gemini-4-hydrogen": "Gemini 4 Hydrogen",
    "gemini-4-argon": "Gemini 4 Argon",
    "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite",
    "openai/gpt-oss-120b": "GPT-OSS 120B",
    "qwen/qwen3.8-27b": "Qwen 3.8 27B",
    "gpt-4o-mini": "GPT-4o mini",
    "gpt-4.1-mini": "GPT-4.1 mini",
    "@cf/meta/llama-3.1-8b-instruct-fast": "Llama 3.1 8B",
    "@cf/meta/llama-3.1-8b-instruct-fp8": "Llama 3.1 8B",
  };

  const BYOK_PROVIDER_LABELS = {
    gemini: "Gemini",
    groq: "Groq",
    openai: "OpenAI",
  };

  function getByokConfig() {
    const key = (sessionStorage.getItem(BYOK_KEY_STORAGE) || "").trim();
    const provider = (sessionStorage.getItem(BYOK_PROVIDER_STORAGE) || "gemini").trim().toLowerCase();
    if (!key) return null;
    return { key, provider: BYOK_PROVIDER_LABELS[provider] ? provider : "gemini" };
  }

  function todayUtcKey() {
    return new Date().toISOString().slice(0, 10);
  }

  function readLocalQuota() {
    const today = todayUtcKey();
    try {
      const raw = JSON.parse(localStorage.getItem(FREE_QUOTA_STORAGE) || "null");
      if (raw && raw.date === today && typeof raw.used === "number") {
        return raw;
      }
    } catch {}
    return { date: today, used: 0 };
  }

  function writeLocalQuota(used) {
    try {
      localStorage.setItem(FREE_QUOTA_STORAGE, JSON.stringify({ date: todayUtcKey(), used: Math.max(0, used) }));
    } catch {}
  }

  function buildAuthHeaders() {
    const headers = { "Content-Type": "application/json" };
    const byok = getByokConfig();
    if (byok) {
      headers["X-User-API-Key"] = byok.key;
      headers["X-User-Provider"] = byok.provider;
    }
    return headers;
  }

  function updateQuotaAndByokUi() {
    const byok = getByokConfig();
    const pill = document.getElementById("chat-quota-pill");
    const byokBtn = document.getElementById("chat-byok-btn");
    const clearByokBtn = document.getElementById("chat-byok-clear");
    const modelSelect = document.getElementById("chat-provider-select");

    if (clearByokBtn) {
      clearByokBtn.style.display = byok ? "inline-block" : "none";
    }
    if (byokBtn) {
      byokBtn.textContent = byok ? `Key: ${BYOK_PROVIDER_LABELS[byok.provider]}` : "API Key";
    }
    if (modelSelect) {
      modelSelect.disabled = Boolean(byok);
      modelSelect.title = byok
        ? `Using your ${BYOK_PROVIDER_LABELS[byok.provider]} API key (clear key to switch back to shared models)`
        : "Select AI model";
    }
    if (pill) {
      pill.classList.remove("quota-byok", "quota-empty");
      if (byok) {
        pill.textContent = `Own Key: ${BYOK_PROVIDER_LABELS[byok.provider]}`;
        pill.classList.add("quota-byok");
        pill.title = "Using your own API key (session only) — click to edit or clear";
      } else {
        const q = readLocalQuota();
        const remaining = Math.max(0, freeQuestionLimit - q.used);
        if (remaining > 0) {
          pill.textContent = `${remaining} free left`;
          pill.title = `${remaining} of ${freeQuestionLimit} free daily demo questions remaining — click to add your own API key`;
        } else {
          pill.textContent = "0 free left · Add Key";
          pill.classList.add("quota-empty");
          pill.title = "Daily free demo limit reached — click to add your own Gemini, Groq, or OpenAI API key";
        }
      }
    }
    syncSqlToggleState();
  }

  function openByokPanel(focusInput = true) {
    const panel = document.getElementById("chat-byok-panel");
    if (!panel) return;
    panel.style.display = "block";
    const byok = getByokConfig();
    const provSel = document.getElementById("chat-byok-provider");
    const keyInput = document.getElementById("chat-byok-input");
    if (provSel && byok) provSel.value = byok.provider;
    if (keyInput && focusInput) keyInput.focus();
  }

  function closeByokPanel() {
    const panel = document.getElementById("chat-byok-panel");
    if (panel) panel.style.display = "none";
  }

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
    if (rawProvider === "openai") return "GPT-4o mini";
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

      if (info && info.free_quota && typeof info.free_quota.max_questions === "number") {
        freeQuestionLimit = Math.max(1, info.free_quota.max_questions);
        updateQuotaAndByokUi();
      }

      const ragBadge = document.getElementById("chat-rag-badge");
      if (ragBadge) {
        const ragOn = Boolean(info && info.rag && info.rag.configured);
        if (ragOn) {
          ragBadge.style.display = "none";
          ragBadge.textContent = "";
        } else {
          ragBadge.style.display = "inline-block";
          ragBadge.textContent = "Paper Search: Offline";
          ragBadge.title = "Cloudflare Vectorize RAG binding is currently off — check /health for details";
          console.warn("BioMedRxiv Ask AI health check: Vectorize RAG is OFF.", info);
        }
      }
    } catch (err) {
      const ragBadge = document.getElementById("chat-rag-badge");
      if (ragBadge) {
        ragBadge.style.display = "inline-block";
        ragBadge.textContent = "AI Proxy: Unreachable";
        ragBadge.title = `Could not reach ${workerUrl}/health`;
      }
      console.warn("BioMedRxiv Ask AI /health check failed:", err);
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
    if (!getByokConfig() && provider === "workers_ai") return false;
    return Boolean(window.BioMedRxivDuckDB);
  }

  function syncSqlToggleState() {
    const provider = getSelectedProvider();
    const toggle = document.getElementById("chat-sql-toggle");
    const label = document.getElementById("chat-sql-toggle-label");
    if (!toggle || !label) return;
    const blocked = !getByokConfig() && provider === "workers_ai";
    toggle.disabled = blocked;
    label.classList.toggle("disabled", blocked);
    label.title = blocked
      ? "Deep Search is disabled on Llama 3.1 8B (uses fast paper search + dashboard summary)"
      : "Automatically queries all 454,051 preprints in your browser when exact filtering or ranking is needed";
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
    setStatusLine("Checking if exact 454K-row Deep Search is needed…");
    const planResp = await fetch(`${workerUrl}/sql-plan`, {
      method: "POST",
      headers: buildAuthHeaders(),
      body: JSON.stringify({ question, context: ctx, provider }),
    });
    if (planResp.status === 429) {
      const limitData = await planResp.json().catch(() => ({}));
      return { rateLimited: true, data: limitData };
    }
    if (!planResp.ok) return null; // Older Worker or transient error -> fall back cleanly to /ask
    const plan = await planResp.json();
    if (!plan || !plan.needs_sql || !plan.sql) return null;

    let sqlResult = null;
    try {
      sqlResult = await window.BioMedRxivDuckDB.runQuery(plan.sql, setStatusLine);
    } catch (firstErr) {
      // One automatic self-repair attempt with DuckDB error feedback
      setStatusLine("Refining database query…");
      const retryResp = await fetch(`${workerUrl}/sql-plan`, {
        method: "POST",
        headers: buildAuthHeaders(),
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

    setStatusLine("Summarizing Deep Search results…");
    const sumResp = await fetch(`${workerUrl}/sql-summarize`, {
      method: "POST",
      headers: buildAuthHeaders(),
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
    if (sumResp.status === 429) {
      return { rateLimited: true, data: sumData };
    }
    if (!sumResp.ok) {
      return {
        answer: `Executed Deep Search directly in your browser (${sqlResult.totalRows.toLocaleString()} rows in ${sqlResult.elapsedMs}ms), though AI summary was unavailable: ${sumData.error || sumResp.status}.`,
        provider: plan.provider || provider,
        model: plan.model || "duckdb-wasm",
        sqlResult,
      };
    }
    return {
      answer: sumData.answer,
      provider: sumData.provider,
      model: sumData.model,
      freeQuota: sumData.free_quota || null,
      byok: Boolean(sumData.byok),
      sqlResult,
    };
  }

  function handleFreeLimitReached(serverMsg) {
    writeLocalQuota(freeQuestionLimit);
    updateQuotaAndByokUi();
    openByokPanel(true);
    appendMessage(
      "bot",
      "Free Demo Limit Reached",
      serverMsg ||
        `You've used all ${freeQuestionLimit} free demo questions for today. Paste your own free **Gemini**, **Groq**, or **OpenAI** API key in the panel above to continue asking unlimited questions (stored only in this browser tab).`
    );
  }

  function recordSuccessfulAnswerQuota(serverQuota, wasByok) {
    if (wasByok || getByokConfig()) {
      updateQuotaAndByokUi();
      return;
    }
    if (serverQuota && typeof serverQuota.used === "number") {
      if (typeof serverQuota.limit === "number") freeQuestionLimit = serverQuota.limit;
      writeLocalQuota(serverQuota.used);
    } else {
      const q = readLocalQuota();
      writeLocalQuota(q.used + 1);
    }
    updateQuotaAndByokUi();
  }

  async function sendQuestion(question) {
    const workerUrl = getWorkerUrl();
    if (!workerUrl) {
      updateSetupVisibility();
      return;
    }

    const byok = getByokConfig();
    if (!byok && readLocalQuota().used >= freeQuestionLimit) {
      handleFreeLimitReached();
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

    const provider = byok ? byok.provider : getSelectedProvider();
    try {
      // Tier 2: Browser DuckDB-Wasm Deep Search over all 454,051 rows
      if (isSqlEnabledForProvider(provider)) {
        try {
          const sqlOut = await tryBrowserSqlWorkflow(workerUrl, question, ctx, provider);
          if (sqlOut && sqlOut.rateLimited) {
            handleFreeLimitReached(sqlOut.data && sqlOut.data.error);
            return;
          }
          if (sqlOut) {
            recordSuccessfulAnswerQuota(sqlOut.freeQuota, sqlOut.byok);
            const label = formatModelName(sqlOut.model, sqlOut.provider);
            const keyTag = sqlOut.byok ? " • Own Key" : "";
            appendMessage(
              "bot",
              `${label} • Deep Search • ${sqlOut.sqlResult.elapsedMs}ms${keyTag}`,
              sqlOut.answer,
              null,
              sqlOut.sqlResult
            );
            if (typeof window.trackDashboardEvent === "function") {
              window.trackDashboardEvent("ai_query", {
                provider: sqlOut.provider || provider,
                model: sqlOut.model || "unknown",
                tier: "sql_454k",
                byok: Boolean(sqlOut.byok),
                sql_elapsed_ms: sqlOut.sqlResult.elapsedMs,
              });
            }
            return;
          }
        } catch (sqlErr) {
          console.warn("Deep Search SQL path fell back to Tier 0/1 RAG:", sqlErr);
        }
      }

      // Tier 0 (Dashboard JSON) + Tier 1 (Vectorize RAG)
      setStatusLine("Searching papers & generating answer…");
      const resp = await fetch(`${workerUrl}/ask`, {
        method: "POST",
        headers: buildAuthHeaders(),
        body: JSON.stringify({ question, context: ctx, provider }),
      });
      const data = await resp.json();
      if (resp.status === 429 && data && data.code === "FREE_LIMIT_REACHED") {
        handleFreeLimitReached(data.error);
        return;
      }
      if (!resp.ok) {
        appendMessage("bot", `Service Notice (HTTP ${resp.status})`, data.error || `HTTP ${resp.status}`);
        return;
      }
      recordSuccessfulAnswerQuota(data.free_quota, data.byok);
      const modelLabel = formatModelName(data.model, data.provider);
      const ragTag = data.rag_used ? " • Papers" : "";
      const keyTag = data.byok ? " • Own Key" : "";
      appendMessage(
        "bot",
        `${modelLabel}${ragTag}${keyTag}`,
        data.answer,
        data.sources
      );
      if (typeof window.trackDashboardEvent === "function") {
        window.trackDashboardEvent("ai_query", {
          provider: data.provider || provider,
          model: data.model || "unknown",
          tier: data.rag_used ? "rag" : "dashboard_json",
          byok: Boolean(data.byok),
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

    // BYOK API Key panel wiring (sessionStorage only)
    const byokBtn = document.getElementById("chat-byok-btn");
    const quotaPill = document.getElementById("chat-quota-pill");
    const byokClose = document.getElementById("chat-byok-close");
    const byokSave = document.getElementById("chat-byok-save");
    const byokClear = document.getElementById("chat-byok-clear");
    const byokProvider = document.getElementById("chat-byok-provider");
    const byokInput = document.getElementById("chat-byok-input");

    if (byokBtn) {
      byokBtn.addEventListener("click", () => {
        const panel = document.getElementById("chat-byok-panel");
        if (panel && panel.style.display === "block") closeByokPanel();
        else openByokPanel(true);
      });
    }
    if (quotaPill) quotaPill.addEventListener("click", () => openByokPanel(true));
    if (byokClose) byokClose.addEventListener("click", closeByokPanel);
    if (byokSave && byokInput && byokProvider) {
      byokSave.addEventListener("click", () => {
        const key = byokInput.value.trim();
        if (key.length < 8) {
          byokInput.focus();
          return;
        }
        const prov = byokProvider.value;
        sessionStorage.setItem(BYOK_KEY_STORAGE, key);
        sessionStorage.setItem(BYOK_PROVIDER_STORAGE, prov);
        byokInput.value = "";
        closeByokPanel();
        updateQuotaAndByokUi();
        appendMessage(
          "bot",
          "Own API Key Active",
          `Now routing your questions using your **${BYOK_PROVIDER_LABELS[prov]}** key for this browser tab (unlimited Deep Search & paper Q&A).`
        );
      });
    }
    if (byokClear) {
      byokClear.addEventListener("click", () => {
        sessionStorage.removeItem(BYOK_KEY_STORAGE);
        sessionStorage.removeItem(BYOK_PROVIDER_STORAGE);
        if (byokInput) byokInput.value = "";
        closeByokPanel();
        updateQuotaAndByokUi();
      });
    }

    updateQuotaAndByokUi();

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
