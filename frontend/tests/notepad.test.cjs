const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../components/NotepadWindow.tsx'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };

function harness() {
  const slots = [], effects = [], listeners = new Map(), requests = [], downloads = [];
  let cursor = 0, tree, resolveSave;
  const editor = { innerHTML: '', innerText: '', focus() {} };
  const react = {
    useRef(value) { const i = cursor++; return slots[i] ?? (slots[i] = { current: value }); },
    useState(value) { const i = cursor++; if (!(i in slots)) slots[i] = value; return [slots[i], (next) => { slots[i] = next; }]; },
    useCallback(fn) { cursor++; return fn; },
    useEffect(fn, deps) {
      const i = cursor++, previous = slots[i];
      if (previous && deps.every((value, n) => Object.is(value, previous.deps[n]))) return;
      effects.push(() => { previous?.cleanup?.(); slots[i] = { deps, cleanup: fn() }; });
    },
  };
  const exports = {};
  vm.runInNewContext(compiled, {
    exports,
    require(name) {
      if (name === 'react') return react;
      if (name === 'react/jsx-runtime') return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
      if (name.endsWith('.css')) return { default: {} };
      if (name === '../lib/api') return { BASE: '', api: { notepad: {
        list: async () => ({ documents: [] }),
        save: (payload) => { requests.push(payload); return new Promise((resolve) => { resolveSave = resolve; }); },
      } } };
      throw Error(name);
    },
    window: {
      confirm: () => false,
      addEventListener(name, fn) { listeners.set(name, fn); },
      removeEventListener(name) { listeners.delete(name); },
    },
    document: { createElement: () => ({ click() { downloads.push(this.href); } }) },
    setTimeout: () => 1, clearTimeout() {}, console,
  });
  function nodes(node) {
    if (!node || typeof node !== 'object') return [];
    if (Array.isArray(node)) return node.flatMap(nodes);
    return [node, ...nodes(node.props?.children)];
  }
  function render() {
    cursor = 0;
    tree = exports.default({ focused: true, windowId: 'pad1' });
    nodes(tree).find((n) => n.props?.['aria-label'] === 'Document content').props.ref.current = editor;
    effects.splice(0).forEach((run) => run());
  }
  render();
  return {
    render, requests, downloads, listeners,
    saveShortcut() { tree.props.onKeyDown({ ctrlKey: true, key: "s", stopPropagation() {}, preventDefault() {} }); },
    find: (label) => nodes(tree).find((n) => n.props?.['aria-label'] === label || n.props?.children === label),
    type(value) { editor.innerHTML = value; editor.innerText = value; this.find('Document content').props.onInput(); render(); },
    finish() { resolveSave({ name: 'Untitled.html', title: 'Untitled', download_url: '/download', directory: '/notes' }); },
  };
}

test('save shortcut works inside the editor and edits during save stay dirty', async () => {
  const h = harness(); h.type('first');
  let prevented = false;
  h.saveShortcut();
  h.render(); h.type('second');
  h.finish(); await flush(); h.render();
  assert.equal(h.requests[0].content, 'first');
  const close = { detail: { id: 'pad1' }, preventDefault() { prevented = true; } };
  h.listeners.get('apex:window-before-close')(close);
  assert.equal(prevented, true);
  h.find('SAVE').props.onClick();
  assert.equal(h.requests[1].content, 'second');
});

test('HTML download waits for current content to save', async () => {
  const h = harness(); h.type('latest');
  h.find('HTML ↓').props.onClick();
  assert.equal(h.downloads.length, 0);
  assert.equal(h.requests[0].content, 'latest');
  h.finish(); await flush();
  assert.deepEqual(h.downloads, ['/download']);
});

test('canceling new document retains unsaved content', () => {
  const h = harness(); h.type('keep me');
  h.find('RECENT DOCUMENTS').props.onClick(); h.render();
  h.find('＋ NEW DOCUMENT').props.onClick(); h.render();
  h.find('SAVE').props.onClick();
  assert.equal(h.requests[0].content, 'keep me');
});


test('recent documents panel starts closed and opens on request', () => {
  const h = harness();
  assert.equal(h.find('＋ NEW DOCUMENT'), undefined);
  h.find('RECENT DOCUMENTS').props.onClick(); h.render();
  assert.ok(h.find('＋ NEW DOCUMENT'));
});
