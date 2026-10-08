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
  terminalUrl, terminalSessionId, isTerminalWindow, isFilesWindow, shouldReleaseTerminal, onDesktop,
  terminalNumber, terminalWindows, isSignedPreviewToken, groupItemsByKind } = moduleExports;

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
    rect: { x: 0, y: 0, w: 400, h: 300 }, maximized: false, minimized: false, desktop: 0, note: '', showNotes: false };
  assert.equal(isTerminalWindow(w), true);
  assert.equal(isTerminalWindow({ ...w, items: [{ url: 'https://x.com/a.png', title: 'a.png' }], kind: 'image' }), false);
});

test('collectPreviewableItems ignores markdown links to plain sites', () => {
  const items = collectPreviewableItems('[Apex](https://opencode.ai) is a site, and so is https://x.com.');
  assert.deepEqual(items, []);
});

test('camera snapshot URLs are images, signed tokens and previewable', () => {
  const url = 'https://apex.local/api/visio/frame/eyJ1IjoiYWxpY2UifQ.abc-DEF_123';
  assert.equal(kindForName('Camera snapshot', url), 'image');
  assert.equal(kindForName('Camera snapshot', '/api/visio/frame/tok.en'), 'image');
  assert.equal(isSignedPreviewToken(url), true);
  assert.equal(isSignedPreviewToken('/api/visio/frame/tok.en'), true);
  const items = collectPreviewableItems(`Snapshot: ${url} and /api/visio/frame/other-token.99`);
  assert.deepEqual(items.map((i) => i.url), [url, '/api/visio/frame/other-token.99']);
});

test('a markdown image opens as an image window even without an extension', () => {
  // Web image search results often carry no file extension; `![]()` is the
  // model saying "this is a picture" and must still land in an image window.
  const items = collectPreviewableItems(
    '![a cat](https://cdn.example.com/photos/cat) then [the same page](https://cdn.example.com/photos/cat)',
  );
  assert.equal(items.length, 1);
  assert.equal(items[0].kind, 'image');
});

test('groupItemsByKind splits a reply into one window per kind, images together', () => {
  const groups = groupItemsByKind([
    { url: 'https://x.com/a.png', title: 'a.png' },
    { url: 'https://y.com/b.jpg', title: 'b.jpg' },
    { url: '/api/editor/download/t1', title: 'Report.docx', kind: 'docx' },
    { url: 'https://z.com/c.pdf', title: 'c.pdf' },
  ]);
  assert.equal(groups.length, 3, 'photos share one window; the document and the PDF get their own');
  assert.deepEqual(groups[0].map((i) => i.title), ['a.png', 'b.jpg']);
  assert.deepEqual(groups[1].map((i) => i.title), ['Report.docx']);
  assert.deepEqual(groups[2].map((i) => i.title), ['c.pdf']);
  assert.deepEqual(groupItemsByKind([]), []);
});

test('windowContextBlock labels focused windows and is empty when nothing is open', () => {
  assert.equal(windowContextBlock([], 'w1'), '');
  const w1 = { id: 'w1', items: [{ url: '/api/editor/download/t', title: 'Budget.xlsx' }], index: 0, kind: 'xlsx',
    rect: { x: 0, y: 0, w: 400, h: 300 }, maximized: false, minimized: false, note: '', showNotes: false };
  const w2 = { id: 'w2', items: [{ url: 'https://x.com/chart.png', title: '' }], index: 0, kind: 'image',
    rect: { x: 0, y: 0, w: 400, h: 300 }, maximized: false, minimized: false, note: '', showNotes: false };
  const block = windowContextBlock([w1, w2], 'w2');
  assert.equal(block, `[Open windows: #1 "Budget.xlsx" (xlsx) · #2 "chart.png" (image, focused)]`);
  // the active virtual desktop is appended so the model stays in sync
  assert.equal(
    windowContextBlock([w1], null, 1),
    `[Open windows: #1 "Budget.xlsx" (xlsx) · active desktop 2/4]`,
  );
});

test('windowContextBlock reports state, and never a desktop number out of thin air', () => {
  const mk = (over) => ({ id: over.id, items: [{ url: 'https://x.com/a.png', title: 'a.png' }], index: 0, kind: 'image',
    rect: { x: 0, y: 0, w: 400, h: 300 }, maximized: false, minimized: false, note: '', showNotes: false, ...over });
  // `desktop` is optional, and undefined + 1 is NaN: a window with no desktop
  // must not produce "on desktop NaN" in a model prompt.
  const noDesktop = mk({ id: 'a' });
  assert.doesNotMatch(windowContextBlock([noDesktop], null, 1), /NaN|undefined/);
  // A window that is minimized is still open, which is exactly why the flag has
  // to be in the block: without it, "restore" is indistinguishable from "open".
  assert.match(windowContextBlock([mk({ id: 'a', minimized: true })], null, 0), /minimized/);
  assert.match(windowContextBlock([mk({ id: 'a', maximized: true })], null, 0), /maximized/);
  assert.match(windowContextBlock([mk({ id: 'a', desktop: 2 })], null, 0), /on desktop 3/);
  // and it is only reported when it is *not* the one being looked at
  assert.doesNotMatch(windowContextBlock([mk({ id: 'a', desktop: 0 })], null, 0), /on desktop/);
});

