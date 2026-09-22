const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const source = fs.readFileSync(
  process.env.APEX_AUTONOMOUS_TEST_SOURCE || path.join(__dirname, '../lib/autonomous.ts'),
  'utf8',
);
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;

const flush = async () => {
  for (let i = 0; i < 12; i++) await Promise.resolve();
};

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

// The hook only needs refs and effects. Keep its browser clock and hook state
// isolated so provider rerenders can be simulated without real-time waits.
function harness(overrides = {}) {
  let now = Date.parse('2026-09-22T10:00:00Z');
  let cursor = 0;
  let nextTimer = 0;
  const slots = [];
  const effects = [];
  const timers = new Map();
  const storage = new Map();
  const calls = { chat: [], publish: [], health: [] };
  const document = {
    hidden: false,
    addEventListener() {},
    removeEventListener() {},
  };
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const react = {
    useRef(value) {
      const index = cursor++;
      return slots[index] || (slots[index] = { current: value });
    },
    useEffect(run, deps) {
      const index = cursor++;
      const previous = slots[index];
      if (previous && deps && deps.every((value, i) => Object.is(value, previous.deps[i]))) return;
      effects.push(() => {
        previous?.cleanup?.();
        slots[index] = { deps, cleanup: run() };
      });
    },
  };
  const exports = {};
  const context = vm.createContext({
    exports,
    require(name) {
      if (name === 'react') return react;
      if (name === './api') return {
        api: { self: { health: async (...args) => {
          calls.health.push(args);
          return { health: { overall: 'healthy', checks: [] } };
        } } },
      };
      throw new Error(`Unexpected module: ${name}`);
    },
    Date: Clock,
    Math: Object.assign(Object.create(Math), { random: () => 0 }),
    console,
    window: {},
    document,
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
    },
    setInterval(fn, delay) {
      const id = ++nextTimer;
      timers.set(id, { fn, delay, due: now + delay });
      return id;
    },
    clearInterval: (id) => timers.delete(id),
  });
  vm.runInContext(compiled, context);
  let options = {
    enabled: true,
    humorLevel: 100,
    sarcasmLevel: 20,
    voiceBudget: 100,
    voiceEnabled: false,
    busy: false,
    orb: 'idle',
    lastUserActivityAt: now - 60000,
    skill: 'general',
    ...overrides,
  };
  function render(changes = {}) {
    options = { ...options, ...changes };
    cursor = 0;
    exports.useAutonomousMode({
      ...options,
      // Each provider render creates fresh callback identities.
      chat: async (prompt) => {
        calls.chat.push(prompt);
        return options.chat ? options.chat(prompt) : 'An autonomous thought.';
      },
      publish: async (text, settings) => { calls.publish.push({ text, voice: settings.voice }); },
    });
    for (const run of effects.splice(0)) run();
  }
  async function advance(ms) {
    const target = now + ms;
    while (true) {
      const due = [...timers.values()].sort((a, b) => a.due - b.due)[0];
      if (!due || due.due > target) break;
      now = due.due;
      due.due += due.delay;
      due.fn();
      await flush();
    }
    now = target;
    await flush();
  }
  render();
  return {
    calls, document, storage, render, advance,
    now: () => now,
    unmount: () => slots.forEach((slot) => slot?.cleanup?.()),
  };
}

test('one-second provider rerenders do not postpone social or health timers', async () => {
  const h = harness();
  for (let second = 0; second < 60; second++) {
    await h.advance(1000);
    h.render();
  }
  assert.equal(h.calls.chat.length, 1);
  assert.equal(h.calls.health.length, 1);
  assert.equal(h.calls.publish[0].text, 'An autonomous thought.');
});

test('ticks read the latest busy state and user activity', async () => {
  const h = harness({ busy: true });
  await h.advance(20000);
  assert.equal(h.calls.chat.length, 0);
  h.render({ busy: false, lastUserActivityAt: h.now() });
  await h.advance(10000);
  assert.equal(h.calls.chat.length, 0);
  await h.advance(10000);
  assert.equal(h.calls.chat.length, 1);
});

test('disabled and hidden tabs do not start autonomous work', async () => {
  const h = harness({ enabled: false });
  await h.advance(60000);
  assert.equal(h.calls.chat.length + h.calls.health.length, 0);
  h.document.hidden = true;
  h.render({ enabled: true });
  await h.advance(60000);
  assert.equal(h.calls.chat.length + h.calls.health.length, 0);
  h.document.hidden = false;
  await h.advance(10000);
  assert.equal(h.calls.chat.length, 1);
});

test('a pending social request does not overlap later social or health work', async () => {
  const response = deferred();
  const h = harness({ chat: () => response.promise });
  await h.advance(630000);
  assert.equal(h.calls.chat.length, 1);
  assert.equal(h.calls.health.length, 0);
  response.resolve('Finished thinking.');
  await flush();
  assert.equal(h.calls.publish.length, 1);
});

for (const guard of ['disabled', 'hidden', 'busy', 'listening', 'activity', 'unmounted']) {
  test(`pending output is suppressed after becoming ${guard}`, async () => {
    const response = deferred();
    const h = harness({ chat: () => response.promise });
    await h.advance(10000);
    assert.equal(h.calls.chat.length, 1);
    if (guard === 'disabled') h.render({ enabled: false });
    if (guard === 'hidden') h.document.hidden = true;
    if (guard === 'busy') h.render({ busy: true });
    if (guard === 'listening') h.render({ orb: 'listening' });
    if (guard === 'activity') h.render({ lastUserActivityAt: h.now() });
    if (guard === 'unmounted') h.unmount();
    response.resolve('This is no longer timely.');
    await flush();
    assert.equal(h.calls.publish.length, 0);
  });
}

test('voice budget is checked when a pending response is delivered', async () => {
  const response = deferred();
  const h = harness({ voiceEnabled: true, voiceBudget: 1, chat: () => response.promise });
  await h.advance(10000);
  h.storage.set('apex:autonomous-voice-count', JSON.stringify({ date: '2026-09-22', count: 1 }));
  response.resolve('The daily voice budget is used up.');
  await flush();
  assert.equal(h.calls.publish.length, 1);
  assert.equal(h.calls.publish[0].voice, false);
  assert.equal(JSON.parse(h.storage.get('apex:autonomous-voice-count')).count, 1);
});

test('pending responses honor the latest voice preference', async () => {
  const response = deferred();
  const h = harness({ voiceEnabled: false, chat: () => response.promise });
  await h.advance(10000);
  h.render({ voiceEnabled: true });
  response.resolve('Voice is now enabled.');
  await flush();
  assert.equal(h.calls.publish.length, 1);
  assert.equal(h.calls.publish[0].voice, true);
  assert.equal(JSON.parse(h.storage.get('apex:autonomous-voice-count')).count, 1);
});
