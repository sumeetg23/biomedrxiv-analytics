/**
 * Cloudflare Worker — Gemini Interactions API Proxy for BioRxiv & MedRxiv Dashboard
 *
 * Zero external dependencies: deploy via `wrangler deploy` OR paste directly into
 * the Cloudflare Workers Web Dashboard editor.
 *
 * Required Secret (set in Cloudflare Dashboard -> Settings -> Variables & Secrets):
 *   - GEMINI_API_KEY
 *
 * Optional Environment Variables (configured in wrangler.toml):
 *   - GEMINI_MODEL           (default: "gemini-4-hydrogen")
 *   - ALLOWED_ORIGINS        (default: "https://sumeetg23.github.io,http://127.0.0.1:8080,http://localhost:8080")
 *   - RATE_LIMIT_MAX         (default: "20" requests per window per IP)
 *   - RATE_LIMIT_WINDOW_SEC  (default: "600" seconds = 10 minutes)
 */

const GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1/interactions";
const DEFAULT_MODEL = "gemini-4-hydrogen";
const MAX_QUESTION_CHARS = 1000;
const MAX_CONTEXT_BYTES = 45_000;

// Lightweight per-isolate sliding-window rate limiter (zero KV cost on Free tier)
const rateBuckets = new Map();

function parseAllowedOrigins(env) {
  const raw =
    (env && env.ALLOWED_ORIGINS) ||
    "https://sumeetg23.github.io,http://127.0.0.1:8080,http://localhost:8080";
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function resolveCorsOrigin(request, env) {
  const origin = request.headers.get("Origin");
  if (!origin) return null;
  const allowed = parseAllowedOrigins(env);
  if (allowed.includes("*")) return "*";
  return allowed.includes(origin) ? origin : null;
}

function corsHeaders(corsOrigin) {
  const headers = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
  if (corsOrigin) {
    headers["Access-Control-Allow-Origin"] = corsOrigin;
  }
  return headers;
}

function jsonResponse(body, status, corsOrigin) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...corsHeaders(corsOrigin),
    },
  });
}

function checkRateLimit(ip, env) {
  const limit = Number((env && env.RATE_LIMIT_MAX) || 20);
  const windowMs = Number((env && env.RATE_LIMIT_WINDOW_SEC) || 600) * 1000;
  const now = Date.now();
  const cutoff = now - windowMs;

  const existing = (rateBuckets.get(ip) || []).filter((ts) => ts > cutoff);
  if (existing.length >= limit) {
    const retryAfterSec = Math.max(1, Math.ceil((existing[0] + windowMs - now) / 1000));
    rateBuckets.set(ip, existing);
    return { allowed: false, remaining: 0, retryAfterSec };
  }

  existing.push(now);
  rateBuckets.set(ip, existing);

  // Prevent unbounded Map growth in long-lived isolates
  if (rateBuckets.size > 5000) {
    for (const [key, timestamps] of rateBuckets.entries()) {
      const fresh = timestamps.filter((ts) => ts > cutoff);
      if (fresh.length === 0) rateBuckets.delete(key);
      else rateBuckets.set(key, fresh);
    }
  }
  return { allowed: true, remaining: limit - existing.length, retryAfterSec: 0 };
}

function extractOutputText(interaction) {
  if (!interaction || typeof interaction !== "object") return "";
  if (typeof interaction.output_text === "string" && interaction.output_text.trim()) {
    return interaction.output_text.trim();
  }

  // Modern Interactions API v1 schema: steps[] -> model_output -> content[] -> text
  if (Array.isArray(interaction.steps)) {
    const chunks = [];
    for (const step of interaction.steps) {
      if (step && step.type === "model_output" && Array.isArray(step.content)) {
        for (const part of step.content) {
          if (part && part.type === "text" && typeof part.text === "string") {
            chunks.push(part.text);
          }
        }
      }
    }
    if (chunks.length) return chunks.join("\n").trim();
  }

  // Fallback for outputs[] shape if returned
  if (Array.isArray(interaction.outputs)) {
    const chunks = [];
    for (const out of interaction.outputs) {
      if (out && out.type === "text" && typeof out.text === "string") {
        chunks.push(out.text);
      }
    }
    if (chunks.length) return chunks.join("\n").trim();
  }

  return "";
}

