const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

/* lib/subagents.ts has no browser dependencies, but it is TypeScript; transpile
   it on the fly the same way tasks.test.cjs does so a plain require works. */
function load(file) {
  const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const exports = {};
  new Function('exports', 'require', compiled)(exports, () => ({}));
  return exports;
}

const { statusTone, lastRunLabel, toolScopeLabel } = load('../lib/subagents.ts');

const agent = (over = {}) => ({
  id: 'a1', user_id: 'u1', name: 'Log watcher', description: '',
  role: '', provider: '', model: '', tools: [], workspace: '/tmp/x',
  enabled: true, conversation_id: '', created_at: 0, updated_at: 0,
  last_run: null, unread: 0, ...over,
});

const run = (over = {}) => ({
  id: 'r1', agent_id: 'a1', user_id: 'u1', prompt: 'p', status: 'ok',
  output: '', error: '', conversation_id: '', started_at: 0,
  finished_at: 0, unread: false, ok: true, ...over,
});

/* ---- statusTone ---- */

test('statusTone maps every run state to a palette key', () => {
  assert.equal(statusTone('running'), 'gold');
  assert.equal(statusTone('error'), 'red');
  assert.equal(statusTone('ok'), 'cyan');
  assert.equal(statusTone(null), 'dim');
  assert.equal(statusTone(undefined), 'dim');
});

/* ---- lastRunLabel ---- */

test('an agent that never ran says so', () => {
  assert.equal(lastRunLabel(agent()), 'never ran');
});

test('lastRunLabel reads the last run state', () => {
  assert.equal(lastRunLabel(agent({ last_run: run({ status: 'running' }) })), 'running now');
  assert.equal(lastRunLabel(agent({ last_run: run({ status: 'error' }) })), 'last run failed');
  assert.equal(lastRunLabel(agent({ last_run: run({ status: 'ok' }) })), 'last run succeeded');
});

/* ---- toolScopeLabel ---- */

test('an empty tool list means every tool, not zero', () => {
  assert.equal(toolScopeLabel([]), 'all tools');
  assert.equal(toolScopeLabel(undefined), 'all tools');
});

test('a scoped tool list is counted, singular and plural', () => {
  assert.equal(toolScopeLabel(['a']), '1 tool');
  assert.equal(toolScopeLabel(['a', 'b', 'c']), '3 tools');
});
