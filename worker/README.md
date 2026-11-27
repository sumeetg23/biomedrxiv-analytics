# Cloudflare Worker — Multi-Provider LLM Proxy (`worker/`)

A zero-dependency Cloudflare Worker (Free Tier: 100k req/day, $0/month, no credit card required) powering the **Ask AI** chat drawer on GitHub Pages (`web/`) with an automatic 3-tier fallback chain:

1. **Primary — Google Gemini** (`gemini-4-hydrogen` → `gemini-4-argon` → `gemini-3.5-flash-lite`) via `GEMINI_API_KEY`.
2. **Fallback 1 — Groq** (`openai/gpt-oss-120b` → `qwen/qwen3.8-27b`, ~1,000+ RPD free) via optional `GROQ_API_KEY`.
3. **Fallback 2 — Cloudflare Workers AI** (`@cf/meta/llama-3.1-8b-instruct-fast` → `@cf/meta/llama-3.1-8b-instruct-fp8`, 10,000 Neurons/day free) via the `AI` binding (zero API key required).

## Security Features
- Keeps `GEMINI_API_KEY` and `GROQ_API_KEY` inside Cloudflare Worker Secrets (never exposed to browsers or GitHub).
- Locks CORS strictly to `ALLOWED_ORIGINS` (`https://sumeetg23.github.io` + localhost dev).
- Enforces a **3 free questions / 24h** per-IP demo cap (`RATE_LIMIT_MAX=3`, `RATE_LIMIT_WINDOW_SEC=86400`), counting 1 Deep Search (`/sql-plan` + `/sql-summarize`) as exactly 1 question.
- Supports optional **Bring Your Own Key (BYOK)** via HTTPS headers (`X-User-API-Key`, `X-User-Provider`: `gemini` | `groq` | `openai`), used in memory only and never logged or persisted.

---

## Option A: Deploy Directly in Cloudflare Web Dashboard (Zero Node.js Needed)

1. Open your Worker **`biomedrxiv-gemini-proxy`** in https://dash.cloudflare.com/ → **Edit code**.
2. Press **`Cmd + A`** → **`Delete`**, paste the full contents of [`src/index.js`](src/index.js), and click **Deploy**.
3. Go to **Settings → Runtime variables and secrets → + Add variable**:
   - `GEMINI_API_KEY` (Type: **Secret**) — from https://aistudio.google.com/apikey
   - `GROQ_API_KEY` *(optional Fallback 1, Type: **Secret**)* — free key from https://console.groq.com/keys
4. Go to **Settings → Bindings → + Add binding**:
   - **Workers AI** → Variable name: `AI` (powers Fallback 2 AND RAG query embeddings; free 10,000 Neurons/day)
   - **Vectorize** *(Step 2 RAG — after running `scripts/upload_vectorize.py --ensure-index`)* → Variable name: `VECTORIZE`, Index: `biomedrxiv-rag`
   - Click **Deploy**.
5. Verify all active providers and RAG status at:
   `https://biomedrxiv-gemini-proxy.<your-subdomain>.workers.dev/health`

---

## Step 2: Populate Cloudflare Vectorize RAG Index (Zero Node.js, 0 Indexing Neurons)

```bash
# 1. Build compact 12,500-doc corpus (all 3,034 trials + 399 NIH iCite + 9,067 top preprints)
.venv/bin/python scripts/build_rag_corpus.py

# 2. Pre-compute all 12,500 384-dim BAAI/bge-small-en-v1.5 vectors locally (0 Cloudflare Neurons!)
.venv/bin/python scripts/upload_vectorize.py --embed-only

# 3. Create Vectorize index & upload cached vectors (only needs Account -> Vectorize -> Edit token)
export CLOUDFLARE_ACCOUNT_ID="<32-char-account-id>"
export CLOUDFLARE_API_TOKEN="<cloudflare-api-token>"
.venv/bin/python scripts/upload_vectorize.py --ensure-index
```

---

## Step 3: Browser DuckDB-Wasm Full 454K-Row SQL (Zero Credit Card)

Compact, sorted, ZSTD-compressed Parquet tables (~21.7 MiB total) live in `web/data/parquet/` (served directly from same-origin GitHub Pages with HTTP Range requests — or overridable to a public Hugging Face Dataset repo via `parquetBaseUrl` in `web/config.js`):

```bash
# Regenerate web/data/parquet/ after a dataset update:
.venv/bin/python scripts/export_web_parquet.py
```

* **`POST /sql-plan`** (Gemini & Groq only; disabled on 8B Workers AI): classifies whether a question needs exact 454K-row SQL and returns a strictly validated read-only `SELECT`/`WITH` query over views `preprints`, `clinical_trials`, and `downstream_clinical`.
* **`POST /sql-summarize`**: summarizes the browser DuckDB-Wasm result rows alongside the interactive SQL result table.

---

## Option B: Deploy via Wrangler CLI (If Node.js Is Installed)

```bash
cd worker
npx wrangler login
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put GROQ_API_KEY
npx wrangler deploy
```
