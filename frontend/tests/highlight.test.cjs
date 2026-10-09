/* Syntax highlighting tokens: classification, and the round-trip guarantee that
   no character is ever lost while colouring. */
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

const { tokenize } = loadTs('lib/highlight.ts');

const SAMPLES = {
  python: 'def area(r):\n    # circle\n    return 3.14 * r ** 2  # pi\n\nclass Point:\n    pass\n',
  javascript: 'const x = `hi ${name}`; // greet\nfunction f(a) { return a + 1; }\n/* block */\n',
  typescript: 'interface User { id: number; name: string; }\nconst u: User = { id: 1, name: "a" };\n',
  bash: '#!/bin/bash\nfor f in *.txt; do\n  echo "$f"  # print\n  rm -f "$f"\ndone\n',
  json: '{"a": 1, "b": [true, null], "c": "x"}',
  css: '.a { color: #fff; /* note */ }',
  html: '<div class="box" id="x"><!-- c -->text</div>',
  rust: 'pub fn main() {\n    let x = 5; // ok\n    println!("{}", x);\n}\n',
  toml: 'key = "value" # note\n',
};

test('token values concatenate back to the exact source', () => {
  for (const [lang, source] of Object.entries(SAMPLES)) {
    const rebuilt = tokenize(source, lang).map((t) => t.value).join('');
    assert.equal(rebuilt, source, `round-trip failed for ${lang}`);
  }
});

test('no token is ever empty', () => {
  for (const [lang, source] of Object.entries(SAMPLES)) {
    for (const t of tokenize(source, lang)) assert.ok(t.value.length > 0, `empty token in ${lang}`);
  }
});

test('python keywords, strings, comments and numbers are labelled', () => {
  const toks = tokenize('def f():\n    return "x" + 3  # yes\n', 'python');
  const by = (type, value) => toks.some((t) => t.type === type && t.value === value);
  assert.ok(by('keyword', 'def'));
  assert.ok(by('keyword', 'return'));
  assert.ok(by('function', 'f'));
  assert.ok(by('string', '"x"'));
  assert.ok(by('number', '3'));
  assert.ok(toks.some((t) => t.type === 'comment' && t.value.includes('# yes')));
});

test('a triple-quoted python string spans lines as one string', () => {
  const toks = tokenize('x = """a\nb"""\n', 'python');
  assert.ok(toks.some((t) => t.type === 'string' && t.value === '"""a\nb"""'));
});

test('a javascript template literal spans lines and keeps its newlines', () => {
  const toks = tokenize('const s = `a\nb`;\n', 'javascript');
  assert.ok(toks.some((t) => t.type === 'string' && t.value === '`a\nb`'));
});

test('a block comment is one token even across lines', () => {
  const toks = tokenize('a /* one\ntwo */ b', 'javascript');
  assert.ok(toks.some((t) => t.type === 'comment' && t.value === '/* one\ntwo */'));
});

test('method calls are properties and free calls are functions', () => {
  const toks = tokenize('console.log(foo())', 'javascript');
  assert.ok(toks.some((t) => t.type === 'property' && t.value === 'log'));
  assert.ok(toks.some((t) => t.type === 'function' && t.value === 'foo'));
});

test('an unclosed fence body still round-trips', () => {
  const source = 'def f(:\n    return "unterminated';
  assert.equal(tokenize(source, 'python').map((t) => t.value).join(''), source);
});

test('html tags, attributes and comments are labelled', () => {
  const toks = tokenize('<a href="/x" data-id=1>t</a>', 'html');
  assert.ok(toks.some((t) => t.type === 'tag' && t.value === 'a'));
  assert.ok(toks.some((t) => t.type === 'attr' && t.value === 'href'));
  assert.ok(toks.some((t) => t.type === 'string' && t.value === '"/x"'));
});

test('an unknown language still produces a complete token stream', () => {
  const source = '??? what = "ever" ~~~';
  assert.equal(tokenize(source, 'brainfuck').map((t) => t.value).join(''), source);
});
