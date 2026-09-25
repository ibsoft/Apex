const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const source = fs.readFileSync(path.join(__dirname, '../lib/windows.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const moduleExports = {};
new Function('exports', compiled)(moduleExports);
const { MAX_WINDOWS, kindForName, layoutRects, collectPreviewableItems, windowContextBlock, windowDownload,
  terminalUrl, terminalSessionId, isTerminalWindow, shouldReleaseTerminal } = moduleExports;

test('kindForName classifies files by extension', () => {
  assert.equal(kindForName('photo.png'), 'image');
  assert.equal(kindForName('logo.svg', ''), 'image');
  assert.equal(kindForName('report.pdf'), 'pdf');
  assert.equal(kindForName('budget.xlsx'), 'xlsx');
  assert.equal(kindForName('deck.pptx'), 'pptx');
  assert.equal(kindForName('notes.txt'), 'text');
  assert.equal(kindForName('script.py'), 'text');
  assert.equal(kindForName('archive.zip'), 'other');
  assert.equal(kindForName('doc.pdf?token=abc'), 'pdf');
});

test('image-browser signed URLs are always images even without an extension', () => {
  assert.equal(kindForName('Cats Wallpaper', 'http://127.0.0.1:5001/api/images/file/abc123'), 'image');
  assert.equal(kindForName('cats', '/api/images/file/def456'), 'image');
});

test('layoutRects keeps every tile inside the desktop for all arrangements', () => {
  const vw = 1200;
  const vh = 800;
  for (const arrangement of ['cascade', 'grid', 'tile-h', 'tile-v', 'center']) {
    for (const total of [1, 2, 5, 10]) {
      const rects = layoutRects(arrangement, total, vw, vh);
      assert.equal(rects.length, total);
      for (const r of rects) {
        assert.ok(r.w > 0 && r.h > 0, `${arrangement} ${total}`);
        assert.ok(r.x >= 0 && r.y >= 0 && r.x + r.w <= vw && r.y + r.h <= vh - 44, `${arrangement} ${total}`);
      }
    }
  }
});

test('cascade offsets successive windows within bounds', () => {
  const rects = layoutRects('cascade', 3, 1200, 800);
  assert.ok(rects[0].x < rects[1].x && rects[1].x < rects[2].x);
  assert.ok(rects[0].y < rects[1].y && rects[1].y < rects[2].y);
});

test('collectPreviewableItems finds and deduplicates tokens, links and raw URLs', () => {
  const content = [
    'See [Chart](/api/editor/download/abc123) and [Photo](/api/images/file/def456).',
    'Shell run: [Output](/api/shell/download/xyz789).',
    'Raw image: https://example.com/a/b.png?size=1. Duplicate: [Chart](/api/editor/download/abc123).',
    'Obsidian: /api/obsidian/file?path=Inbox/note.md',
    'https://other.example.org/page.pdf and https://skip.example.com/chart.docx?x=1',
  ].join('\n');
  const items = collectPreviewableItems(content);
  const urls = items.map((i) => i.url);
  assert.equal(new Set(urls).size, urls.length, 'no duplicates');
  assert.ok(items.some((i) => i.url === '/api/editor/download/abc123' && i.title === 'Chart'));
  assert.ok(items.some((i) => i.url === '/api/images/file/def456' && i.title === 'Photo'));
  assert.ok(items.some((i) => i.url === '/api/shell/download/xyz789' && i.title === 'Output'));
  assert.ok(items.some((i) => i.url === 'https://example.com/a/b.png?size=1' && i.title === 'b.png'));
  assert.ok(items.some((i) => i.url.startsWith('/api/obsidian/file?path=Inbox/note.md')));
  assert.ok(items.some((i) => i.url === 'https://other.example.org/page.pdf'));
  assert.ok(!items.some((i) => i.url.includes('chart.docx')), 'docx without a signed token is excluded');
  assert.equal(items.length, 6);
});

test('windowDownload returns the backend endpoint for signed links and the original for external ones', () => {
  assert.deepEqual(windowDownload({ url: '/api/editor/download/t0k', title: 'report.docx' }), {
    href: '/api/editor/download/t0k',
    download: 'report.docx',
  });
  assert.deepEqual(windowDownload({ url: '/api/shell/download/t0k', title: 'output.txt' }), {
    href: '/api/shell/download/t0k',
    download: 'output.txt',
  });
  assert.deepEqual(windowDownload({ url: 'https://example.com/a.png', title: 'a.png' }), {
    href: 'https://example.com/a.png',
    download: 'a.png',
  });
  assert.equal(windowDownload({ url: '', title: '' }), null);
});

test('terminal items carry a synthetic url, identify sessions, and never download', () => {
  const url = terminalUrl('sess123');
  assert.equal(url, 'terminal:sess123');
  assert.equal(terminalSessionId({ url }), 'sess123');
  assert.equal(terminalSessionId({ url: 'terminal:' }), null);
  assert.equal(terminalSessionId({ url: 'https://x.com/a.png' }), null);
  assert.equal(windowDownload({ url, title: 'Terminal' }), null);
  const w = { id: 'w1', items: [{ url, title: 'Terminal' }], index: 0, kind: 'terminal',
    rect: { x: 0, y: 0, w: 400, h: 300 }, maximized: false, minimized: false, note: '', showNotes: false };
  assert.equal(isTerminalWindow(w), true);
  assert.equal(isTerminalWindow({ ...w, items: [{ url: 'https://x.com/a.png', title: 'a.png' }], kind: 'image' }), false);
});

test('collectPreviewableItems ignores markdown links to plain sites', () => {
  const items = collectPreviewableItems('[Apex](https://opencode.ai) is a site, and so is https://x.com.');
  assert.deepEqual(items, []);
});

test('windowContextBlock labels focused windows and is empty when nothing is open', () => {
  assert.equal(windowContextBlock([], 'w1'), '');
  const w1 = { id: 'w1', items: [{ url: '/api/editor/download/t', title: 'Budget.xlsx' }], index: 0, kind: 'xlsx',
    rect: { x: 0, y: 0, w: 400, h: 300 }, maximized: false, minimized: false, note: '', showNotes: false };
  const w2 = { id: 'w2', items: [{ url: 'https://x.com/chart.png', title: '' }], index: 0, kind: 'image',
    rect: { x: 0, y: 0, w: 400, h: 300 }, maximized: false, minimized: false, note: '', showNotes: false };
  const block = windowContextBlock([w1, w2], 'w2');
  assert.equal(block, `[Open windows: #1 "Budget.xlsx" (xlsx) · #2 "chart.png" (image, focused)]`);
});

test('windowContextBlock tags terminal windows with their session id', () => {
  const w = { id: 'w1', items: [{ url: terminalUrl('abcdef1234567890'), title: 'Terminal' }], index: 0, kind: 'terminal',
    rect: { x: 0, y: 0, w: 400, h: 300 }, maximized: false, minimized: false, note: '', showNotes: false };
  assert.equal(windowContextBlock([w], 'w1'), `[Open windows: #1 "Terminal abcdef12" (terminal, focused)]`);
});

test('shouldReleaseTerminal skips the StrictMode phantom cleanup', () => {
  const mountedAt = Date.now();
  assert.equal(shouldReleaseTerminal(mountedAt), false);
  assert.equal(shouldReleaseTerminal(mountedAt - 100), false);
  assert.equal(shouldReleaseTerminal(mountedAt - 5000), true);
});