/**
 * Cloudflare Worker — Multi-Provider LLM Proxy for BioRxiv & MedRxiv Dashboard
 *
 * Step 1 of the Unified Architecture: Multi-Provider LLM Fallback Chain
 *   1. Primary:    Google Gemini Interactions API (gemini-4-hydrogen -> gemini-4-argon -> gemini-3.5-flash-lite)
 *   2. Fallback 1: Groq OpenAI-compatible API (openai/gpt-oss-120b -> qwen/qwen3.8-27b)
 *   3. Fallback 2: Cloudflare Workers AI binding (`env.AI`, 10,000 Neurons/day free)
 *
 * Zero external dependencies: deploy via `wrangler deploy` OR paste directly into
 * the Cloudflare Workers Web Dashboard editor.
 *
 * Secrets (Cloudflare Dashboard -> Settings -> Runtime variables and secrets):
 *   - GEMINI_API_KEY   (Primary — Google AI Studio)
 *   - GROQ_API_KEY     (Optional Fallback 1 — https://console.groq.com/keys)
 *
 * Bindings (Cloudflare Dashboard -> Settings -> Bindings -> Add binding -> Workers AI):
 *   - AI               (Optional Fallback 2 — zero API key required)
 *
 * Optional Text Variables:
 *   - GEMINI_MODEL           (default: "gemini-4-hydrogen")
 *   - GEMINI_FALLBACK_MODELS (default: "gemini-4-argon,gemini-3.5-flash-lite")
 *   - GROQ_MODELS            (default: "openai/gpt-oss-120b,qwen/qwen3.8-27b")
 *   - WORKERS_AI_MODELS      (default: "@cf/meta/llama-3.1-8b-instruct-fast,@cf/meta/llama-3.1-8b-instruct-fp8")
 *   - ALLOWED_ORIGINS        (default: "https://sumeetg23.github.io,http://127.0.0.1:8080,http://localhost:8080")
 *   - RATE_LIMIT_MAX         (default: "20" requests per window per IP)
 *   - RATE_LIMIT_WINDOW_SEC  (default: "600" seconds = 10 minutes)
 */

const GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1/interactions";
const GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions";

const DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen";
const DEFAULT_GEMINI_FALLBACKS = ["gemini-4-argon", "gemini-3.5-flash-lite"];
const DEFAULT_GROQ_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"];
const DEFAULT_WORKERS_AI_MODELS = [
  "@cf/meta/llama-3.1-8b-instruct-fast",
  "@cf/meta/llama-3.1-8b-instruct-fp8",
];

const MAX_QUESTION_CHARS = 1000;
const MAX_CONTEXT_BYTES = 45_000;

// Lightweight per-isolate sliding-window rate limiter (zero KV cost on Free tier)
const rateBuckets = new Map();

function parseCsvList(raw, fallback) {
  if (!raw || typeof raw !== "string") return fallback;
  const parsed = raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return parsed.length ? parsed : fallback;
}

function parseAllowedOrigins(env) {
  return parseCsvList(env && env.ALLOWED_ORIGINS, [
    "https://sumeetg23.github.io",
    "http://127.0.0.1:8080",
    "http://localhost:8080",
  ]);
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

  if (rateBuckets.size > 5000) {
    for (const [key, timestamps] of rateBuckets.entries()) {
      const fresh = timestamps.filter((ts) => ts > cutoff);
      if (fresh.length === 0) rateBuckets.delete(key);
      else rateBuckets.set(key, fresh);
    }
  }
  return { allowed: true, remaining: limit - existing.length, retryAfterSec: 0 };
}

function stripReasoningTags(text) {
  if (!text || typeof text !== "string") return "";
  return text.replace(/<think>[\s\S]*?<\/think>/gi, "").trim();
}

function extractGeminiOutputText(interaction) {
  if (!interaction || typeof interaction !== "object") return "";
  if (typeof interaction.output_text === "string" && interaction.output_text.trim()) {
    return interaction.output_text.trim();
  }
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

async function tryGemini(question, systemInstruction, env, attempts) {
  if (!env || !env.GEMINI_API_KEY) return null;

  const primary = env.GEMINI_MODEL || DEFAULT_GEMINI_MODEL;
  const fallbacks = parseCsvList(env.GEMINI_FALLBACK_MODELS, DEFAULT_GEMINI_FALLBACKS);
  const models = [primary, ...fallbacks.filter((m) => m !== primary)];

  for (const model of models) {
    try {
      const resp = await fetch(GEMINI_INTERACTIONS_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-goog-api-key": env.GEMINI_API_KEY,
        },
        body: JSON.stringify({
          model,
          input: question,
          system_instruction: systemInstruction,
          store: false,
        }),
      });

      const data = await resp.json();
      if (resp.ok) {
        const answer = stripReasoningTags(extractGeminiOutputText(data));
        if (answer) return { provider: "gemini", model, answer };
        attempts.push(`gemini/${model}: empty response`);
        continue;
      }

      const msg = (data && data.error && data.error.message) || `HTTP ${resp.status}`;
      attempts.push(`gemini/${model}: ${msg}`);
      // Stop trying other Gemini models only on auth/bad-request errors; continue on 429/5xx
      if (resp.status === 400 || resp.status === 401 || resp.status === 403) {
        return null;
      }
    } catch (err) {
      attempts.push(`gemini/${model}: ${err && err.message ? err.message : String(err)}`);
    }
  }
  return null;
}

