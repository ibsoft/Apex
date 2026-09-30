const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

/* Both modules are transpiled on the fly, the same way windows.test.cjs does it:
   commands.ts pulls in panelBridge and notepad, so a plain require would drag
   the whole browser-facing graph into a Node test. */
function load(file) {
  const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const exports = {};
  new Function('exports', 'require', compiled)(exports, () => ({}));
  return exports;
}

const { parseLocalCommand, parseTaskCommand } = load('../lib/commands.ts');
const tasks = load('../lib/tasks.ts');
const { relativeTime, nextRunLabel, lastRunLabel, describeTasks, describeOneTask,
  taskNotification, sortTasks, filterTasks, resolveTarget, oneOffLabel } = tasks;

const NOW = Date.UTC(2026, 2, 10, 7, 30, 0);
const task = (over = {}) => ({
  id: 1, title: "Disk check", prompt: "df -h /", plan: "", skill: "",
  schedule: "cron", cron: "0 9 * * 1-5", run_at: null,
  next_run: NOW + 3600_000, last_run: NOW - 86400_000, last_status: "ok",
  last_output: "root is 41% full", last_error: null,
  runs: 3, failures: 0, enabled: true, unread: false, created: NOW - 200000,
  repeating: true, schedule_label: "at 09:00 on weekdays", status: "pending",
  conversation_id: "c1", ...over,
});

/* ---- command parsing ---- */

test('the deterministic task commands are local, in English', () => {
  const cases = [
    ['list tasks', 'list'],
    ['show my tasks', 'list'],
    ['tasks', 'list'],
    ['what tasks are running', 'list'],
    ['open tasks', 'open'],
    ['show the tasks tab', 'open'],
    ['run task 2', 'run'],
    ['run the last task now', 'run'],
    ['start task 2', 'run'],
    ['pause task 2', 'pause'],
    ['stop task 3', 'pause'],
    ['resume task 2', 'resume'],
    ['unpause task 2', 'resume'],
    ['delete task 2', 'delete'],
    ['remove task 2', 'delete'],
    ['cancel task 2', 'delete'],
    ['status of task 2', 'show'],
    ['what is task 2', 'show'],
    ['tell me about task 2', 'show'],
    ['task 2 status', 'show'],
  ];
  for (const [text, action] of cases) {
    const got = parseTaskCommand(text, false);
    assert.ok(got, `expected a local command for: ${text}`);
    assert.equal(got.type, 'task', text);
    assert.equal(got.action, action, text);
  }
});

test('a spoken number word and a digit are the same index', () => {
  assert.equal(parseTaskCommand('run task 2', false).target, 2);
  assert.equal(parseTaskCommand('run the second task', false).target, 2);
  assert.equal(parseTaskCommand('delete the third task', false).target, 3);
  assert.equal(parseTaskCommand('run the last task', false).target, -1);
  assert.equal(parseTaskCommand('run the latest task', false).target, -1);
});

test('Greek task commands are recognised too', () => {
  const cases = [
    ['δειξε τις εργασιες μου', 'list'],
    ['ποιες εργασιες τρεχουν', 'list'],
    ['τρεξε την εργασια 2', 'run'],
    ['παυση της εργασιας 2', 'pause'],
    ['συνεχισε την εργασια 2', 'resume'],
    ['διαγραψε την εργασια 2', 'delete'],
    ['τι ειναι η εργασια 2', 'show'],
  ];
  for (const [text, action] of cases) {
    const got = parseTaskCommand(text, true);
    assert.ok(got, `expected a local command for: ${text}`);
    assert.equal(got.action, action, text);
  }
});

test('a final sigma does not stop a Greek command from matching', () => {
  // normalize() folds U+03C2 into U+03C3, so "εργασίες" reaches the matcher as
  // "εργασιεσ". Writing the accented form silently breaks this.
  assert.equal(parseTaskCommand('δείξε τις εργασίες μου', true).action, 'list');
  assert.equal(parseTaskCommand('διέγραψε την εργασία 2', true).action, 'delete');
});

