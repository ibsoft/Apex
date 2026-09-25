const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const vm = require('node:vm');
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../lib/notepad.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;

function bridge(dispatchEvent) {
  const exports = {};
  class CustomEvent {
    constructor(type, options) { this.type = type; this.detail = options.detail; this.defaultPrevented = false; }
    preventDefault() { this.defaultPrevented = true; }
  }
  vm.runInNewContext(compiled, { exports, window: { dispatchEvent }, CustomEvent, setTimeout, clearTimeout, setInterval, clearInterval });
  return exports;
}

test('commands wait for mounting and execute exactly once on the chosen editor', async () => {
  let attempts = 0, executions = 0;
  const api = bridge((event) => {
    attempts++;
    if (attempts < 2) return true;
    assert.equal(event.detail.windowId, 'pad2');
    assert.equal(event.detail.content, 'exact output\n');
    event.preventDefault(); executions++;
    setTimeout(() => event.detail.respond('Added text.'), 80);
    return false;
  });
  assert.equal(await api.sendNotepadCommand({ type: 'notepad', action: 'write', content: 'exact output\n' }, 'pad2'), 'Added text.');
  assert.equal(executions, 1);
});

test('live context collects unsaved document content', () => {
  const api = bridge((event) => { event.detail.collect({ title: 'Draft', dirty: true, text: 'Unsaved notes' }); return true; });
  const context = api.notepadContext();
  assert.match(context, /Unsaved notes/);
  assert.match(context, /"dirty":true/);
});
