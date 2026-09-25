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
  ['minimize', ['minimize', 'μίκρυνε το παράθυρο']],
  ['restore', ['normalize', 'restore the preview', 'επαναφέρε την προεπισκόπηση']],
  ['next', ['next image', 'forward', 'επόμενη εικόνα', 'επόμενο', 'μπροστά']],
  ['previous', ['previous photo', 'back', 'earlier page', 'προηγούμενη φωτογραφία', 'πίσω']],
]) {
  for (const phrase of phrases) test(`window: ${phrase}`, () => assert.deepEqual(parse(phrase), { type: 'window', action }));
}

test('window commands target an explicit window by number or ordinal', () => {
  assert.deepEqual(parse('close window 2', 'en'), { type: 'window', action: 'close', target: 2 });
  assert.deepEqual(parse('focus the second window', 'en'), { type: 'window', action: 'focus', target: 2 });
  assert.deepEqual(parse('maximize window number three', 'en'), { type: 'window', action: 'maximize', target: 3 });
  assert.deepEqual(parse('κλείσε το δεύτερο παράθυρο'), { type: 'window', action: 'close', target: 2 });
  assert.deepEqual(parse('μεγιστοποίησε το τρίτο παράθυρο'), { type: 'window', action: 'maximize', target: 3 });
  assert.deepEqual(parse('focus window #4', 'en'), { type: 'window', action: 'focus', target: 4 });
});

test('window close-all, arrange and list commands parse in both languages', () => {
  assert.deepEqual(parse('close all windows', 'en'), { type: 'window', action: 'close_all' });
  assert.deepEqual(parse('κλείσε όλα τα παράθυρα'), { type: 'window', action: 'close_all' });
  assert.deepEqual(parse('arrange windows', 'en'), { type: 'window', action: 'arrange', arrangement: 'cascade' });
  assert.deepEqual(parse('arrange the windows in a grid', 'en'), { type: 'window', action: 'arrange', arrangement: 'grid' });
  assert.deepEqual(parse('arrange windows side by side', 'en'), { type: 'window', action: 'arrange', arrangement: 'tile-v' });
  assert.deepEqual(parse('arrange windows stacked', 'en'), { type: 'window', action: 'arrange', arrangement: 'tile-h' });
  assert.deepEqual(parse('ταξινόμησε τα παράθυρα'), { type: 'window', action: 'arrange', arrangement: 'cascade' });
  assert.deepEqual(parse('διάταξε τα παράθυρα σε στήλες'), { type: 'window', action: 'arrange', arrangement: 'tile-v' });
  assert.deepEqual(parse('list windows', 'en'), { type: 'window', action: 'list' });
  assert.deepEqual(parse('δείξε τα ανοιχτά παράθυρα'), { type: 'window', action: 'list' });
});

