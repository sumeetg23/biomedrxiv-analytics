# Cloudflare Worker — Gemini Proxy (`worker/`)

A zero-dependency Cloudflare Worker (Free Tier: 100k req/day, \$0/month, no credit card required) that securely proxies the **Ask AI** chat drawer on GitHub Pages (`web/`) to the Gemini Interactions API (`gemini-4-hydrogen`).

## Security Features
- Keeps `GEMINI_API_KEY` inside Cloudflare Worker Secrets (never exposed to browsers or GitHub).
- Locks CORS strictly to `ALLOWED_ORIGINS` (`https://sumeetg23.github.io` + localhost dev).
- Enforces per-IP sliding-window rate limiting (`20` requests per 10 minutes by default).
- Sets `store: false` on Gemini Interactions API calls so visitor queries are stateless.

---

## Option A: Deploy Directly in Cloudflare Web Dashboard (Zero Node.js Needed)

1. Sign in (or create a free account) at https://dash.cloudflare.com/.
2. Go to **Workers & Pages → Create → Create Worker**, name it `biomedrxiv-gemini-proxy`, and click **Deploy**.
3. Click **Edit code**, replace the default script with the contents of [`src/index.js`](src/index.js), and click **Deploy**.
4. Go to the Worker's **Settings → Variables and Secrets → Add**:
   - **Type:** Secret | **Name:** `GEMINI_API_KEY` | **Value:** your key from https://aistudio.google.com/apikey
   - *(Optional)* **Type:** Text | **Name:** `ALLOWED_ORIGINS` | **Value:** `https://<your-github-username>.github.io,http://127.0.0.1:8080,http://localhost:8080`
5. Copy your Worker URL (e.g., `https://biomedrxiv-gemini-proxy.<your-subdomain>.workers.dev`) and paste it into [`web/config.js`](../web/config.js):
   ```js
   window.BIOMEDRXIV_CHAT_CONFIG = {
     workerUrl: "https://biomedrxiv-gemini-proxy.<your-subdomain>.workers.dev",
   };
   ```
6. Commit & push `web/config.js` to `main` — GitHub Pages automatically redeploys `web/` with the live AI drawer!

---

## Option B: Deploy via Wrangler CLI (If Node.js Is Installed)

```bash
cd worker
npx wrangler login
npx wrangler secret put GEMINI_API_KEY
npx wrangler deploy
```

Verify your deployed Worker:
```bash
curl https://biomedrxiv-gemini-proxy.<your-subdomain>.workers.dev/health
```
