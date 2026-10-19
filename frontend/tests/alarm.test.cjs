const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const source = fs.readFileSync(path.join(__dirname, '../lib/alarm.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const moduleExports = {};
new Function('exports', compiled)(moduleExports);
const { FANCY_ALARM_NOTES, alarmLoopSeconds } = moduleExports;

test('the alarm is a multi-note melody, not a single beep', () => {
  assert.ok(FANCY_ALARM_NOTES.length >= 4);
});

test('every note is well-formed', () => {
  for (const note of FANCY_ALARM_NOTES) {
    assert.ok(note.freq > 0, 'freq');
    assert.ok(note.at >= 0, 'at');
    assert.ok(note.dur > 0, 'dur');
    assert.ok(note.gain > 0 && note.gain <= 0.5, 'gain');
    assert.ok(['sine', 'triangle', 'square', 'sawtooth'].includes(note.type), 'type');
  }
});

test('the loop is long enough to cover every note plus a gap before repeating', () => {
  const end = FANCY_ALARM_NOTES.reduce((m, n) => Math.max(m, n.at + n.dur), 0);
  assert.ok(alarmLoopSeconds() > end, 'leaves a breath before the repeat');
  assert.ok(alarmLoopSeconds() < end + 2, 'does not stall silently for long');
});

test('alarmLoopSeconds is computed from whatever notes it is given', () => {
  assert.equal(alarmLoopSeconds([{ freq: 440, at: 1, dur: 0.5, gain: 0.1, type: 'sine' }]), 2.1);
});
