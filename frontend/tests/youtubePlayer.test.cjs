const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

function loader() {
  const source = fs.readFileSync(path.join(__dirname, '../components/YoutubePlayer.tsx'), 'utf8');
  const start = source.indexOf('let apiPromise:');
  const end = source.indexOf('\ntype Props =', start);
  const js = ts.transpileModule(source.slice(start, end), {
    compilerOptions: { target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const scripts = [];
  const timers = new Map();
  const window = {
    setTimeout(fn) { timers.set(fn, fn); return fn; },
    clearTimeout(id) { timers.delete(id); },
  };
  const document = {
    createElement() { return { remove() {} }; },
    head: { appendChild(script) { scripts.push(script); } },
  };
  const load = new Function('window', 'document', `${js}; return loadYoutubeApi;`)(window, document);
  return { load, window, scripts, timers };
}

test('first load resolves when YouTube calls its window-level ready callback', async () => {
  const { load, window, scripts, timers } = loader();
  let previousCalled = false;
  window.onYouTubeIframeAPIReady = () => { previousCalled = true; };
  const first = load();
  assert.equal(load(), first);
  assert.equal(scripts.length, 1);
  window.YT = { Player() {} };
  window.onYouTubeIframeAPIReady();
  assert.equal(await first, window.YT);
  assert.equal(previousCalled, true);
  assert.equal(timers.size, 0);
  assert.equal(await load(), window.YT);
});

test('script failure allows a new loading attempt', async () => {
  const { load, scripts } = loader();
  const first = load();
  scripts[0].onerror();
  await assert.rejects(first, /failed to load/);
  const second = load();
  assert.equal(scripts.length, 2);
  scripts[1].onerror();
  await assert.rejects(second, /failed to load/);
});

// Exercise the component's real effects and API callbacks without a browser.
function componentHarness() {
  const source = fs.readFileSync(path.join(__dirname, '../components/YoutubePlayer.tsx'), 'utf8');
  const js = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const slots = [];
  let cursor = 0;
  let pending = [];
  const timers = new Map();
  let events;
  const calls = [];
  const player = {
    getIframe: () => ({ getAttribute: () => 'autoplay' }),
    setVolume(value) { calls.push(['volume', value]); },
    unMute() { calls.push(['unmute']); },
    playVideo() { calls.push(['play']); }, pauseVideo() {}, destroy() {},
    loadVideoById() {},
    mute() { throw new Error('blocked playback must not silently mute'); },
  };
  const window = {
    location: { origin: 'http://localhost' },
    YT: { Player: function (_mount, options) { events = options.events; return player; } },
    setTimeout(fn) { timers.set(fn, fn); return fn; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return 1; }, clearInterval() {},
  };
  const node = () => ({ style: {}, appendChild() {}, remove() {} });
  const hooks = {
    useRef(value) { const i = cursor++; return slots[i] ??= { current: value }; },
    useState(value) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = value;
      return [slots[i], v => { slots[i] = typeof v === 'function' ? v(slots[i]) : v; }];
    },
    useEffect(fn, deps) {
      const i = cursor++;
      const old = slots[i];
      if (!old || deps.some((d, j) => !Object.is(d, old.deps[j]))) {
        pending.push(() => { old?.cleanup?.(); slots[i] = { deps, cleanup: fn() }; });
      }
    },
  };
  const jsx = (_type, props) => {
    if (props.ref && !props.ref.current) props.ref.current = node();
    return props;
  };
  const exports = {};
  new Function('require', 'exports', 'window', 'document', js)(
    name => name === 'react' ? hooks : { jsx, jsxs: jsx }, exports, window,
    { createElement: node },
  );
  return {
    player, timers, calls,
    get events() { return events; },
    render(props) {
      cursor = 0;
      exports.default(props);
      const effects = pending; pending = [];
      effects.forEach(fn => fn());
    },
  };
}

const props = overrides => ({
  videoId: 'first', seq: 0, playing: true, controls: 1,
  onEnded() {}, onPause() {}, onResume() {}, failText: 'Failed', ...overrides,
});

test('a refused track does not suppress failures for the next track', async () => {
  const h = componentHarness();
  const failures = [];
  const first = props({ onFail: failure => failures.push(failure.code) });
  h.render(first);
  await Promise.resolve();
  h.events.onReady({ target: h.player });
  h.events.onError({ data: 150 });
  h.render({ ...first, videoId: 'second', seq: 1 });
  h.events.onError({ data: 100 });
  assert.deepEqual(failures, [150, 100]);
});

test('an iframe that never becomes ready reports a retryable failure', async () => {
  const h = componentHarness();
  const failures = [];
  h.render(props({ onFail: failure => failures.push(failure) }));
  await Promise.resolve();
  for (const fn of h.timers.values()) fn();
  assert.equal(failures.length, 1);
  assert.match(failures[0].detail, /did not initialize/);
});

test('ready clears the watchdog and blocked autoplay stays waiting for a click', async () => {
  const h = componentHarness();
  let blocked = 0;
  h.render(props({ onBlocked: () => blocked++ }));
  await Promise.resolve();
  h.events.onReady({ target: h.player });
  assert.equal(h.timers.size, 0);
  h.events.onAutoplayBlocked();
  assert.equal(blocked, 1);
});

 test('a music request starts with sound as soon as the player is ready', async () => {
  const h = componentHarness();
  h.render(props());
  await Promise.resolve();
  h.events.onReady({ target: h.player });
  assert.deepEqual(h.calls, [['volume', 100], ['unmute'], ['play']]);
});
