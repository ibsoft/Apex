const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const source = fs.readFileSync(path.join(__dirname, '../lib/apps.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
// apps.ts only has type-only imports, so no runtime module is ever required.
const moduleExports = {};
new Function('exports', 'require', compiled)(moduleExports, (id) => { throw new Error(`unexpected require: ${id}`); });
const { APPS, getApps, registerApp } = moduleExports;

test('registry is seeded with the terminal and file-manager apps', () => {
  const ids = getApps().map((app) => app.id);
  assert.ok(ids.includes('terminal'));
  assert.ok(ids.includes('files'));
});

test('registerApp ignores duplicate ids and empty payloads', () => {
  const before = getApps().length;
  registerApp({ id: 'files', name: 'dupe', icon: 'x', open: () => {} });
  assert.equal(getApps().length, before, 'duplicate id is ignored');
  registerApp(null);
  registerApp({ name: 'no id', icon: 'x', open: () => {} });
  assert.equal(getApps().length, before, 'null / missing id is ignored');
  registerApp({ id: 'x', name: 'x', icon: 'x', open: () => {} });
  assert.equal(getApps().length, before + 1, 'new app is appended');
});

test('getApps returns a copy so callers cannot mutate the registry', () => {
  const snapshot = getApps();
  snapshot.length = 0;
  assert.ok(getApps().length > 0, 'registry untouched by snapshot mutation');
});

test('terminal app opens a fresh terminal via ctx.openTerminal', async () => {
  const app = getApps().find((a) => a.id === 'terminal');
  assert.ok(app, 'terminal app present');
  let calls = 0;
  const ctx = { openTerminal: async () => { calls += 1; return null; }, windowOpenNew: () => {} };
  await app.open(ctx);
  assert.equal(calls, 1);
});

test('file-manager app opens a new files window via ctx.windowOpenNew', async () => {
  const app = getApps().find((a) => a.id === 'files');
  assert.ok(app, 'files app present');
  const opened = [];
  const ctx = { openTerminal: async () => null, windowOpenNew: (items, opts) => opened.push({ items, opts }) };
  await app.open(ctx);
  assert.equal(opened.length, 1);
  assert.equal(opened[0].opts.kind, 'files');
  assert.equal(opened[0].items[0].url, 'files:');
});