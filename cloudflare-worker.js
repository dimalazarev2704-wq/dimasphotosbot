// Free Cloudflare Worker: gives the website a nicer address (yourname.workers.dev) and keeps the Render service awake.
// Set ORIGIN to your Render address (no trailing slash).
const ORIGIN = "https://dimasphotosbot-5787.onrender.com";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const target = new Request(ORIGIN + url.pathname + url.search, request);
    return fetch(target);
  },
  // runs on the schedule you set (every 5 minutes) so the free Render service doesn't fall asleep
  async scheduled() {
    await fetch(ORIGIN + "/").catch(() => {});
  },
};
