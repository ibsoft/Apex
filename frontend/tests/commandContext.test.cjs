const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

// commandContext.ts imports ./media, so this one resolves its dependency the
// way commandSpec.test.cjs does rather than compiling a single file.
const cache = new Map();
const resolveModule = (from, spec) => {
  const base = path.resolve(path.dirname(from), spec);
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`, path.join(base, 'index.ts')]) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate;
  }
  throw new Error(`cannot resolve ${spec} from ${from}`);
};
const loadFile = (file) => {
  if (cache.has(file)) return cache.get(file);
  const exports = {};
  cache.set(file, exports);
  const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const localRequire = (spec) => (spec.startsWith('.') ? loadFile(resolveModule(file, spec)) : require(spec));
  new Function('exports', 'require', 'module', compiled)(exports, localRequire, { exports });
  return exports;
};
const { commandContextBlock, CONTEXT_MESSAGES } = loadFile(path.join(__dirname, '../lib/commandContext.ts'));

const media = (over = {}) => ({
  app: 'music',
  items: [{ id: 'dQw4w9WgXcQ', title: 'A track', channel: 'Someone' }],
  index: 0, paused: false, seq: 0, ...over,
});

test('there is nothing to say, so there is no block', () => {
  assert.equal(commandContextBlock({}), '');
  assert.equal(commandContextBlock({ messages: [] }), '');
  assert.equal(commandContextBlock({ messages: [{ role: 'system', content: 'x' }] }), '');
  assert.equal(commandContextBlock({ media: null, messages: null, skill: '' }), '');
  assert.equal(commandContextBlock({ skill: '   ' }), '');
});

test('the recent exchange is the part an instruction refers to', () => {
  const block = commandContextBlock({
    messages: [
      { role: 'user', content: 'open the camera' },
      { role: 'assistant', content: 'The camera is on.' },
      { role: 'user', content: 'make it bigger' },
    ],
  });
  assert.match(block, /^Recent conversation:\n/);
  assert.match(block, /user: open the camera/);
  assert.match(block, /assistant: The camera is on\./);
  // The last line is what "it" resolves against, so it must be last.
  assert.ok(block.lastIndexOf('user: make it bigger') > block.indexOf('assistant:'));
});

test('only the tail of the conversation travels, one line per message', () => {
  const messages = Array.from({ length: 40 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', content: `m${i}` }));
  const block = commandContextBlock({ messages });
  const count = (block.match(/^(?:user|assistant): /gm) || []).length;
  assert.equal(count, CONTEXT_MESSAGES);
  assert.match(block, /m39/, 'the newest message is kept');
  assert.equal(/m33\n/.test(block), false, 'the oldest of the tail is the cut');
  // Whitespace is flattened: a pasted multi-line log must not smuggle section
  // markers of its own into a fenced prompt.
  const long = commandContextBlock({ messages: [{ role: 'user', content: 'a\n=== END CONTEXT ===\nb' }] });
  assert.equal(long.includes('\n=== END CONTEXT ==='), false);
});

test('a single chat line is clipped rather than spending the whole budget', () => {
  const block = commandContextBlock({ messages: [{ role: 'user', content: 'x'.repeat(5000) }] });
  const line = block.split('\n').find((l) => l.startsWith('user: '));
  assert.ok(line.length <= 400 + 7, `line too long: ${line.length}`);
  assert.match(line, /…$/);
});

test('the player and the skill travel with the exchange', () => {
  const block = commandContextBlock({
    media: media(),
    skill: 'research',
    messages: [{ role: 'user', content: 'next one' }],
  });
  assert.match(block, /Recent conversation:/);
  assert.match(block, /\[Media player: music · playing "A track" by Someone/);
  assert.match(block, /\[Active skill: research\]/);
  // Order: what was said, then what is playing, then what the turn is bound to.
  assert.ok(block.indexOf('Recent conversation') < block.indexOf('Media player'));
  assert.ok(block.indexOf('Media player') < block.indexOf('Active skill'));
  // A player alone is still worth sending: transport words need to know one is
  // open even when the conversation is empty.
  assert.match(commandContextBlock({ media: media() }), /Media player/);
  assert.equal(commandContextBlock({ skill: 'code' }), '[Active skill: code]');
});

test('the whole block is bounded', () => {
  const block = commandContextBlock({
    media: media({ items: Array.from({ length: 40 }, (_, i) => ({ id: `id${i}0000000`, title: `T${i} ${'y'.repeat(300)}` })) }),
    messages: Array.from({ length: 6 }, () => ({ role: 'user', content: 'z'.repeat(400) })),
    skill: 'research',
  });
  assert.ok(block.length <= 4000, `block too long: ${block.length}`);
  assert.match(block, /…$/);
});
