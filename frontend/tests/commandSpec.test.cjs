const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

/* Load a .ts module and the modules it pulls in, transpiled on the fly.

   Unlike most of the test files, this one cannot stub `require`: commandSpec.ts
   imports real functions (the parsers, the terminal numbering), because the
   validator's job is to check the model's answer *against the same code the
   deterministic path uses*. A stubbed import would make it test the absence of
   the thing it depends on. */
const cache = new Map();
const FRONTEND = path.join(__dirname, '..');

function resolveModule(fromFile, specifier) {
  const base = path.resolve(path.dirname(fromFile), specifier);
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`, path.join(base, 'index.ts')]) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate;
  }
  throw new Error(`cannot resolve ${specifier} from ${fromFile}`);
}

function loadFile(file) {
  if (cache.has(file)) return cache.get(file);
  const exports = {};
  cache.set(file, exports);
  const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const localRequire = (specifier) => {
    if (!specifier.startsWith('.')) return require(specifier);
    return loadFile(resolveModule(file, specifier));
  };
  new Function('exports', 'require', 'module', compiled)(exports, localRequire, { exports });
  return exports;
}

const loadTs = (rel) => loadFile(path.join(FRONTEND, rel));

const { parseLocalCommand } = loadTs('lib/commands.ts');
const {
  LOCAL_ACTIONS,
  MAX_ACTION_CHAIN,
  buildUiState,
  renderActionCatalogue,
  renderUiState,
  sanitizeActions,
} = loadTs('lib/commandSpec.ts');

const parse = (text, language = 'en') => parseLocalCommand(text, language, [], Date.now());
const skills = [];

/* ---------- deterministic bulk phrases ---------- */

test('a quantifier makes any window-ish verb a bulk request', () => {
  for (const verb of ['restore', 'minimize', 'maximize', 'close']) {
    assert.equal(parse(`${verb} all terminals`).action, `${verb}_all`, verb);
    assert.equal(parse(`${verb} every terminal`).action, `${verb}_all`, verb);
    assert.equal(parse(`${verb} all the terminals`).action, `${verb}_all`, verb);
    assert.equal(parse(`${verb} both terminals`).action, `${verb}_all`, verb);
  }
});

test('a bare plural is bulk too, with no quantifier at all', () => {
  // Greek "τα τερματικά" has no "all" in it, and means every one of them.
  assert.equal(parse('επαναφέρε τα τερματικά').action, 'restore_all');
  assert.equal(parse('κλείσε τις κονσόλες').action, 'close_all');
  assert.equal(parse('close all files').action, 'close_all');
});

test('a word ending in final sigma still matches its quantifier', () => {
  // normalize() folds ς to σ, so "όλους"/"τους" reach the matcher as "ολουσ"/
  // "τουσ". A vocabulary written the way it is said and folded at build time is
  // the only spelling that can match; written out by hand it silently cannot.
  assert.equal(parse('επαναφέρε όλους τους τερματικούς').action, 'restore_all');
  assert.equal(parse('κλείσε όλα τα αρχεία').action, 'close_all');
});

test('the paraphrases that used to be nothing now parse', () => {
  for (const phrase of ['unminimize all terminals', 'normalize all terminals', 'show all terminals']) {
    assert.deepEqual(parse(phrase), { type: 'terminal', action: 'restore_all' }, phrase);
  }
  assert.deepEqual(parse('minimize all terminal windows'), { type: 'terminal', action: 'minimize_all' });
});

test('bulk works for every window-ish kind, not just windows', () => {
  assert.equal(parse('restore all windows').action, 'restore_all');
  assert.equal(parse('maximize all windows').action, 'maximize_all');
  assert.equal(parse('close all file managers').action, 'close_all');
  assert.equal(parse('minimize every file browser').action, 'minimize_all');
  assert.equal(parse('κλείσε όλα τα παράθυρα αρχείων').action, 'close_all');
  assert.equal(parse('close all notepads').action, 'close_all');
  assert.equal(parse('κλείσε όλα τα σημειωματάρια').action, 'close_all');
});

test('the bare bulk form closes every window', () => {
  // "close all" needed the word "windows"; restore_all already accepted it bare.
  assert.deepEqual(parse('close all'), { type: 'window', action: 'close_all' });
});

test('a singular noun with no quantifier is NOT bulk', () => {
  // The regression this whole branch risks: "restore terminal" has always meant
  // the one focused window, and must not become "every terminal".
  assert.deepEqual(parse('restore terminal'), { type: 'terminal', action: 'restore' });
  assert.deepEqual(parse('restore the terminal'), { type: 'terminal', action: 'restore' });
  assert.deepEqual(parse('close the notepad'), { type: 'notepad', action: 'close' });
});

test('a numbered target still wins over bulk', () => {
  assert.deepEqual(parse('restore terminal 1'), { type: 'terminal', action: 'restore', target: 1 });
  assert.deepEqual(parse('close terminal 2'), { type: 'terminal', action: 'close', target: 2 });
});

test('opening a count of terminals is unaffected', () => {
  assert.deepEqual(parse('open 4 terminals'), { type: 'terminal', action: 'open', create: true, count: 4 });
  assert.deepEqual(parse('open a terminal'), { type: 'terminal', action: 'open' });
});

test('the notepad editing actions are untouched by the bulk branch', () => {
  assert.deepEqual(parse('save the notepad'), { type: 'notepad', action: 'save' });
  assert.deepEqual(parse('clear the notepad'), { type: 'notepad', action: 'clear' });
  assert.deepEqual(parse('write hello in the notepad'), { type: 'notepad', action: 'write', content: 'hello' });
});

test('a question still reaches the agent', () => {
  for (const phrase of [
    'how do I restore all terminals',
    'what does restore all terminals do',
    'write a script that restores terminals',
    'can you restore all terminals',
  ]) {
    assert.equal(parse(phrase), null, phrase);
  }
});

/* ---------- the state snapshot ---------- */

const win = (over) => ({
  id: over.id,
  items: [{ url: over.url ?? 'https://x/y.png', title: over.title ?? 'y.png' }],
  index: 0,
  kind: over.kind ?? 'image',
  rect: { x: 0, y: 0, w: 100, h: 100 },
  maximized: !!over.maximized,
  minimized: !!over.minimized,
  desktop: over.desktop ?? 0,
  note: '',
  showNotes: false,
});

test('the snapshot numbers terminals separately from windows', () => {
  const windows = [
    win({ id: 'a', kind: 'image', title: 'one.png' }),
    win({ id: 'b', kind: 'terminal', url: 'terminal:s1' }),
    win({ id: 'c', kind: 'terminal', url: 'terminal:s2' }),
  ];
  const state = buildUiState({ windows, focusedWindowId: 'b', activeDesktop: 0 });
  assert.equal(state.windows.length, 3);
  // The second window on screen is terminal #1: this is the numbering the
  // title bar shows and the operator says out loud.
  assert.equal(state.windows[1].terminal, 1);
  assert.equal(state.windows[2].terminal, 2);
  assert.equal(state.windows[0].terminal, null);
  assert.equal(state.focusedTerminal, 1);
});

test('the snapshot carries the state flags the agent cannot otherwise see', () => {
  const state = buildUiState({
    windows: [
      win({ id: 'a', kind: 'terminal', url: 'terminal:s1', minimized: true }),
      win({ id: 'b', kind: 'terminal', url: 'terminal:s2', maximized: true, desktop: 2 }),
    ],
    focusedWindowId: null,
    activeDesktop: 1,
  });
  const text = renderUiState(state);
  assert.match(text, /terminal #1 .*minimized/);
  assert.match(text, /terminal #2 .*maximized/);
  assert.match(text, /on desktop 3/);
  assert.match(text, /currently on 2/);
});

test('a text preview is not reported as an editor', () => {
  // isNotepadWindow also accepts any text preview; telling the model a .txt is
  // an editor invites it to pick notepad for a window with no editor behind it.
  const state = buildUiState({
    windows: [win({ id: 'a', kind: 'text', url: 'https://x/notes.txt', title: 'notes.txt' })],
    focusedWindowId: null,
    activeDesktop: 0,
  });
  assert.equal(state.windows[0].kind, 'text');
});

test('the catalogue names every action and both numbering schemes', () => {
  const text = renderActionCatalogue();
  assert.match(text, /terminal\.restore_all/);
  assert.match(text, /notepad\.close_all/);
  // The window number and the terminal number are both "1"; the model has to be
  // told they are different or it will echo the wrong one.
  assert.match(text, /counted among terminal windows only/);
  assert.match(text, /position in the full open-window list/);
  // Content-extracting actions stay with the parsers that slice the operator's
  // own words out of the sentence.
  assert.doesNotMatch(text, /reminder\.set|timer\.set/);
  for (const spec of LOCAL_ACTIONS) {
    assert.ok(spec.about && spec.about.length > 8, `${spec.type}.${spec.action} needs a description`);
  }
});

/* ---------- the trust boundary ---------- */

const stateWith = (windows, focused = null, activeDesktop = 0) =>
  buildUiState({ windows, focusedWindowId: focused, activeDesktop });

const clean = (utterance, language = 'en', windows = [], focused = null) => {
  const state = stateWith(windows, focused);
  return sanitizeActions(
    [{ type: 'terminal', action: 'restore_all' }],
    { state, utterance, language, skills },
  );
};

test('a proposed action in the catalogue is accepted', () => {
  const got = clean('restore all terminals');
  assert.deepEqual(got, [{ type: 'terminal', action: 'restore_all' }]);
});

test('an action the browser does not support is dropped, not executed', () => {
  const state = stateWith([]);
  for (const bogus of [
    { type: 'terminal', action: 'reboot' },
    { type: 'filesystem', action: 'delete_all' },
    { type: 'skill', action: 'shell' },
    { action: 'restore_all' },
    { type: 123, action: 'restore_all' },
  ]) {
    assert.deepEqual(
      sanitizeActions([bogus], { state, utterance: 'do a thing', language: 'en', skills }),
      [],
      JSON.stringify(bogus),
    );
  }
});

test('a number the screen does not show is discarded, not aimed at something else', () => {
  const windows = [win({ id: 'a', kind: 'terminal', url: 'terminal:s1' })];
  const state = stateWith(windows);
  // There is one terminal. "terminal 4" addresses nothing, so it is dropped.
  // It must not be stripped, because "close terminal 4" would then close the
  // focused window - a different window from the one that was named.
  assert.deepEqual(
    sanitizeActions([{ type: 'terminal', action: 'close', target: 4 }], { state, utterance: 'x', language: 'en', skills }),
    [],
  );
  assert.deepEqual(
    sanitizeActions([{ type: 'terminal', action: 'restore', target: 4 }], { state, utterance: 'x', language: 'en', skills }),
    [],
  );
});

test('an action nobody numbered still means the focused window', () => {
  // The untargeted form is how the deterministic parsers already say it, so it
  // is the one case where the focused window is the intended answer.
  const windows = [win({ id: 'a', kind: 'terminal', url: 'terminal:s1' })];
  assert.deepEqual(
    sanitizeActions([{ type: 'terminal', action: 'restore' }], { state: stateWith(windows), utterance: 'x', language: 'en', skills }),
    [{ type: 'terminal', action: 'restore' }],
  );
});

test('a target that does exist is kept', () => {
  const windows = [win({ id: 'a', kind: 'terminal', url: 'terminal:s1' })];
  const got = sanitizeActions(
    [{ type: 'terminal', action: 'restore', target: 1 }],
    { state: stateWith(windows), utterance: 'x', language: 'en', skills },
  );
  assert.deepEqual(got, [{ type: 'terminal', action: 'restore', target: 1 }]);
});

test('a chain runs in order and is capped', () => {
  const state = stateWith([]);
  const chain = [
    { type: 'terminal', action: 'minimize_all' },
    { type: 'window', action: 'close', target: 99 },
    { type: 'files', action: 'restore_all' },
  ];
  const got = sanitizeActions(chain, { state, utterance: 'x', language: 'en', skills });
  // The invalid middle step is dropped; the good ones around it survive, and
  // they survive in the order they were asked for.
  assert.deepEqual(got, [
    { type: 'terminal', action: 'minimize_all' },
    { type: 'files', action: 'restore_all' },
  ]);
  // An over-long chain is truncated rather than run in full. Distinct actions,
  // because an identical repeat is deduplicated and would make the count a
  // measurement of the wrong thing.
  const many = [
    { type: 'terminal', action: 'close_all' },
    { type: 'terminal', action: 'minimize_all' },
    { type: 'terminal', action: 'maximize_all' },
    { type: 'terminal', action: 'restore_all' },
    { type: 'window', action: 'close_all' },
    { type: 'window', action: 'minimize_all' },
    { type: 'window', action: 'maximize_all' },
    { type: 'window', action: 'restore_all' },
    { type: 'files', action: 'close_all' },
    { type: 'files', action: 'minimize_all' },
    { type: 'files', action: 'maximize_all' },
    { type: 'files', action: 'restore_all' },
  ];
  assert.equal(sanitizeActions(many, { state, utterance: 'x', language: 'en', skills }).length, MAX_ACTION_CHAIN);
});

/* ---------- the shape a real model actually answers with ----------
   Recorded from the live router (gpt-4o-mini, `backend/tools/command_router.py`
   asked over the real catalogue). The catalogue prints its entries dotted, and
   the model copied the printed key straight into "type", never setting "action".
   Looked up as-is that builds the key "terminal.open." - in no map - so every
   entry of the chain was dropped, the turn reached the agent, and "show me two
   terminals and a notepad" opened two terminals and no notepad. */

test('a dotted "type" is read as the catalogue key it is', () => {
  const state = stateWith([]);
  const utterance = 'show me two terminals and a notepad';
  const expected = [
    { type: 'terminal', action: 'open', count: 2 },
    { type: 'notepad', action: 'open' },
  ];
  assert.deepEqual(
    sanitizeActions([{ type: 'terminal.open', count: 2 }, { type: 'notepad.open' }], {
      state, utterance, language: 'en', skills,
    }),
    expected,
    'dotted type',
  );
  // The same answer with the key used as an object key, which the model also
  // produced. Both must land on the same commands as the documented shape.
  assert.deepEqual(
    sanitizeActions([{ 'terminal.open': { count: 2 } }, { 'notepad.open': {} }], {
      state, utterance, language: 'en', skills,
    }),
    expected,
    'dotted object key',
  );
});

test('an explicit action is not overwritten by the one in the dotted type', () => {
  const state = stateWith([]);
  assert.deepEqual(
    sanitizeActions([{ type: 'terminal.open', action: 'focus', target: 1 }], {
      state: stateWith([
        win({ id: 'a', kind: 'terminal', url: 'terminal:s1' }),
        win({ id: 'b', kind: 'terminal', url: 'terminal:s2' }),
      ]),
      utterance: 'x', language: 'en', skills,
    }),
    [{ type: 'terminal', action: 'focus', target: 1 }],
  );
});

test('reshaping the answer cannot invent an action', () => {
  const state = stateWith([]);
  const input = { state, utterance: 'x', language: 'en', skills };
  // The dotted rewrite is only safe because the result still has to pass the
  // catalogue lookup. These are the three ways it could have failed to.
  for (const raw of [
    [{ type: 'terminal.explode' }],
    [{ 'terminal.explode': {} }],
    [{ 'terminal': { action: 'explode' } }],
    [{ type: 'terminal', action: 'explode' }],
  ]) {
    assert.deepEqual(sanitizeActions(raw, input), [], JSON.stringify(raw));
  }
});

test('a repeated action is not run twice', () => {
  const state = stateWith([]);
  const got = sanitizeActions(
    [
      { type: 'terminal', action: 'minimize_all' },
      { type: 'terminal', action: 'minimize_all' },
    ],
    { state, utterance: 'x', language: 'en', skills },
  );
  assert.equal(got.length, 1);
});

test('signing out needs the parser to agree, not just the model', () => {
  const state = stateWith([]);
  const ctx = { state, skills };
  // The whole point of anchoring "sign out" to the whole utterance: someone
  // talking about signing out must not sign out. The model's opinion is not
  // enough, so it is checked against the parser that owns that rule.
  assert.deepEqual(
    sanitizeActions([{ type: 'signout' }], { ...ctx, utterance: 'how do I sign out', language: 'en' }),
    [],
  );
  assert.deepEqual(
    sanitizeActions([{ type: 'signout' }], { ...ctx, utterance: 'sign out', language: 'en' }),
    [{ type: 'signout' }],
  );
  assert.deepEqual(
    sanitizeActions([{ type: 'lock' }], { ...ctx, utterance: 'how does locking work', language: 'en' }),
    [],
  );
});

test('a free-text payload is the operator\'s words, capped, and only where allowed', () => {
  const windows = [win({ id: 'a', kind: 'image', title: 'one.png' })];
  const state = stateWith(windows);
  const got = sanitizeActions(
    [{ type: 'window', action: 'note', target: 1, note: 'check the totals' }],
    { state, utterance: 'x', language: 'en', skills },
  );
  assert.equal(got[0].note, 'check the totals');
  // A field the action does not declare is dropped rather than passed through.
  const noField = sanitizeActions(
    [{ type: 'terminal', action: 'restore_all', note: 'sneaky' }],
    { state, utterance: 'x', language: 'en', skills },
  );
  assert.equal(noField[0].note, undefined);
});

test('enumerated fields accept only the listed value', () => {
  const state = stateWith([]);
  const ctx = { state, utterance: 'x', language: 'en', skills };
  assert.equal(sanitizeActions([{ type: 'panel', action: 'open', tab: 'tasks' }], ctx)[0].tab, 'tasks');
  assert.equal(sanitizeActions([{ type: 'panel', action: 'open', tab: 'nonsense' }], ctx)[0].tab, undefined);
  assert.equal(sanitizeActions([{ type: 'window', action: 'arrange', arrangement: 'grid' }], ctx)[0].arrangement, 'grid');
  assert.equal(sanitizeActions([{ type: 'window', action: 'arrange', arrangement: 'spiral' }], ctx)[0].arrangement, undefined);
});

test('a desktop number is clamped to the desktops that exist', () => {
  const state = stateWith([]);
  const ctx = { state, utterance: 'x', language: 'en', skills };
  assert.equal(sanitizeActions([{ type: 'desktop', action: 'switch', desktop: 2 }], ctx)[0].desktop, 1);
  assert.equal(sanitizeActions([{ type: 'desktop', action: 'switch', desktop: 9 }], ctx)[0].desktop, undefined);
});

/* ---------- running a chain ---------- */

/* Mirrors ApexProvider::runResolvedCommands. It is a closure over live window
   state, so it cannot be imported; the contract is what matters here - order,
   and the difference between "not applicable" (null) and "not a local command"
   (an empty reply from every step). */
async function runChain(commands, execute) {
  if (!commands.length) return null;
  const replies = [];
  for (const command of commands) {
    const reply = await execute(command);
    if (reply) replies.push(reply);
  }
  if (!replies.length) return null;
  return replies.map((r) => r.trim().replace(/[.\s]+$/, '')).filter(Boolean).map((p) => `${p}.`).join(' ');
}

test('a chain runs in the order it was asked for', async () => {
  const ran = [];
  const reply = await runChain(
    sanitizeActions([
      { type: 'terminal', action: 'minimize_all' },
      { type: 'window', action: 'arrange', arrangement: 'grid' },
      { type: 'files', action: 'restore_all' },
    ], { state: stateWith([]), utterance: 'x', language: 'en', skills }),
    async (command) => {
      ran.push(`${command.type}.${command.action}`);
      return `Did ${command.action}.`;
    },
  );
  assert.deepEqual(ran, ['terminal.minimize_all', 'window.arrange', 'files.restore_all']);
  // One full stop per step: a voice turn is spoken as one breath, and four
  // run-on sentences read as a stumble.
  assert.equal(reply, 'Did minimize_all. Did arrange. Did restore_all.');
});

test('a step that does not apply does not stop the rest', async () => {
  // null is "the window it names is not open" - not a failure of the chain.
  const ran = [];
  const reply = await runChain(
    [{ type: 'terminal', action: 'minimize_all' }, { type: 'files', action: 'close_all' }],
    async (command) => {
      ran.push(command.action);
      return command.action === 'minimize_all' ? null : 'Closed them.';
    },
  );
  assert.deepEqual(ran, ['minimize_all', 'close_all']);
  assert.equal(reply, 'Closed them.');
});

test('a chain where nothing said anything is not a local command', async () => {
  // The signal to send the turn to the agent, so it must not be a string.
  assert.equal(await runChain([{ type: 'terminal', action: 'minimize_all' }], async () => ''), null);
  assert.equal(await runChain([], async () => 'unused'), null);
});

test('all four window-ish kinds can be minimized in one utterance', async () => {
  const state = stateWith([]);
  const bulk = sanitizeActions([
    { type: 'window', action: 'minimize_all' },
    { type: 'terminal', action: 'minimize_all' },
    { type: 'files', action: 'minimize_all' },
    { type: 'notepad', action: 'minimize_all' },
  ], { state, utterance: 'minimize everything', language: 'en', skills });
  assert.equal(bulk.length, 4);
  const ran = [];
  await runChain(bulk, async (command) => {
    ran.push(command.type);
    return `Minimized ${command.type}.`;
  });
  assert.deepEqual(ran, ['window', 'terminal', 'files', 'notepad']);
});

/* ---------- the two entry points agree on the order ---------- */

test('both the voice and the typed path run parser, then executor, then model, then agent', () => {
  // The reasoning layer is only a fallback. If it were consulted before the
  // parser, every turn would cost a model call and the deterministic path would
  // stop being deterministic; if it were consulted after the agent, it would be
  // dead code. The order is the contract, so it is asserted rather than trusted.
  const provider = fs.readFileSync(path.join(FRONTEND, 'components/ApexProvider.tsx'), 'utf8');
  const voice = provider.match(/async function handleVoiceCommand\(text: string\) \{[\s\S]*?\n  \}/);
  assert.ok(voice, 'handleVoiceCommand not found');
  const voiceBody = voice[0];
  const voiceOrder = ['parseLocalCommand', 'executeLocalCommand', 'resolveCommandWithModel', 'sendRef.current'];
  let at = -1;
  for (const step of voiceOrder) {
    const found = voiceBody.indexOf(step, at + 1);
    assert.ok(found > at, `${step} is out of order in handleVoiceCommand`);
    at = found;
  }
  // An empty proposal must not be treated as an answer: [] means "send it on".
  assert.match(voiceBody, /if \(resolved\.length\)/);
  assert.match(voiceBody, /if \(reply !== null\)/);

  const typed = provider.match(/const sendMessage = useCallback\([\s\S]*?\n  \}, \[/);
  assert.ok(typed, 'sendMessage not found');
  const typedBody = typed[0];
  const typedOrder = ['parseLocalCommand', 'executeLocalCommand', 'resolveCommandWithModel'];
  at = -1;
  for (const step of typedOrder) {
    const found = typedBody.indexOf(step, at + 1);
    assert.ok(found > at, `${step} is out of order in sendMessage`);
    at = found;
  }
  assert.match(typedBody, /if \(resolved\.length\)/);
  // The orb must not be left "thinking" when a chain answers instead of the agent.
  assert.match(typedBody, /setOrb\("idle"\)/);
});

test('rubbish in place of a list of actions yields nothing', () => {
  const state = stateWith([]);
  const ctx = { state, utterance: 'x', language: 'en', skills };
  for (const junk of [null, undefined, 'restore all terminals', 42, {}, { actions: 'nope' }]) {
    assert.deepEqual(sanitizeActions(junk, ctx), [], JSON.stringify(junk));
  }
  // A wrapper object is accepted, because a model that answers in prose often
  // puts the list under a key.
  assert.deepEqual(
    sanitizeActions({ actions: [{ type: 'terminal', action: 'restore_all' }] }, ctx),
    [{ type: 'terminal', action: 'restore_all' }],
  );
});