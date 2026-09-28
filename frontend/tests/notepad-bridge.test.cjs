const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const vm = require('node:vm');
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../lib/notepad.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;

function bridge(dispatchEvent, fetch = () => {}) {
  const exports = {};
  class CustomEvent {
    constructor(type, options) { this.type = type; this.detail = options.detail; this.defaultPrevented = false; }
    preventDefault() { this.defaultPrevented = true; }
  }
  vm.runInNewContext(compiled, { exports, window: { dispatchEvent }, CustomEvent, setTimeout, clearTimeout, setInterval, clearInterval, fetch, TextDecoder });
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


test('manual and output requests explicitly target Notepad', () => {
  const api = bridge(() => true);
  for (const text of [
    'show me the manual of the command df and show it on notepad',
    'put the command output in the notepad',
    'write a report into my notepad',
    'δείξε το εγχειρίδιο στο σημειωματάριο',
  ]) assert.equal(api.requestsNotepadOutput(text), true, text);
  for (const text of ['show me the manual of df', 'what is Notepad?', 'open notepad']) {
    assert.equal(api.requestsNotepadOutput(text), false, text);
  }
});


test('text loading preserves UTF-8 and requests credentials only for backend files', async () => {
  const calls = [];
  const api = bridge(() => true, async (url, options) => { calls.push(options); return new Response('Καλημέρα\n<code>raw</code>'); });
  const signal = new AbortController().signal;
  assert.equal(await api.loadNotepadText('/be/api/files/download/token', true, signal), 'Καλημέρα\n<code>raw</code>');
  await api.loadNotepadText('https://example.test/file.txt', false, signal);
  assert.equal(calls[0].credentials, 'include');
  assert.equal(calls[1].credentials, 'same-origin');
  assert.equal(calls[0].signal, signal);
});

test('expired and oversized text sources show an error instead of an empty success', async () => {
  const signal = new AbortController().signal;
  const expired = bridge(() => true, async () => new Response('', { status: 410 }));
  await assert.rejects(expired.loadNotepadText('/file', true, signal), /410/);
  const large = bridge(() => true, async () => new Response('x'.repeat(2 * 1024 * 1024 + 1)));
  await assert.rejects(large.loadNotepadText('/file', true, signal), /2 MB/);
});