test('virtual-desktop commands parse in both languages', () => {
  assert.deepEqual(parse('go to virtual desktop 1', 'en'), { type: 'desktop', action: 'switch', desktop: 0 });
  assert.deepEqual(parse('switch to desktop 2', 'en'), { type: 'desktop', action: 'switch', desktop: 1 });
  assert.deepEqual(parse('desktop 3', 'en'), { type: 'desktop', action: 'switch', desktop: 2 });
  assert.deepEqual(parse('focus the fourth desktop', 'en'), { type: 'desktop', action: 'switch', desktop: 3 });
  assert.deepEqual(parse('switch to workspace 2', 'en'), { type: 'desktop', action: 'switch', desktop: 1 });
  assert.deepEqual(parse('desktop four', 'en'), { type: 'desktop', action: 'switch', desktop: 3 });
  assert.deepEqual(parse('πήγαινε στην εικονική επιφάνεια εργασίας 2'), { type: 'desktop', action: 'switch', desktop: 1 });
  assert.deepEqual(parse('μετάβα στην επιφάνεια εργασίας τέταρτη'), { type: 'desktop', action: 'switch', desktop: 3 });
  assert.deepEqual(parse('εναλλαγή στην επιφάνεια εργασίας 2'), { type: 'desktop', action: 'switch', desktop: 1 });
  assert.deepEqual(parse('desktop 9', 'en'), { type: 'desktop', action: 'switch', desktop: 3 }, 'clamps to the last desktop');

  assert.deepEqual(parse('next desktop', 'en'), { type: 'desktop', action: 'next', desktop: 0 });
  assert.deepEqual(parse('go to the previous desktop', 'en'), { type: 'desktop', action: 'previous', desktop: 0 });
  assert.deepEqual(parse('επόμενη επιφάνεια εργασίας'), { type: 'desktop', action: 'next', desktop: 0 });
  assert.deepEqual(parse('προηγούμενη εικονική επιφάνεια εργασίας'), { type: 'desktop', action: 'previous', desktop: 0 });

  assert.deepEqual(parse('move window 2 to desktop 1', 'en'), { type: 'desktop', action: 'move', target: 2, desktop: 0 });
  assert.deepEqual(parse('move the third window to desktop four', 'en'), { type: 'desktop', action: 'move', target: 3, desktop: 3 });
  assert.deepEqual(parse('μετακίνησε το παράθυρο 2 στην επιφάνεια εργασίας 1'), { type: 'desktop', action: 'move', target: 2, desktop: 0 });

  // window/gallery nav must NOT be swallowed by the desktop parser
  assert.deepEqual(parse('next image', 'en'), { type: 'window', action: 'next' });
  assert.deepEqual(parse('επόμενη εικόνα'), { type: 'window', action: 'next' });
  assert.deepEqual(parse('previous photo', 'en'), { type: 'window', action: 'previous' });
  assert.deepEqual(parse('προηγούμενη φωτογραφία'), { type: 'window', action: 'previous' });
});

test('notes attach to a window and preserve the original note text', () => {
  assert.deepEqual(parse('add a note to the second window saying Keep this open', 'en'), {
    type: 'window', action: 'note', target: 2, note: 'Keep this open',
  });
  assert.deepEqual(parse('πρόσθεσε σημείωση στο δεύτερο παράθυρο να λέει Μην κλείσεις αύριο'), {
    type: 'window', action: 'note', target: 2, note: 'Μην κλείσεις αύριο',
  });
  assert.deepEqual(parse('put a note on window 3: νερό στα φυτά', 'en'), {
    type: 'window', action: 'note', target: 3, note: 'νερό στα φυτά',
  });
});

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

test('terminal commands parse in both languages with optional ordinal targets', () => {
  assert.deepEqual(parse('open terminal', 'en'), { type: 'terminal', action: 'open' });
  assert.deepEqual(parse('open a new terminal', 'en'), { type: 'terminal', action: 'open', create: true });
  assert.deepEqual(parse('start another terminal', 'en'), { type: 'terminal', action: 'open', create: true });
  assert.deepEqual(parse('open terminal 2', 'en'), { type: 'terminal', action: 'open', target: 2 });
  assert.deepEqual(parse('open new terminal 3', 'en'), { type: 'terminal', action: 'open', target: 3, create: true });
  assert.deepEqual(parse('άνοιξε τερματικό'), { type: 'terminal', action: 'open' });
  assert.deepEqual(parse('άνοιξε νέο τερματικό'), { type: 'terminal', action: 'open', create: true });
  assert.deepEqual(parse('άνοιξε ένα ακόμα τερματικό'), { type: 'terminal', action: 'open', create: true });
  assert.deepEqual(parse('ξεκίνα το τερματικό 3'), { type: 'terminal', action: 'open', target: 3 });

  assert.deepEqual(parse('close terminal', 'en'), { type: 'terminal', action: 'close' });
  assert.deepEqual(parse('close the terminal 2', 'en'), { type: 'terminal', action: 'close', target: 2 });
  assert.deepEqual(parse('κλείσε τερματικό'), { type: 'terminal', action: 'close' });
  assert.deepEqual(parse('κλείσε το τερματικό 4'), { type: 'terminal', action: 'close', target: 4 });
  assert.deepEqual(parse('κλείσε το δεύτερο τερματικό'), { type: 'terminal', action: 'close', target: 2 });

  assert.deepEqual(parse('focus terminal', 'en'), { type: 'terminal', action: 'focus' });
  assert.deepEqual(parse('focus on terminal 2', 'en'), { type: 'terminal', action: 'focus', target: 2 });
  assert.deepEqual(parse('switch to the terminal', 'en'), { type: 'terminal', action: 'focus' });
  assert.deepEqual(parse('εστίασε στο τερματικό'), { type: 'terminal', action: 'focus' });
  assert.deepEqual(parse('φέρε το τερματικό 2'), { type: 'terminal', action: 'focus', target: 2 });
});