function buildSystemInstruction(contextJson) {
  return [
    "You are a concise biomedical data analyst embedded in the BioRxiv & MedRxiv Preprint Analytics Dashboard.",
    "Answer the user's question STRICTLY from the JSON data slice provided below (reflecting the user's currently active Subject Category, Year Range, and Server filters).",
    "Rules:",
    "1. Never invent statistics, journals, trial IDs, or institutions not present in the provided JSON slice.",
    "2. If the question asks for paper-level ad-hoc SQL outside this aggregated slice (e.g., an unlisted specific paper or abstract text), clearly state what IS in the current slice and mention they can query all 454k rows locally via `scripts/run_qa_app.py` (DuckDB).",
    "3. When comparing recent years (2025–2026), briefly note peer-review right-censoring.",
    "4. Keep answers clear, well-structured, and concise (bullet points + exact numbers with commas and 1–2 decimal percentages).",
    "",
    "ACTIVE DASHBOARD DATA SLICE (JSON):",
    contextJson,
  ].join("\n");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const corsOrigin = resolveCorsOrigin(request, env);

    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      if (!corsOrigin) {
        return new Response("Origin not allowed", { status: 403 });
      }
      return new Response(null, { status: 204, headers: corsHeaders(corsOrigin) });
    }

    // Health check
    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/health")) {
      return jsonResponse(
        {
          status: "ok",
          service: "biomedrxiv-gemini-proxy",
          model: (env && env.GEMINI_MODEL) || DEFAULT_MODEL,
          api_key_configured: Boolean(env && env.GEMINI_API_KEY),
        },
        200,
        corsOrigin || "*"
      );
    }

    if (request.method !== "POST" || url.pathname !== "/ask") {
      return jsonResponse({ error: "Not found. Use GET /health or POST /ask." }, 404, corsOrigin);
    }

    // Enforce CORS origin allowlist for browser requests
    const reqOrigin = request.headers.get("Origin");
    if (reqOrigin && !corsOrigin) {
      return jsonResponse(
        { error: `Origin '${reqOrigin}' is not in ALLOWED_ORIGINS.` },
        403,
        null
      );
    }

    if (!env || !env.GEMINI_API_KEY) {
      return jsonResponse(
        { error: "Server misconfigured: GEMINI_API_KEY secret is not set on this Worker." },
        503,
        corsOrigin
      );
    }

    // Per-IP rate limit
    const clientIp = request.headers.get("CF-Connecting-IP") || "unknown";
    const rl = checkRateLimit(clientIp, env);
    if (!rl.allowed) {
      return jsonResponse(
        {
          error: `Rate limit exceeded. Please wait ${rl.retryAfterSec} seconds before asking again.`,
        },
        429,
        corsOrigin
      );
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResponse({ error: "Invalid JSON request body." }, 400, corsOrigin);
    }

    const question = typeof body.question === "string" ? body.question.trim() : "";
    if (question.length < 2 || question.length > MAX_QUESTION_CHARS) {
      return jsonResponse(
        { error: `Question must be between 2 and ${MAX_QUESTION_CHARS} characters.` },
        400,
        corsOrigin
      );
    }

    const contextObj = body.context && typeof body.context === "object" ? body.context : {};
    const contextJson = JSON.stringify(contextObj);
    if (contextJson.length > MAX_CONTEXT_BYTES) {
      return jsonResponse(
        { error: `Context payload too large (${contextJson.length} bytes; max ${MAX_CONTEXT_BYTES}).` },
        413,
        corsOrigin
      );
    }

    const model = (env && env.GEMINI_MODEL) || DEFAULT_MODEL;
    const geminiPayload = {
      model,
      input: question,
      system_instruction: buildSystemInstruction(contextJson),
      store: false,
    };

    try {
      const upstream = await fetch(GEMINI_INTERACTIONS_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-goog-api-key": env.GEMINI_API_KEY,
        },
        body: JSON.stringify(geminiPayload),
      });

      const upstreamData = await upstream.json();
      if (!upstream.ok) {
        const msg =
          (upstreamData && upstreamData.error && upstreamData.error.message) ||
          `Gemini API HTTP ${upstream.status}`;
        return jsonResponse({ error: msg }, upstream.status >= 500 ? 502 : upstream.status, corsOrigin);
      }

      const answer = extractOutputText(upstreamData);
      if (!answer) {
        return jsonResponse(
          { error: "Gemini returned an empty response." },
          502,
          corsOrigin
        );
      }

      return jsonResponse(
        {
          answer,
          model,
          scope: contextObj.scope || null,
          remaining_requests: rl.remaining,
        },
        200,
        corsOrigin
      );
    } catch (err) {
      return jsonResponse(
        { error: `Failed to reach Gemini API: ${err && err.message ? err.message : String(err)}` },
        502,
        corsOrigin
      );
    }
  },
};
