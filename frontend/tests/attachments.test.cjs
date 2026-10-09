/* Chat attachment helpers: the prompt block for uploaded documents and the
   mapping from the memory-upload response to composer chips. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

function loadTs(relPath) {
  const source = fs.readFileSync(path.join(__dirname, '..', relPath), 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const exports = {};
  new Function('exports', 'require', 'module', compiled)(exports, require, { exports });
  return exports;
}

const { buildDocumentContext, attachmentsFromUpload } = loadTs('lib/attachments.ts');

test('an attachment becomes a clearly labelled, bounded document block', () => {
  const block = buildDocumentContext([
    { name: 'spec.pdf', chunks: 3, preview: 'The system shall...' },
  ]);
  assert.match(block, /Attached document: spec\.pdf/);
  assert.match(block, /3 chunks in long-term memory/);
  assert.match(block, /The system shall\.\.\./);
});

test('a single chunk reads in the singular', () => {
  const block = buildDocumentContext([{ name: 'a.txt', chunks: 1, preview: 'hi' }]);
  assert.match(block, /1 chunk in long-term memory/);
});

test('an empty preview is not sent to the model', () => {
  assert.equal(buildDocumentContext([{ name: 'empty.txt', chunks: 0, preview: '   ' }]), '');
  assert.equal(buildDocumentContext([]), '');
});

test('the upload response maps to chips and drops failures', () => {
  const chips = attachmentsFromUpload([
    { filename: 'ok.md', chunks: 4, preview: 'body' },
    { filename: 'bad.pdf', error: 'PDF support requires PyPDF2' },
    { filename: 'zero.txt', chunks: 0, preview: '' },
  ]);
  assert.deepEqual(chips, [
    { name: 'ok.md', chunks: 4, preview: 'body' },
    { name: 'zero.txt', chunks: 0, preview: '' },
  ]);
});