async function tryGroq(question, systemInstruction, env, attempts) {
  if (!env || !env.GROQ_API_KEY) return null;

  const models = parseCsvList(env.GROQ_MODELS, DEFAULT_GROQ_MODELS);
  for (const model of models) {
    try {
      const resp = await fetch(GROQ_CHAT_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${env.GROQ_API_KEY}`,
        },
        body: JSON.stringify({
          model,
          temperature: 0.2,
          max_tokens: 900,
          messages: [
            { role: "system", content: systemInstruction },
            { role: "user", content: question },
          ],
        }),
      });

      const data = await resp.json();
      if (resp.ok) {
        const raw =
          data &&
          data.choices &&
          data.choices[0] &&
          data.choices[0].message &&
          data.choices[0].message.content;
        const answer = stripReasoningTags(raw);
        if (answer) return { provider: "groq", model, answer };
        attempts.push(`groq/${model}: empty response`);
        continue;
      }

      const msg = (data && data.error && data.error.message) || `HTTP ${resp.status}`;
      attempts.push(`groq/${model}: ${msg}`);
      if (resp.status === 401 || resp.status === 403) {
        return null;
      }
    } catch (err) {
      attempts.push(`groq/${model}: ${err && err.message ? err.message : String(err)}`);
    }
  }
  return null;
}

async function tryWorkersAI(question, systemInstruction, env, attempts) {
  if (!env || !env.AI || typeof env.AI.run !== "function") return null;

  const models = parseCsvList(env.WORKERS_AI_MODELS, DEFAULT_WORKERS_AI_MODELS);
  for (const model of models) {
    try {
      const result = await env.AI.run(model, {
        messages: [
          { role: "system", content: systemInstruction },
          { role: "user", content: question },
        ],
        max_tokens: 800,
        temperature: 0.2,
      });

      const raw =
        (typeof result === "string" ? result : null) ||
        (result && typeof result.response === "string" ? result.response : null) ||
        (result &&
          result.choices &&
          result.choices[0] &&
          result.choices[0].message &&
          result.choices[0].message.content);
      const answer = stripReasoningTags(raw);
      if (answer) return { provider: "workers-ai", model, answer };
      attempts.push(`workers-ai/${model}: empty response`);
    } catch (err) {
      attempts.push(`workers-ai/${model}: ${err && err.message ? err.message : String(err)}`);
    }
  }
  return null;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const corsOrigin = resolveCorsOrigin(request, env);

    if (request.method === "OPTIONS") {
      if (!corsOrigin) {
        return new Response("Origin not allowed", { status: 403 });
      }
      return new Response(null, { status: 204, headers: corsHeaders(corsOrigin) });
    }

    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/health")) {
      return jsonResponse(
        {
          status: "ok",
          service: "biomedrxiv-gemini-proxy",
          providers: {
            gemini: {
              configured: Boolean(env && env.GEMINI_API_KEY),
              primary_model: (env && env.GEMINI_MODEL) || DEFAULT_GEMINI_MODEL,
            },
            groq: {
              configured: Boolean(env && env.GROQ_API_KEY),
              models: parseCsvList(env && env.GROQ_MODELS, DEFAULT_GROQ_MODELS),
            },
            workers_ai: {
              configured: Boolean(env && env.AI && typeof env.AI.run === "function"),
              models: parseCsvList(env && env.WORKERS_AI_MODELS, DEFAULT_WORKERS_AI_MODELS),
            },
          },
        },
        200,
        corsOrigin || "*"
      );
    }

    if (request.method !== "POST" || url.pathname !== "/ask") {
      return jsonResponse({ error: "Not found. Use GET /health or POST /ask." }, 404, corsOrigin);
    }

    const reqOrigin = request.headers.get("Origin");
    if (reqOrigin && !corsOrigin) {
      return jsonResponse(
        { error: `Origin '${reqOrigin}' is not in ALLOWED_ORIGINS.` },
        403,
        null
      );
    }

    const hasAnyProvider = Boolean(
      (env && env.GEMINI_API_KEY) ||
        (env && env.GROQ_API_KEY) ||
        (env && env.AI && typeof env.AI.run === "function")
    );
    if (!hasAnyProvider) {
      return jsonResponse(
        {
          error:
            "Server misconfigured: set at least one provider (GEMINI_API_KEY, GROQ_API_KEY, or Workers AI binding `AI`).",
        },
        503,
        corsOrigin
      );
    }

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

    const systemInstruction = buildSystemInstruction(contextJson);
    const attempts = [];

    // Multi-provider fallback cascade: Gemini -> Groq -> Cloudflare Workers AI
    const result =
      (await tryGemini(question, systemInstruction, env, attempts)) ||
      (await tryGroq(question, systemInstruction, env, attempts)) ||
      (await tryWorkersAI(question, systemInstruction, env, attempts));

    if (result) {
      return jsonResponse(
        {
          answer: result.answer,
          provider: result.provider,
          model: result.model,
          fallback_used: result.provider !== "gemini",
          scope: contextObj.scope || null,
          remaining_requests: rl.remaining,
        },
        200,
        corsOrigin
      );
    }

    return jsonResponse(
      {
        error: `All configured LLM providers are currently unavailable. Attempts: ${attempts.join(" | ")}`,
      },
      503,
      corsOrigin
    );
  },
};