test('the filter narrows the list instead of answering with everything', () => {
  assert.equal(parseTaskCommand('what tasks are running', false).filter, 'running');
  assert.equal(parseTaskCommand('show the paused tasks', false).filter, 'paused');
  assert.equal(parseTaskCommand('which tasks are broken', false).filter, 'error');
  assert.equal(parseTaskCommand('show enabled tasks', false).filter, 'enabled');
  assert.equal(parseTaskCommand('list tasks', false).filter, 'all');
});

test('creating a task is left to the agent, never handled locally', () => {
  const requests = [
    'create a task that checks the disk every morning at 8',
    'every morning check the disk and make it a task',
    'add a task to email me the weather at 9',
    'schedule a task for friday',
    'remind me every hour as a task',
    'what would you schedule for me',
    'can you make a task that backs up my files',
  ];
  for (const text of requests) {
    assert.equal(parseLocalCommand(text, 'en', []), null, text);
  }
});

test('a task request in Greek also reaches the agent', () => {
  assert.equal(parseLocalCommand('φτιάξε μια εργασία που ελέγχει τον δίσκο', 'el', []), null);
});

test('"run task 2" does not get read as a window or desktop request', () => {
  const got = parseLocalCommand('run task 2', 'en', []);
  assert.equal(got.type, 'task');
  assert.equal(got.target, 2);
});

test('"open tasks" opens the tab and "tasks" lists them', () => {
  assert.equal(parseLocalCommand('open tasks', 'en', []).action, 'open');
  assert.equal(parseLocalCommand('tasks', 'en', []).action, 'list');
});

test('task words do not shadow the existing shell commands', () => {
  // "close panel" and "lock" must keep winning: they are actions on the UI, and
  // a task word nowhere near them must not change that.
  assert.equal(parseLocalCommand('close panel', 'en', []).type, 'panel');
  assert.equal(parseLocalCommand('lock', 'en', []).type, 'lock');
  assert.equal(parseLocalCommand('cancel reminders', 'en', []).type, 'cancelReminders');
  assert.equal(parseLocalCommand('cancel timers', 'en', []).type, 'cancelTimers');
});

test('the tasks tab is one of the panel tabs', () => {
  assert.equal(parseLocalCommand('open the tasks tab', 'en', []).type, 'task');
  const opened = parseLocalCommand('show tasks', 'en', []);
  assert.equal(opened.action, 'list');
});

/* ---- model helpers ---- */

test('relativeTime reads correctly in both directions', () => {
  assert.equal(relativeTime(null, NOW, 'en'), 'never');
  assert.equal(relativeTime(null, NOW, 'el'), 'ποτέ');
  assert.equal(relativeTime(NOW + 90_000, NOW, 'en'), 'in 2 minutes');
  assert.equal(relativeTime(NOW - 90_000, NOW, 'en'), '2 minutes ago');
  assert.equal(relativeTime(NOW - 3 * 3600_000, NOW, 'en'), '3 hours ago');
  assert.equal(relativeTime(NOW - 2 * 86400_000, NOW, 'en'), '2 days ago');
  assert.equal(relativeTime(NOW + 3600_000, NOW, 'en'), 'in 1 hour');
  // Greek takes the SINGULAR for one: "1 ώρα", never "1 ώρες".
  assert.equal(relativeTime(NOW + 3600_000, NOW, 'el'), 'σε 1 ώρα');
  assert.equal(relativeTime(NOW + 7200_000, NOW, 'el'), 'σε 2 ώρες');
  assert.equal(relativeTime(NOW + 120_000, NOW, 'el'), 'σε 2 λεπτά');
});

test('a paused task is not described as having a next run', () => {
  assert.equal(nextRunLabel(task({ enabled: false, status: 'paused' }), NOW, 'en'), 'paused');
  assert.equal(lastRunLabel(task({ last_run: null }), NOW, 'en'), 'never ran');
  assert.match(lastRunLabel(task({ last_status: 'error' }), NOW, 'en'), /^failed /);
});

test('sorting puts what needs attention first', () => {
  const rows = [
    task({ id: 1, title: 'paused', status: 'paused', enabled: false }),
    task({ id: 2, title: 'fine', status: 'ok' }),
    task({ id: 3, title: 'broken', status: 'error' }),
    task({ id: 4, title: 'busy', status: 'running' }),
    task({ id: 5, title: 'waiting', status: 'pending' }),
  ];
  assert.deepEqual(sortTasks(rows).map((t) => t.title), ['broken', 'busy', 'waiting', 'fine', 'paused']);
});

