# Cloudflare Worker — Multi-Provider LLM Proxy (`worker/`)

A zero-dependency Cloudflare Worker (Free Tier: 100k req/day, $0/month, no credit card required) powering the **Ask AI** chat drawer on GitHub Pages (`web/`) with an automatic 3-tier fallback chain:

1. **Primary — Google Gemini** (`gemini-4-hydrogen` → `gemini-4-argon` → `gemini-3.5-flash-lite`) via `GEMINI_API_KEY`.
2. **Fallback 1 — Groq** (`openai/gpt-oss-120b` → `qwen/qwen3.8-27b`, ~1,000+ RPD free) via optional `GROQ_API_KEY`.
3. **Fallback 2 — Cloudflare Workers AI** (`@cf/meta/llama-3.1-8b-instruct-fast` → `@cf/meta/llama-3.1-8b-instruct-fp8`, 10,000 Neurons/day free) via the `AI` binding (zero API key required).

## Security Features
- Keeps `GEMINI_API_KEY` and `GROQ_API_KEY` inside Cloudflare Worker Secrets (never exposed to browsers or GitHub).
- Locks CORS strictly to `ALLOWED_ORIGINS` (`https://sumeetg23.github.io` + localhost dev).
- Enforces per-IP sliding-window rate limiting (`20` requests per 10 minutes by default).

---

## Option A: Deploy Directly in Cloudflare Web Dashboard (Zero Node.js Needed)

1. Open your Worker **`biomedrxiv-gemini-proxy`** in https://dash.cloudflare.com/ → **Edit code**.
2. Press **`Cmd + A`** → **`Delete`**, paste the full contents of [`src/index.js`](src/index.js), and click **Deploy**.
3. Go to **Settings → Runtime variables and secrets → + Add variable**:
   - `GEMINI_API_KEY` (Type: **Secret**) — from https://aistudio.google.com/apikey
   - `GROQ_API_KEY` *(optional Fallback 1, Type: **Secret**)* — free key from https://console.groq.com/keys
4. Go to **Settings → Bindings → + Add binding → Workers AI** *(optional Fallback 2, free 10,000 Neurons/day)*:
   - **Variable name:** `AI`
   - Click **Deploy**.
5. Verify all active providers at:
   `https://biomedrxiv-gemini-proxy.<your-subdomain>.workers.dev/health`

---

## Option B: Deploy via Wrangler CLI (If Node.js Is Installed)

```bash
cd worker
npx wrangler login
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put GROQ_API_KEY
npx wrangler deploy
```
