// Free Cloudflare Worker: gives the website a nicer address (yourname.workers.dev), keeps the Render service awake,
// shows a friendly page when Render is down/asleep, and has a /status page.
// Set ORIGIN to your Render address (no trailing slash).
const ORIGIN = "https://dimasphotosbot-5787.onrender.com";
const CONTACT = "photosfromdima@yahoo.com";
const WAIT_MS = 25000; // how long to wait for Render before showing the "waking up" page

const page = (title, body, status, refresh) => new Response(
  `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">` +
  (refresh ? `<meta http-equiv="refresh" content="${refresh}">` : "") +
  `<title>${title}</title><meta name="theme-color" content="#0b7f70"><style>
  :root{--bg:#f4f8f7;--card:#fff;--tx:#12302c;--mut:#5b716d;--ac:#0b7f70}
  @media(prefers-color-scheme:dark){:root{--bg:#0e1a18;--card:#16262a;--tx:#e6f2ef;--mut:#9bb3ae;--ac:#3fc5b1}}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--tx);font:18px/1.5 system-ui,sans-serif;padding:16px;box-sizing:border-box}
  .c{max-width:420px;background:var(--card);border-radius:20px;padding:28px 24px;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.08)}
  h1{font-size:24px;margin:0 0 8px}p{margin:8px 0;color:var(--mut)}b{color:var(--tx)}.ok{color:var(--ac)}.dot{font-size:40px}
  a{color:var(--ac)}</style></head><body><div class="c">${body}</div></body></html>`,
  { status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });

async function up() {  // is the real service answering?
  try {
    const r = await fetch(ORIGIN + "/", { signal: AbortSignal.timeout(WAIT_MS), redirect: "manual" });
    return r.status < 500;
  } catch (e) { return false; }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/status") {
      const ok = await up();
      const when = new Date().toUTCString();
      return page("Status", ok
        ? `<div class="dot">✅</div><h1>Dima's Photos is <span class="ok">UP</span></h1><p>The website and the bot are running.</p><p>Checked ${when}</p>`
        : `<div class="dot">🛠️</div><h1>Dima's Photos is <b>DOWN</b></h1><p>It may be waking up or being fixed. This page checks again every 30 seconds.</p><p>Questions? <a href="mailto:${CONTACT}">${CONTACT}</a></p><p>Checked ${when}</p>`,
        ok ? 200 : 503, 30);
    }
    // the bare address opens the website
    if (url.pathname === "/") return Response.redirect(url.origin + "/web", 302);
    const target = new Request(ORIGIN + url.pathname + url.search, request);
    // the logo and the home-screen icons never change between updates: Cloudflare keeps a copy so they load fast (even while Render sleeps)
    const still = request.method === "GET" && /^\/(logo\.png|icon-(180|192|512)\.png)$/.test(url.pathname);
    if (still) {
      const hit = await caches.default.match(request);
      if (hit) return hit;
    }
    const stop = new AbortController();
    const timer = setTimeout(() => stop.abort(), WAIT_MS * 2);  // (only waits for the answer to start; long photo downloads are not cut off)
    try {
      const r = await fetch(target, { redirect: "manual", signal: stop.signal });
      clearTimeout(timer);
      if (still && r.status === 200) ctx.waitUntil(caches.default.put(request, r.clone()));
      if (r.status < 502 || r.status > 504) return r;  // all good (only gateway errors count as "down")
    } catch (e) { clearTimeout(timer); /* fall through to the friendly page */ }
    if ((request.headers.get("accept") || "").includes("text/html"))
      return page("We'll be right back",
        `<div class="dot">📷</div><h1>Dima's Photos will be right back</h1><p>It is waking up or being fixed. This page tries again by itself every 30 seconds.</p><p>Need something now? <a href="mailto:${CONTACT}">${CONTACT}</a></p>`,
        503, 30);
    return new Response(JSON.stringify({ error: "Dima's Photos is waking up or being fixed. Please try again in a minute." }),
      { status: 503, headers: { "content-type": "application/json", "retry-after": "30" } });
  },
  // runs on the schedule you set (every 5 minutes) so the free Render service doesn't fall asleep
  async scheduled() {
    await fetch(ORIGIN + "/").catch(() => {});
  },
};
