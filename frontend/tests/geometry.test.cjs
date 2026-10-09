/* The geometry language: shape parsing, labels, errors. */
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

const { parseGeometry } = loadTs('lib/geometry.ts');

test('a line and a circle parse into primitives', () => {
  const g = parseGeometry('line 0 0 10 10\ncircle 5 5 3');
  assert.equal(g.error, '');
  assert.deepEqual(g.elements[0], { kind: 'line', x1: 0, y1: 0, x2: 10, y2: 10 });
  assert.deepEqual(g.elements[1], { kind: 'circle', cx: 5, cy: 5, r: 3 });
});

test('polygons and polylines differ only in whether they close', () => {
  const poly = parseGeometry('polygon 0 0 10 0 5 8').elements[0];
  const line = parseGeometry('polyline 0 0 10 0 5 8').elements[0];
  assert.equal(poly.kind, 'poly');
  assert.equal(poly.closed, true);
  assert.equal(line.closed, false);
  assert.deepEqual(poly.points, [[0, 0], [10, 0], [5, 8]]);
});

test('a point takes an optional quoted or bare label', () => {
  assert.equal(parseGeometry('point 2 3 A').elements[0].label, 'A');
  assert.equal(parseGeometry('point 2 3 "vertex A"').elements[0].label, 'vertex A');
  assert.equal(parseGeometry('point 2 3').elements[0].label, '');
});

test('an angle gets a sensible default radius or the one given', () => {
  const auto = parseGeometry('angle 0 0 10 0 0 10').elements[0];
  assert.equal(auto.kind, 'angle');
  assert.ok(auto.r > 0 && auto.r <= 10);
  const fixed = parseGeometry('angle 0 0 10 0 0 10 4').elements[0];
  assert.equal(fixed.r, 4);
});

test('comments and blank lines are ignored', () => {
  const g = parseGeometry('# a triangle\n\nline 0 0 5 5 # side\n');
  assert.equal(g.error, '');
  assert.equal(g.elements.length, 1);
});

test('an unknown command is reported, not silently dropped', () => {
  const g = parseGeometry('line 0 0 5 5\nsquiggle 1 2');
  assert.match(g.error, /unknown command "squiggle"/);
  // Whatever parsed before the bad line is still available.
  assert.equal(g.elements.length, 1);
});

test('malformed numbers are reported with the line number', () => {
  const g = parseGeometry('line 0 0 5 oops');
  assert.match(g.error, /line 1/);
  assert.match(g.error, /4 numbers/);
});

test('a degenerate circle is rejected', () => {
  assert.match(parseGeometry('circle 1 1 0').error, /r > 0/);
});

test('an empty source is an empty, error-free diagram', () => {
  const g = parseGeometry('   \n  ');
  assert.equal(g.error, '');
  assert.deepEqual(g.elements, []);
});
