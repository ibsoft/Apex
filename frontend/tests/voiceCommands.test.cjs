const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const source = fs.readFileSync(path.join(__dirname, '../lib/voiceCommands.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const moduleExports = {};
new Function('exports', compiled)(moduleExports);
const {
  wakePattern, isWakeOnlyText, isWakeWordFragment, isSleepCommand, recognitionLanguage,
  accumulateResults, commandText, emptyResultSnapshot, sliceAfterLastWake,
} = moduleExports;

test('Greek default wake aliases preserve command text and recognize Unicode boundaries', () => {
  for (const alias of ['Apex', 'Άπεξ', 'Απέξ', 'απεξ', 'ΑΠΕΞ', 'Άπεξ']) {
    const transcript = `Γεια σου ${alias}, βάλε υπενθύμιση σε δέκα λεπτά.`;
    const match = transcript.match(wakePattern('apex', 'el'));
    assert.ok(match, alias);
    assert.equal(transcript.slice(match.index + match[0].length), ', βάλε υπενθύμιση σε δέκα λεπτά.');
    assert.equal(isWakeOnlyText(`${alias}!`, 'apex', 'el'), true);
    assert.equal(isWakeOnlyText(transcript, 'apex', 'el'), false);
  }
  for (const phrase of ['apexes', 'superapex', 'παπεξ', 'απεξα', '_apex', 'apex2']) {
    assert.equal(wakePattern('apex', 'el').test(phrase), false, phrase);
  }
  assert.equal(wakePattern('apex', 'en').test('Άπεξ'), false);
});

test('custom wake words remain literal and also support Greek words', () => {
  assert.equal(wakePattern('Athena', 'el').test('Athena, hello'), true);
  assert.equal(wakePattern('Athena', 'el').test('Άπεξ'), false);
  assert.equal(wakePattern('Αθηνά', 'el').test('ΑΘΗΝΑ, γεια σου'), true);
  assert.equal(wakePattern('Άλεξ', 'el').test('Άλεξ'), true);
  assert.equal(wakePattern('A.pex', 'en').test('A.pex'), true);
  assert.equal(wakePattern('A.pex', 'en').test('Apex'), false);
  assert.equal(wakePattern('Café', 'en').test('Café'), true);
  assert.equal(wakePattern('Café', 'en').test('Cafe'), false);
  assert.equal(wakePattern('', 'el').test('hello'), false);
});

test('interim Greek wake fragments are not sent as commands', () => {
  for (const text of ['α', 'Άπ', 'Απέ', 'Άπεξ', 'a', 'ape', 'apex']) {
    assert.equal(isWakeWordFragment(text, 'apex', 'el'), true, text);
  }
  assert.equal(isWakeWordFragment('βάλε χρονόμετρο', 'apex', 'el'), false);
  assert.equal(isWakeWordFragment('να', 'apex', 'el'), false);
});

test('English voice sleep vocabulary works in both selected languages', () => {
  for (const phrase of ['stop', 'sleep', 'goodbye', 'good bye', 'goodnight', 'good night',
    'never mind', "that's all", 'thats all', 'that’s all', 'dismiss', 'quiet', 'go to sleep', 'stand down',
    'stop listening', 'sleep now', 'stop please', 'goodbye, thank you', "that's all, thanks!"]) {
    assert.equal(isSleepCommand(phrase, 'en'), true, phrase);
    assert.equal(isSleepCommand(phrase, 'el'), true, phrase);
  }
  assert.equal(isSleepCommand('sleeping', 'en'), false);
  for (const phrase of ['stop talking', 'stop timers', 'stop all reminders', 'stop autonomous mode']) {
    assert.equal(isSleepCommand(phrase, 'en'), false, phrase);
    assert.equal(isSleepCommand(phrase, 'el'), false, phrase);
  }
});

test('Greek sleep phrases accept accents and punctuation only when Greek is selected', () => {
  for (const phrase of ['σταμάτα', 'σταμάτησε', 'κοιμήσου', 'πήγαινε για ύπνο', 'καληνύχτα',
    'αντίο', 'άστο', "άσ' το", 'άσ’ το', 'άσ το', 'αυτό ήταν', 'αυτά ήταν', 'τέλος', 'άκυρο',
    'ΣΤΑΜΑΤΑ!', 'σταμάτα, παρακαλώ', 'κοιμήσου σε παρακαλώ', 'αυτό ήταν, ευχαριστώ',
    'σταμάτα να ακούς', 'σταμάτησε να ακούς', 'μπες σε αναμονή', 'πήγαινε σε αναμονή']) {
    assert.equal(isSleepCommand(phrase, 'el'), true, phrase);
    assert.equal(isSleepCommand(phrase, 'en'), false, phrase);
  }
  for (const phrase of ['σταμάτα το χρονόμετρο', 'άκυρο το προηγούμενο ραντεβού', 'βάλε χρονόμετρο', 'σιωπή']) {
    assert.equal(isSleepCommand(phrase, 'el'), false, phrase);
  }
});

test('Greek recognition supports complete wake-and-command utterances in standby and barge-in', () => {
  for (const phase of ['standby', 'awake', 'thinking', 'speaking']) {
    assert.equal(recognitionLanguage('el', 'apex', phase, false), 'el-GR');
    assert.equal(recognitionLanguage('el', 'Αθηνά', phase, false), 'el-GR');
    assert.equal(recognitionLanguage('en', 'apex', phase, false), 'en-US');
  }
});

test('custom Latin wake words retain English standby and Greek command/follow-up sessions', () => {
  assert.equal(recognitionLanguage('el', 'Athena', 'standby', false), 'en-US');
  assert.equal(recognitionLanguage('el', 'Athena', 'speaking', false), 'en-US');
  assert.equal(recognitionLanguage('el', 'Athena', 'awake', true), 'el-GR');
  assert.equal(recognitionLanguage('el', 'Athena', 'standby', true), 'el-GR');
});

test('endpointing merges a finalized chunk plus a trailing interim (pause-safe)', () => {
  // Model the real `SpeechRecognition` shape: results[...][0].transcript.
  const res = (isFinal, transcript) => ({ isFinal, 0: { transcript } });
  let snap = emptyResultSnapshot(0);
  snap = accumulateResults([res(true, 'book me a flight')], snap);
  assert.equal(commandText(snap), 'book me a flight');
  // User pauses; Chrome finalized the first chunk and appends a NEW result.
  snap = accumulateResults([
    res(true, 'book me a flight'),
    res(false, 'to Athens'),
  ], snap);
  assert.equal(commandText(snap), 'book me a flight to Athens');
  // Same session, more appended chunks.
  snap = accumulateResults([
    res(true, 'book me a flight'),
    res(true, 'to Athens'),
    res(false, 'tonight at 7'),
  ], snap);
  assert.equal(commandText(snap), 'book me a flight to Athens tonight at 7');
});

test('accumulateResults reads the first alternative of each result (Chrome shape)', () => {
  // Regression: SpeechRecognitionResult nests the transcript at results[i][0],
  // not results[i].transcript. Reading the wrong field silently drops speech.
  const snap = emptyResultSnapshot(0);
  const out = accumulateResults([
    { isFinal: true, 0: { transcript: 'wake word working' } },
    { isFinal: false, 0: { transcript: 'but full sentence here' } },
  ], snap);
  assert.equal(commandText(out), 'wake word working but full sentence here');
  // Alternatives beyond the first are ignored, matching the listener.
  const withMulti = accumulateResults([
    { isFinal: true, 0: { transcript: 'chosen best' }, 1: { transcript: 'second best' } },
  ], emptyResultSnapshot(0));
  assert.equal(commandText(withMulti), 'chosen best');
});

test('endpointing tolerates flat result objects as a fallback', () => {
  let snap = emptyResultSnapshot(0);
  snap = accumulateResults([{ isFinal: true, transcript: 'flat text works' }], snap);
  assert.equal(commandText(snap), 'flat text works');
});

test('endpointing overlays an extended interim without duplicating text', () => {
  const res = (isFinal, transcript) => ({ isFinal, 0: { transcript } });
  let snap = emptyResultSnapshot(0);
  snap = accumulateResults([res(false, 'what time')], snap);
  assert.equal(commandText(snap), 'what time');
  // Chrome extends the SAME last index while still interim.
  snap = accumulateResults([res(false, 'what time is it')], snap);
  assert.equal(commandText(snap), 'what time is it');
  snap = accumulateResults([res(true, 'what time is it')], snap);
  assert.equal(commandText(snap), 'what time is it');
  // New interim after the final: no duplication of the committed part.
  snap = accumulateResults([
    res(true, 'what time is it'),
    res(false, 'in Boston'),
  ], snap);
  assert.equal(commandText(snap), 'what time is it in Boston');
});

test('endpointing excludes results before the collection snapshot (wake noise / TTS echo)', () => {
  const res = (isFinal, transcript) => ({ isFinal, 0: { transcript } });
  const snap = emptyResultSnapshot(2);
  const out = accumulateResults([
    res(true, 'ignored earlier chatter'),
    res(true, 'also ignored'),
    res(false, 'set a timer'),
  ], snap);
  assert.equal(commandText(out), 'set a timer');
});

test('sliceAfterLastWake removes the wake word and leading punctuation', () => {
  assert.equal(sliceAfterLastWake('apex what time is it', 'apex', 'en'), 'what time is it');
  assert.equal(sliceAfterLastWake('Apex, open the app', 'apex', 'en'), 'open the app');
  assert.equal(sliceAfterLastWake('apex', 'apex', 'en'), '');
  assert.equal(sliceAfterLastWake('   apex ,  hello world', 'apex', 'en'), 'hello world');
  // Multiple wake occurrences slice at the LAST one.
  assert.equal(sliceAfterLastWake('apex stop apex soon', 'apex', 'en'), 'soon');
  // Greek wake aliases and Greek punctuation.
  assert.equal(sliceAfterLastWake('Άπεξ βάλε χρονόμετρο', 'apex', 'el'), 'βάλε χρονόμετρο');
  assert.equal(sliceAfterLastWake('Απεξ, τι ώρα είναι;', 'apex', 'el'), 'τι ώρα είναι;');
  assert.equal(sliceAfterLastWake('πες απεξ κάτι', 'Απεξ', 'el').length > 0, true);
  assert.equal(sliceAfterLastWake('apex', 'apex', 'el'), '');
});

test('Greek endpointing merges chunks across a pause and keeps accented text', () => {
  const res = (isFinal, transcript) => ({ isFinal, 0: { transcript } });
  let snap = emptyResultSnapshot(0);
  snap = accumulateResults([res(true, 'Άπεξ βάλε')], snap);
  snap = accumulateResults([
    res(true, 'Άπεξ βάλε'),
    res(false, 'υπενθύμιση'),
  ], snap);
  const sliced = sliceAfterLastWake(commandText(snap), 'apex', 'el');
  assert.equal(sliced, 'βάλε υπενθύμιση');
});

test('endpointing keeps a finalized chunk that arrived before the current interim', () => {
  const res = (isFinal, transcript) => ({ isFinal, 0: { transcript } });
  let snap = emptyResultSnapshot(0);
  snap = accumulateResults([res(true, 'x'), res(false, 'stop the music')], snap);
  assert.equal(commandText(snap), 'x stop the music');
});
