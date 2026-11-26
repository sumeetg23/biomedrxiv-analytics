/* Public frontend configuration for the GitHub Pages AI Chat Drawer.
 *
 * After deploying `worker/src/index.js` to Cloudflare Workers (Free Tier),
 * paste your Worker URL below (no trailing slash) and push to main.
 * Example:
 *   workerUrl: "https://biomedrxiv-gemini-proxy.your-handle.workers.dev"
 *
 * NOTE: Never put GEMINI_API_KEY here — store it only as a Cloudflare Worker Secret!
 */
window.BIOMEDRXIV_CHAT_CONFIG = {
  workerUrl: "",
};