test('terminal phrasing does not hijack skills, windows or ordinary sentences', () => {
  assert.deepEqual(parse('χρησιμοποίησε τερματικό'), { type: 'skill', skill: 'shell', rest: '' }); // skill select, not a terminal window
  for (const text of ['open terminal node', 'close terminal care', 'focus on terminal velocity']) {
    assert.equal(parse(text, 'en'), null, text);
  }
});

test('maximize/minimize/restore target a terminal directly (and by ordinal)', () => {
  assert.deepEqual(parse('maximize terminal', 'en'), { type: 'terminal', action: 'maximize' });
  assert.deepEqual(parse('normalize terminal', 'en'), { type: 'terminal', action: 'restore' });
  assert.deepEqual(parse('minimize the terminal', 'en'), { type: 'terminal', action: 'minimize' });
  assert.deepEqual(parse('maximize terminal 2', 'en'), { type: 'terminal', action: 'maximize', target: 2 });
  assert.deepEqual(parse('μεγιστοποίησε το τερματικό'), { type: 'terminal', action: 'maximize' });
  assert.deepEqual(parse('ελαχιστοποίησε το τερματικό'), { type: 'terminal', action: 'minimize' });
  assert.deepEqual(parse('επαναφέρε το δεύτερο τερματικό'), { type: 'terminal', action: 'restore', target: 2 });
});

test('maximize/minimize/restore the file manager', () => {
  assert.deepEqual(parse('minimize file manager', 'en'), { type: 'files', action: 'minimize' });
  assert.deepEqual(parse('maximize the file manager', 'en'), { type: 'files', action: 'maximize' });
  assert.deepEqual(parse('restore file browser', 'en'), { type: 'files', action: 'restore' });
  assert.deepEqual(parse('ελαχιστοποίησε τον διαχειριστή αρχείων'), { type: 'files', action: 'minimize' });
  assert.deepEqual(parse('μεγιστοποίησε τον διαχειριστή αρχείων'), { type: 'files', action: 'maximize' });
  assert.deepEqual(parse('επαναφέρε τον διαχειριστή αρχείων'), { type: 'files', action: 'restore' });
});

test('minimize/restore all windows', () => {
  assert.deepEqual(parse('minimize all windows', 'en'), { type: 'window', action: 'minimize_all' });
  assert.deepEqual(parse('minimize every window', 'en'), { type: 'window', action: 'minimize_all' });
  assert.deepEqual(parse('shrink all the windows', 'en'), { type: 'window', action: 'minimize_all' });
  assert.deepEqual(parse('restore all windows', 'en'), { type: 'window', action: 'restore_all' });
  assert.deepEqual(parse('restore all', 'en'), { type: 'window', action: 'restore_all' });
  assert.deepEqual(parse('ελαχιστοποίησε όλα τα παράθυρα'), { type: 'window', action: 'minimize_all' });
  assert.deepEqual(parse('μίκρυνε τα παράθυρα'), { type: 'window', action: 'minimize_all' });
  assert.deepEqual(parse('επαναφέρε όλα τα παράθυρα'), { type: 'window', action: 'restore_all' });
  // targeted/other actions still win over the "all" phrasing
  assert.deepEqual(parse('minimize window 2', 'en'), { type: 'window', action: 'minimize', target: 2 });
  assert.deepEqual(parse('maximize the preview', 'en'), { type: 'window', action: 'maximize' });
});

