/* APEX service worker.

   APEX is not a content site. Almost every request it makes is either a live
   conversation or a piece of private, session-bound state, so this worker is
   deliberately near-inert: it caches exactly two things and hands back every
   other request without even calling respondWith().

   What it caches
     1. The app shell ("/"), network-first, used only when the network is gone.
        The shell renders the orb and the sign-in screen with no backend at all
        (ApexProvider's refresh() swallows failures and clears `loading`), so a
        cold offline start is a real screen rather than a browser error page.
     2. /_next/static/**, cache-first. Those filenames are content-hashed and
        served immutable, so this is the one place a cache cannot go stale.

   What it must never touch
     * POST /be/api/chat - the SSE turn stream, read through res.body with no
       abort signal (lib/api.ts). Intermediating it risks buffering, which
       would stall a reply mid-stream.
     * GET /be/api/terminal/session/<id>/drain?from=N - polled every 160ms and
       NOT consuming: the response carries the next cursor and the caller keeps
       its own. A cached or reordered drain response makes the terminal blank.
     * Every signed download/preview URL. The tokens are short-lived and
       single-user; caching one would serve another operator's document.
     * GET /be/api/auth/status - it mints the CSRF token, and a cached copy
       would hand back a token for a session that no longer exists.

   Each of those is a GET or a POST under /be/, so the two rules below already
   exclude them by construction. The explicit guard list is kept anyway: it is
   the thing a future edit has to delete to break the app, which makes the
   invariant reviewable instead of implicit.

   Bump VERSION on any change to this file. The cache names are derived from it,
   so a new version gets new buckets and activate() drops the old ones.
*/

const VERSION = "v1";
const SHELL_CACHE = `apex-shell-${VERSION}`;
const ASSET_CACHE = `apex-assets-${VERSION}`;
const KEEP = [SHELL_CACHE, ASSET_CACHE];

/* Resolved against the worker's own origin, because this file is served from
   /sw.js and therefore has root scope. */
const SHELL_URL = "/";

/* Prefixes that must reach the network untouched. See the list above. */
const NEVER_CACHE = ["/be", "/api"];

/* ---------- lifecycle ---------- */

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      // `reload` bypasses the HTTP cache: precaching a stale 304 would defeat
      // the network-first update path on the very first offline load.
      await cache.add(new Request(SHELL_URL, { cache: "reload" })).catch(() => {
        /* Offline at install time is fine. The fetch handler will populate it. */
      });
      /* No self.skipWaiting() here on purpose. An update that takes over
         mid-session swaps the cache set out from under a page that is still
         streaming a reply, so the app asks for the handover explicitly
         (PwaManager posts SKIP_WAITING) and reloads on controllerchange. */
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(
        names.filter((name) => !KEEP.includes(name)).map((name) => caches.delete(name)),
      );
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("message", (event) => {
  /* No skipWaiting() at install: the handover is the operator's decision (see
     the install handler above), and this is the only thing that performs it. */
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});

/* ---------- routing ---------- */

function isPassthrough(request, url) {
  if (request.method !== "GET") return true;
  if (request.headers.has("range")) return true; // media scrubbing, not caching
  return NEVER_CACHE.some((prefix) => url.pathname === prefix || url.pathname.startsWith(`${prefix}/`));
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  let url;
  try {
    url = new URL(request.url);
  } catch {
    return; // not addressable; nothing sensible to do
  }

  // Bail out before doing any work. Returning without respondWith means the
  // browser performs the request itself, on the original code path, with no
  // added latency - which matters because getUserMedia and
  // SpeechRecognition in lib/voice.ts have to start inside the tap gesture.
  if (isPassthrough(request, url)) return;
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(handleNavigation(request));
    return;
  }

  // Content-hashed build output: safe to serve from cache indefinitely.
  if (url.pathname.startsWith("/_next/static/")) {
    event.respondWith(handleStatic(request));
  }
  // Anything else - the manifest, the icons, /api/weather - simply returns.
});

async function handleNavigation(request) {
  try {
    const fresh = await fetch(request);
    // Refresh the offline copy on the way through, so the fallback is the most
    // recent shell rather than the one captured at install time.
    if (fresh && fresh.ok && fresh.type === "basic") {
      const cache = await caches.open(SHELL_CACHE);
      await cache.put(SHELL_URL, fresh.clone());
    }
    return fresh;
  } catch {
    const cached = await caches.match(SHELL_URL);
    if (cached) return cached;
    return offlineResponse();
  }
}

async function handleStatic(request) {
  const cache = await caches.open(ASSET_CACHE);
  const hit = await cache.match(request);
  if (hit) return hit;
  const fresh = await fetch(request);
  if (fresh && fresh.ok && fresh.type === "basic") {
    await cache.put(request, fresh.clone());
  }
  return fresh;
}

function offlineResponse() {
  return new Response(
    `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>APEX — offline</title>
<style>
  html,body{height:100%;margin:0;background:#04080f;color:#f0ede8;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    display:flex;align-items:center;justify-content:center;text-align:center}
  h1{font-size:15px;letter-spacing:.32em;font-weight:600;margin:0 0 14px}
  p{font-size:11.5px;letter-spacing:.05em;color:rgba(240,237,232,.55);margin:0}
</style></head>
<body><div><h1>APEX IS OFFLINE</h1>
<p>The APEX backend is unreachable. Reconnect and reload.</p></div></body></html>`,
    { status: 503, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } },
  );
}