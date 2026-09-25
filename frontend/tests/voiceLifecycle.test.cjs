const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../lib/voiceLifecycle.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const exported = {};
new Function('exports', compiled)(exported);
const { settleVoiceCommand } = exported;

test('silent terminal opening waits for completion and releases processing', async () => {
  let done, phase = 'thinking', resumes = 0;
  const pending = new Promise(resolve => { done = resolve; });
  const work = settleVoiceCommand(() => pending, () => phase === 'thinking', () => { phase = 'standby'; resumes++; }, () => assert.fail('unexpected error'));
  assert.equal(phase, 'thinking');
  done(); await work;
  assert.equal(phase, 'standby');
  assert.equal(resumes, 1);
  phase = 'thinking';
  await settleVoiceCommand(() => {}, () => phase === 'thinking', () => { phase = 'standby'; }, () => {});
  assert.equal(phase, 'standby', 'a subsequent command also completes');
});

test('spoken acknowledgements retain speech ownership of completion', async () => {
  let phase = 'thinking';
  await settleVoiceCommand(() => { phase = 'speaking'; }, () => phase === 'thinking', () => assert.fail('must not interrupt speech'), () => {});
  assert.equal(phase, 'speaking');
});

test('failed commands report the error and resume listening', async () => {
  for (const run of [() => { throw Error('failed'); }, () => Promise.reject(Error('failed'))]) {
    let resumed = false, error;
    await settleVoiceCommand(run, () => true, () => { resumed = true; }, e => { error = e; });
    assert.equal(error.message, 'failed'); assert.equal(resumed, true);
  }
});

test('an older command cannot finish a newer voice turn', async () => {
  let done, epoch = 1;
  const work = settleVoiceCommand(() => new Promise(resolve => { done = resolve; }), () => epoch === 1, () => assert.fail('stale completion'), () => assert.fail('stale error'));
  epoch = 2; done(); await work;
});
