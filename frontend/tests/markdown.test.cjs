/* Fenced-code splitting for chat rendering.

   The renderer component cannot be loaded by this harness, so the block
   parser - the part with fence state and the unclosed-fence edge case - is
   tested here. */
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

const { splitFencedCode } = loadTs('lib/markdown.ts');

test('a fenced block is pulled out of the prose', () => {
  const segs = splitFencedCode('Before\n```js\nconst x = 1;\n```\nAfter');
  assert.deepEqual(segs, [
    { type: 'text', text: 'Before' },
    { type: 'code', language: 'js', code: 'const x = 1;' },
    { type: 'text', text: 'After' },
  ]);
});

test('a fence with no language still becomes a code bubble', () => {
  const segs = splitFencedCode('```\nplain\n```');
  assert.deepEqual(segs, [{ type: 'code', language: '', code: 'plain' }]);
});

test('an unclosed fence runs to the end of the message', () => {
  // What a streaming reply looks like before its closing fence arrives.
  const segs = splitFencedCode('Here:\n```python\nprint(1)\nprint(2)');
  assert.deepEqual(segs, [
    { type: 'text', text: 'Here:' },
    { type: 'code', language: 'python', code: 'print(1)\nprint(2)' },
  ]);
});

test('tilde fences work and do not close a backtick fence', () => {
  const segs = splitFencedCode('~~~sh\necho hi\n~~~');
  assert.deepEqual(segs, [{ type: 'code', language: 'sh', code: 'echo hi' }]);

  // A backtick block containing a literal tilde fence must not be cut short.
  const mixed = splitFencedCode('```\n~~~\nstill code\n```');
  assert.deepEqual(mixed, [{ type: 'code', language: '', code: '~~~\nstill code' }]);
});

test('indented fences and trailing fence characters are accepted', () => {
  const segs = splitFencedCode('  ```ts\n  let a = 1;\n  ```');
  assert.deepEqual(segs, [{ type: 'code', language: 'ts', code: '  let a = 1;' }]);
});

test('plain prose is a single text segment', () => {
  assert.deepEqual(splitFencedCode('just words\nand more'), [
    { type: 'text', text: 'just words\nand more' },
  ]);
});

test('text with no fences keeps inline backticks intact', () => {
  const segs = splitFencedCode('use `code` here');
  assert.deepEqual(segs, [{ type: 'text', text: 'use `code` here' }]);
});