test('bare arrangement words resolve to the matching arrange style', () => {
  assert.deepEqual(parse('cascade', 'en'), { type: 'window', action: 'arrange', arrangement: 'cascade' });
  assert.deepEqual(parse('cascade the windows', 'en'), { type: 'window', action: 'arrange', arrangement: 'cascade' });
  assert.deepEqual(parse('grid', 'en'), { type: 'window', action: 'arrange', arrangement: 'grid' });
  assert.deepEqual(parse('stack the windows', 'en'), { type: 'window', action: 'arrange', arrangement: 'tile-h' });
  assert.deepEqual(parse('side by side', 'en'), { type: 'window', action: 'arrange', arrangement: 'tile-v' });
  assert.deepEqual(parse('center', 'en'), { type: 'window', action: 'arrange', arrangement: 'center' });
  assert.deepEqual(parse('centre', 'en'), { type: 'window', action: 'arrange', arrangement: 'center' });
  assert.deepEqual(parse('κασκάντα'), { type: 'window', action: 'arrange', arrangement: 'cascade' });
  assert.deepEqual(parse('πλέγμα'), { type: 'window', action: 'arrange', arrangement: 'grid' });
  assert.deepEqual(parse('διπλά διπλά'), { type: 'window', action: 'arrange', arrangement: 'tile-v' });
});

test('file-manager phrasing opens, closes and focuses the files window in both languages', () => {
  assert.deepEqual(parse('open file manager', 'en'), { type: 'files', action: 'open' });
  assert.deepEqual(parse('open the file manager', 'en'), { type: 'files', action: 'open' });
  assert.deepEqual(parse('show me the files window', 'en'), { type: 'files', action: 'open' });
  assert.deepEqual(parse('browse files', 'en'), { type: 'files', action: 'open' });
  assert.deepEqual(parse('close the files window', 'en'), { type: 'files', action: 'close' });
  assert.deepEqual(parse('focus the file browser', 'en'), { type: 'files', action: 'focus' });
  assert.deepEqual(parse('switch to the explorer', 'en'), { type: 'files', action: 'focus' });
  assert.deepEqual(parse('άνοιξε τον διαχειριστή αρχείων'), { type: 'files', action: 'open' });
  assert.deepEqual(parse('ξεκίνησε τον φυλλομετρητή αρχείων'), { type: 'files', action: 'open' });
  assert.deepEqual(parse('δείξε μου τα αρχεία'), { type: 'files', action: 'open' });
  assert.deepEqual(parse('κλείσε το παράθυρο αρχείων'), { type: 'files', action: 'close' });
  assert.deepEqual(parse('εστίασε στον διαχειριστή αρχείων'), { type: 'files', action: 'focus' });
});

test('"new" / "another" phrasing opens an additional file-manager window', () => {
  assert.deepEqual(parse('open new file manager', 'en'), { type: 'files', action: 'open', create: true });
  assert.deepEqual(parse('open another file manager', 'en'), { type: 'files', action: 'open', create: true });
  assert.deepEqual(parse('start a new file browser', 'en'), { type: 'files', action: 'open', create: true });
  assert.deepEqual(parse('άνοιξε νέο διαχειριστή αρχείων'), { type: 'files', action: 'open', create: true });
  assert.deepEqual(parse('άνοιξε ένα ακόμα διαχειριστή αρχείων'), { type: 'files', action: 'open', create: true });
  assert.deepEqual(parse('ξεκίνησε καινούργιο φυλλομετρητή αρχείων'), { type: 'files', action: 'open', create: true });
  assert.deepEqual(parse('open a file manager', 'en'), { type: 'files', action: 'open' }, 'plain "a" still focuses');
});

