/* Public frontend configuration for the GitHub Pages AI Chat Drawer.
 *
 * Points to your deployed Cloudflare Worker Gemini proxy (no trailing slash).
 * NOTE: Never put GEMINI_API_KEY here — store it only as a Cloudflare Worker Secret!
 */
window.BIOMEDRXIV_CHAT_CONFIG = {
  workerUrl: "https://biomedrxiv-gemini-proxy.sumeetg23.workers.dev",
  // Compact sorted Parquet bundle for browser DuckDB-Wasm (Tier 2: 454,051 rows).
  // Defaults to same-origin GitHub Pages (`./data/parquet`, zero CORS issues, zero credit card).
  // Can also be pointed to a public Hugging Face Dataset URL:
  //   "https://huggingface.co/datasets/<user>/<repo>/resolve/main"
  parquetBaseUrl: "./data/parquet",
};
