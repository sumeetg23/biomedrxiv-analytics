/* Public frontend configuration for the GitHub Pages AI Chat Drawer.
 *
 * Points to your deployed Cloudflare Worker Gemini proxy (no trailing slash).
 * NOTE: Never put GEMINI_API_KEY here — store it only as a Cloudflare Worker Secret!
 */
window.BIOMEDRXIV_CHAT_CONFIG = {
  workerUrl: "https://biomedrxiv-gemini-proxy.sumeetg23.workers.dev",
};