test('onDesktop filters windows to their virtual desktop', () => {
  const mk = (id, desktop) => ({ id, desktop, items: [], index: 0, kind: 'other',
    rect: { x: 0, y: 0, w: 400, h: 300 }, maximized: false, minimized: false, note: '', showNotes: false });
  const windows = [mk('a', 0), mk('b', 1), mk('c', 2), mk('d', 3), mk('e', 0)];
  assert.deepEqual(onDesktop(windows, 0).map(w => w.id), ['a', 'e']);
  assert.deepEqual(onDesktop(windows, 2).map(w => w.id), ['c']);
  assert.deepEqual(onDesktop(windows, 3).map(w => w.id), ['d']);
  assert.deepEqual(onDesktop([], 1), []);
});

test('windowContextBlock tags terminal windows with their session id and number', () => {
  const w = { id: 'w1', items: [{ url: terminalUrl('abcdef1234567890'), title: 'Terminal' }], index: 0, kind: 'terminal',
    rect: { x: 0, y: 0, w: 400, h: 300 }, maximized: false, minimized: false, desktop: 0, note: '', showNotes: false };
  // "terminal #N" is the number painted in the title bar, so the model is handed
  // the same number the operator says out loud.
  assert.equal(windowContextBlock([w], 'w1'), `[Open windows: terminal #1 "Terminal abcdef12" (terminal, focused)]`);
});

test('shouldReleaseTerminal skips the StrictMode phantom cleanup', () => {
  const mountedAt = Date.now();
  assert.equal(shouldReleaseTerminal(mountedAt), false);
  assert.equal(shouldReleaseTerminal(mountedAt - 100), false);
  assert.equal(shouldReleaseTerminal(mountedAt - 5000), true);
});

test('file-manager windows are identified by kind or synthetic url, and files: never downloads', () => {
  const base = { id: 'w1', index: 0, kind: 'files',
    items: [{ url: 'files:', title: 'File Manager' }],
    rect: { x: 0, y: 0, w: 600, h: 400 }, maximized: false, minimized: false, desktop: 0, note: '', showNotes: false };
  assert.equal(isFilesWindow(base), true);
  assert.equal(isFilesWindow({ ...base, kind: 'other' }), true, 'url signals a files window even without the kind');
  assert.equal(isFilesWindow({ ...base, items: [{ url: 'https://x.com/a.png', title: 'a.png' }], kind: 'image' }), false);
  assert.equal(windowDownload({ url: 'files:', title: 'File Manager' }), null);
});

test('file-manager signed download links are treated as backend downloads', () => {
  assert.deepEqual(windowDownload({ url: '/api/fm/download/t0k', title: 'photo.jpg' }), {
    href: '/api/fm/download/t0k',
    download: 'photo.jpg',
  });
  const items = collectPreviewableItems('See [Bundle](/api/fm/download/abc123) or [Photo](/api/images/file/def456).');
  assert.ok(items.some((i) => i.url === '/api/fm/download/abc123' && i.title === 'Bundle'));
  assert.ok(items.some((i) => i.url === '/api/images/file/def456' && i.title === 'Photo'));
});

test('text windows use Notepad controls while other preview kinds remain unchanged', () => {
  for (const extension of ['txt', 'md', 'log', 'json', 'py', 'html']) {
    const item = { title: 'file.' + extension, url: '/api/files/download/token' };
    assert.equal(moduleExports.isNotepadWindow({ kind: kindForName(item.title), items: [item], index: 0 }), true, extension);
  }
  for (const kind of ['pdf', 'docx', 'image']) {
    assert.equal(moduleExports.isNotepadWindow({ kind, items: [{ title: 'file', url: '/file', kind }], index: 0 }), false);
  }
});

/* ---------- terminal numbering ---------- */
const win = (id, kind, extra) => ({
  id, kind, index: 0, items: [{ url: kind === 'terminal' ? 'terminal:s' + id : '/f/' + id, title: id }],
  rect: { x: 0, y: 0, w: 10, h: 10 }, maximized: false, minimized: false, desktop: 0, note: '', showNotes: false,
  ...extra,
});

test('terminals are numbered by their position among terminals, not among all windows', () => {
  const windows = [win('a', 'image'), win('t1', 'terminal'), win('b', 'pdf'), win('t2', 'terminal')];
  assert.equal(terminalNumber(windows, windows[0]), null, 'a non-terminal has no spoken number');
  assert.equal(terminalNumber(windows, windows[1]), 1, 'first terminal is 1 even though it is window 2');
  assert.equal(terminalNumber(windows, windows[3]), 2, 'second terminal is 2 even though it is window 4');
  assert.equal(terminalWindows(windows).map((w) => w.id).join(','), 't1,t2');
  assert.equal(terminalNumber(windows, win('ghost', 'terminal')), null, 'unknown window is not numbered');
});

test('the window context block tells the model the number shown on screen', () => {
  const windows = [win('a', 'image'), win('t1', 'terminal'), win('t2', 'terminal')];
  const block = windowContextBlock(windows, 't2');
  // The operator says "terminal 2", so the model must be handed that same 2.
  assert.match(block, /terminal #1 "Terminal st1" \(terminal\)/);
  assert.match(block, /terminal #2 "Terminal st2" \(terminal, focused\)/);
  assert.match(block, /#1 "a" \(image\)/, 'non-terminals keep their window position');
});

test('a terminal window item is recognised by its terminal: url regardless of kind', () => {
  const mislabelled = win('t1', 'other');
  mislabelled.items[0].url = 'terminal:sABC';
  assert.equal(isTerminalWindow(mislabelled), true);
  assert.equal(terminalNumber([mislabelled], mislabelled), 1);
});
