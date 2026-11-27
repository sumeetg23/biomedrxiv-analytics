/**
 * Cloudflare Worker — Multi-Provider LLM + Vectorize RAG Proxy for BioRxiv & MedRxiv Dashboard
 *
 * Unified Architecture (Steps 1, 2 & 3):
 *   LLM Fallback Chain:
 *     1. Primary:    Google Gemini Interactions API (gemini-4-hydrogen -> gemini-4-argon -> gemini-3.5-flash-lite)
 *     2. Fallback 1: Groq OpenAI-compatible API (openai/gpt-oss-120b -> qwen/qwen3.8-27b)
 *     3. Fallback 2: Cloudflare Workers AI binding (`env.AI`, 10,000 Neurons/day free)
 *   Data Retrieval Tiers:
 *     - Tier 0: Active 4.7 KB Dashboard JSON slice (KPIs, trends, charts)
 *     - Tier 1: Cloudflare Vectorize RAG (`env.VECTORIZE` + `@cf/baai/bge-small-en-v1.5`)
 *               covering all 3,034 clinical trials + 399 NIH iCite papers + ~9,067 top preprints
 *     - Tier 2: Browser DuckDB-Wasm SQL over all 454,051 rows (`POST /sql-plan` + `POST /sql-summarize`,
 *               enabled on Gemini & Groq only; disabled on 8B Workers AI fallback)
 *
 * Zero external dependencies: deploy via `wrangler deploy` OR paste directly into
 * the Cloudflare Workers Web Dashboard editor.
 *
 * Secrets (Cloudflare Dashboard -> Settings -> Runtime variables and secrets):
 *   - GEMINI_API_KEY   (Primary — Google AI Studio)
 *   - GROQ_API_KEY     (Optional Fallback 1 — https://console.groq.com/keys)
 *
 * Bindings (Cloudflare Dashboard -> Settings -> Bindings):
 *   - AI               (Workers AI — powers Fallback 2 AND RAG query embeddings)
 *   - VECTORIZE        (Optional Vectorize index `biomedrxiv-rag` — enables Tier 1 RAG)
 *
 * Optional Text Variables:
 *   - GEMINI_MODEL           (default: "gemini-4-hydrogen")
 *   - GEMINI_FALLBACK_MODELS (default: "gemini-4-argon,gemini-3.5-flash-lite")
 *   - GROQ_MODELS            (default: "openai/gpt-oss-120b,qwen/qwen3.8-27b")
 *   - WORKERS_AI_MODELS      (default: "@cf/meta/llama-3.1-8b-instruct-fast,@cf/meta/llama-3.1-8b-instruct-fp8")
 *   - RAG_EMBEDDING_MODEL    (default: "@cf/baai/bge-small-en-v1.5")
 *   - RAG_TOP_K              (default: "8")
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
const DEFAULT_RAG_EMBEDDING_MODEL = "@cf/baai/bge-small-en-v1.5";
const DEFAULT_RAG_TOP_K = 8;
const RAG_MIN_SCORE = 0.3;

const MAX_QUESTION_CHARS = 1000;
const MAX_CONTEXT_BYTES = 45_000;
const MAX_SQL_ROWS_SUMMARY = 40;
const MAX_SQL_RESULT_BYTES = 28_000;
const SQL_FORBIDDEN_RE =
  /\b(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|COPY|EXPORT|IMPORT|PRAGMA|LOAD|INSTALL|CALL|EXECUTE|SET|RESET|VACUUM|CHECKPOINT|USE|SECRET|READ_CSV|READ_CSV_AUTO|READ_JSON|READ_JSON_AUTO|READ_PARQUET|READ_TEXT|READ_BLOB|PARQUET_SCAN|CSV_SCAN|SQLITE_SCAN|GLOB|HTTP_GET)\b/i;

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

function isRagConfigured(env) {
  return Boolean(
    env &&
      env.VECTORIZE &&
      typeof env.VECTORIZE.query === "function" &&
      env.AI &&
      typeof env.AI.run === "function"
  );
}

async function retrieveRagSources(question, contextObj, env) {
  if (!isRagConfigured(env)) return { sources: [], ragNote: null };

  const embedModel = (env && env.RAG_EMBEDDING_MODEL) || DEFAULT_RAG_EMBEDDING_MODEL;
  const topK = Math.min(15, Math.max(1, Number((env && env.RAG_TOP_K) || DEFAULT_RAG_TOP_K)));
  const scope = (contextObj && contextObj.scope) || {};

  // Build embedding query enriched with active category/server hints when narrowed
  const queryParts = [question];
  if (scope.server && scope.server !== "all") queryParts.push(`server: ${scope.server}`);
  if (scope.category && scope.category !== "all") queryParts.push(`category: ${scope.category}`);
  const embedInput = queryParts.join(" | ").slice(0, 600);

  try {
    const emb = await env.AI.run(embedModel, { text: [embedInput] });
    const vector =
      (emb && Array.isArray(emb.data) && Array.isArray(emb.data[0]) && emb.data[0]) ||
      (emb && emb.result && Array.isArray(emb.result.data) && emb.result.data[0]) ||
      null;
    if (!vector) return { sources: [], ragNote: "embedding_unavailable" };

    const queryResult = await env.VECTORIZE.query(vector, {
      topK: Math.min(24, topK * 2 + 4),
      returnMetadata: "all",
    });
    const rawMatches = (queryResult && Array.isArray(queryResult.matches) && queryResult.matches) || [];
    if (!rawMatches.length) return { sources: [], ragNote: "no_matches" };

    const qLower = question.toLowerCase();
    const wantsTrials = /\b(trial|trials|nct\d*|phase\s*[1-4i]|enrollment|sponsor|randomized|clinicaltrials)\b/i.test(
      qLower
    );
    const wantsDownstream = /\b(icite|downstream|translational|apt|rcr|cited by clinical|guideline)\b/i.test(qLower);

    const scored = rawMatches
      .filter((m) => m && m.metadata && (typeof m.score !== "number" || m.score >= RAG_MIN_SCORE))
      .map((m) => {
        const meta = m.metadata;
        let rankScore = typeof m.score === "number" ? m.score : 0;
        if (scope.server && scope.server !== "all" && meta.server === scope.server) rankScore += 0.05;
        if (scope.category && scope.category !== "all" && meta.category === scope.category) rankScore += 0.08;
        if (
          typeof scope.year_start === "number" &&
          typeof scope.year_end === "number" &&
          typeof meta.year === "number" &&
          meta.year >= scope.year_start &&
          meta.year <= scope.year_end
        ) {
          rankScore += 0.03;
        }
        if (wantsTrials && meta.doc_type === "clinical_trial") rankScore += 0.06;
        if (wantsDownstream && meta.doc_type === "downstream_clinical") rankScore += 0.06;

        return {
          id: m.id,
          score: typeof m.score === "number" ? Number(m.score.toFixed(4)) : null,
          _rank: rankScore,
          doc_type: meta.doc_type || "preprint",
          title: meta.title || "",
          doi: meta.doi || "",
          doi_link: meta.doi_link || (meta.doi ? `https://doi.org/${meta.doi}` : ""),
          server: meta.server || null,
          category: meta.category || null,
          year: typeof meta.year === "number" ? meta.year : null,
          institution: meta.institution || null,
          country: meta.country || null,
          published: typeof meta.published === "boolean" ? meta.published : null,
          publisher: meta.publisher || null,
          published_doi: meta.published_doi || null,
          trial_id: meta.trial_id || null,
          registry: meta.registry || null,
          phase: meta.phase || null,
          status: meta.status || null,
          sponsor: meta.sponsor || null,
          enrollment: typeof meta.enrollment === "number" ? meta.enrollment : null,
          conditions: meta.conditions || null,
          cited_by_clin: typeof meta.cited_by_clin === "number" ? meta.cited_by_clin : null,
          apt: typeof meta.apt === "number" ? meta.apt : null,
          rcr: typeof meta.rcr === "number" ? meta.rcr : null,
          topics: meta.topics || null,
        };
      })
      .sort((a, b) => b._rank - a._rank)
      .slice(0, topK)
      .map(({ _rank, ...clean }) => clean);

    return { sources: scored, ragNote: null };
  } catch (err) {
    return {
      sources: [],
      ragNote: `rag_error:${err && err.message ? err.message : String(err)}`,
    };
  }
}

function formatRagEvidenceForPrompt(sources) {
  if (!sources || !sources.length) return "";
  const compact = sources.map((s, idx) => {
    const entry = {
      rank: idx + 1,
      type: s.doc_type,
      title: s.title,
      doi: s.doi,
      server: s.server,
      category: s.category,
      year: s.year,
    };
    if (s.institution) entry.institution = s.country ? `${s.institution} (${s.country})` : s.institution;
    if (s.published !== null) entry.published = s.published;
    if (s.publisher) entry.publisher = s.publisher;
    if (s.published_doi) entry.published_doi = s.published_doi;
    if (s.trial_id) entry.trial_id = s.trial_id;
    if (s.phase) entry.phase = s.phase;
    if (s.status) entry.status = s.status;
    if (s.enrollment !== null) entry.enrollment = s.enrollment;
    if (s.sponsor) entry.sponsor = s.sponsor;
    if (s.conditions) entry.conditions = s.conditions;
    if (s.cited_by_clin !== null) entry.cited_by_clin = s.cited_by_clin;
    if (s.apt !== null) entry.apt = s.apt;
    if (s.rcr !== null) entry.rcr = s.rcr;
    return entry;
  });
  return JSON.stringify(compact);
}

function validateReadOnlySql(rawSql) {
  if (!rawSql || typeof rawSql !== "string") {
    return { ok: false, error: "Empty SQL query." };
  }
  let sql = rawSql
    .replace(/^\s*```(?:sql)?\s*/i, "")
    .replace(/\s*```\s*$/, "")
    .trim();
  // Remove single trailing semicolon
  sql = sql.replace(/;\s*$/, "").trim();
  if (!sql) return { ok: false, error: "Empty SQL query." };
  if (sql.length > 4000) return { ok: false, error: "SQL query exceeds 4,000 characters." };
  if (!/^(SELECT|WITH)\b/i.test(sql)) {
    return { ok: false, error: "Only read-only SELECT / WITH queries are permitted." };
  }
  if (sql.includes(";")) {
    return { ok: false, error: "Multiple SQL statements are not permitted." };
  }
  if (SQL_FORBIDDEN_RE.test(sql)) {
    return { ok: false, error: "SQL contains a forbidden keyword or file/network function." };
  }
  if (!/\bLIMIT\s+\d+\b/i.test(sql)) {
    sql = `${sql}\nLIMIT 100`;
  }
  return { ok: true, sql };
}

function parseSqlPlanJson(rawText) {
  if (!rawText || typeof rawText !== "string") return null;
  let cleaned = stripReasoningTags(rawText)
    .replace(/^\s*```(?:json)?\s*/i, "")
    .replace(/\s*```\s*$/, "")
    .trim();
  const firstBrace = cleaned.indexOf("{");
  const lastBrace = cleaned.lastIndexOf("}");
  if (firstBrace !== -1 && lastBrace > firstBrace) {
    cleaned = cleaned.slice(firstBrace, lastBrace + 1);
  }
  try {
    const obj = JSON.parse(cleaned);
    if (obj && typeof obj === "object") return obj;
  } catch {
    return null;
  }
  return null;
}

function buildSqlPlannerSystemPrompt(contextObj, previousSql, previousError) {
  const scope = (contextObj && contextObj.scope) || {};
  const scopeDesc = JSON.stringify({
    label: scope.label || "2013 - 2026 | All Servers",
    server: scope.server || "all",
    category: scope.category || "all",
    year_start: scope.year_start || 2013,
    year_end: scope.year_end || 2026,
  });

  const lines = [
    "You are a DuckDB SQL planner for the BioRxiv & MedRxiv Preprint Analytics Dashboard (454,051 canonical preprints).",
    "Decide whether the user's question needs an exact read-only DuckDB SQL query over the full dataset, OR can already be answered accurately from the high-level dashboard summary + semantic RAG.",
    "",
    "SET `needs_sql: true` when the user asks for:",
    "- Exact counts, rankings, filtering, sorting, percentiles, or multi-condition combinations across all 454,051 preprints, 3,034 clinical trials, or 399 NIH iCite papers (e.g., specific journals/publishers, exact enrollments, specific institutions/countries, specific years + keywords + publication status, version thresholds, top-N tables).",
    "SET `needs_sql: false` when the user asks for:",
    "- High-level summary of the active dashboard slice, general trends already shown on charts, or conceptual explanations.",
    "",
    "AVAILABLE DUCKDB VIEWS (query ONLY these 3 views — NEVER call read_parquet/read_csv):",
    "1. `preprints` (454,051 unique DOIs, 1 row per preprint):",
    "   - doi VARCHAR, title VARCHAR, server VARCHAR ('biorxiv'|'medrxiv'), category VARCHAR (lowercase, e.g. 'neuroscience', 'infectious diseases (except hiv/aids)', 'epidemiology', 'bioinformatics'),",
    "   - year SMALLINT, first_posted_date DATE, latest_posted_date DATE, num_versions SMALLINT, author_count SMALLINT, revision_span_days INTEGER,",
    "   - is_published BOOLEAN, published_doi VARCHAR, publisher_name VARCHAR,",
    "   - institution VARCHAR (ROR-normalized canonical name, e.g. 'Harvard University', 'Stanford University'), country VARCHAR (e.g. 'United States', 'United Kingdom', 'Germany', 'China'), institution_type VARCHAR,",
    "   - ever_withdrawn BOOLEAN,",
    "   - Topic flags (BOOLEAN): kw_covid_sarscov2, kw_single_cell, kw_deep_learning, kw_crispr, kw_alphafold, kw_llm_genai, kw_spatial_omics, kw_mrna_vaccine, kw_glp1.",
    "2. `clinical_trials` (3,034 trial-linked preprints):",
    "   - doi VARCHAR, trial_id VARCHAR (e.g. 'NCT04280705'), registry VARCHAR, title VARCHAR, server VARCHAR, category VARCHAR, year SMALLINT,",
    "   - is_published BOOLEAN, published_doi VARCHAR, publisher_name VARCHAR, institution VARCHAR, country VARCHAR,",
    "   - phase VARCHAR (e.g. 'PHASE3', 'PHASE2', 'PHASE1', 'PHASE2/PHASE3', 'NA'), overall_status VARCHAR (e.g. 'COMPLETED', 'RECRUITING'),",
    "   - sponsor_class VARCHAR ('INDUSTRY'|'OTHER'|'NIH'|'FED'|'NETWORK'), sponsor_name VARCHAR, enrollment_count INTEGER, has_results BOOLEAN, conditions VARCHAR (comma-separated).",
    "3. `downstream_clinical` (399 mature published papers linked to NIH iCite):",
    "   - doi VARCHAR, published_doi VARCHAR, pmid VARCHAR, title VARCHAR, server VARCHAR, category VARCHAR, year SMALLINT, publisher_name VARCHAR, institution VARCHAR, country VARCHAR,",
    "   - is_clinical BOOLEAN, apt DOUBLE, cited_by_clin_count INTEGER, citation_count INTEGER, relative_citation_ratio DOUBLE.",
    "",
    "IMPORTANT DOMAIN TIPS:",
    "- Journal matching: `publisher_name` stores the publisher (e.g. 'Massachusetts Medical Society' for NEJM, 'Elsevier' for The Lancet, 'Springer Nature', 'BMJ', 'American Medical Association (AMA)'), while `published_doi` identifies specific journals:",
    "  * NEJM: published_doi ILIKE '10.1056/%' OR publisher_name ILIKE '%Massachusetts Medical%' OR publisher_name ILIKE '%New England%'",
    "  * Lancet: published_doi ILIKE '10.1016/s0140-6736%' OR published_doi ILIKE '10.1016/s2%' OR publisher_name ILIKE '%Lancet%'",
    "  * JAMA: published_doi ILIKE '10.1001/%' OR publisher_name ILIKE '%American Medical Association%'",
    "  * BMJ: published_doi ILIKE '10.1136/bmj%' OR publisher_name ILIKE '%BMJ%'",
    "  * Nature family: published_doi ILIKE '10.1038/%'",
    "  * Science family: published_doi ILIKE '10.1126/%'",
    "- Phase matching: use `phase ILIKE '%PHASE3%'` (because combined phases like 'PHASE2/PHASE3' exist).",
    "- Respect the user's ACTIVE DASHBOARD FILTER SLICE below unless the question explicitly specifies a different server/year/category:",
    `  Active Slice: ${scopeDesc}`,
    "- Always include `ORDER BY` + `LIMIT` (<= 50 rows for lists, <= 100 for aggregates) and keep column aliases short and clear.",
    "- Output STRICT valid JSON ONLY (no markdown fences, no commentary): {\"needs_sql\": boolean, \"reason\": string, \"sql\": string|null}",
  ];

  if (previousSql && previousError) {
    lines.push(
      "",
      "PREVIOUS SQL FAILED IN DUCKDB — FIX IT:",
      `Previous SQL: ${previousSql}`,
      `DuckDB Error: ${previousError}`
    );
  }
  return lines.join("\n");
}

function buildSqlSummarySystemPrompt(contextJson, sql, columns, rows, totalRows) {
  return [
    "You are a concise biomedical data analyst embedded in the BioRxiv & MedRxiv Preprint Analytics Dashboard.",
    "An exact read-only DuckDB SQL query just ran directly in the user's browser across all 454,051 canonical preprints / 3,034 clinical trials / 399 NIH iCite papers.",
    "Rules:",
    "1. Answer the user's question STRICTLY from the SQL Query Result below (and Active Dashboard Slice only for background context if needed).",
    "2. Never invent rows, counts, DOIs, NCT IDs, or journals not present in the SQL result.",
    "3. If the SQL returned 0 rows, state that transparently and briefly suggest broadening a filter (e.g., publisher/DOI prefix or phase).",
    "4. Keep answers clear, crisp, and scannable (bullet points, comma-formatted integers, 1–2 decimal percentages). Briefly note peer-review right-censoring if comparing 2025–2026 publication rates.",
    "",
    `EXECUTED DUCKDB SQL:\n${sql}`,
    `COLUMNS: ${JSON.stringify(columns)}`,
    `TOTAL ROWS RETURNED: ${totalRows} (showing up to ${rows.length} rows below):`,
    JSON.stringify(rows),
    "",
    "ACTIVE DASHBOARD CONTEXT (for scope reference):",
    contextJson.slice(0, 4000),
  ].join("\n");
}

async function runProviderCall(requestedProvider, question, systemInstruction, env, attempts, allowWorkersAi = true) {
  if (requestedProvider === "gemini") {
    if (!env || !env.GEMINI_API_KEY) {
      return { error: "Provider 'gemini' is not configured (missing GEMINI_API_KEY).", status: 503 };
    }
    const r = await tryGemini(question, systemInstruction, env, attempts);
    return r ? { result: r } : null;
  }
  if (requestedProvider === "groq") {
    if (!env || !env.GROQ_API_KEY) {
      return { error: "Provider 'groq' is not configured (missing GROQ_API_KEY).", status: 503 };
    }
    const r = await tryGroq(question, systemInstruction, env, attempts);
    return r ? { result: r } : null;
  }
  if (requestedProvider === "workers_ai") {
    if (!allowWorkersAi) {
      return { error: "SQL planning is disabled on the lightweight Workers AI 8B model.", status: 400 };
    }
    if (!env || !env.AI || typeof env.AI.run !== "function") {
      return { error: "Provider 'workers_ai' is not configured (missing Workers AI binding `AI`).", status: 503 };
    }
    const r = await tryWorkersAI(question, systemInstruction, env, attempts);
    return r ? { result: r } : null;
  }
  // Auto cascade
  const r =
    (await tryGemini(question, systemInstruction, env, attempts)) ||
    (await tryGroq(question, systemInstruction, env, attempts)) ||
    (allowWorkersAi ? await tryWorkersAI(question, systemInstruction, env, attempts) : null);
  return r ? { result: r } : null;
}

function buildSystemInstruction(contextJson, ragSources) {
  const hasRag = Array.isArray(ragSources) && ragSources.length > 0;
  const lines = [
    "You are a concise biomedical data analyst embedded in the BioRxiv & MedRxiv Preprint Analytics Dashboard.",
    hasRag
      ? "Answer the user's question STRICTLY from (A) the Active Dashboard Data Slice JSON (aggregate counts/KPIs/trends) and (B) the Retrieved Paper- & Trial-Level Evidence below (semantic search matches from our indexed corpus of 3,034 clinical trials, 400 NIH iCite translational papers, and ~9,000 top preprints)."
      : "Answer the user's question STRICTLY from the JSON data slice provided below (reflecting the user's currently active Subject Category, Year Range, and Server filters).",
    "Rules:",
    "1. Never invent statistics, journals, trial IDs, enrollments, DOIs, or institutions not present in the provided data.",
    hasRag
      ? "2. Use the Dashboard JSON for corpus-wide totals, rates, and trends; cite specific paper titles, DOIs, NCT IDs, phases, enrollments, and publishers from the Retrieved Evidence when relevant. Note clearly that semantic retrieval returns the top matching records from the indexed subset (~12.5k records), not an exhaustive SQL scan across all 454,051 rows (for exhaustive SQL, mention `scripts/run_qa_app.py`)."
      : "2. If the question asks for paper-level ad-hoc SQL outside this aggregated slice (e.g., an unlisted specific paper or abstract text), clearly state what IS in the current slice and mention they can query all 454k rows locally via `scripts/run_qa_app.py` (DuckDB).",
    "3. When comparing recent years (2025–2026), briefly note peer-review right-censoring.",
    "4. Keep answers clear, well-structured, and concise (bullet points + exact numbers with commas and 1–2 decimal percentages).",
    "",
    "ACTIVE DASHBOARD DATA SLICE (JSON):",
    contextJson,
  ];
  if (hasRag) {
    lines.push(
      "",
      `RETRIEVED PAPER- & TRIAL-LEVEL EVIDENCE (Top ${ragSources.length} semantic matches, JSON):`,
      formatRagEvidenceForPrompt(ragSources)
    );
  }
  return lines.join("\n");
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
          rag: {
            configured: isRagConfigured(env),
            embedding_model: (env && env.RAG_EMBEDDING_MODEL) || DEFAULT_RAG_EMBEDDING_MODEL,
            top_k: Number((env && env.RAG_TOP_K) || DEFAULT_RAG_TOP_K),
          },
          sql_wasm: {
            configured: Boolean((env && env.GEMINI_API_KEY) || (env && env.GROQ_API_KEY)),
            eligible_providers: ["gemini", "groq"],
            views: ["preprints", "clinical_trials", "downstream_clinical"],
          },
        },
        200,
        corsOrigin || "*"
      );
    }

    const ALLOWED_POST_PATHS = ["/ask", "/sql-plan", "/sql-summarize"];
    if (request.method !== "POST" || !ALLOWED_POST_PATHS.includes(url.pathname)) {
      return jsonResponse(
        { error: "Not found. Use GET /health or POST /ask, /sql-plan, /sql-summarize." },
        404,
        corsOrigin
      );
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

    const VALID_PROVIDERS = ["auto", "gemini", "groq", "workers_ai", "workers-ai"];
    const rawProvider =
      typeof body.provider === "string" && body.provider.trim()
        ? body.provider.trim().toLowerCase()
        : "auto";
    if (!VALID_PROVIDERS.includes(rawProvider)) {
      return jsonResponse(
        { error: `Invalid provider '${rawProvider}'. Expected one of: auto, gemini, groq, workers_ai.` },
        400,
        corsOrigin
      );
    }
    const requestedProvider = rawProvider === "workers-ai" ? "workers_ai" : rawProvider;

    // ------------------------------------------------------------------
    // Route 1: POST /sql-plan (Gemini & Groq ONLY — disabled on 8B Workers AI)
    // ------------------------------------------------------------------
    if (url.pathname === "/sql-plan") {
      const hasSqlCapableProvider = Boolean((env && env.GEMINI_API_KEY) || (env && env.GROQ_API_KEY));
      if (requestedProvider === "workers_ai" || !hasSqlCapableProvider) {
        return jsonResponse(
          {
            needs_sql: false,
            sql: null,
            reason: "SQL generation is enabled for Gemini and Groq; using Vectorize RAG for Workers AI.",
            remaining_requests: rl.remaining,
          },
          200,
          corsOrigin
        );
      }

      const prevSql = typeof body.previous_sql === "string" ? body.previous_sql.slice(0, 2000) : "";
      const prevErr = typeof body.previous_error === "string" ? body.previous_error.slice(0, 600) : "";
      const planSystemPrompt = buildSqlPlannerSystemPrompt(contextObj, prevSql, prevErr);
      const attempts = [];
      const call = await runProviderCall(requestedProvider, question, planSystemPrompt, env, attempts, false);
      if (call && call.error) {
        return jsonResponse({ error: call.error }, call.status || 503, corsOrigin);
      }
      if (!call || !call.result) {
        return jsonResponse(
          {
            needs_sql: false,
            sql: null,
            reason: `SQL planner unavailable (${attempts.join(" | ")}); falling back to RAG.`,
            remaining_requests: rl.remaining,
          },
          200,
          corsOrigin
        );
      }

      const parsed = parseSqlPlanJson(call.result.answer);
      if (!parsed || !parsed.needs_sql || !parsed.sql) {
        return jsonResponse(
          {
            needs_sql: false,
            sql: null,
            reason: (parsed && parsed.reason) || "Answerable directly from dashboard summary + Vectorize RAG.",
            provider: call.result.provider,
            model: call.result.model,
            remaining_requests: rl.remaining,
          },
          200,
          corsOrigin
        );
      }

      const checked = validateReadOnlySql(parsed.sql);
      if (!checked.ok) {
        return jsonResponse(
          {
            needs_sql: false,
            sql: null,
            reason: `Generated SQL rejected by safety validator: ${checked.error}`,
            provider: call.result.provider,
            model: call.result.model,
            remaining_requests: rl.remaining,
          },
          200,
          corsOrigin
        );
      }

      return jsonResponse(
        {
          needs_sql: true,
          sql: checked.sql,
          reason: parsed.reason || "Running read-only DuckDB SQL over all 454,051 preprints.",
          provider: call.result.provider,
          model: call.result.model,
          remaining_requests: rl.remaining,
        },
        200,
        corsOrigin
      );
    }

    // ------------------------------------------------------------------
    // Route 2: POST /sql-summarize (Synthesizes browser DuckDB-Wasm rows)
    // ------------------------------------------------------------------
    if (url.pathname === "/sql-summarize") {
      const executedSql = typeof body.sql === "string" ? body.sql.trim() : "";
      const columns = Array.isArray(body.columns) ? body.columns.slice(0, 40) : [];
      const rawRows = Array.isArray(body.rows) ? body.rows.slice(0, MAX_SQL_ROWS_SUMMARY) : [];
      const totalRows = typeof body.total_rows === "number" ? body.total_rows : rawRows.length;

      const checked = validateReadOnlySql(executedSql);
      if (!checked.ok) {
        return jsonResponse({ error: `Invalid SQL provided for summary: ${checked.error}` }, 400, corsOrigin);
      }
      if (JSON.stringify(rawRows).length > MAX_SQL_RESULT_BYTES) {
        return jsonResponse({ error: "SQL result payload too large to summarize." }, 413, corsOrigin);
      }

      const summaryPrompt = buildSqlSummarySystemPrompt(contextJson, checked.sql, columns, rawRows, totalRows);
      const attempts = [];
      const call = await runProviderCall(requestedProvider, question, summaryPrompt, env, attempts, true);
      if (call && call.error) {
        return jsonResponse({ error: call.error }, call.status || 503, corsOrigin);
      }
      if (call && call.result) {
        return jsonResponse(
          {
            answer: call.result.answer,
            provider: call.result.provider,
            model: call.result.model,
            requested_provider: requestedProvider,
            fallback_used: requestedProvider === "auto" && call.result.provider !== "gemini",
            sql_used: true,
            scope: contextObj.scope || null,
            remaining_requests: rl.remaining,
          },
          200,
          corsOrigin
        );
      }
      return jsonResponse(
        { error: `Could not summarize SQL results. Attempts: ${attempts.join(" | ")}` },
        503,
        corsOrigin
      );
    }

    // ------------------------------------------------------------------
    // Route 3: POST /ask (Tier 0 Dashboard JSON + Tier 1 Vectorize RAG)
    // ------------------------------------------------------------------
    const useRag = body.use_rag !== false;
    const ragResult = useRag
      ? await retrieveRagSources(question, contextObj, env)
      : { sources: [], ragNote: "disabled_by_client" };
    const ragSources = ragResult.sources;

    const systemInstruction = buildSystemInstruction(contextJson, ragSources);
    const attempts = [];
    const call = await runProviderCall(requestedProvider, question, systemInstruction, env, attempts, true);
    if (call && call.error) {
      return jsonResponse({ error: call.error }, call.status || 503, corsOrigin);
    }

    if (call && call.result) {
      return jsonResponse(
        {
          answer: call.result.answer,
          provider: call.result.provider,
          model: call.result.model,
          requested_provider: requestedProvider,
          fallback_used: requestedProvider === "auto" && call.result.provider !== "gemini",
          rag_used: ragSources.length > 0,
          sources: ragSources,
          scope: contextObj.scope || null,
          remaining_requests: rl.remaining,
        },
        200,
        corsOrigin
      );
    }

    return jsonResponse(
      {
        error: `Requested provider (${requestedProvider}) unavailable. Attempts: ${attempts.join(" | ")}`,
      },
      503,
      corsOrigin
    );
  },
};
