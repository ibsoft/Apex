const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const source = fs.readFileSync(path.join(__dirname, '../lib/fm.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const moduleExports = {};
// fm.ts imports ../lib siblings at runtime (api.BASE, windows.kindForName); the
// node harness has no require harness, so hand it stubs for those two modules.
const shimRequire = (id) => {
  if (id === './api') return { BASE: '' };
  if (id === './windows') return { kindForName: (name) => 'other' };
  throw new Error(`unexpected require: ${id}`);
};
new Function('exports', 'require', compiled)(moduleExports, shimRequire);
const {
  FM_CLIPBOARD_KEY, FM_CHANNEL, FM_ICON_SIZES, FM_SETTINGS_KEY, FmApiError, fm, fmUpload,
  serializeClipboard, parseClipboard, readStoredClipboard, storeStoredClipboard, clipboardAfterMove,
  fmJoin, formatFmBytes, formatFmDate, entryWindowKind,
  DEFAULT_FM_SETTINGS, serializeSettings, parseSettings, readStoredSettings, storeStoredSettings,
} = moduleExports;

test('settings fall back to defaults and sanitize bad payloads', () => {
  assert.deepEqual(parseSettings(null), DEFAULT_FM_SETTINGS);
  assert.deepEqual(parseSettings(''), DEFAULT_FM_SETTINGS);
  assert.deepEqual(parseSettings('not json'), DEFAULT_FM_SETTINGS);
  assert.deepEqual(parseSettings('{"iconSize": 99, "showHidden": "yes"}'), DEFAULT_FM_SETTINGS, 'icon size must be one of the fixed steps, showHidden must be boolean');
  assert.deepEqual(parseSettings('{"iconSize": 26, "showHidden": true}'), { iconSize: 26, showHidden: true, view: 'list', sortKey: 'name', sortDir: 1, lastPath: null });
  assert.deepEqual(parseSettings('{"iconSize": 19}'), { iconSize: 19, showHidden: false, view: 'list', sortKey: 'name', sortDir: 1, lastPath: null }, 'missing fields fall back to defaults');
  assert.deepEqual(parseSettings('{"view": "grid", "sortKey": "mtime_ns", "sortDir": -1, "lastPath": "/tmp/x"}'),
    { iconSize: 19, showHidden: false, view: 'grid', sortKey: 'mtime_ns', sortDir: -1, lastPath: '/tmp/x' });
  assert.deepEqual(parseSettings('{"view": "cards", "sortKey": "foo", "sortDir": 2, "lastPath": ""}'),
    { iconSize: 19, showHidden: false, view: 'list', sortKey: 'name', sortDir: 1, lastPath: null },
    'unknown view/sortKey fall back, sortDir coerces to 1|-1, empty lastPath is null');
});

test('settings serialize and round-trip through stored helpers', () => {
  const settings = { iconSize: 33, showHidden: true, view: 'grid', sortKey: 'size_bytes', sortDir: -1, lastPath: '/tmp/x' };
  const raw = serializeSettings(settings);
  const parsed = parseSettings(raw);
  assert.equal(parsed.iconSize, 33);
  assert.equal(parsed.showHidden, true);
  assert.equal(parsed.view, 'grid');
  assert.equal(parsed.sortKey, 'size_bytes');
  assert.equal(parsed.sortDir, -1);
  assert.equal(parsed.lastPath, '/tmp/x');
  const stored = new Map();
  const storage = {
    getItem: (k) => stored.get(k) ?? null,
    setItem: (k, v) => stored.set(k, v),
  };
  storeStoredSettings(settings, storage);
  assert.equal(stored.get(FM_SETTINGS_KEY), raw);
  assert.deepEqual(readStoredSettings(storage), settings);
  assert.deepEqual(readStoredSettings({ getItem: () => null }), DEFAULT_FM_SETTINGS, 'missing key returns defaults');
  assert.ok(FM_ICON_SIZES.length >= 3 && FM_ICON_SIZES.includes(19), 'default 19px is a valid step');
});

test('clipboard serializes and round-trips', () => {
  const cut = { mode: 'cut', paths: ['/a/b.txt', '/a/c'], at: 1234 };
  assert.equal(serializeClipboard(cut), JSON.stringify(cut));
  assert.deepEqual(parseClipboard(serializeClipboard(cut)), cut);
  assert.deepEqual(parseClipboard(serializeClipboard(null)), null);
});

test('parseClipboard rejects malformed or empty payloads', () => {
  assert.equal(parseClipboard(null), null);
  assert.equal(parseClipboard(''), null);
  assert.equal(parseClipboard('not json'), null);
  assert.equal(parseClipboard('{"mode":"copy"}'), null, 'no paths');
  assert.equal(parseClipboard('{"mode":"move","paths":["/a"]}'), null, 'unknown mode');
  assert.equal(parseClipboard('{"mode":"copy","paths":[]}'), null, 'empty paths');
  assert.deepEqual(parseClipboard('{"mode":"copy","paths":["/a", 42]}'), { mode: 'copy', paths: ['/a'], at: 0 }, 'non-string paths are sanitized away');
});

test('clipboardAfterMove clears only cut items that actually moved', () => {
  const copy = { mode: 'copy', paths: ['/a/b.txt'], at: 1 };
  assert.deepEqual(clipboardAfterMove(copy, ['/a/b.txt']), copy, 'copies survive a move');
  const cut = { mode: 'cut', paths: ['/x/1.txt', '/x/2.txt'], at: 2 };
  const partial = clipboardAfterMove(cut, ['/x/2.txt', '/elsewhere']);
  assert.equal(partial.mode, 'cut');
  assert.deepEqual(partial.paths, ['/x/1.txt']);
  assert.equal(clipboardAfterMove(cut, cut.paths), null, 'everything moved clears the clipboard');
  assert.equal(clipboardAfterMove(null, ['/x']), null);
});

test('stored clipboard uses the provided storage and the canonical key', () => {
  const storage = new Map();
  const mock = {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  };
  const clip = { mode: 'copy', paths: ['/a'], at: 7 };
  storeStoredClipboard(clip, mock);
  assert.deepEqual(readStoredClipboard(mock), clip);
  assert.ok(storage.has(FM_CLIPBOARD_KEY));
  storeStoredClipboard(null, mock);
  assert.equal(readStoredClipboard(mock), null);
  assert.ok(!storage.has(FM_CLIPBOARD_KEY));
});

test('fmJoin joins paths without doubling separators', () => {
  assert.equal(fmJoin('/a/b', 'c.txt'), '/a/b/c.txt');
  assert.equal(fmJoin('/a/b/', 'c.txt'), '/a/b/c.txt');
  assert.equal(fmJoin('/', 'root.txt'), '/root.txt');
});

test('formatFmBytes handles units and unknown sizes', () => {
  assert.equal(formatFmBytes(null), '—');
  assert.equal(formatFmBytes(undefined), '—');
  assert.equal(formatFmBytes(0), '0 B');
  assert.equal(formatFmBytes(1023), '1023 B');
  assert.equal(formatFmBytes(1536), '1.5 KB');
  assert.equal(formatFmBytes(1245), '1.2 KB');
  assert.equal(formatFmBytes(1024 * 1024 * 1024), '1.0 GB');
});

test('formatFmDate returns a dash for missing timestamps', () => {
  assert.equal(formatFmDate(0), '—');
  assert.equal(formatFmDate(undefined), '—');
  assert.ok(formatFmDate(Date.now() * 1e6).includes('2026'), 'resolves ns to a date');
});

test('entryWindowKind maps backend kinds onto window-manager kinds', () => {
  assert.equal(entryWindowKind({ name: 'dir', kind: 'folder' }), 'other');
  assert.equal(entryWindowKind({ name: 'ln', kind: 'link' }), 'other');
  assert.equal(entryWindowKind({ name: 'a.png', kind: 'image' }), 'image');
  assert.equal(entryWindowKind({ name: 'a.pdf', kind: 'pdf' }), 'pdf');
  assert.equal(entryWindowKind({ name: 'a.docx', kind: 'docx' }), 'docx');
  assert.equal(entryWindowKind({ name: 'a.xlsx', kind: 'xlsx' }), 'xlsx');
  assert.equal(entryWindowKind({ name: 'a.pptx', kind: 'pptx' }), 'pptx');
  assert.equal(entryWindowKind({ name: 'a.txt', kind: 'text' }), 'text');
  assert.equal(entryWindowKind({ name: 'archive.zip', kind: 'other' }), 'other');
});

test('the fm client exposes every endpoint and typed errors carry a status', () => {
  for (const key of ['roots', 'list', 'stat', 'mkdir', 'rename', 'trash', 'listTrash',
    'restore', 'cleanTrash', 'createJob', 'jobStatus', 'cancelJob', 'tickets', 'zip']) {
    assert.equal(typeof fm[key], 'function', key);
  }
  assert.equal(typeof fmUpload, 'function');
  assert.equal(FM_CHANNEL, 'apex-fm-clipboard');
  const err = new FmApiError('boom', 409, [{ source: '/a', target: '/b', is_dir: false }]);
  assert.equal(err.status, 409);
  assert.equal(err.conflicts.length, 1);
  assert.ok(err instanceof Error);
});