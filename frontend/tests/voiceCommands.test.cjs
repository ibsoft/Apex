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
const { wakePattern, isWakeOnlyText, isWakeWordFragment, isSleepCommand, recognitionLanguage } = moduleExports;

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
