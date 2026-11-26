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

  function updateSetupVisibility() {
    const setupBox = document.getElementById("chat-setup-box");
    const form = document.getElementById("chat-drawer-form");
    const suggestions = document.getElementById("chat-drawer-suggestions");
    const url = getWorkerUrl();
    if (url) {
      setupBox.style.display = "none";
      form.style.display = "flex";
      suggestions.style.display = "flex";
    } else {
      setupBox.style.display = "block";
      form.style.display = "none";
      suggestions.style.display = "none";
    }
  }

  function appendMessage(role, meta, text) {
    const log = document.getElementById("chat-drawer-log");
    const div = document.createElement("div");
    div.className = `chat-msg ${role === "user" ? "chat-msg-user" : "chat-msg-bot"}`;
    div.innerHTML =
      `<div class="chat-msg-meta">${esc(meta)}</div>` +
      `<div class="chat-msg-body">${role === "user" ? esc(text) : renderMarkdownLite(text)}</div>`;
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

    try {
      const resp = await fetch(`${workerUrl}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, context: ctx }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || `HTTP ${resp.status}`);
      }
      appendMessage(
        "bot",
        `Gemini Analyst (${data.model || "gemini-4-hydrogen"})`,
        data.answer
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
