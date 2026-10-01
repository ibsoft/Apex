const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const ROOT = path.join(__dirname, '..');

function loadTs(relative) {
  const source = fs.readFileSync(path.join(ROOT, relative), 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(compiled, { module, exports: module.exports, require, URL, console });
  return module.exports;
}

const pwa = loadTs('lib/pwa.ts');
const { classifyRequest, isStandalone, UNCACHEABLE_PATHS, SW_URL, MANIFEST_URL } = pwa;

/* ── the real service worker ─────────────────────────────────────────────────
   public/sw.js is loaded and executed, not re-implemented. The whole point of
   the worker is that it stays out of the way, and the only honest way to test
   that is to dispatch the same fetch events the browser would and see which
   ones it answers. A stub that mirrors the logic could agree with a broken
   worker; this cannot. */

const SW_SOURCE = fs.readFileSync(path.join(ROOT, 'public/sw.js'), 'utf8');

/* A response as the browser hands one to a worker: `type` is "basic" only for
   same-origin network responses. A constructed Response is "default", which the
   worker correctly refuses to cache - so the stub has to say "basic" or the
   caching assertions test nothing. */
function networkResponse(body = 'network', status = 200) {
  const response = new Response(body, { status });
  Object.defineProperty(response, 'type', { value: 'basic' });
  return response;
}

function makeCacheStorage() {
  const buckets = new Map();
  const normalize = (key) => (typeof key === 'string' ? new URL(key, 'https://apex.test').href : key.url);
  return {
    buckets,
    async open(name) {
      if (!buckets.has(name)) buckets.set(name, new Map());
      const store = buckets.get(name);
      return {
        async add(request) {
          store.set(normalize(request), networkResponse('precached'));
        },
        async put(request, response) {
          store.set(normalize(request), response);
        },
        async match(request) {
          return store.get(normalize(request)) || undefined;
        },
      };
    },
    async match(request) {
      for (const store of buckets.values()) {
        const hit = store.get(normalize(request));
        if (hit) return hit;
      }
      return undefined;
    },
    async keys() {
      return [...buckets.keys()];
    },
    async delete(name) {
      return buckets.delete(name);
    },
  };
}

function loadWorker({ offline = false, body = 'network' } = {}) {
  const listeners = new Map();
  const caches = makeCacheStorage();
  const fetched = [];
  let skipWaitingCalls = 0;

  const self = {
    location: { origin: 'https://apex.test' },
    clients: { async claim() {} },
    skipWaiting() { skipWaitingCalls += 1; },
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
  };

  const ctx = vm.createContext({
    self,
    caches,
    URL,
    Request: class Request {
      constructor(input, init = {}) {
        this.url = typeof input === 'string' ? new URL(input, 'https://apex.test').href : input.url;
        this.method = init.method || 'GET';
        this.headers = new Map(Object.entries(init.headers || {}));
        this.mode = init.mode;
      }
      has(key) { return this.headers.has(key); }
    },
    Response: globalThis.Response,
    fetch: async (request) => {
      fetched.push(typeof request === 'string' ? request : request.url);
      if (offline) throw new TypeError('Failed to fetch');
      return networkResponse(body);
    },
    console,
  });

  vm.runInContext(SW_SOURCE, ctx, { filename: 'sw.js' });

  // Fire a fetch event the way the browser does and report whether the worker
  // answered it. Not calling respondWith means the browser handles the request
  // itself, on the untouched original path.
  async function dispatch(request) {
    const waits = [];
    let answered = null;
    const event = {
      request,
      respondWith(promise) { answered = promise; },
      waitUntil(promise) { waits.push(promise); },
    };
    for (const handler of listeners.get('fetch') || []) handler(event);
    if (!answered) return { handled: false, response: null };
    const response = await answered;
    await Promise.all(waits);
    return { handled: true, response };
  }

  // Lifecycle events do all their work inside waitUntil, which the browser
  // keeps alive. Awaiting them is what makes the assertions deterministic.
  async function fire(type) {
    const waits = [];
    for (const handler of listeners.get(type) || []) {
      handler({ waitUntil: (promise) => waits.push(promise) });
    }
    await Promise.all(waits);
  }

  return {
    dispatch,
    fire,
    fetched,
    caches,
    listeners,
    get skipWaitingCalls() { return skipWaitingCalls; },
  };
}

const req = (url, init = {}) => ({
  url: new URL(url, 'https://apex.test').href,
  method: init.method || 'GET',
  mode: init.mode,
  headers: { has: (key) => Boolean(init.headers && init.headers[key]) },
});

test('sw: the SSE chat stream is never answered by the worker', async () => {
  const worker = loadWorker();
  const result = await worker.dispatch(req('/be/api/chat', { method: 'POST' }));
  assert.equal(result.handled, false, 'a cached/mediated SSE turn breaks the reply stream');
  assert.equal(worker.fetched.length, 0, 'the worker must not even touch the network for it');
});

test('sw: terminal drain polling is never answered by the worker', async () => {
  const worker = loadWorker();
  // Two consecutive polls of the same URL with different cursors. A cache would
  // return the first response twice and the terminal would look frozen.
  for (const from of [0, 4, 9]) {
    const result = await worker.dispatch(req(`/be/api/terminal/session/t1/drain?from=${from}`));
    assert.equal(result.handled, false, 'drain is not consuming: each poll must reach the server');
  }
  assert.equal(worker.fetched.length, 0);
});

test('sw: terminal input, auth status and signed downloads are never answered', async () => {
  const worker = loadWorker();
  for (const target of UNCACHEABLE_PATHS) {
    assert.equal((await worker.dispatch(req(target))).handled, false, `${target} must reach the network`);
  }
  // And a same-origin *navigation* to a backend path, which is the one case a
  // path-prefix guard could plausibly get wrong.
  const nav = await worker.dispatch(req('/be/api/files/download/tok123', { mode: 'navigate' }));
  assert.equal(nav.handled, false, 'a signed URL opened as a top-level page must not be served from cache');
});

test('sw: non-GET and range requests pass straight through', async () => {
  const worker = loadWorker();
  assert.equal((await worker.dispatch(req('/be/api/tasks', { method: 'POST' }))).handled, false);
  assert.equal((await worker.dispatch(req('/be/api/tasks', { method: 'PATCH' }))).handled, false);
  assert.equal((await worker.dispatch(req('/be/api/tasks', { method: 'DELETE' }))).handled, false);
  // A cached 206 is not a resource, it is a slice of one.
  assert.equal((await worker.dispatch(req('/be/api/shell/download/tok', { headers: { range: 'bytes=0-' } }))).handled, false);
  assert.equal(worker.fetched.length, 0);
});

test('sw: a navigation is network-first and refreshes the cached shell', async () => {
  const worker = loadWorker();
  const result = await worker.dispatch(req('/', { mode: 'navigate' }));
  assert.equal(result.handled, true);
  assert.equal(result.response.status, 200);
  assert.deepEqual(worker.fetched, ['https://apex.test/'], 'network first, not cache first');
  const shell = worker.caches.buckets.get('apex-shell-v1');
  assert.ok(shell && shell.has('https://apex.test/'), 'the offline fallback must be populated on the way through');
});

test('sw: an offline navigation serves the cached shell, not the error page', async () => {
  // Warm the cache exactly as a real first load would, then go offline. The
  // cached shell boots the orb and the sign-in screen, which is a better answer
  // than "APEX IS OFFLINE" for someone who opened the app from the home screen.
  const online = loadWorker({ body: '<html>the app shell</html>' });
  await online.dispatch(req('/', { mode: 'navigate' }));
  const shell = await online.caches.match('/');
  assert.ok(shell, 'nothing was cached on the first load');

  const offline = loadWorker({ offline: true });
  await offline.caches.open('apex-shell-v1').then((cache) => cache.put('/', shell));
  const result = await offline.dispatch(req('/', { mode: 'navigate' }));
  assert.equal(result.handled, true);
  assert.equal(result.response.status, 200, 'the offline fallback was not used');
  assert.match(await result.response.text(), /the app shell/);
});

test('sw: an offline navigation with no cached shell returns the offline page', async () => {
  const offline = loadWorker({ offline: true });
  const result = await offline.dispatch(req('/', { mode: 'navigate' }));
  assert.equal(result.handled, true);
  assert.equal(result.response.status, 503);
  assert.match(result.response.headers.get('Content-Type'), /text\/html/);
  assert.match(await result.response.text(), /OFFLINE/);
});

test('sw: /_next/static assets are served from cache without a second request', async () => {
  const worker = loadWorker();
  const url = '/_next/static/chunks/main-abc123.js';
  const first = await worker.dispatch(req(url));
  assert.equal(first.handled, true);
  assert.equal(worker.fetched.length, 1);
  const second = await worker.dispatch(req(url));
  assert.equal(second.handled, true);
  assert.equal(worker.fetched.length, 1, 'content-hashed chunks must not be re-fetched');
});

test('sw: unrelated same-origin GETs are left alone', async () => {
  const worker = loadWorker();
  for (const url of ['/manifest.webmanifest', '/icon-192.png', '/favicon.ico', '/sw.js']) {
    assert.equal((await worker.dispatch(req(url))).handled, false, `${url} should not be mediated`);
  }
  assert.equal(worker.fetched.length, 0);
});

test('sw: cross-origin requests are ignored', async () => {
  const worker = loadWorker();
  const result = await worker.dispatch({
    url: 'https://api.open-meteo.com/v1/forecast?latitude=1',
    method: 'GET',
    headers: { has: () => false },
  });
  assert.equal(result.handled, false);
});

test('sw: install does not take over on its own, message does', async () => {
  const worker = loadWorker();
  await worker.fire('install');
  assert.equal(worker.skipWaitingCalls, 0, 'self-skipWaiting would swap the cache set under a streaming reply');
  for (const handler of worker.listeners.get('message') || []) {
    handler({ data: 'SKIP_WAITING' });
  }
  assert.equal(worker.skipWaitingCalls, 1);
});

test('sw: install precaches the shell so the first offline launch has something', async () => {
  const worker = loadWorker();
  await worker.fire('install');
  assert.ok(await worker.caches.match('/'), 'the shell was not precached');
});

test('sw: activate drops other generations of the caches', async () => {
  const worker = loadWorker();
  await worker.caches.open('apex-shell-v1');
  await worker.caches.open('apex-assets-v1');
  await worker.caches.open('apex-shell-v0');
  await worker.caches.open('apex-assets-v0');
  await worker.fire('activate');
  const names = await worker.caches.keys();
  assert.deepEqual(names.sort(), ['apex-assets-v1', 'apex-shell-v1']);
});

/* ── lib/pwa.ts ──────────────────────────────────────────────────────────── */

test('classifyRequest agrees with the worker on every APEX endpoint', () => {
  // The worker is the thing that ships; this is the thing that documents it. If
  // one is changed without the other, this fails.
  assert.equal(classifyRequest({ url: '/be/api/chat', method: 'POST' }), 'passthrough');
  assert.equal(classifyRequest({ url: '/be/api/terminal/session/t1/drain?from=4' }), 'passthrough');
  assert.equal(classifyRequest({ url: '/be/api/tasks', method: 'PATCH' }), 'passthrough');
  assert.equal(classifyRequest({ url: '/be/api/auth/status' }), 'passthrough');
  assert.equal(classifyRequest({ url: '/be/api/files/download/tok123' }), 'passthrough');
  assert.equal(classifyRequest({ url: '/api/weather' }), 'passthrough');
  assert.equal(classifyRequest({ url: '/be/api/tasks' }), 'passthrough');
  assert.equal(classifyRequest({ url: '/api/weather', mode: 'navigate' }), 'passthrough');

  assert.equal(classifyRequest({ url: '/', mode: 'navigate' }), 'navigation');
  assert.equal(classifyRequest({ url: '/_next/static/chunks/a.js' }), 'static-asset');

  // /be exact, and the prefix must not swallow a sibling like /bearer.
  assert.equal(classifyRequest({ url: '/be', mode: 'navigate' }), 'passthrough');
  assert.equal(classifyRequest({ url: '/bearer-token', mode: 'navigate' }), 'navigation');
  assert.equal(classifyRequest({ url: '/apiary', mode: 'navigate' }), 'navigation');
});

test('classifyRequest treats absolute URLs, ranges and methods correctly', () => {
  assert.equal(classifyRequest({ url: 'https://apex.test/be/api/chat', method: 'POST' }), 'passthrough');
  assert.equal(classifyRequest({ url: 'https://apex.test/be/api/chat' }), 'passthrough');
  assert.equal(classifyRequest({ url: '/_next/static/a.js', range: true }), 'passthrough');
  assert.equal(classifyRequest({ url: '/', mode: 'navigate', method: 'POST' }), 'passthrough');
});

test('isStandalone reads both the media query and the iOS property', () => {
  const none = { matchMedia: () => ({ matches: false }) };
  assert.equal(isStandalone(none), false);
  assert.equal(isStandalone({ standalone: true, matchMedia: () => ({ matches: false }) }), true, 'iOS');
  assert.equal(isStandalone({ matchMedia: (q) => ({ matches: q.includes('standalone') }) }), true, 'Android/desktop');
  assert.equal(isStandalone({ matchMedia: (q) => ({ matches: q.includes('minimal-ui') }) }), true);
  assert.equal(isStandalone(undefined), false);
  // A browser without matchMedia must not throw during the first render.
  assert.equal(isStandalone({}), false);
  assert.equal(isStandalone({ matchMedia: () => { throw new Error('unsupported query'); } }), false);
});

test('the worker URL is at the origin root with the shipped version', () => {
  assert.match(SW_URL, /^\/sw\.js\?v=\w+$/);
  // A worker served from a subpath would only control part of the app, and the
  // app shell would never come under its scope.
  assert.equal(new URL(SW_URL, 'https://apex.test').pathname, '/sw.js');
  assert.equal(MANIFEST_URL, '/manifest.webmanifest');
});

/* ── the install-prompt capture contract ───────────────────────────────────
   beforeinstallprompt is single-shot. The layout installs an early script that
   holds it; PwaManager reads the store. These run the real script, because a
   source grep cannot prove the store and the forwarded event actually work. */

test('the early capture script holds beforeinstallprompt across hydration', () => {
  const { INSTALL_CAPTURE_SCRIPT, INSTALL_AVAILABLE_EVENT } = pwa;
  assert.equal(INSTALL_AVAILABLE_EVENT, 'apex:install-available');

  const listeners = new Map();
  const window = {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    dispatchEvent(event) {
      for (const fn of listeners.get(event.type) || []) fn(event);
      return true;
    },
  };
  const Event = class { constructor(type) { this.type = type; } };
  vm.runInNewContext(INSTALL_CAPTURE_SCRIPT, { window, Event });

  // The offer arrives before React has mounted anything.
  let prevented = false;
  const bip = { type: 'beforeinstallprompt', preventDefault() { prevented = true; } };
  window.dispatchEvent(bip);
  assert.equal(prevented, true, 'Chrome drops the event with a warning unless it is prevented');
  assert.equal(window.__apexInstallPrompt, bip, 'a pre-hydration offer must be held for a late listener');

  // And a spent offer is cleared so it cannot come back on a remount.
  window.dispatchEvent(new Event('appinstalled'));
  assert.equal(window.__apexInstallPrompt, null);
});

test('the layout ships the tested capture script, not a divergent copy', () => {
  const layout = fs.readFileSync(path.join(ROOT, 'app/layout.tsx'), 'utf8');
  assert.match(layout, /INSTALL_CAPTURE_SCRIPT/, 'an inline copy can drift from the tested one');
  assert.match(layout, /strategy="beforeInteractive"/, 'a listener attached after hydration can miss the event');
});

test('PwaManager consumes the captured offer rather than racing for its own', () => {
  const source = fs.readFileSync(path.join(ROOT, 'components/PwaManager.tsx'), 'utf8');
  assert.match(source, /INSTALL_AVAILABLE_EVENT/);
  assert.match(source, /__apexInstallPrompt/);
});

/* ── the shipped manifest ─────────────────────────────────────────────────── */

test('manifest.webmanifest is valid and installable', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, 'public/manifest.webmanifest'), 'utf8'));
  assert.equal(manifest.display, 'standalone');
  assert.equal(manifest.scope, '/');
  assert.equal(manifest.id, '/');
  assert.match(manifest.start_url, /^\//);
  assert.match(manifest.theme_color, /^#[0-9a-f]{6}$/i);

  // Chrome requires a 192 and a 512, and a maskable icon for a themed launcher.
  const sizes = manifest.icons.map((i) => i.sizes);
  assert.ok(sizes.includes('192x192'), 'no 192px icon');
  assert.ok(sizes.includes('512x512'), 'no 512px icon');
  const purposes = manifest.icons.map((i) => i.purpose || 'any');
  assert.ok(purposes.includes('maskable'), 'no maskable icon: the launcher would crop the ring');

  for (const icon of manifest.icons) {
    assert.equal(fs.existsSync(path.join(ROOT, 'public', icon.src.replace(/^\//, ''))), true, `${icon.src} missing`);
  }

  // A shortcut that names a tab the panel cannot render would acknowledge a
  // command it does not perform.
  const bridge = loadTs('lib/panelBridge.ts');
  for (const shortcut of manifest.shortcuts || []) {
    const tab = new URL(shortcut.url, 'https://apex.test').searchParams.get('panel');
    assert.ok(tab, `${shortcut.name} has no ?panel=`);
    assert.ok(bridge.PANEL_TAB_NAMES.includes(tab), `${shortcut.name} names unknown panel "${tab}"`);
  }
});

test('every icon the manifest references is a real PNG of the declared size', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, 'public/manifest.webmanifest'), 'utf8'));
  for (const icon of manifest.icons) {
    const buf = fs.readFileSync(path.join(ROOT, 'public', icon.src.replace(/^\//, '')));
    assert.equal(buf.subarray(0, 8).toString('hex'), '89504e470d0a1a0a', `${icon.src} is not a PNG`);
    const declared = Number(icon.sizes.split('x')[0]);
    assert.equal(buf.readUInt32BE(16), declared, `${icon.src} width != ${icon.sizes}`);
    assert.equal(buf.readUInt32BE(20), declared, `${icon.src} height != ${icon.sizes}`);
  }
});