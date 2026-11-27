/* Collapsible AI Analyst Chat Drawer backed by the Cloudflare Worker Gemini proxy */

(function () {
  const STORAGE_KEY = "biomedrxiv_worker_url_override";

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
    pill.textContent = `Context: ${ctx.scope.label}`;
  }

  let lastHealthUrl = "";

  function updateSetupVisibility() {
    const setupBox = document.getElementById("chat-setup-box");
    const form = document.getElementById("chat-drawer-form");
    const suggestions = document.getElementById("chat-drawer-suggestions");
    const providerBar = document.getElementById("chat-provider-bar");
    const url = getWorkerUrl();
    if (url) {
      setupBox.style.display = "none";
      form.style.display = "flex";
      suggestions.style.display = "flex";
      if (providerBar) providerBar.style.display = "flex";
      refreshProviderHealth(url);
    } else {
      setupBox.style.display = "block";
      form.style.display = "none";
      suggestions.style.display = "none";
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
        gemini: `Gemini (${(providers.gemini.models && providers.gemini.models[0]) || "gemini-4-hydrogen"})`,
        groq: `Groq (${(providers.groq.models && providers.groq.models[0]) || "openai/gpt-oss-120b"})`,
        workers_ai: `Workers AI (${(providers.workers_ai.models && providers.workers_ai.models[0].split("/").pop()) || "llama-3.1-8b"})`,
      };
      for (const opt of select.options) {
        const key = opt.value;
        if (key === "auto" || !providers[key]) continue;
        const ok = Boolean(providers[key].configured);
        opt.disabled = !ok;
        opt.textContent = ok ? labels[key] : `${labels[key]} — not configured`;
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

  function appendMessage(role, meta, text, sources) {
    const log = document.getElementById("chat-drawer-log");
    const div = document.createElement("div");
    div.className = `chat-msg ${role === "user" ? "chat-msg-user" : "chat-msg-bot"}`;
    div.innerHTML =
      `<div class="chat-msg-meta">${esc(meta)}</div>` +
      `<div class="chat-msg-body">${role === "user" ? esc(text) : renderMarkdownLite(text)}</div>` +
      (role === "bot" ? renderSourcesHtml(sources) : "");
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
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
      const providerTag = data.provider ? `${data.provider} • ` : "";
      const ragTag = data.rag_used ? " • RAG" : "";
      appendMessage(
        "bot",
        `AI Analyst (${providerTag}${data.model || "gemini-4-hydrogen"}${ragTag})`,
        data.answer,
        data.sources
      );
    } catch (err) {
      appendMessage(
        "bot",
        "Connection Error",
        `Could not reach Worker at ${workerUrl}/ask: ${err.message}`
      );
    } finally {
      btn.disabled = false;
      btn.textContent = "Send";
      input.focus();
    }
  }

  function openDrawer() {
    document.getElementById("chat-drawer").classList.add("open");
    document.getElementById("chat-backdrop").classList.add("open");
    updateContextBanner();
    updateSetupVisibility();
  }

  function closeDrawer() {
    document.getElementById("chat-drawer").classList.remove("open");
    document.getElementById("chat-backdrop").classList.remove("open");
  }

  function initChatDrawer() {
    const openBtn = document.getElementById("open-chat-btn");
    const closeBtn = document.getElementById("close-chat-btn");
    const backdrop = document.getElementById("chat-backdrop");
    const form = document.getElementById("chat-drawer-form");
    const input = document.getElementById("chat-drawer-input");
    const saveUrlBtn = document.getElementById("chat-save-url-btn");
    const urlInput = document.getElementById("chat-worker-url-input");

    if (!openBtn) return;

    openBtn.addEventListener("click", openDrawer);
    closeBtn.addEventListener("click", closeDrawer);
    backdrop.addEventListener("click", closeDrawer);

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawer();
    });

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (!q) return;
      input.value = "";
      sendQuestion(q);
    });

    document.querySelectorAll(".chat-chip").forEach((chip) => {
      chip.addEventListener("click", () => sendQuestion(chip.textContent.trim()));
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

    // Keep context pill fresh whenever dashboard filters change
    window.addEventListener("biomedrxiv:filterchange", updateContextBanner);
  }

  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", initChatDrawer);
  } else {
    initChatDrawer();
  }
})();