test('the filters select the badge the operator asked about', () => {
  const rows = [
    task({ id: 1, status: 'error' }),
    task({ id: 2, status: 'running' }),
    task({ id: 3, status: 'paused', enabled: false }),
    task({ id: 4, status: 'pending' }),
  ];
  assert.equal(filterTasks(rows, 'running').length, 1);
  assert.equal(filterTasks(rows, 'paused').length, 1);
  assert.equal(filterTasks(rows, 'error').length, 1);
  assert.equal(filterTasks(rows, 'enabled').length, 3);
  assert.equal(filterTasks(rows, 'all').length, 4);
  assert.equal(filterTasks(rows, undefined).length, 4);
});

test('a spoken number resolves to the same row the tab shows', () => {
  // The number painted on a row is its index in the SORTED list, so this is
  // the only correct pairing: resolve against sortTasks, as the panel does.
  const rows = sortTasks([
    task({ id: 1, title: 'paused', status: 'paused', enabled: false }),
    task({ id: 2, title: 'broken', status: 'error' }),
  ]);
  assert.equal(resolveTarget(rows, 1).title, 'broken');
  assert.equal(resolveTarget(rows, 2).title, 'paused');
  assert.equal(resolveTarget(rows, -1).title, 'paused');
  assert.equal(resolveTarget(rows, 9), null);
  assert.equal(resolveTarget([], -1), null);
  assert.equal(resolveTarget(rows, undefined), null);
});

test('the spoken list names every row and its state', () => {
  const said = describeTasks([task({ status: 'running' }), task({ id: 2, title: 'Weather' })], 'en');
  assert.match(said, /You have 2 tasks/);
  assert.match(said, /1\. Disk check \(running now\)/);
  assert.match(said, /2\. Weather \(waiting\)/);
  assert.equal(describeTasks([], 'en'), 'You have no scheduled tasks.');
  assert.match(describeTasks([], 'el'), /Δεν έχεις εργασίες/);
  assert.match(describeTasks([task()], 'en'), /You have 1 task:/);
});

test('a spoken single task includes the schedule and the last result', () => {
  const said = describeOneTask(task(), 'en');
  assert.match(said, /Task "Disk check"/);
  assert.match(said, /at 09:00 on weekdays/);
  assert.match(said, /has run 3 times/);
  assert.match(said, /root is 41% full/);
});

test('the completion notice reads differently for a failure', () => {
  const ok = taskNotification(task({ last_run: NOW - 60_000 }), 'en');
  assert.match(ok, /finished/);
  const bad = taskNotification(
    task({ status: 'error', last_status: 'error', last_error: 'no route to host', last_output: null, last_run: NOW - 60_000 }), 'en');
  assert.match(bad, /failed/);
  assert.match(bad, /no route to host/);
  assert.doesNotMatch(bad, /41% full/);
});

test('a very long result is truncated in the notice', () => {
  const said = taskNotification(task({ last_output: 'x'.repeat(2000) }), 'en');
  assert.ok(said.length < 600, `notice was ${said.length} chars`);
  assert.match(said, /…$/);
});

test('a one-off is edited as the local wall clock, not a UTC instant', () => {
  // toISOString() would give UTC with a Z. The backend reads a schedule with no
  // offset as machine-local, so the Z form moved a one-off by the machine's UTC
  // offset and it fired late - and the field was unreadable besides.
  const stamp = Date.UTC(2026, 2, 12, 8, 30) / 1000;
  const text = oneOffLabel(stamp);
  assert.match(text, /^once at \d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
  assert.ok(!/Z|[+]\d{2}:\d{2}$/.test(text), `carries an offset: ${text}`);

  // Round-trips: reading it back as local gives the minute that was stored.
  const parsed = new Date(text.replace('once at ', '').replace(' ', 'T'));
  const back = new Date(parsed.getTime());
  assert.equal(back.getMinutes(), 30);
  assert.equal(oneOffLabel(null), '');
});