test('file-manager wording does not collide with images, skills or ordinary sentences', () => {
  assert.deepEqual(parse('show images', 'en'), { type: 'images', query: '', source: 'web' });
  assert.equal(parse('open file names one by one', 'en'), null);
  assert.equal(parse('arrange the files nicely', 'en'), null);
  assert.equal(parse('τι είναι τα αρχεία',), null, 'a question is not a file-manager command');
});

test('duration acknowledgements use the selected language and singular/plural units', () => {
  assert.equal(formatDuration(0), '0 seconds');
  assert.equal(formatDuration(3661), '1 hour 1 minute 1 second');
  assert.equal(formatDuration(7322), '2 hours 2 minutes 2 seconds');
  assert.equal(formatDuration(3661, 'el'), '1 ώρα 1 λεπτό 1 δευτερόλεπτο');
  assert.equal(formatDuration(7322, 'el-GR'), '2 ώρες 2 λεπτά 2 δευτερόλεπτα');
});


test('Notepad commands work in chat and voice phrasing', () => {
  const cases = [
    ['open notepad', { type: 'notepad', action: 'open' }],
    ['focus the notepad', { type: 'notepad', action: 'focus' }],
    ['minimize notepad', { type: 'notepad', action: 'minimize' }],
    ['maximize my notepad', { type: 'notepad', action: 'maximize' }],
    ['restore notepad', { type: 'notepad', action: 'restore' }],
    ['save notepad', { type: 'notepad', action: 'save' }],
    ['download notepad', { type: 'notepad', action: 'download' }],
    ['new notepad', { type: 'notepad', action: 'new' }],
    ['close notepad', { type: 'notepad', action: 'close' }],
    ['write Call Maria at five in notepad', { type: 'notepad', action: 'write', content: 'Call Maria at five' }],
    ['in the notepad, write Meeting notes', { type: 'notepad', action: 'write', content: 'Meeting notes' }],
  ];
  for (const [phrase, expected] of cases) assert.deepEqual(parseLocalCommand(phrase, 'en', []), expected, phrase);
  assert.deepEqual(parseLocalCommand('γράψε Καλημέρα στο σημειωματάριο', 'el', []), {
    type: 'notepad', action: 'write', content: 'Καλημέρα',
  });
});


test('Notepad supports natural editing and compound output commands without losing punctuation', () => {
  const cases = [
    ['write to notepad Hello, world!', 'write', 'Hello, world!'],
    ['open notepad and write Hello.', 'write', 'Hello.'],
    ['append More notes in notepad', 'write', 'More notes'],
    ['open notepad and add command output', 'command_output'],
    ['copy the last terminal output to notepad', 'command_output'],
    ['show recent documents in notepad', 'recent'],
    ['hide recent documents', 'hide_recent'],
    ['replace notepad contents with New text.', 'replace', 'New text.'],
    ['rename notepad to Meeting Notes', 'title', 'Meeting Notes'],
    ['open Meeting Notes in notepad', 'open_document', 'Meeting Notes'],
    ['format notepad bold', 'format', 'bold'],
    ['undo in notepad', 'undo'],
    ['export notepad as text', 'export_text'],
    ['clear notepad', 'clear'],
  ];
  for (const [text, action, content] of cases) {
    const result = parseLocalCommand(text, 'en', []);
    assert.equal(result?.type, 'notepad', text);
    assert.equal(result?.action, action, text);
    assert.equal(result?.content, content, text);
  }
  for (const text of ['run uname -a and put the command output in notepad', 'open notepad and write a poem about spring', 'open notepad and write Hello then save']) {
    assert.equal(parseLocalCommand(text, 'en', []), null, text + ' should reach the agent');
  }
});
