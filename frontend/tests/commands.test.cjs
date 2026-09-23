const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const source = fs.readFileSync(path.join(__dirname, '../lib/commands.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const moduleExports = {};
new Function('exports', compiled)(moduleExports);
const { parseLocalCommand, formatDuration } = moduleExports;
const skills = ['general', 'code', 'research', 'translator', 'obsidian', 'shell',
  'skill_creator', 'FILE_SEARCH', 'EDITOR', 'custom', 'custom helper'].map(name => ({ name }));
const now = new Date(2026, 8, 23, 10, 15).getTime();
const parse = (text, language = 'el') => parseLocalCommand(text, language, skills, now);

test('the requested reminder works in both languages and preserves the task', () => {
  assert.deepEqual(parse('set reminder to call John in 10 minutes', 'en'), {
    type: 'reminder', name: 'call John', fireAt: now + 600000,
  });
  assert.deepEqual(parse('βάλε υπενθύμιση να καλέσω τον Γιάννη σε 10 λεπτά'), {
    type: 'reminder', name: 'καλέσω τον Γιάννη', fireAt: now + 600000,
  });
});

for (const input of [
  'remind me in ten minutes to Call John',
  'remind me to Call John in ten minutes',
  'set a reminder in ten minutes to Call John',
  'add a reminder to Call John in ten minutes',
  'reminder Call John in ten minutes',
  'θύμισέ μου σε δέκα λεπτά να Call John',
  'υπενθύμισέ μου να Call John σε δέκα λεπτά',
  'όρισε μια υπενθύμιση σε δέκα λεπτά να Call John',
  'πρόσθεσε υπενθύμιση να Call John σε δέκα λεπτά',
  'ΥΠΕΝΘΥΜΙΣΗ να Call John σε ΔΕΚΑ ΛΕΠΤΑ!',
  'βάλε μου υπενθύμιση να Call John σε δέκα λεπτά',
  'κάνε μια υπενθύμιση να Call John σε δέκα λεπτά',
  'κάνε μου υπενθύμιση να Call John σε δέκα λεπτά',
]) {
  test(`relative reminder: ${input}`, () => {
    assert.deepEqual(parse(input), { type: 'reminder', name: 'Call John', fireAt: now + 600000 });
  });
}

test('normalization preserves decomposed accents and punctuation in captured names', () => {
  const task = 'τηλεφωνήσω στη Μαρία (κινητό)';
  assert.deepEqual(parse(`ΘΎΜΙΣΈ μου να ${task} σε ΔΈΚΑ λεπτά`), {
    type: 'reminder', name: task, fireAt: now + 600000,
  });
});

test('reminders can omit their task and localize their default name', () => {
  assert.deepEqual(parse('remind me in 5 seconds', 'en'), { type: 'reminder', name: 'Reminder', fireAt: now + 5000 });
  assert.deepEqual(parse('θύμισέ μου σε πέντε δευτερόλεπτα'), { type: 'reminder', name: 'Υπενθύμιση', fireAt: now + 5000 });
});

test('the time delimiter inside a task does not confuse a later valid time', () => {
  assert.deepEqual(parse('remind me to check in on John in ten minutes', 'en'), {
    type: 'reminder', name: 'check in on John', fireAt: now + 600000,
  });
});

for (const [text, hour, minute, day] of [
  ['remind me at 3 PM to Call John', 15, 0, 23],
  ['set reminder to Call John at 15:30', 15, 30, 23],
  ['θύμισέ μου στις 3 μ.μ. να Call John', 15, 0, 23],
  ['βάλε υπενθύμιση να Call John στις 3:30 μ.μ.', 15, 30, 23],
  ['θύμισέ μου στη 1 μμ να Call John', 13, 0, 23],
  ['θύμισέ μου στις 8 π.μ. να Call John', 8, 0, 24],
  ['θύμισέ μου στις 12 π.μ. να Call John', 0, 0, 24],
  ['θύμισέ μου στις 12 μ.μ. να Call John', 12, 0, 23],
  ['θύμισέ μου στις 10:15 να Call John', 10, 15, 24],
]) {
  test(`absolute reminder: ${text}`, () => {
    assert.deepEqual(parse(text), {
      type: 'reminder', name: 'Call John', fireAt: new Date(2026, 8, day, hour, minute).getTime(),
    });
  });
}

for (const expression of ['24:00', '99:12', '12:60', '0 PM', '13 PM', '3', '3:3', '3 μ.μ. extra']) {
  test(`reject invalid or incomplete clock: ${expression}`, () => {
    assert.equal(parse(`θύμισέ μου στις ${expression} να τηλεφωνήσω`), null);
  });
}

test('absolute and relative reminder prepositions cannot be interchanged', () => {
  for (const input of ['remind me at 5 minutes', 'remind me in 3 PM', 'θύμισέ μου στις 5 λεπτά', 'θύμισέ μου σε 15:30']) {
    assert.equal(parse(input), null, input);
  }
});

for (const [input, seconds] of [
  ['set a timer for 5 seconds', 5],
  ['start timer for ten minutes', 600],
  ['create a countdown for one hour', 3600],
  ['countdown 1 hour 2 minutes 3 seconds', 3723],
  ['timer twenty-one minutes and five seconds', 1265],
  ['βάλε χρονόμετρο για δέκα λεπτά', 600],
  ['όρισε ένα χρονόμετρο για μία ώρα', 3600],
  ['ξεκίνα αντίστροφη μέτρηση για ένα λεπτό', 60],
  ['δημιούργησε χρονόμετρο για ένας λεπτό', 60],
  ['χρονόμετρο δύο ώρες και τρία λεπτά', 7380],
  ['αντίστροφη μέτρηση δυο λεπτά, πέντε δευτερόλεπτα', 125],
  ['χρονόμετρο μία ώρα, και είκοσι ένα λεπτά', 4860],
  ['ΧΡΟΝΟΜΕΤΡΟ ΤΡΕΙΣ ΩΡΕΣ ΚΑΙ ΤΕΣΣΕΡΑ ΛΕΠΤΑ', 11040],
  ['χρονόμετρο μισή ώρα', 1800],
  ['χρονόμετρο μιάμιση ώρα', 5400],
  ['χρονόμετρο ενάμισι λεπτό', 90],
  ['βάλε μου χρονόμετρο για δέκα λεπτά', 600],
  ['κάνε χρονόμετρο για δέκα λεπτά', 600],
  ['κάνε μου αντίστροφη μέτρηση για πέντε λεπτά', 300],
]) {
  test(`timer duration: ${input}`, () => {
    assert.deepEqual(parse(input), { type: 'timer', name: 'Χρονόμετρο', seconds });
  });
}

test('all Greek written number forms are recognized without accent or case dependence', () => {
  for (const [number, amount] of [
    ['εφτά', 7], ['επτά', 7], ['οχτώ', 8], ['οκτώ', 8], ['εννιά', 9], ['εννέα', 9],
    ['ένδεκα', 11], ['δώδεκα', 12], ['δεκατρείς', 13], ['δεκατρία', 13],
    ['δεκατέσσερις', 14], ['δεκατέσσερα', 14], ['δεκαπέντε', 15], ['δεκαέξι', 16],
    ['δεκαεπτά', 17], ['δεκαεφτά', 17], ['δεκαοκτώ', 18], ['δεκαοχτώ', 18],
    ['δεκαεννέα', 19], ['δεκαεννιά', 19], ['είκοσι', 20], ['τριάντα', 30],
    ['σαράντα', 40], ['πενήντα', 50], ['εξήντα', 60], ['σαράντα πέντε', 45],
  ]) assert.equal(parse(`χρονόμετρο ${number} λεπτά`).seconds, amount * 60, number);
});

test('named timers retain the original label', () => {
  assert.deepEqual(parse('timer five minutes called Tea Time', 'en'), { type: 'timer', name: 'Tea Time', seconds: 300 });
  assert.deepEqual(parse('βάλε χρονόμετρο πέντε λεπτά με όνομα Τσάι της Μαρίας'), {
    type: 'timer', name: 'Τσάι της Μαρίας', seconds: 300,
  });
  assert.deepEqual(parse('χρονόμετρο 5 λεπτά για τα Μακαρόνια'), { type: 'timer', name: 'τα Μακαρόνια', seconds: 300 });
});

test('invalid or partial durations are never silently accepted', () => {
  for (const input of [
    'timer 0 seconds', 'timer -1 minute', 'timer 1.5 minutes', 'timer 5 minutes foo',
    'timer one hour and banana minutes', 'timer and 5 minutes', 'timer 999999999999999999 hours',
    'χρονόμετρο δέκα μπανάνες', 'χρονόμετρο πέντε λεπτά και κάτι', 'χρονόμετρο μηδέν λεπτά',
    'θύμισέ μου να καλέσω σε δέκα λεπτά αύριο', 'remind me to call John in 5 minutes sometime',
    'timer 5 seconds ; delete files',
  ]) assert.equal(parse(input), null, input);
});

test('Greek commands are enabled only by the Greek language setting', () => {
  for (const text of [
    'θύμισέ μου σε 5 λεπτά', 'χρονόμετρο 5 λεπτά', 'κλείσε την προεπισκόπηση',
    'ακύρωσε όλα τα χρονόμετρα', 'ενεργοποίησε αυτονομία', 'ησυχία',
    'δείξε εικόνες με γάτες', 'χρησιμοποίησε έρευνα', 'είμαι ο χειριστής',
  ]) {
    assert.equal(parse(text, 'en'), null, text);
    assert.equal(parse(text, 'fr'), null, text);
    assert.notEqual(parse(text, 'el-GR'), null, text);
  }
  assert.equal(parse('timer δέκα minutes', 'en'), null);
  assert.equal(parse('remind me at 3 μ.μ.', 'en'), null);
  assert.equal(parse('use έρευνα', 'en'), null);
  assert.equal(parse('set a timer for 5 seconds', 'fr').seconds, 5);
});

for (const [text, type] of [
  ['cancel all timers', 'cancelTimers'], ['stop timer', 'cancelTimers'],
  ['clear reminders', 'cancelReminders'], ['cancel all reminders', 'cancelReminders'],
  ['ακύρωσε όλα τα χρονόμετρα', 'cancelTimers'], ['σταμάτα το χρονόμετρο', 'cancelTimers'],
  ['διέγραψε τις υπενθυμίσεις', 'cancelReminders'], ['ακύρωσε όλες τις υπενθυμίσεις', 'cancelReminders'],
]) test(`cancellation: ${text}`, () => assert.deepEqual(parse(text), { type }));

for (const [action, phrases] of [
  ['close', ['close', 'hide the preview', 'dismiss it', 'κλείσε την προεπισκόπηση', 'κρύψε το παράθυρο']],
  ['maximize', ['maximize', 'full screen', 'enlarge window', 'μεγιστοποίησε το παράθυρο', 'πλήρης οθόνη']],
  ['restore', ['normalize', 'minimize', 'restore the preview', 'επαναφέρε την προεπισκόπηση', 'μίκρυνε το παράθυρο']],
  ['next', ['next image', 'forward', 'επόμενη εικόνα', 'επόμενο', 'μπροστά']],
  ['previous', ['previous photo', 'back', 'earlier page', 'προηγούμενη φωτογραφία', 'πίσω']],
]) {
  for (const phrase of phrases) test(`preview: ${phrase}`, () => assert.deepEqual(parse(phrase), { type: 'preview', action }));
}

test('operator declarations keep the operator name', () => {
  for (const text of ['I am your operator, name is John', "I'm operator John", 'call me operator John']) {
    assert.deepEqual(parse(text), { type: 'operator', name: 'John' });
  }
  assert.deepEqual(parse('Είμαι ο χειριστής σου, με λένε Γιάννη'), { type: 'operator', name: 'Γιάννη' });
  assert.deepEqual(parse('είμαι η χειρίστρια Μαρία'), { type: 'operator', name: 'Μαρία' });
  assert.deepEqual(parse('είμαι ο χειριστής'), { type: 'operator' });
});

test('autonomy and silence controls work in both languages', () => {
  for (const text of ['enable autonomous mode', 'start autonomy', 'turn on autonomous mode', 'ενεργοποίησε την αυτόνομη λειτουργία', 'άνοιξε αυτονομία']) {
    assert.deepEqual(parse(text), { type: 'autonomy', enabled: true });
  }
  for (const text of ['disable autonomous mode', 'turn off autonomy', 'stop autonomy', 'απενεργοποίησε την αυτονομία', 'κλείσε την αυτόνομη λειτουργία']) {
    assert.deepEqual(parse(text), { type: 'autonomy', enabled: false });
  }
  for (const text of ['be quiet', 'silence', 'pause autonomy', 'stop talking', 'σιωπή', 'κάνε ησυχία', 'μη μιλάς', 'σταμάτα να μιλάς', 'παύση αυτονομίας']) {
    assert.deepEqual(parse(text), { type: 'silence' });
  }
});

for (const [text, query, source] of [
  ['open image browser', '', 'web'], ['browse gallery', '', 'web'],
  ['show my pictures', '', 'local'], ['show local images', '', 'local'],
  ['show images from my computer', '', 'local'], ['show me images of Blue Cats', 'Blue Cats', 'web'],
  ['find photos of Summer Trip from my folder', 'Summer Trip', 'local'],
  ['find images of Local Flowers', 'Local Flowers', 'web'],
  ['άνοιξε τις εικόνες', '', 'web'], ['δείξε τις φωτογραφίες μου', '', 'local'],
  ['άνοιξε τις τοπικές εικόνες', '', 'local'], ['δείξε εικόνες από τον υπολογιστή μου', '', 'local'],
  ['δείξε μου εικόνες με Γάτες', 'Γάτες', 'web'], ['βρες φωτογραφίες για Νησιά στο διαδίκτυο', 'Νησιά', 'web'],
  ['αναζήτησε εικόνες με Διακοπές από τον φάκελό μου', 'Διακοπές', 'local'],
  ['δείξε τις φωτογραφίες μου με Γάτες', 'Γάτες', 'local'],
  ['βρες τοπική εικόνα με Τριαντάφυλλα', 'Τριαντάφυλλα', 'local'],
]) test(`image command: ${text}`, () => assert.deepEqual(parse(text), { type: 'images', query, source }));

for (const [alias, skill] of [
  ['γενικά', 'general'], ['γενική βοήθεια', 'general'], ['βοήθεια', 'general'],
  ['κώδικας', 'code'], ['προγραμματισμός', 'code'], ['προγραμματιστής', 'code'],
  ['έρευνα', 'research'], ['μελέτη', 'research'], ['μεταφραστής', 'translator'],
  ['μετάφραση', 'translator'], ['σημειώσεις', 'obsidian'], ['οψιδιανός', 'obsidian'],
  ['σημειωματάριο', 'obsidian'], ['τερματικό', 'shell'], ['κέλυφος', 'shell'],
  ['κονσόλα', 'shell'], ['δημιουργός δεξιοτήτων', 'skill_creator'],
  ['δημιουργία δεξιοτήτων', 'skill_creator'], ['αναζήτηση αρχείων', 'FILE_SEARCH'],
  ['αρχεία', 'FILE_SEARCH'], ['ψάξε αρχεία', 'FILE_SEARCH'],
  ['συντάκτης', 'EDITOR'], ['επεξεργαστής εγγράφων', 'EDITOR'], ['έγγραφα', 'EDITOR'],
  ['επεξεργαστής', 'EDITOR'], ['word', 'EDITOR'], ['excel', 'EDITOR'],
]) {
  test(`Greek skill alias: ${alias}`, () => {
    assert.deepEqual(parse(`χρησιμοποίησε ${alias}, Δοκιμή με Όνομα.`), { type: 'skill', skill, rest: 'Δοκιμή με Όνομα.' });
  });
}

test('all canonical skill names, selection verbs, and custom skills remain supported', () => {
  for (const { name } of skills) {
    assert.deepEqual(parse(`switch to skill ${name}`, 'en'), { type: 'skill', skill: name, rest: '' });
    assert.deepEqual(parse(`use ${name}`, 'en'), { type: 'skill', skill: name, rest: '' });
    assert.deepEqual(parse(`activate the skill ${name}`, 'en'), { type: 'skill', skill: name, rest: '' });
  }
  for (const prefix of ['χρησιμοποίησε', 'ενεργοποίησε', 'επίλεξε', 'άλλαξε σε', 'μετάβαση σε']) {
    assert.deepEqual(parse(`${prefix} τη δεξιότητα έρευνα`), { type: 'skill', skill: 'research', rest: '' });
  }
  assert.deepEqual(parse('USE THE SKILL Custom Helper: Keep This Text', 'en'), {
    type: 'skill', skill: 'custom helper', rest: 'Keep This Text',
  });
  assert.deepEqual(parse('χρησιμοποίησε code, Γράψε Python'), { type: 'skill', skill: 'code', rest: 'Γράψε Python' });
  assert.deepEqual(parse('χρησιμοποίησε τον μεταφραστή'), { type: 'skill', skill: 'translator', rest: '' });
  assert.deepEqual(parse('ενεργοποίησε τον συντάκτη'), { type: 'skill', skill: 'EDITOR', rest: '' });
  assert.deepEqual(parse('χρησιμοποίησε τη δεξιότητα κώδικα'), { type: 'skill', skill: 'code', rest: '' });
  assert.equal(parseLocalCommand('χρησιμοποίησε έρευνα', 'el', [{ name: 'general' }], now), null);
});

test('skill switch strips leading "and"/"και" connector from the rest', () => {
  assert.deepEqual(parse('use obsidian and search for my car plate', 'en'), {
    type: 'skill', skill: 'obsidian', rest: 'search for my car plate',
  });
  assert.deepEqual(parse('switch to skill code and write a function', 'en'), {
    type: 'skill', skill: 'code', rest: 'write a function',
  });
  assert.deepEqual(parse('χρησιμοποίησε οψιδιανό και ψάξε την πινακίδα μου'), {
    type: 'skill', skill: 'obsidian', rest: 'ψάξε την πινακίδα μου',
  });
});

test('whole phrases and skill boundaries avoid hijacking ordinary questions', () => {
  for (const text of [
    'close my account', 'next week I travel', 'back up my files', 'silence is golden',
    'use codeine carefully', 'use researcher notes', 'switch to custom_helper',
    'ακύρωσε την πτήση', 'κλείσε το ραντεβού', 'επόμενη εβδομάδα φεύγω',
    'ενεργοποίησε έρευνες', 'χρησιμοποίησε κώδικας123', 'τι σημαίνει σιωπή',
    'remind me how timers work', 'explain timer 10 minutes', '',
  ]) assert.equal(parse(text), null, text);
});

test('duration acknowledgements use the selected language and singular/plural units', () => {
  assert.equal(formatDuration(0), '0 seconds');
  assert.equal(formatDuration(3661), '1 hour 1 minute 1 second');
  assert.equal(formatDuration(7322), '2 hours 2 minutes 2 seconds');
  assert.equal(formatDuration(3661, 'el'), '1 ώρα 1 λεπτό 1 δευτερόλεπτο');
  assert.equal(formatDuration(7322, 'el-GR'), '2 ώρες 2 λεπτά 2 δευτερόλεπτα');
});
