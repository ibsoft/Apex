/* Math segmentation: what is an equation and what is just money. */
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

const { splitMath } = loadTs('lib/math.ts');

test('inline math between dollar signs is split out', () => {
  assert.deepEqual(splitMath('the area is $a^2$ exactly'), [
    { type: 'text', text: 'the area is ', display: false },
    { type: 'math', text: 'a^2', display: false },
    { type: 'text', text: ' exactly', display: false },
  ]);
});

test('double dollars produce display math and may span lines', () => {
  const segs = splitMath('before\n$$\\int_0^1 x\\,dx$$\nafter');
  const math = segs.find((s) => s.type === 'math');
  assert.ok(math);
  assert.equal(math.display, true);
  assert.equal(math.text, '\\int_0^1 x\\,dx');
});

test('LaTeX \\( \\) and \\[ \\] delimiters are recognised', () => {
  assert.equal(splitMath('a \\(x+1\\) b').find((s) => s.type === 'math').display, false);
  assert.equal(splitMath('a \\[x+1\\] b').find((s) => s.type === 'math').display, true);
});

test('currency is not mistaken for math', () => {
  const segs = splitMath('it costs $5 and $7 total');
  assert.equal(segs.length, 1);
  assert.equal(segs[0].type, 'text');
});

test('an escaped dollar stays literal', () => {
  const segs = splitMath('pay \\$5 now');
  assert.equal(segs.length, 1);
  assert.equal(segs[0].text, 'pay \\$5 now');
});

test('plain prose is one text segment', () => {
  assert.deepEqual(splitMath('hello world'), [{ type: 'text', text: 'hello world', display: false }]);
});

test('an unclosed dollar is left as text', () => {
  assert.deepEqual(splitMath('half $x'), [{ type: 'text', text: 'half $x', display: false }]);
});
