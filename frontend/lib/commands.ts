/** Shared parser for typed and spoken local commands. Greek is opt-in; English
 * remains available in every language. Captures always retain the user's text. */
import type { NotepadCommand } from "./notepad";
import type { WindowArrangement } from "./windows";

/* The *_all actions are bulk: "restore all terminals" is one request about every
 * window of a kind, not one request per window. They exist on every window-ish
 * type (window, terminal, files, notepad) because the kinds are otherwise
 * asymmetric - the window parser has had *_all all along, so "restore all
 * windows" worked while "restore all terminals" did nothing. */
export type BulkWindowAction = "close_all" | "minimize_all" | "maximize_all" | "restore_all";

export type LocalCommand =
  | ({
      type: "window";
      action: "open" | "close" | "focus" | "maximize" | "minimize"
        | "restore" | "arrange" | "next" | "previous" | "list" | "note" | BulkWindowAction;
      target?: number;
      arrangement?: WindowArrangement;
      note?: string;
    })
  | ({ type: "terminal"; action: "open" | "close" | "focus" | "minimize" | "maximize" | "restore" | BulkWindowAction; target?: number; create?: boolean; count?: number })
  | ({ type: "files"; action: "open" | "close" | "focus" | "minimize" | "maximize" | "restore" | BulkWindowAction; create?: boolean })
  | NotepadCommand
  | { type: "desktop"; action: "switch" | "next" | "previous" | "move"; desktop: number; target?: number; targets?: number[]; terminals?: boolean }
  | { type: "cancelTimers" }
  | { type: "cancelReminders" }
  | { type: "timer"; name: string; seconds: number }
  | { type: "reminder"; name: string; fireAt: number }
  | { type: "operator"; name?: string }
  | { type: "autonomy"; enabled: boolean }
  | { type: "silence" }
  | { type: "images"; query: string; source: "web" | "local" }
  /* The chat panel itself: open/close, which tab is showing, whether it fills
     the screen width (`maximize`/`normalize`), and the two input commands.
     `write` only fills the box, `send` only delivers it, so the operator can
     compose first and commit second. */
  | { type: "panel"; action: "open" | "close" | "toggle" | "maximize" | "normalize"; tab?: PanelTabName }
  | { type: "chatinput"; action: "write" | "send"; text?: string }
  | { type: "lock"; action: "lock" }
  | { type: "signout" }
  /* Scheduled tasks. `list`/`show`/`run`/`pause`/`resume`/`delete` are
     deterministic - the operator asked for one of five things to happen to a
     numbered row, so spending a model turn on it is pure latency. `target` is
     the number as spoken (or -1 for "the last one"), which is the same index
     the TASKS tab shows. Creation is deliberately NOT here: "every morning
     check the disk" has to reach the agent, which turns it into a schedule. */
  | { type: "task"; action: "list" | "show" | "run" | "pause" | "resume" | "delete" | "open";
      target?: number; filter?: "all" | "running" | "paused" | "enabled" | "error" }
  | { type: "skill"; skill: string; rest: string };


const isGreek = (language: string) => /^el(?:-|$)/i.test(language);
const normalize = (text: string) => text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().replace(/ς/g, "σ");

// Normalization changes offsets for decomposed accents. Map slices back to the
// original string so names, image searches and skill instructions stay intact.
function originalSlice(source: string, start: number, end?: number): string {
  const starts: number[] = [];
  const ends: number[] = [];
  let offset = 0;
  for (const char of source) {
    const part = normalize(char);
    for (let i = 0; i < part.length; i++) {
      starts.push(offset);
      ends.push(offset + char.length);
    }
    if (!part && ends.length) ends[ends.length - 1] = offset + char.length;
    offset += char.length;
  }
  return source.slice(starts[start] ?? source.length, end === undefined ? source.length : (ends[end - 1] ?? 0));
}

function afterPrefix(source: string, pattern: RegExp): string | null {
  const match = normalize(source).match(pattern);
  return match ? originalSlice(source, match[0].length).trim() : null;
}

const EN_NUMBERS: Record<string, number> = {
  zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7,
  eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, thirteen: 13,
  fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18,
  nineteen: 19, twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60,
};
const EL_NUMBERS: Record<string, number> = {
  μηδεν: 0, ενασ: 1, ενα: 1, μια: 1, δυο: 2, τρεισ: 3, τρια: 3,
  τεσσερισ: 4, τεσσερα: 4, πεντε: 5, εξι: 6, επτα: 7, εφτα: 7,
  οκτω: 8, οχτω: 8, εννεα: 9, εννια: 9, δεκα: 10, ενδεκα: 11,
  δωδεκα: 12, δεκατρεισ: 13, δεκατρια: 13, δεκατεσσερισ: 14,
  δεκατεσσερα: 14, δεκαπεντε: 15, δεκαεξι: 16, δεκαεπτα: 17,
  δεκαεφτα: 17, δεκαοκτω: 18, δεκαοχτω: 18, δεκαεννεα: 19,
  δεκαεννια: 19, εικοσι: 20, τριαντα: 30, σαραντα: 40, πενηντα: 50,
  εξηντα: 60, μιση: 0.5, μισο: 0.5, εναμιση: 1.5,
  μιαμιση: 1.5, εναμισι: 1.5,
};

function parseNumber(text: string, greek: boolean): number | null {
  const clean = normalize(text).trim();
  if (/^\d+$/.test(clean)) {
    const n = Number(clean);
    return Number.isSafeInteger(n) ? n : null;
  }
  const numbers = greek ? { ...EN_NUMBERS, ...EL_NUMBERS } : EN_NUMBERS;
  if (numbers[clean] !== undefined) return numbers[clean];
  const parts = clean.split(/[\s-]+/);
  if (parts.length === 2 && numbers[parts[0]] >= 20 && numbers[parts[0]] % 10 === 0
      && numbers[parts[1]] > 0 && numbers[parts[1]] < 10) {
    return numbers[parts[0]] + numbers[parts[1]];
  }
  return null;
}

function parseDuration(text: string, greek: boolean): number | null {
  const clean = normalize(text).trim();
  const units = greek
    ? /(?:hours?|minutes?|seconds?|ωρα|ωρεσ|λεπτο|λεπτα|δευτερολεπτο|δευτερολεπτα)(?=$|[\s,])/g
    : /(?:hours?|minutes?|seconds?)(?=$|[\s,])/g;
  let offset = 0;
  let total = 0;
  let count = 0;
  for (const match of clean.matchAll(units)) {
    let amount = clean.slice(offset, match.index).trim();
    if (count) amount = amount.replace(greek ? /^(?:,\s*(?:(?:and|και)\s+)?|(?:and|και)\s+)/ : /^(?:,\s*(?:and\s+)?|and\s+)/, "");
    const value = parseNumber(amount, greek);
    if (value === null) return null;
    const unit = match[0];
    const multiplier = /^(?:hour|ωρ)/.test(unit) ? 3600 : /^(?:minute|λεπτ)/.test(unit) ? 60 : 1;
    total += value * multiplier;
    offset = match.index! + unit.length;
    count++;
  }
  return count && !clean.slice(offset).trim() && Number.isSafeInteger(total) && total > 0 ? total : null;
}

function parseClock(text: string, greek: boolean, now: number): number | null {
  const clean = normalize(text).trim();
  const match = clean.match(greek
    ? /^(\d{1,2})(?::(\d{2}))?\s*(am|pm|π\.?\s*μ\.?|μ\.?\s*μ\.?)?$/
    : /^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/);
  if (!match || (!match[2] && !match[3])) return null;
  let hour = Number(match[1]);
  const minute = Number(match[2] || 0);
  const suffix = match[3]?.replace(/[.\s]/g, "");
  if (minute > 59 || (suffix ? hour < 1 || hour > 12 : hour > 23)) return null;
  if (suffix) {
    hour %= 12;
    if (suffix === "pm" || suffix === "μμ") hour += 12;
  }
  const current = new Date(now);
  const target = new Date(current.getFullYear(), current.getMonth(), current.getDate(), hour, minute, 0, 0);
  if (target.getTime() <= now) target.setDate(target.getDate() + 1);
  return Number.isFinite(target.getTime()) ? target.getTime() : null;
}

function parseTimer(text: string, greek: boolean): LocalCommand | null {
  let body = afterPrefix(text, /^(?:(?:set|start|create)\s+(?:a\s+)?)?(?:timer|countdown)\s+(?:for\s+)?/);
  if (body === null && greek) body = afterPrefix(text, /^(?:(?:βαλε|ορισε|ξεκινα|ξεκινησε|δημιουργησε|κανε)\s+(?:(?:ενα|μια)\s+)?(?:μου\s+)?)?(?:χρονομετρο|αντιστροφη\s+μετρηση)\s+(?:για\s+)?/);
  if (!body) return null;
  const seconds = parseDuration(body, greek);
  if (seconds) return { type: "timer", name: greek ? "Χρονόμετρο" : "Timer", seconds };
  // Named timers use an explicit separator; arbitrary prose containing a
  // duration must not accidentally start a timer.
  const separators = greek ? /\s+(?:named|called|for|με\s+ονομα|για)\s+/g : /\s+(?:named|called|for)\s+/g;
  for (const match of normalize(body).matchAll(separators)) {
    const duration = originalSlice(body, 0, match.index);
    const name = originalSlice(body, match.index! + match[0].length).trim();
    const amount = parseDuration(duration, greek);
    if (amount && name) return { type: "timer", name, seconds: amount };
  }
  return null;
}

function parseReminder(text: string, greek: boolean, now: number): LocalCommand | null {
  let body = afterPrefix(text, /^(?:remind\s+me|(?:(?:add|set|create)\s+(?:a\s+)?)?reminder)\s+/);
  if (body === null && greek) body = afterPrefix(text, /^(?:(?:θυμισε|θυμησε|υπενθυμισε)\s+μου|(?:(?:βαλε|ορισε|προσθεσε|δημιουργησε|κανε)\s+(?:(?:μια|ενα)\s+)?(?:μου\s+)?)?υπενθυμιση)\s+/);
  if (!body) return null;
  const resolve = (time: string, relative: boolean) => {
    if (!relative) return parseClock(time, greek, now);
    const seconds = parseDuration(time, greek);
    const target = seconds === null ? NaN : now + seconds * 1000;
    return Number.isFinite(new Date(target).getTime()) ? target : null;
  };
  const timePrefix = normalize(body).match(greek ? /^(in|at|σε|στισ|στη|στην)\s+/ : /^(in|at)\s+/);
  if (timePrefix) {
    const relative = timePrefix[1] === "in" || timePrefix[1] === "σε";
    const rest = originalSlice(body, timePrefix[0].length);
    const fireAt = resolve(rest, relative);
    if (fireAt !== null) return { type: "reminder", name: greek ? "Υπενθύμιση" : "Reminder", fireAt };
    for (const match of normalize(rest).matchAll(greek ? /\s+(?:to|να)\s+/g : /\s+to\s+/g)) {
      const time = originalSlice(rest, 0, match.index);
      const name = originalSlice(rest, match.index! + match[0].length).trim();
      const target = resolve(time, relative);
      if (target !== null && name) return { type: "reminder", name, fireAt: target };
    }
    return null;
  }
  body = afterPrefix(body, greek ? /^(?:to|να)\s+/ : /^to\s+/) ?? body;
  for (const match of normalize(body).matchAll(greek ? /\s+(in|at|σε|στισ|στη|στην)\s+/g : /\s+(in|at)\s+/g)) {
    const name = originalSlice(body, 0, match.index).trim();
    const time = originalSlice(body, match.index! + match[0].length);
    const fireAt = resolve(time, match[1] === "in" || match[1] === "σε");
    if (fireAt !== null && name) return { type: "reminder", name, fireAt };
  }
  return null;
}

const SKILL_ALIASES: Record<string, string[]> = {
  general: ["γενικα", "γενικη", "γενική βοήθεια", "βοήθεια", "γενικός βοηθός"],
  code: ["κωδικασ", "κωδικα", "προγραμματισμοσ", "προγραμματισμο", "προγραμματιστής"],
  research: ["ερευνα", "ερευνητησ", "ερευνητη", "μελέτη", "μελετη"],
  translator: ["μεταφραστησ", "μεταφραστη", "μεταφραση"],
  obsidian: ["σημειωσεισ", "οψιδιανοσ", "οψιδιανο", "σημειωματάριο", "σημειωματαριο"],
  shell: ["τερματικο", "κελυφοσ", "κονσόλα", "κονσολα"],
  skill_creator: ["δημιουργοσ δεξιοτητων", "δημιουργο δεξιοτητων", "δημιουργια δεξιοτητων"],
  file_search: ["αναζητηση αρχειων", "αρχεία", "αρχεια", "ψάξε αρχεία", "ψαξε αρχεια"],
  editor: ["συντακτησ", "συντακτη", "επεξεργαστησ εγγραφων", "επεξεργαστη εγγραφων", "εγγραφα", "επεξεργαστής", "επεξεργαστη", "word", "excel"],
};

function parseSkill(text: string, greek: boolean, skills: Array<{ name: string }>): LocalCommand | null {
  let body = afterPrefix(text, /^(?:use|switch\s+to|activate|enable)\s+(?:the\s+)?(?:skill\s+)?/);
  if (body === null && greek) body = afterPrefix(text, /^(?:χρησιμοποιησε|ενεργοποιησε|επιλεξε|αλλαξε\s+σε|μεταβαση\s+σε)\s+(?:(?:τη|την|το|τον)\s+)?(?:δεξιοτητα\s+)?/);
  if (body === null) return null;
  const names = skills.flatMap((skill) => [skill.name, ...(greek ? SKILL_ALIASES[skill.name.toLowerCase()] ?? [] : [])]
    .map((alias) => ({ name: normalize(alias), skill: skill.name }))).sort((a, b) => b.name.length - a.name.length);
  const normalized = normalize(body);
  for (const { name, skill } of names) {
    if (normalized.startsWith(name) && (!normalized[name.length] || /^[\s,.:;!?—–-]$/.test(normalized[name.length]))) {
      const rest = originalSlice(body, name.length)
        .replace(/^[\s,.:;!?—–-]+/, "")
        .replace(/^(?:and|και)\s+/i, "")
        .trim();
      return { type: "skill", skill, rest };
    }
  }
  return null;
}

function parseImages(text: string, greek: boolean): LocalCommand | null {
  let source: "web" | "local" = "web";
  const imagePrefix = /^(?:show|open|browse|search|find)\s+(?:me\s+)?(?:all\s+)?(?:(my|local|web)\s+)?(?:the\s+)?(?:an?\s+)?(?:image\s+)?(?:browser|gallery|images?|pictures?|pics?|photos?)(?=$|\s)/;
  const elPrefix = /^(?:δειξε|ανοιξε|προβαλε|εμφανισε|αναζητησε|ψαξε|βρεσ)\s+(?:μου\s+)?(?:ολεσ\s+)?(?:(?:τισ|την|τη|το|μια|μιαν)\s+)?(?:(τοπικεσ|διαδικτυακεσ|τοπικη|διαδικτυακη)\s+)?(?:εικονεσ|εικονα|φωτογραφιεσ|φωτογραφια|συλλογη\s+εικονων|γκαλερι)(?=$|\s)/;
  const prefix = normalize(text).match(imagePrefix) ?? (greek ? normalize(text).match(elPrefix) : null);
  if (!prefix) return null;
  if (prefix[1]) source = /^(my|local|τοπικεσ|τοπικη)$/.test(prefix[1]) ? "local" : "web";
  let query = originalSlice(text, prefix[0].length).trim();
  if (greek && normalize(query) === "μου") return { type: "images", query: "", source: "local" };
  if (greek && normalize(query).startsWith("μου ")) {
    source = "local";
    query = originalSlice(query, 4).trim();
  }
  // Source qualifiers at the end are removed from the search query.
  const sourceSuffix = normalize(` ${query}`).match(greek
    ? /\s+(from\s+my\s+(?:pc|computer|folder|pictures)|on\s+my\s+computer|my\s+(?:folder|computer|pictures)|local|from\s+(?:the\s+)?web|online|απο\s+(?:τον?\s+)?υπολογιστη\s+μου|απο\s+(?:τον?\s+)?φακελο\s+μου|απο\s+τισ\s+φωτογραφιεσ\s+μου|τοπικα|απο\s+το\s+διαδικτυο|στο\s+διαδικτυο)$/
    : /\s+(from\s+my\s+(?:pc|computer|folder|pictures)|on\s+my\s+computer|my\s+(?:folder|computer|pictures)|local|from\s+(?:the\s+)?web|online)$/);
  if (sourceSuffix) {
    source = /(?:web|online|διαδικτυο)$/.test(sourceSuffix[1]) ? "web" : "local";
    query = originalSlice(` ${query}`, 0, sourceSuffix.index).trim();
  }
  query = afterPrefix(query, greek ? /^(?:of|for|για|με|απο)\s+/ : /^(?:of|for)\s+/) ?? query;
  return { type: "images", query, source };
}

/* ---------- window commands ---------- */

const EN_ORDINALS: Record<string, number> = {
  first: 1, second: 2, third: 3, fourth: 4, fifth: 5, sixth: 6, seventh: 7, eighth: 8, ninth: 9, tenth: 10,
};
const EL_ORDINALS: Record<string, number> = {
  πρωτο: 1, πρωτη: 1, δευτερο: 2, δευτερη: 2, τριτο: 3, τριτη: 3,
  τεταρτο: 4, τεταρτη: 4, πεμπτο: 5, πεμπτη: 5, εκτο: 6, εκτη: 6,
  εβδομο: 7, εβδομη: 7, ογδοο: 8, ογδοη: 8, ενατο: 9, ενατη: 9, δεκατο: 10, δεκατη: 10,
};

/* Consume a window index written as "#N", "N", an English ordinal/number word
 * or a Greek ordinal word; return the target and the consumed length so note
 * text following it can be sliced back to the original casing. */
function consumeWindowTarget(norm: string, greek: boolean): { index: number; consumed: number } | null {
  let s = norm;
  let used = 0;
  const lead = s.match(/^(?:the\s+|a\s+|το\s+|τη\s+|την\s+|η\s+|ο\s+|στο\s+|στη\s+|στην\s+)/);
  if (lead) { used += lead[0].length; s = s.slice(lead[0].length); }
  const leadWin = s.match(/^(?:window\s+|windows\s+|παραθυρο\s+|παραθυρα\s+)/);
  if (leadWin) { used += leadWin[0].length; s = s.slice(leadWin[0].length); }
  const numWord = s.match(/^(?:number\s+|αριθμοσ\s+|αριθμο\s+)/);
  if (numWord) { used += numWord[0].length; s = s.slice(numWord[0].length); }
  let index: number | null = null;
  let token = "";
  const digits = s.match(/^#?(\d{1,2})(?=$|[\s.,:;!?·;#'-])/);
  if (digits) {
    index = Number(digits[1]);
    token = digits[0];
  } else {
    const dict: Record<string, number> = { ...EN_ORDINALS, ...EN_NUMBERS, ...(greek ? EL_ORDINALS : {}) };
    const keys = Object.keys(dict).sort((a, b) => b.length - a.length);
    for (const key of keys) {
      if (s.startsWith(key) && (s.length === key.length || /[\s.,:;!?·;#'-]/.test(s[key.length]))) {
        index = dict[key];
        token = key;
        break;
      }
    }
  }
  if (index === null) return null;
  used += token.length;
  return { index, consumed: used };
}

function arrangementFromWord(word: string | undefined, greek: boolean): WindowArrangement {
  const w = (word || "").trim().toLowerCase();
  if (/grid|tile|πλεγμα|τετραγωνα|πλεγματα/.test(w)) return "grid";
  if (/side\s*[- ]?by\s*[- ]?side|column|στηλεσ|vertical|vertically|διπλα\s+διπλα/.test(w)) return "tile-v";
  if (/stack|row|γραμμεσ|stacked|horizontal|horizontally/.test(w)) return "tile-h";
  if (/centre?d?|center|central|κεντρο/.test(w)) return "center";
  return "cascade";
}

function parseWindowCommand(text: string, greek: boolean): LocalCommand | null {
  const clean = text.trim().replace(/[.!?;·;]+$/, "").trim();
  if (!clean) return null;
  const normalized = normalize(clean);

  // close all windows
  if (/^(?:close|hide|dismiss|shut)\s+(?:(?:all|every|the)\s+)?windows$/.test(normalized)
      || /^close\s+all$/.test(normalized)
      || (greek && /^(?:κλεισε|κρυψε|αποκρυψε)\s+(?:(?:ολα|ολα\s+τα|τα)\s+)?παραθυρα$/.test(normalized)))
    return { type: "window", action: "close_all" };

  // minimize / restore all windows
  if (/^(?:minim(?:ize|ise)|shrink|collapse)\s+(?:(?:(?:all|every|the)\s+)+)?windows?$/.test(normalized)
      || (greek && /^(?:ελαχιστοποιησε|μικρυνε|σμικρυνε|κρυψε)\s+(?:(?:ολα|ολα\s+τα|τα)\s+)?παραθυρα$/.test(normalized)))
    return { type: "window", action: "minimize_all" };
  if (/^(?:restore|bring\s+back|return)\s+(?:(?:(?:all|every|the)\s+)+)?windows?$/.test(normalized)
      || /^restore\s+all(?:\s+windows)?$/.test(normalized)
      || (greek && /^(?:επαναφερε|κανονικοποιησε)\s+(?:(?:ολα|ολα\s+τα|τα)\s+)?παραθυρα$/.test(normalized)))
    return { type: "window", action: "restore_all" };
  // "maximize all windows" - the one bulk action this parser never had, because
  // the hand-written forms above cover only the three that existed when they
  // were written. The shared matcher keeps it from drifting again.
  {
    const bulk = bulkWindowAction(
      normalized,
      "windows?|παραθυρα?|παραθυρο",
      "windows|παραθυρα",
    );
    if (bulk === "maximize_all") return { type: "window", action: bulk };
  }

  // arrange windows
  const arrangeEn = normalized.match(/^arrange\s+(?:(?:the|all)\s+)?windows?(?:\s+(?:in|as|in\s+a)\s+)?([\p{L}\s-]+)?$/u);
  if (arrangeEn) return { type: "window", action: "arrange", arrangement: arrangementFromWord(arrangeEn[1], greek) };
  if (greek) {
    const arrangeEl = normalized.match(/^(?:ταξινομησε|διαταξε|κανονισε|στοιχισε)\s+(?:τα\s+|τισ\s+)?(?:ανοιχτα\s+)?παραθυρα(?:\s+(?:σε|σε\s+στυλ)\s+)?([\p{L}\s-]+)?$/u);
    if (arrangeEl) return { type: "window", action: "arrange", arrangement: arrangementFromWord(arrangeEl[1], greek) };
  }
  // bare arrangement words: "cascade the windows", "grid", "stack", "side by side", "center"
  const bareArr = normalized.match(/^(cascade|grid|stack(?:ed)?|side\s*[- ]?by\s*[- ]?side|tile|center|centre)(?:\s+(?:the\s+)?(?:windows?))?$/)
    || (greek && normalized.match(/^(κασκαντα|πλεγμα|πλεγματα|στοιβα|στοιβαγμενα|διπλα\s+διπλα|κεντρο|κεντραρισμενα)(?:\s+(?:τα\s+|τισ\s+)?(?:παραθυρα))?$/));
  if (bareArr) return { type: "window", action: "arrange", arrangement: arrangementFromWord(bareArr[1], greek) };

  // list windows
  if (/^(?:list|show|count|enumerate)(?:\s+me)?\s+(?:the\s+)?(?:open\s+)?windows?$/.test(normalized)
      || (greek && /^(?:λιστε|δειξε|μετρησε|απαριθμησε)\s+(?:τα\s+|τισ\s+)?(?:ανοιχτα\s+)?παραθυρα$/.test(normalized)))
    return { type: "window", action: "list" };

  // next / previous window (or image/photo, kept for parity with the old preview)
  if (/^(?:next|forward)(?:\s+(?:window|image|photo|picture|one|page))?$/.test(normalized)
      || (greek && /^(?:επομενο|επομενη|μπροστα)(?:\s+(?:παραθυρο|εικονα|φωτογραφια|σελιδα|εγγραφο))?$/.test(normalized)))
    return { type: "window", action: "next" };
  if (/^(?:previous|back|last|prev|earlier)(?:\s+(?:window|image|photo|picture|one|page))?$/.test(normalized)
      || (greek && /^(?:προηγουμενο|προηγουμενη|πισω)(?:\s+(?:παραθυρο|εικονα|φωτογραφια|σελιδα|εγγραφο))?$/.test(normalized)))
    return { type: "window", action: "previous" };

  // targeted actions: close / focus / maximize / minimize / restore
  const referent = greek
    ? "(?:window|preview|image|document|view|that|it|one|terminal|παραθυρο|προεπισκοπηση|εικονα|εγγραφο|αυτο|φωτογραφια|τερματικο)"
    : "(?:window|preview|image|document|view|that|it|one|terminal)";
  const verbs: Array<["close" | "focus" | "maximize" | "minimize" | "restore", string, string]> = [
    ["close", "close|hide|dismiss|shut", "κλεισε|κρυψε|αποκρυψε"],
    ["focus", "focus|select|go\\s+to|switch\\s+to|bring\\s+up|bring\\s+forward", "εστιασε|επιλεξε|φερε|δειξε"],
    ["maximize", "maxim(?:ize|ise)|expand|enlarge|full[-\\s]?screen", "μεγιστοποιησε|μεγεθυνε|επεκτεινε|πληρησ?\\s+οθονη"],
    ["minimize", "minim(?:ize|ise)|shrink", "ελαχιστοποιησε|μικρυνε|σμικρυνε"],
    ["restore", "restore|normali(?:ze|ise)|back\\s+to\\s+normal", "επαναφερε|κανονικοποιησε"],
  ];
  for (const [action, en, el] of verbs) {
    const rest = afterPrefix(clean, new RegExp(`^(?:${en}${greek ? `|${el}` : ""})(?:\\s+(?:(?:the|on|to|at)\\s+)?(?:window\\s+)?)?`));
    if (rest === null) continue;
    const nRest = normalize(rest);
    if (!nRest) return { type: "window", action };
    const bare = nRest.replace(/^(?:the\s+|a\s+|το\s+|τη\s+|την\s+|η\s+|ο\s+)/, "").trim();
    if (new RegExp(`^(?:${referent})$`).test(bare)) return { type: "window", action };
    const target = consumeWindowTarget(nRest, greek);
    if (target) return { type: "window", action, target: target.index };
  }

  // notes: "add a note to the second window saying remember this"
  const notePrefix = greek
    ? /^(?:βαλε|προσθεσε|γραψε)\s+(?:μια\s+|ενα\s+)?(?:σημειωση|σημειωματα|σημειωμα)\s+(?:στο|στην|σε|πανω\s+σε)\s+/
    : /^(?:add\s+(?:a\s+)?|put\s+(?:a\s+)?|write\s+(?:a\s+)?)?note\s+(?:to|on)\s+/;
  const noteRest = afterPrefix(clean, notePrefix);
  if (noteRest !== null) {
    const nRest = normalize(noteRest);
    const target = consumeWindowTarget(nRest, greek);
    let start = 0;
    if (target) {
      start = target.consumed;
      const tail = nRest.slice(start).match(/^\s*(?:window|windows|παραθυρο|παραθυρα)(?=\s|$)/);
      if (tail) start += tail[0].length;
      const sep = nRest.slice(start).match(/^(?:\s*(?:saying|to\s+say|να\s+λεει|λεει)\s+|[:,\-\s]+)/);
      if (sep) start += sep[0].length;
    } else {
      const sep = nRest.slice(start).match(/^(?:saying\s+|to\s+say\s+|να\s+λεει\s+|λεει\s+|[:,\-\s]+)/);
      if (!sep) return null;
      start += sep[0].length;
    }
    const note = originalSlice(noteRest, start).trim();
    return { type: "window", action: "note", target: target ? target.index : undefined, note };
  }

  return null;
}

/* ---------- bulk window actions ----------
 *
 * "restore all terminals" fits none of the single-target parsers. The verb is
 * followed by a quantifier and a plural noun; the single-target branches need
 * either a number or an empty tail, so nothing matched, the utterance fell
 * through to the agent, and nothing happened - the agent has no tool that can
 * un-minimize a window either. That is the whole bug: a phrase with a missing
 * regex slot, not a missing capability.
 *
 * A request is BULK when a quantifier is present ("all", "every", "both",
 * "όλα τα", "μόνα") OR when the noun is plural on its own ("τα τερματικά").
 * A singular noun with no quantifier is deliberately NOT bulk, so "restore
 * terminal" keeps meaning the one focused window exactly as before.
 *
 * All three window-ish parsers need this identically, so it is one function
 * rather than three regex families that drift apart - the drift is what let the
 * window parser keep its *_all forms while terminal and files lost theirs. */

/** Fold a vocabulary written as a person writes it, so the patterns match.
 *
 * `normalize()` strips accents and folds the final sigma (`ς` → `σ`), so a word
 * spelled the way it is actually said never matches a pattern that spells it out
 * by hand: "κλείσε τις κονσόλες" arrives here as "κλεισε τισ κονσολεσ", and an
 * alternation containing "τις" or "κονσόλες" cannot match it. "όλους" and "τους"
 * had been in these lists for months in a spelling that could never fire. Every
 * vocabulary is written normally and folded once, here, instead of being
 * remembered in its folded form - remembering is the bug. */
const fold = (words: string) => words.split("|").map(normalize).join("|");
const ALL_WORDS = fold("all|every|each|both|those|όλα|όλες|όλους|όλων|μόνα|καθε");

/* Determiners that may sit between the quantifier and the noun ("all the
   terminals", "όλα τα τερματικά"). Longest alternative first on purpose: an
   ordered alternation is tried left to right, and "την" must not be consumed as
   "τη" with a stray "ν" left for the noun. "τις" is here because the accusative
   plural article is the most ordinary Greek phrasing there is: "κλείσε τις
   κονσόλες" is not exotic. */
const DETERMINERS = fold("των|τους|τις|την|τη|τα|το|τον|οι|ο|η|στις|στον|στο|στη|στην|those|the|my|them");

/** Verbs per bulk action, English and Greek in one alternation. `show` is
 *  deliberately absent: for a notepad "show" already means focus, and for a
 *  terminal it is ambiguous with focusing. `restore` verbs are unambiguous. */
const BULK_VERBS: Array<[BulkWindowAction, string]> = [
  ["close_all", "close|shut|dismiss|kill|terminate|κλείσε|κρύψε|απόκρυψε|σταμάτα|σταμάτησε|τερμάτισε"],
  ["minimize_all", "minim(?:ize|ise)|shrink|ελαχιστοποίησε|μικρύνε|σμίκρυνε"],
  ["maximize_all", "maxim(?:ize|ise)|expand|enlarge|full[-\\s]?screen|μεγιστοποίησε|μεγέθυνε|επεκτείνε"],
  ["restore_all", "restore|normali(?:ze|ise)|bring\\s+back|unminimiz(?:e|ise)|unmaximiz(?:e|ise)"
    + "|επαναφέρε|κανονικοποίησε|επαναφορά|ξαναφέρε"],
];

/** Match a bulk request for one noun, or return null.
 *
 * `noun` is every spelling the noun has in both languages; `plural` is only the
 * subset that is plural by itself, because Greek plurals are not derivable from
 * the singular the way English "terminals" is. Returns the action only when a
 * quantifier was actually spoken or the noun was spoken in the plural, so
 * returning null is the normal "this was not a bulk request" answer.
 *
 * `extraRestoreVerbs` widens only the restore verb list, which is how "show all
 * terminals" reads as restore for a terminal or a file manager. It is not the
 * default because in the notepad parser a bare "show the notepad" has always
 * meant focus - widening there would change an existing meaning rather than add
 * one - and it is deliberately not a general widen: BULK_VERBS is ordered, so
 * widening every list would make "show all terminals" match close_all first. */
function bulkWindowAction(norm: string, noun: string, plural: string, extraRestoreVerbs = ""): BulkWindowAction | null {
  // Folded here too, so a caller cannot get it wrong by passing a noun in the
  // spelling a person would use. See `fold` above for why this is not optional.
  const isPlural = new RegExp(`^(?:${normalize(plural)})$`);
  for (const [action, verbsBase] of BULK_VERBS) {
    const verbs = action === "restore_all" && extraRestoreVerbs
      ? `(?:${normalize(verbsBase)})|(?:${normalize(extraRestoreVerbs)})`
      : normalize(verbsBase);
    const m = norm.match(new RegExp(
      `^(?:${verbs})\\s+` +
      `((?:${ALL_WORDS})\\s+)?` + // 1: the quantifier, which forces bulk
      `(?:(?:${DETERMINERS})\\s*)?` +
      `(${normalize(noun)})` + // 2: the noun in any of its forms
      `(?:\\s+(?:windows?|παραθυρα?))?` + // "all terminal windows"
      `\\s*$`,
    ));
    if (m && (m[1] || isPlural.test(m[2]))) return action;
  }
  return null;
}

/* ---------- terminal commands ---------- */

/* "open terminal [2]", "close terminal", "focus on terminal 2", with Greek
 * equivalents. Runs before the generic window parse so "terminal N" targets a
 * terminal window (matched by its ordinal among terminals), not window N. */
const EN_ORD = "(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)";
const EL_ORD = "(?:πρωτο|πρωτη|δευτερο|δευτερη|τριτο|τριτη|τεταρτο|τεταρτη|πεμπτο|πεμπτη|εκτο|εκτη|εβδομο|εβδομη|ογδοο|ογδοη|ενατο|ενατη|δεκατο|δεκατη)";

function parseTerminalCommand(text: string, greek: boolean): LocalCommand | null {
  const clean = text.trim().replace(/[.!?;·;]+$/, "").trim();
  if (!clean) return null;
  const norm = normalize(clean);
  const ordDict = greek ? EL_ORDINALS : EN_ORDINALS;
  const ordSlot = greek
    ? `(?:(?:(${EL_ORD})|καινουργιο|νεο|αλλο)\\s+)?`
    : `(?:(${EN_ORD})\\s+)?`;
  const ordinalOf = (m: RegExpMatchArray): number | undefined => (m[1] ? ordDict[m[1]] : undefined);
  // Trailing "terminal 2", "terminal one" targets; "" yields undefined.
  const trailingOf = (rest: string): number | undefined => {
    const t = consumeWindowTarget(rest, greek);
    return t ? t.index : NaN;
  };
  const finish = (rest: string, ordinal: number | undefined, action: "open" | "close" | "focus" | "minimize" | "maximize" | "restore", createOpt: {}): LocalCommand | null => {
    const target = ordinal ?? (rest ? trailingOf(rest) : undefined);
    if (target !== undefined && !Number.isNaN(target)) return { type: "terminal", action, target, ...createOpt };
    if (rest.trim() !== "") return null;
    return { type: "terminal", action, ...createOpt };
  };

  // "close all terminals", "restore every terminal", "επαναφέρε όλα τα τερματικά".
  // Checked before the single-target branches: none of them can match a
  // quantifier, and a bare plural ("τα τερματικά") reads as "all of them".
  const bulk = bulkWindowAction(
    norm,
    "terminal|terminals|console|consoles|τερματικό|τερματικά|τερματικές|τερματικούς|τερματικών|κονσόλα|κονσόλες",
    "terminals|consoles|τερματικά|τερματικές|τερματικούς|τερματικών|κονσόλες",
    "show|δείξε|εμφάνισε",
  );
  if (bulk) return { type: "terminal", action: bulk };

  // "open 4 terminals" / "open four terminals" / "άνοιξε 4 τερματικά".
  // A PLURAL terminal with a count always means "open N of them" and never
  // "target terminal N", so this is checked before the single-target patterns.
  // Plurality is what signals a count, so "open one terminal" and "open
  // terminal 2" keep their old meaning (one window / a target).
  const mCount = norm.match(new RegExp(
    // "open 3 terminals" / "άνοιξε 3 τερματικά"
    `^(?:open|start|launch|spawn|ανοιξε|ξεκινα|ξεκινησε|εναρξη|δημιουργησε)\\s+` +
    `(?:(?:a|the|another|new|ενα|ενα ακομα|ακομα ενα|μια)\\s+)*` +
    `(\\d+|[a-zα-ω]+)\\s+(?:(?:new|windows?|παραθυρα?)\\s+)*(?:terminals|τερματικα?|τερματικου)(?![a-zα-ω])` +
    // The same request with the count last: "open 3 terminal windows". The old
    // pattern had this alternative without a verb, so it could only ever match
    // a sentence that *began* with a number.
    `|(?:open|start|launch|spawn|ανοιξε|ξεκινα|ξεκινησε|εναρξη|δημιουργησε)\\s+` +
    `(?:(?:a|the|another|new|ενα|ενα ακομα|ακομα ενα|μια)\\s+)*` +
    `(\\d+|[a-zα-ω]+)\\s+(?:terminal|τερματικο)\\s+(?:windows?|παραθυρα?)(?![a-zα-ω])`));
  if (mCount) {
    const raw = mCount[1] ?? mCount[2];
    const num = /^\d+$/.test(raw) ? Number(raw) : (greek ? EL_NUMBERS : EN_NUMBERS)[raw];
    // An unknown word ("open a terminals") is not a count: fall through.
    if (num !== undefined && num >= 1) {
      /* Anchored to the whole sentence. This branch is the one place in this
         parser that used to match a *prefix* and return, so "open 3 terminals
         one notepad and a file manager" was read as "open 3 terminals" and the
         rest of the sentence was silently dropped - the operator got two
         thirds of what they asked for, with no error anywhere. Every other
         branch here goes through `finish`, which declines when text remains and
         hands the turn to the reasoning layer instead. */
      if (norm.slice(mCount[0].length).trim() !== "") return null;
      return { type: "terminal", action: "open", create: true, count: Math.min(num, 10) };
    }
  }

  const mOpen = norm.match(new RegExp(
    `^(?:open|start|launch|spawn)\\s+(?:(?:(?:a|the|another)\\s+)?new\\s+|(?:a|the|another)\\s+)?${ordSlot}terminal\\s*`)) as RegExpMatchArray | null;
  const mOpenEl = greek && !mOpen ? norm.match(new RegExp(
    `^(?:ανοιξε|ξεκινα|ξεκινησε|εναρξη|δημιουργησε)\\s+(?:(?:ενα ακομα|ακομα ενα|ενα|το|τη|την|μια)\\s+)?${ordSlot}τερματικο\\s*`)) as RegExpMatchArray | null : null;
  const openM = mOpen ?? mOpenEl;
  if (openM) {
    // "open NEW/ANOTHER terminal" must always open an additional window, never
    // focus an existing one (Greek: νέο, καινούργιο, άλλο, ακόμα ένα).
    const create = greek
      ? /(?:νεο|καινουργιο|αλλο|ακομα)(?=\s|$)/.test(norm)
      : /\b(?:new|another)\b/i.test(norm);
    return finish(norm.slice(openM[0].length), ordinalOf(openM), "open", create ? { create: true } : {});
  }

  const mClose = norm.match(new RegExp(
    `^(?:close|shut|kill|terminate)\\s+(?:(?:the|this)\\s+)?${ordSlot}terminal\\s*`)) as RegExpMatchArray | null;
  const mCloseEl = greek && !mClose ? norm.match(new RegExp(
    `^(?:κλεισε|σταματα|σταματησε|τερματισε)\\s+(?:(?:το|τη|την)\\s+)?${ordSlot}τερματικο\\s*`)) as RegExpMatchArray | null : null;
  const closeM = mClose ?? mCloseEl;
  if (closeM) return finish(norm.slice(closeM[0].length), ordinalOf(closeM), "close", {});

  const mFocus = norm.match(new RegExp(
    `^(?:focus|select|go\\s+to|switch\\s+to)\\s+(?:(?:on|to|at)\\s+)?(?:(?:the)\\s+)?${ordSlot}terminal\\s*`)) as RegExpMatchArray | null;
  const mFocusEl = greek && !mFocus ? norm.match(new RegExp(
    `^(?:εστιασε|φερε|επιλεξε|μεταβα|πηγαινε)\\s+(?:(?:στο|στη|στην|σε|το)\\s+)?${ordSlot}τερματικο\\s*`)) as RegExpMatchArray | null : null;
  const focusM = mFocus ?? mFocusEl;
  if (focusM) return finish(norm.slice(focusM[0].length), ordinalOf(focusM), "focus", {});

  // minimize / maximize / restore a terminal
  const termActs: Array<["minimize" | "maximize" | "restore", string, string]> = [
    ["minimize", "minim(?:ize|ise)|shrink", "ελαχιστοποιησε|μικρυνε|σμικρυνε"],
    ["maximize", "maxim(?:ize|ise)|expand|enlarge|full[-\\s]?screen", "μεγιστοποιησε|μεγεθυνε|επεκτεινε|πληρησ?\\s+οθονη"],
    ["restore", "restore|normali(?:ze|ise)|back\\s+to\\s+normal", "επαναφερε|κανονικοποιησε"],
  ];
  for (const [action, en, el] of termActs) {
    const re = new RegExp(`^(?:${en})\\s+(?:(?:the|this)\\s+)?${ordSlot}terminal\\s*`);
    const reEl = greek ? new RegExp(`^(?:${el})\\s+(?:(?:το|τη|την)\\s+)?${ordSlot}τερματικο\\s*`) : null;
    const m = norm.match(re) ?? (reEl ? norm.match(reEl) : null);
    if (m) return finish(norm.slice(m[0].length), ordinalOf(m), action, {});
  }

  return null;
}

/* ---------- file-manager commands ---------- */

/* "open the file manager", "open new file manager", "show files", "close the
 * files window"; Greek equivalents. Runs before the generic window parse so
 * "focus file manager" targets the files window instead of being treated as an
 * unknown referent. "new"/"another" (νέο, άλλο, ακόμα ένα) always opens an
 * additional window instead of focusing the current one. */
function parseFilesCommand(text: string, greek: boolean): LocalCommand | null {
  const clean = text.trim().replace(/[.!?;·;]+$/, "").trim();
  if (!clean) return null;
  const norm = normalize(clean);
  const noun = greek
    ? "(?:διαχειριστησ\\s+αρχειων|φυλλομετρητησ\\s+αρχειων|διαχειριστη\\s+αρχειων|φυλλομετρητη\\s+αρχειων|παραθυρο\\s+αρχειων|αρχεια)"
    : "(?:file\\s+manager|file\\s+browser|file\\s+explorer|explorer|files?\\s+window|files)";
  const article = greek ? "(?:(?:το|τον|την|την|η|ο|ενα|μια|τα)\\s+)?" : "(?:(?:the|a|an)\\s+)?";
  const end = "(?=$|\\s*[.,!?])";
  const newSlot = greek ? "(?:(?:ενα\\s+ακομα|ακομα\\s+ενα|ενα\\s+νιο|νιο|νεο|καινουργιο|αλλο)\\s+)?" : "(?:(?:a\\s+|an\\s+|another\\s+)?(?:new|another)\\s+)?";

  // "close all file managers", "minimize every file browser", "κλείσε όλα τα
  // παράθυρα αρχείων". Same rule as the terminal parser: a quantifier or a
  // plural noun makes it bulk, anything else stays single-target.
  const bulk = bulkWindowAction(
    norm,
    "file\\s+managers?|file\\s+browsers?|file\\s+explorers?|explorers?|files?\\s+windows?|files"
    + "|διαχειριστής?\\s+αρχείων|φυλλομετρητής?\\s+αρχείων|παραθυρ(?:ο|α)\\s+αρχεί(?:ων|α)|αρχεία|αρχείων",
    "files|file\\s+managers|file\\s+browsers|file\\s+explorers|explorers"
    + "|διαχειριστές\\s+αρχείων|φυλλομετρητές\\s+αρχείων|παράθυρα\\s+αρχείων|παράθυρα\\s+αρχεία|αρχεία|αρχείων",
    "show|δείξε|εμφάνισε",
  );
  if (bulk) return { type: "files", action: bulk };

  const openEn = greek ? null : norm.match(new RegExp(`^(?:open|start|launch|browse|show|display)\\s+(?:me\\s+)?${newSlot}${article}${noun}${end}`));
  const openEl = greek && !openEn ? norm.match(new RegExp(`^(?:ανοιξε|ξεκινα|ξεκινησε|δειξε|εμφανισε|προβαλε)\\s+(?:μου\\s+)?${newSlot}${article}${noun}${end}`)) : null;
  if (openEn || openEl) {
    const span = norm.slice(0, (openEn ?? openEl)![0].length);
    const create = greek
      ? /(?:ακομα|νεο|νιο|καινουργιο|αλλο)(?=\s|$)/.test(span)
      : /\b(?:new|another)\b/.test(span);
    return create ? { type: "files", action: "open", create: true } : { type: "files", action: "open" };
  }

  const closeEn = greek ? null : norm.match(new RegExp(`^(?:close|hide|dismiss|shut)\\s+(?:(?:the|this)\\s+)?${noun}${end}`));
  const closeEl = greek && !closeEn ? norm.match(new RegExp(`^(?:κλεισε|κρυψε|αποκρυψε)\\s+(?:(?:το|τη|την|ο|η|τα)\\s+)?${noun}${end}`)) : null;
  if (closeEn || closeEl) return { type: "files", action: "close" };

  const focusEn = greek ? null : norm.match(new RegExp(`^(?:focus|select|go\\s+to|switch\\s+to)\\s+(?:(?:on|to|at|in|the)\\s+)?${noun}${end}`));
  const focusEl = greek && !focusEn ? norm.match(new RegExp(`^(?:εστιασε|επιλεξε|μεταβασε|πηγαινε)\\s+(?:(?:στον|στο|στη|στην|σε|το|τη|την|στο)\\s+)?${noun}${end}`)) : null;
  if (focusEn || focusEl) return { type: "files", action: "focus" };

  // minimize / maximize / restore the file manager
  const fmActs: Array<["minimize" | "maximize" | "restore", string, string]> = [
    ["minimize", "minim(?:ize|ise)|shrink", "ελαχιστοποιησε|μικρυνε|σμικρυνε"],
    ["maximize", "maxim(?:ize|ise)|expand|enlarge|full[-\\s]?screen", "μεγιστοποιησε|μεγεθυνε|επεκτεινε|πληρησ?\\s+οθονη"],
    ["restore", "restore|normali(?:ze|ise)|back\\s+to\\s+normal", "επαναφερε|κανονικοποιησε"],
  ];
  for (const [action, en, el] of fmActs) {
    const mEn = greek ? null : norm.match(new RegExp(`^(?:${en})\\s+(?:(?:the|this)\\s+)?${noun}${end}`));
    const mEl = greek && !mEn ? norm.match(new RegExp(`^(?:${el})\\s+${article}${noun}${end}`)) : null;
    if (mEn || mEl) return { type: "files", action };
  }

  return null;
}

/* ---------- Notepad commands ---------- */

function parseNotepadCommand(text: string, greek: boolean): LocalCommand | null {
  const clean = text.trim().replace(/^(?:please|can you|could you)\s+/i, "");
  const norm = normalize(clean).replace(/[.!?;]+$/, "");
  if (/^(?:open|launch|start)\s+(?:a\s+)?(?:new|another)\s+(?:notepad|note\s*pad)(?:\s+window)?$/.test(norm)) {
    return { type: "notepad", action: "open", create: true };
  }
  const noun = "(?:notepad|note\\s*pad|editor)";
  const article = "(?:(?:the|my)\\s+)?";
  // "close all notepads", "restore every editor", "κλείσε όλα τα
  // σημειωματάρια". The editor's own actions (write/save/format) are untouched:
  // they carry content and are extracted by the branches below, not here.
  const bulk = bulkWindowAction(
    norm,
    "notepads?|note\\s+pads?|editors?|σημειωματάρια?|κειμενογράφα?|κειμενογράφων",
    "notepads|note\\s+pads|editors|σημειωματάρια|κειμενογράφα|κειμενογράφων",
  );
  if (bulk) return { type: "notepad", action: bulk };
  const actions: Array<["open" | "close" | "focus" | "minimize" | "maximize" | "restore" | "new" | "save" | "download", string, string]> = [
    ["open", "open|launch|start", "ανοιξε|ξεκινα|ξεκινησε"],
    ["close", "close|quit|exit", "κλεισε|τερματισε"],
    ["focus", "focus|show|go\\s+to", "εστιασε|δειξε|πηγαινε"],
    ["minimize", "minim(?:ize|ise)|shrink", "ελαχιστοποιησε|μικρυνε"],
    ["maximize", "maxim(?:ize|ise)|expand|full[-\\s]?screen", "μεγιστοποιησε|μεγεθυνε"],
    ["restore", "restore|normali(?:ze|ise)", "επαναφερε|κανονικοποιησε"],
    ["new", "new|create\\s+(?:a\\s+)?new", "νεο|δημιουργησε\\s+νεο"],
    ["save", "save", "αποθηκευσε"],
    ["download", "download|export", "κατεβασε|εξαγαγε"],
  ];
  for (const [action, en, el] of actions) {
    if (new RegExp("^(?:" + en + ")\\s+" + article + noun + "(?:\\s+document)?$").test(norm)
        || (greek && new RegExp("^(?:" + el + ")\\s+(?:(?:το|τον)\\s+)?(?:σημειωματαριο|κειμενογραφο)(?:\\s+εγγραφο)?$").test(norm))) {
      return { type: "notepad", action };
    }
  }
  // Output references and multi-step tasks go to the agent, not into the document as literal text.
  if (/\b(?:command|terminal|shell)\s+output\b|\boutput\s+of\b/i.test(clean)) {
    if (/^(?:(?:open|launch)\s+(?:the\s+)?notepad\s+and\s+)?(?:add|insert|paste|copy|append)(?:\s+the)?(?:\s+(?:last|latest))?\s+(?:command|terminal|shell)\s+output(?:\s+(?:to|in|into)\s+(?:the\s+)?notepad)?[.!?]?$/i.test(clean)) return { type: "notepad", action: "command_output" };
    return null;
  }
  const target = "(?:(?:the|my)\\s+)?(?:notepad|note\\s*pad|editor)";
  const simple: Array<[NotepadCommand["action"], string]> = [
    ["recent", "(?:show|open|list)(?:\\s+the)?\\s+(?:recent\\s+)?documents(?:\\s+(?:in|of|for)\\s+" + target + ")|(?:show|open)\\s+" + target + "\\s+(?:recent\\s+)?documents"],
    ["hide_recent", "(?:hide|close)\\s+(?:the\\s+)?(?:recent\\s+documents|documents\\s+panel)(?:\\s+in\\s+" + target + ")?"],
    ["clear", "(?:clear|empty|erase)\\s+" + target],
    ["read", "(?:read|show)(?:\\s+me)?\\s+" + target + "(?:\\s+(?:text|content|contents))?"],
    ["select_all", "select\\s+all(?:\\s+text)?\\s+in\\s+" + target],
    ["undo", "undo(?:\\s+in)?\\s+" + target], ["redo", "redo(?:\\s+in)?\\s+" + target],
    ["export_text", "(?:download|export)\\s+" + target + "\\s+(?:as\\s+)?(?:text|txt|plain\\s+text)"],
  ];
  for (const [action, pattern] of simple) if (new RegExp("^(?:" + pattern + ")[.!?]?$", "i").test(clean)) return { type: "notepad", action };
  const title = clean.match(new RegExp("^(?:rename|title|name)\\s+" + target + "\\s+(?:(?:to|as)\\s+)?(.+)$", "i"));
  if (title) return { type: "notepad", action: "title", content: title[1] };
  const doc = clean.match(new RegExp("^open\\s+(?:document\\s+)?(.+?)\\s+in\\s+" + target + "$", "i"));
  if (doc) return { type: "notepad", action: "open_document", content: doc[1] };
  const format = clean.match(new RegExp("^(?:format|make)\\s+" + target + "(?:\\s+text)?\\s+(?:as\\s+)?(bold|italic|underline|heading 1|heading 2|paragraph|bullet list|numbered list|align left|align center|align right)$", "i"));
  if (format) return { type: "notepad", action: "format", content: format[1].toLowerCase() };
  const replace = clean.match(new RegExp("^replace\\s+" + target + "(?:\\s+(?:content|contents|text))?\\s+with\\s+([\\s\\S]+)$", "i"));
  if (replace) return { type: "notepad", action: "replace", content: replace[1] };
  const compound = clean.match(new RegExp("^(?:open|launch|start)\\s+" + target + "\\s+(?:and|then|and then)\\s+(?:write|type|add|insert|append)\\s+([\\s\\S]+)$", "i"));
  const writeFirst = clean.match(/^(?:write|type|add|insert|append)\s+([\s\S]+?)\s+(?:in|into|to)\s+(?:the\s+|my\s+)?(?:notepad|note\s*pad|editor)[.!?]?$/i);
  const padFirst = clean.match(/^(?:(?:in|into|to)\s+(?:the\s+|my\s+)?(?:notepad|note\s*pad|editor)[,:]?\s+(?:write|type|add|insert|append)|(?:write|type|add|insert|append)\s+(?:in|into|to)\s+(?:the\s+|my\s+)?(?:notepad|note\s*pad|editor))[,:]?\s+([\s\S]+)$/i);
  const greekWrite = greek ? clean.match(/^(?:γρ[άα]ψε|πρ[όο]σθεσε|β[άα]λε)\s+([\s\S]+?)\s+(?:στο|στον|μ[έε]σα\s+στο)\s+(?:σημειωματ[άα]ριο|κειμενογρ[άα]φο)$/i) : null;
  const content = compound?.[1] ?? writeFirst?.[1] ?? padFirst?.[1] ?? greekWrite?.[1];
  if (content && (/\b(?:and then|then)\s+(?:save|close|rename|download|format)\b|\band\s+(?:save|close|download)\b/i.test(content)
      || /^(?:a|an|the)\s+(?:poem|summary|report|story|essay|letter|email|list)\b/i.test(content))) return null;
  return content?.trim() ? { type: "notepad", action: "write", content: content.trim() } : null;
}

/* ---------- virtual-desktop commands ---------- */

/* "go to virtual desktop 1", "switch to desktop 2", "desktop 3", "next desktop",
 * "move window 2 to desktop 1"; Greek equivalents. Runs before the window
 * parser so desktop phrases are never interpreted as window referents. */
const DESK_NUMBER_WORDS: Record<string, number> = {
  one: 1, first: 1, two: 2, second: 2, three: 3, third: 3, four: 4, fourth: 4,
  ενασ: 1, ενα: 1, μια: 1, πρωτο: 1, πρωτη: 1, δυο: 2, δευτερο: 2, δευτερη: 2,
  τρεισ: 3, τρια: 3, τριτο: 3, τριτη: 3, τεσσερισ: 4, τεσσερα: 4, τεταρτο: 4, τεταρτη: 4,
};

function parseDesktopNumber(token: string): number | null {
  const t = token.toLowerCase();
  let n: number | null = null;
  if (/^\d+$/.test(t)) n = Number(t);
  else if (DESK_NUMBER_WORDS[t] !== undefined) n = DESK_NUMBER_WORDS[t];
  if (n === null) return null;
  return Math.max(1, Math.min(4, n)); // there are exactly DESKTOPS desktops
}

/* Panel, chat input and lock commands.
 *
 * These act on the UI shell, so they must be recognised before anything can
 * fall through to the agent: "close panel" is not a question, and routing it to
 * the model would spend a turn (and a token) to accomplish nothing.
 */
/* Panel, chat-input and lock commands.
 *
 * These act on the UI shell, so they are recognised before anything can fall
 * through to the agent: "close panel" is not a question, and routing it to the
 * model would spend a turn (and tokens) to accomplish nothing.
 */
/* The tab list is defined once, in panelBridge, and re-exported here. It used to
   be spelled out in both files, which is how the TASKS tab ended up missing
   from the panel while the parser knew the word: the parser and the panel that
   renders the tabs are the same concept and must be the same union. */
export type { PanelTabName } from "./panelBridge";
import type { PanelTabName } from "./panelBridge";

/* Ordered, most specific first. "chat history" contains "chat", so a naive
   scan would open the chat tab when the operator asked for the history; and the
   whole list is a subset check, not equality, so "chat history" and "tools"
   still resolve.
   "εργασι" is in the tasks pattern, which is why the desktop phrases that also
   contain it are excluded from the tasks pattern below: Greek "εικονικη
   επιφανεια εργασιας" is a VIRTUAL DESKTOP, and matching it as a tab request
   would stop "πήγαινε στην εικονικη επιφανεια εργασιας 2" from ever reaching
   the desktop parser. Desktop wins for that phrase; the two are genuinely
   ambiguous in Greek and the pre-existing behaviour is the safer one. */
const PANEL_TAB_PATTERNS: Array<[PanelTabName, RegExp]> = [
  ["history", /history|chat\s*history|archive|past\s*chats?|ιστορικ|αρχειοθετημ/],
  ["settings", /settings|preference|config|options|ρυθμισ|προτιμησ/],
  ["memory", /memor|memories|μνημ|ισχνομνι/],
  // A virtual desktop IS a workspace, and the Greek word for it (εικονικη επιφανεια
  // εργασιας) contains the word for task. "go to virtual desktop 1" must reach
  // the desktop parser, so the tab patterns yield to an explicit destination
  // phrase - see tabFromWords.
  ["tasks", /task|job|schedul|εργασι|εργασιων/],
  ["apps", /app|application|tool|widget|εφαρμογ|εργαλει/],
  ["chat", /chat|conversation|message|συνομιλ|μηνυμ|συνομιλι/],
];

/* Destinations that are NOT tabs. The panel parser's "go to <destination>" arm
   captures free text and maps it through tabFromWords, so without this guard
 * "go to virtual desktop 1" resolves to the TASKS tab (επιφανεια εργασιας
   contains εργασι) and the desktop parser never sees it. Everything listed here
 * is a real destination the window/desktop layer owns. */
const PANEL_DESTINATION_WORDS = /desktop|workspace|παραθυρ|window|εικονικ|επιφανει|εργασι/;

function tabFromWords(words: string): PanelTabName | null {
  const norm = normalize(words);
  for (const [tab, pattern] of PANEL_TAB_PATTERNS) {
    // Skip tabs whose keyword is also part of a non-tab destination. Only the
    // bare form reaches the panel ("open tasks"), never "go to ... tasks ..."
    // where the trailing words carry the real meaning.
    if (tab === "tasks" && PANEL_DESTINATION_WORDS.test(norm)) continue;
    if (pattern.test(norm)) return tab;
  }
  return null;
}

function parsePanelCommand(text: string, greek: boolean): LocalCommand | null {
  const clean = text.trim().replace(/[.!?;·;]+$/, "").trim();
  if (!clean) return null;
  const norm = normalize(clean);

  /* Width commands come first because `expand` is claimed here and would
     otherwise be read as "open" by the arm below. `minimize` stays on the
     close arm on purpose: it is the established way to hide the panel, and
     repurposing it as "shrink the width" would change a phrase operators
     already use. */
  if (/^(?:maximi[sz]e|expand|enlarge|widen|full[\s-]?screen)(?:\s+the)?(?:\s+chat)?\s+panel$/.test(norm)
      || /^(?:make|set)(?:\s+the)?(?:\s+chat)?\s+panel\s+(?:wider|wide|full)$/.test(norm)) {
    return { type: "panel", action: "maximize" };
  }
  if (/^(?:normali[sz]e|unmaximi[sz]e|restore|shrink|un[\s-]?shrink)(?:\s+the)?(?:\s+chat)?\s+panel$/.test(norm)
      || /^(?:reset|return)(?:\s+the)?(?:\s+chat)?\s+panel(?:\s+to\s+(?:its\s+)?(?:norma?l|default|original))?$/.test(norm)) {
    return { type: "panel", action: "normalize" };
  }
  if (/^(?:open|show|expand|unhide)(?:\s+the)?(?:\s+chat)?\s+panel$/.test(norm)
      || /^(?:toggle|switch)(?:\s+the)?(?:\s+chat)?\s+panel$/.test(norm)) {
    return /toggle|switch/.test(norm) ? { type: "panel", action: "toggle" } : { type: "panel", action: "open" };
  }
  if (/^(?:close|hide|collapse|minimi[sz]e|fold)(?:\s+the)?(?:\s+chat)?\s+panel$/.test(norm)) {
    return { type: "panel", action: "close" };
  }
  if (greek) {
    if (/^(?:μεγιστοποιησε|μεγαλωσε|αναπτυξε|πλατυνε)(?:\s+το)?\s*πανελ$/.test(norm)) return { type: "panel", action: "maximize" };
    if (/^(?:κανονικοποιησε|επαναφερε|μικρυνε|σμικρυνε)(?:\s+το)?\s*πανελ$/.test(norm)) return { type: "panel", action: "normalize" };
    if (/^(?:ανοιξε|δειξε|εμφανισε)(?:\s+το)?\s*πανελ$/.test(norm)) return { type: "panel", action: "open" };
    if (/^(?:κλεισε|κρυψε|συμπτυξε|παραθεσε)\s*(?:το|την|τη)?\s*πανελ$/.test(norm)) return { type: "panel", action: "close" };
    if (/^(?:εναλλαξε|αλλαξε|τροπε)\s*(?:το|την|τη)?\s*πανελ$/.test(norm)) return { type: "panel", action: "toggle" };
  }

  // "go to <anything>" - the destination is captured as free text and mapped,
  // so synonyms work and a non-tab destination is left to the agent.
  const goEn = /^(?:go|switch|jump|navigate|take\s+me)\s+to\s+(?:the\s+)?(.{1,40}?)(?:\s+(?:panel|tab|page|screen))?$/.exec(norm);
  const goEl = greek
    /* Both sigma spellings are listed: normalize() folds a final U+03C2 into
       U+03C3, so "στις" reaches the matcher as "στισ" and "τους" as "τουσ".
       Writing only the accented-original form silently fails to match. */
    ? /^(?:πηγαινε|παμε|μεταβαινε|οδηγησε)\s+(?:σ(?:τ(?:η|ην|ο|ισ|ις|α|ου|εσ|ες)|τη|την|το|τουσ|τους|των))\s*(.{1,40}?)(?:\s*(?:πανελ|νομα)?)$/.exec(norm)
    : null;
  const go = goEn ?? goEl;
  if (go) {
    const tab = tabFromWords(go[1]);
    return tab ? { type: "panel", action: "open", tab } : null;
  }

  /* Bare-noun form: "open settings", "show me memory", "view history". Only a
     short phrase counts, and the word limit is the whole point: "show me the
     history of the Roman empire" is a question for the agent, not a request to
     switch tabs, and without the limit the first tab keyword in a long sentence
     would silently swallow it. */
  const bare = /^(?:open|show|view|display|bring\s+up)(?:\s+me)?\s+(?:the\s+)?((?:[\p{L}\p{N}_-]+\s*){1,3})$/u.exec(norm);
  if (bare) {
    const tab = tabFromWords(bare[1]);
    if (tab) return { type: "panel", action: "open", tab };
  }
  return null;
}

function parseChatInputCommand(text: string, greek: boolean): LocalCommand | null {
  /* The *raw* text, not the caller's punctuation-stripped copy. "write in
     chat: what is the time?" must keep its question mark - stripping trailing
     punctuation is right for a command phrase and wrong for a message the
     operator dictated. */
  const raw = text.trim();
  if (!raw) return null;
  const norm = normalize(raw);

  const CHAT_NOUN = String.raw`(?:chat|chat\s*box|chatbox|input|message\s*box|box|prompt|συνομιλι(?:α|εσ)|πλαισιο)`;

  /* Both forms capture the spoken text as an explicit group and derive its
     position from `match.index` plus that group's own length. Deriving the
     position from the end of the whole match is wrong for the second form,
     where the text sits in the middle and the tail ("in the chat") follows it.
   */
  // "write in chat: hello" / "type into the chatbox, hi"
  const lead = new RegExp(
    String.raw`^(?:(?:write|type|put|add|enter|set|γραψε|προσθεσε|βαλε)\s+(?:in|into|to|on|στο|στην|στη|στα|στις)?\s*(?:the\s*)?${CHAT_NOUN}(?:\s*box|\s*field|\s*input)?\s*(?::|,|-|–|\s)?\s*)((?:.|\n)*)$`,
  ).exec(norm);
  // "write hello in the chat" - the text sits in the middle.
  const mid = new RegExp(
    /* Three groups so the text's position is exact: 1 = verb, 2 = the text,
       3 = the trailing "in the chat". Deriving the offset from the end of the
       whole match is wrong for this form, where the tail follows the text. */
    String.raw`^((?:write|type|put|add|enter|γραψε|προσθεσε|βαλε)\s+)(.{1,500}?)(\s+(?:in|into|to|σ(?:το|την|τη|τα|τις|του))\s+(?:the\s*)?${CHAT_NOUN}(?:\s*box)?\s*[.!;·;]*)$`,
  ).exec(norm);

  if (lead) {
    // The capture runs to the end of the match, so its start is its length back.
    const len = lead[1].length;
    const value = originalSlice(raw, lead.index + lead[0].length - len, lead.index + lead[0].length).trim();
    return { type: "chatinput", action: "write", text: value };
  }
  if (mid) {
    // Group 2 starts right after the verb (group 1) and ends where group 3
    // begins, so its offset is exact and independent of the tail.
    const start = mid.index + mid[1].length;
    const value = originalSlice(raw, start, start + mid[2].length).trim();
    return { type: "chatinput", action: "write", text: value };
  }

  const send = /^(?:send|submit)(?:\s+(?:the|my|it))?(?:\s+(?:chat|message|text|prompt))?$/.test(norm)
    || /^(?:send|submit)(?:\s+(?:chat|message|text|prompt))\s+now$/.test(norm)
    || (greek && /^(?:στελ(?:ε|λε)|αποστελ(?:ε|λε))\s*(?:το|την|τη|το)?\s*(?:μηνυμα|κειμενο|συνομιλι(?:α|εσ))?$/.test(norm));
  if (send) return { type: "chatinput", action: "send" };
  return null;
}

function parseLockCommand(text: string, greek: boolean): LocalCommand | null {
  const norm = normalize(text.trim().replace(/[.!?;·;]+$/, "").trim());
  if (!norm) return null;
  // Guard against "unlock"/"ξεκλείδωσε": a bare "lock" must not match them, or
  // the operator would re-lock the screen while trying to get in.
  if (/^(?:unlock|un\s*lock|ξεκλειδωσε|ανοιξε)/.test(norm)) return null;
  if (/^lock$/.test(norm)) return { type: "lock", action: "lock" };
  if (/^(?:lock|secure|enable)\s+(?:the\s+)?(?:screen|apex|system|desktop|session)$/.test(norm)) {
    return { type: "lock", action: "lock" };
  }
  if (/^(?:go|switch)\s+to\s+(?:sleep|lock)(?:\s+mode)?$/.test(norm)) {
    return { type: "lock", action: "lock" };
  }
  if (greek && /^κλειδωσε(?:\s+τ(?:ην|ο))?(?:\s+οθονη|\s+το\s+apex|\s+συστημα|\s+οθονη\s+κλειδωμο)?$/.test(norm)) {
    return { type: "lock", action: "lock" };
  }
  return null;
}


function parseSignOutCommand(text: string, greek: boolean): LocalCommand | null {
  /* Ending the session is consequential, so every pattern is anchored to the
     whole utterance. A question about signing out ("how do I log out of this")
     has words in front of it and must reach the agent as a question - matching
     a phrase anywhere in the sentence would end the session because someone was
     talking about it. */
  const norm = normalize(text.trim().replace(/[.!?;·]+$/, "").trim());
  if (!norm) return null;
  if (/^(?:sign|log)\s*out$/.test(norm)) return { type: "signout" };
  if (/^(?:sign|log)\s*out\s+of\s+(?:apex|the\s+app|this\s+app|the\s+system)$/.test(norm)) {
    return { type: "signout" };
  }
  if (/^(?:please\s+)?(?:end|terminate|close)\s+(?:the\s+)?(?:session|sign\s*in)$/.test(norm)) {
    return { type: "signout" };
  }
  if (/^(?:log|get)\s+me\s+out$/.test(norm)) return { type: "signout" };
  /* `normalize()` folds a final sigma, so "βγες" arrives as "βγεσ". Both the
     imperative and the noun form are accepted for the same reason the lock
     parser accepts both spellings. */
  if (greek && /^(?:αποσυνδε(?:σ|θε)|βγεσ(?:\s+εξω)?|κανε\s+(?:εξοδο|αποσυνδεσ|αποσυνδεσμο)|τελοσ\s+συνεδριασ)/.test(norm)) {
    return { type: "signout" };
  }
  return null;
}


function parseDesktopCommand(text: string, greek: boolean): LocalCommand | null {
  const clean = text.trim().replace(/[.!?;·;]+$/, "").trim();
  if (!clean) return null;
  const norm = normalize(clean);
  const noun = "(?:(?:virtual\\s+|εικονικ(?:η|εσ)\\s+)?(?:desktop|workspace|επιφανει(?:α|εσ)\\s+εργασιασ|περιοχ(?:η|εσ)\\s+εργασιασ))";
  const verb =
    "(?:(?:go|switch|jump|move|open|focus)\\s+to\\s+|(?:go|switch|open|focus)\\s+|" +
    "(?:πηγαινε|μεταβ(?:α|ησ)|ανοιξε|εστιασε|επιλεξε|εναλλαξε|εναλλαγη)\\s+(?:(?:στο|στη|στην|σε|στον|το|τη|την)\\s+)?)?";
  const head = `^${verb}(?:(?:the|to|on)\\s+)?`;
  const switchFor = (token: string): LocalCommand | null => {
    const desktop = parseDesktopNumber(token);
    return desktop === null ? null : { type: "desktop", action: "switch", desktop: desktop - 1 };
  };

  // switch, number first: "go to virtual desktop 1", "switch desktop 2", "desktop 3"
  const mNumFirst = norm.match(new RegExp(`${head}${noun}\\s+(\\S+)$`));
  if (mNumFirst) {
    const cmd = switchFor(mNumFirst[1]);
    if (cmd) return cmd;
  }
  // switch, ordinal first: "focus the fourth desktop", "δεύτερη επιφάνεια εργασίας"
  const mOrdFirst = norm.match(new RegExp(`${head}(\\S+)\\s+${noun}$`));
  if (mOrdFirst) {
    const cmd = switchFor(mOrdFirst[1]);
    if (cmd) return cmd;
  }

  // move a window: "move window 2 to desktop 1", "move the second window to
  // desktop one", "μετακίνησε το παράθυρο 2 στην επιφάνεια εργασίας 1"
  const moveTail = norm.replace(/^(?:move|send|relocate)\s+|^(?:μετακινησε|στειλε)\s+/, "");
  if (moveTail !== norm) {
    // Terminals are addressed by their own number, not their window position:
    // "move terminal 2 to desktop 3", "move terminals 1 and 2 to desktop 2",
    // "μετακινησε τα τερματικα 1 και 2 στην επιφανεια εργασιας 2".
    // Two groups: the terminal list, then the destination desktop token.
    const listRe = greek
      ? "(\\d{1,2}(?:\\s*(?:και|,|&)\\s*\\d{1,2})*)"
      : "(\\d{1,2}(?:\\s*(?:and|,|&)\\s*\\d{1,2})*)";
    const headRe = greek
      ? "^(?:τα|το|τη|την)\\s+"
      : "^(?:the\\s+)?";
    const nounRe = greek ? "(?:τερματικα|τερματικο|τερματικες)" : "terminals?";
    const toRe = greek
      ? "(?:στο|στη|στην|στον)"
      : "(?:to|into|onto|on|in)";
    const termMove = moveTail.match(
      new RegExp(`${headRe}${nounRe}\\s+${listRe}\\s+${toRe}\\s+(?:the\\s+)?${noun}\\s+(\\S+)$`, "u"),
    );
    if (termMove) {
      const targets = termMove[1]
        .split(/\s*(?:and|και|,|&)\s*/i)
        .map((part) => Number(part.trim()))
        .filter((n) => Number.isFinite(n) && n >= 1 && n <= 99);
      const desktop = parseDesktopNumber(termMove[2]);
      if (desktop !== null && targets.length) {
        return { type: "desktop", action: "move", desktop: desktop - 1, targets, terminals: true };
      }
    }
    const target = consumeWindowTarget(moveTail, greek);
    if (target) {
      // consumeWindowTarget leaves a trailing window noun when the ordinal came
      // first ("the third window to desktop four"): drop it before the "to".
      const rest = moveTail
        .slice(target.consumed)
        .trim()
        .replace(/^(?:window|παραθυρο)\s+/, "");
      const mTo = rest.match(new RegExp(`^(?:(?:to|into|onto|on|in|στο|στη|στην|στον|σε)\\s+)?${noun}\\s+(\\S+)$`));
      const desktop = mTo ? parseDesktopNumber(mTo[1]) : null;
      if (desktop !== null) return { type: "desktop", action: "move", target: target.index, desktop: desktop - 1 };
    }
  }

  // previous / next desktop
  const traverse = /^(?:(?:switch|go)\s+to\s+|go\s+)?(?:the\s+)?(?:next|following|previous|επομεν(?:η|ο|ε)|προηγουμεν(?:η|ο|ε))\s+(?:virtual\s+|εικονικ(?:η|ε)\s+)?(?:desktop|workspace|επιφανει(?:α|εσ)\s+εργασιασ|περιοχ(?:η|εσ)\s+εργασιασ)(?=\s*$)/;
  const mTraverse = norm.match(traverse);
  if (mTraverse) {
    const action = /^(?:next|following|επομεν)/.test(mTraverse[0]) ? "next" : "previous";
    return { type: "desktop", action, desktop: 0 };
  }

  return null;
}

/* ---- scheduled tasks ----
 *
 * Every pattern here is anchored at BOTH ends. That is the whole safety story
 * of this parser: it runs on every utterance the operator types or says, and
 * the ones that must reach the agent are exactly the ones containing the word
 * "task" ("every morning check the disk, make it a task", "what would you
 * schedule?"). A substring match would swallow all of them and the agent would
 * never see a task request at all, so creation is left to the model on purpose
 * and only the closed-class verbs (list, run, pause, resume, delete) are
 * handled here.
 */
const TASK_NUMBER_WORDS: Record<string, number> = {
  one: 1, first: 1, two: 2, second: 2, three: 3, third: 3, four: 4, fourth: 4,
  five: 5, fifth: 5, six: 6, sixth: 6, seven: 7, seventh: 7, eight: 8, eighth: 8,
  nine: 9, ninth: 9, ten: 10, tenth: 10,
  ενα: 1, ενασ: 1, πρωτο: 1, πρωτη: 1, δυο: 2, δευτερο: 2, δευτερη: 2,
  τρια: 3, τρεις: 3, τριτο: 3, τριτη: 3, τεσσερα: 4, τεσσερις: 4, τεταρτο: 4, τεταρτη: 4,
  πεντε: 5, πεμπτο: 5, εξι: 6, εκτο: 6, επτα: 7, εβδομο: 7, οκτω: 8, ογδο: 8,
  εννεα: 9, ενατο: 9, δεκα: 10, δεκατο: 10,
};

/* "the last one" is -1: the provider resolves it against the loaded list, which
   is the only place that knows how many tasks there are. */
const TASK_LAST_WORDS = /(?:last|latest|final|τελευται|τελευταιο)/;
const TASK_NUM = `(?:\\d+|${Object.keys(TASK_NUMBER_WORDS).join("|")})`;

function taskNumber(token: string | undefined): number | null {
  if (!token) return null;
  const t = token.trim().toLowerCase();
  if (/^\d+$/.test(t)) return Number(t);
  if (TASK_NUMBER_WORDS[t] !== undefined) return TASK_NUMBER_WORDS[t];
  return null;
}

/** "running", "broken", "paused" -> the badge filter the tab renders.
 *
 * Leading filler is stripped first: the adjective-first pattern captures
 * whatever sits between the verb and the noun, so "show the paused tasks"
 * arrives here as "the paused", and an article left in place would turn a
 * real filter into "all" - the operator would be told about every task. */
function taskFilter(word: string | undefined): "all" | "running" | "paused" | "enabled" | "error" {
  const w = (word || "").toLowerCase()
    .replace(/^(?:the|my|all|of|that|are|is|τα|τη|την|τισ|μου)\s+/, "")
    .trim();
  if (/^(?:running|active|going|busy|current)|^(?:τρεχ|ενεργ|ισχυρα|τωρα)/.test(w)) return "running";
  if (/^(?:paused|on\s+hold|suspended|stopped)|^(?:παυ|σταματημεν|αναστολ)/.test(w)) return "paused";
  if (/^(?:broken|failing|failed|error|errors)|^(?:σφαλμ|χαλασμ|αποτυχ)/.test(w)) return "error";
  if (/^(?:enabled|scheduled|upcoming|planned)|^(?:ενεργοποιημεν|προγραμματισμεν)/.test(w)) return "enabled";
  return "all";
}

/* Verb alternations, English and Greek side by side so a new action is one line
   instead of two. Greek is matched in its normalized form (accents stripped,
   final sigma folded), so it is written without accents and with the accented
   form spelled out for final-sigma positions. The noun is open-ended because
   Greek inflects it freely: "εργασια", "εργασιεσ", "εργασιων", "εργασιασ". */
const TASK_VERBS = {
  list: "list|show|display|see|view|browse|read|open|give|tell|which|what(?:'s| is| are)?|ποια|ποιεσ|ποιο|τι\\s+(?:ειναι|εχει)|δειξε|δειξου|εμφανισε|λιστα",
  open: "open|show|view|browse|go\\s+to|switch\\s+to|jump\\s+to|navigate\\s+to|take\\s+me\\s+to|ανοιξε|δειξε|πηγαινε\\s+στ",
  run: "run|start|execute|trigger|fire|launch|τρεξε|τρεξτου|ξεκινα|εκτελεσε",
  pause: "pause|stop|hold|suspend|freeze|disable|παυση|παυσε|σταματα|σταματησε|κρυψε|αναστολη",
  resume: "resume|unpause|enable|activate|restart|re-?enable|unsuspend|συνεχισε|συνεχιση|ενεργοποιησε|ξαναενεργοποιησε",
  delete: "delete|remove|cancel|drop|get\\s+rid\\s+of|forget|διαγραψε|διεγραψε|αφαιρεσε|καταργησε",
  show: "what(?:'s| is| are)?|which|tell\\s+me\\s+about|show|check|describe|read|τι\\s+(?:ειναι|κανει|εκανε|εχει)|δειξε|πες\\s+μου|εξηγησε",
} as const;

const TASK_NOUN = "(?:tasks?|jobs?|εργασι[\\p{L}]*)";
/* "the task", "my second task", "η εργασία" - all optional filler. */
const TASK_OBJ = "(?:the\\s+|my\\s+|τ(?:η|ο|ην|ησ|οσ|ων|εσ|ια)ς?\\s+|μου\\s+|η\\s+|ο\\s+)?";
/* The number can sit on either side of the noun, because a dictated "run the
   last task" is far more natural than "run task last", and "run task 2" is the
   natural form of the same request. Three branches, three named groups; the
   caller reads whichever one matched. */
const TASK_REF = `(?:${[
  `(?:(?<before>${TASK_NUM}|${TASK_LAST_WORDS.source})\\s+${TASK_NOUN})`,
  `(?:${TASK_NOUN}\\s+(?<after>${TASK_NUM}|${TASK_LAST_WORDS.source}))`,
  `(?:(?<bare>${TASK_NUM}|${TASK_LAST_WORDS.source}))`,
].join("|")})`;

/** The one number a reference matched, whichever order the words came in. */
function taskRefNumber(m: RegExpMatchArray): number {
  const groups = m.groups ?? {};
  return taskNumber(groups.before ?? groups.after ?? groups.bare) ?? -1;
}

function alt(key: keyof typeof TASK_VERBS): string {
  return `(?:${TASK_VERBS[key]})`;
}

export function parseTaskCommand(text: string, greek: boolean): LocalCommand | null {
  const clean = text.trim().replace(/[.!?;·;]+$/, "").trim();
  if (!clean) return null;
  const norm = normalize(clean);
  // Greek-only alternations are always tried: `normalize()` has folded the
  // accents away, so a Greek utterance is recognisable whatever the UI
  // language is set to, which is the same rule the rest of this file follows.
  void greek;

  // A filter narrows the list: "what tasks are running" must not answer with
  // the six paused ones, because that is a different question.
  // Every Greek literal below is written in its NORMALIZED spelling: normalize()
  // folds U+03C2 to U+03C3 everywhere, not only word-finally, so "τις" reaches
  // the matcher as "τισ" and the accented-original form silently fails.
  const filtered = norm.match(new RegExp(
    `^(?:${alt("list")})\\s+(?:me\\s+)?(?:my\\s+|the\\s+|τισ\\s+|την\\s+|τ\\s+)?${TASK_NOUN}\\s+(?:that\\s+are\\s+|currently\\s+|are\\s+|που\\s+(?:τρεχουν|εχουν|ειναι)\\s+)?([\\p{L}\\s-]+?)$`, "u"));
  if (filtered) {
    const filter = taskFilter(filtered[1]);
    if (filter !== "all") return { type: "task", action: "list", filter };
  }

  // The same question with the filter in front of the noun: "show the paused
  // tasks", "list broken tasks". The capture is everything between the verb and
  // the noun, so an unrecognised word ("show my tasks") falls through to the
  // plain list instead of being answered with the wrong subset.
  const filteredFirst = norm.match(new RegExp(
    `^(?:${alt("list")})\\s+(?:me\\s+)?([\\p{L}\\s-]+?)\\s+(?:my\\s+|the\\s+|τισ\\s+|την\\s+)?${TASK_NOUN}$`, "u"));
  if (filteredFirst) {
    const filter = taskFilter(filteredFirst[1]);
    if (filter !== "all") return { type: "task", action: "list", filter };
  }

  // list everything / open the tab
  const list = norm.match(new RegExp(
    `^(?:${alt("list")})?\\s*(?:me\\s+)?(?:my\\s+|the\\s+|all\\s+|all\\s+of\\s+my\\s+|of\\s+my\\s+|τισ\\s+|την\\s+|το\\s+|τ\\s+)?${TASK_NOUN}(?:\\s+(?:list|tab|panel|page|please|λιστα|πινακα))?(?:\\s+μου)?$`, "u"));
  if (list) {
    // "show the tasks tab" and "open tasks" are an intent to see the panel;
    // "show tasks" and a bare "tasks" are a question. The named destination
    // ("tab", "panel") is the unambiguous signal and a leading open/browse is
    // the other - "show" alone is deliberately NOT one, because "show tasks"
    // and "show the tasks tab" are both things an operator says.
    const first = norm.split(/\s+/)[0] ?? "";
    const wantsTab = /(?:^|\s)(?:tab|panel|page|πινακα)$/.test(norm)
      || /^(?:open|browse|view|go|switch|navigate|ανοιξε|πηγαινε)/.test(first);
    return wantsTab ? { type: "task", action: "open" } : { type: "task", action: "list", filter: "all" };
  }

  /* The number may sit on either side of the noun ("task 2", "the second
     task"), so both capture groups are consulted; whichever matched is it. */
  const act = (action: "run" | "pause" | "resume" | "delete", tail = "") => {
    const m = norm.match(new RegExp(`^${alt(action)}\\s+${TASK_OBJ}${TASK_REF}${tail}$`, "u"));
    if (!m) return null;
    return { type: "task", action, target: taskRefNumber(m) } as LocalCommand;
  };

  return act("delete") ?? act("pause") ?? act("resume")
    // "run task 2 now" - the "now" is optional noise, not a different action.
    ?? act("run", "(?:\\s+(?:now|right\\s+now|immediately|please|τωρα|αμεσα|αμεσως))?")
    // "status of task 2", "what is task 2", "task 2 status"
    ?? (() => {
      const m = norm.match(new RegExp(
        `^(?:${alt("show")})?\\s*${TASK_OBJ}(?:status\\s+(?:of\\s+)?|κατασταση\\s+(?:της\\s+)?)?${TASK_REF}(?:\\s+(?:status|state|κατασταση))?$`, "u"));
      if (!m) return null;
      return { type: "task", action: "show", target: taskRefNumber(m) } as LocalCommand;
    })();
}

export function parseLocalCommand(text: string, language: string, skills: Array<{ name: string }>, now = Date.now()): LocalCommand | null {
  const clean = text.trim().replace(/[.!?;·;]+$/, "").trim();
  if (!clean) return null;
  const greek = isGreek(language);
  const normalized = normalize(clean);
  const notepad = parseNotepadCommand(text.trim(), greek);
  if (notepad) return notepad;
  // Before the panel parser: "show tasks" must answer the question, not just
  // switch tabs (the local handler opens the tab as well), and "run task 2"
  // would otherwise be read as a request to open something called "2".
  const task = parseTaskCommand(clean, greek);
  if (task) return task;
  // Shell commands first: "close panel" and "lock" are actions, not requests.
  const panel = parsePanelCommand(clean, greek);
  if (panel) return panel;
  const chatInput = parseChatInputCommand(text, greek);
  if (chatInput) return chatInput;
  const lock = parseLockCommand(clean, greek);
  if (lock) return lock;
  // After lock, so "lock" is never read as a sign-out and vice versa.
  const signOut = parseSignOutCommand(clean, greek);
  if (signOut) return signOut;
  const terminal = parseTerminalCommand(clean, greek);
  if (terminal) return terminal;
  const files = parseFilesCommand(clean, greek);
  if (files) return files;
  const desktop = parseDesktopCommand(clean, greek);
  if (desktop) return desktop;
  const win = parseWindowCommand(clean, greek);
  if (win) return win;
  if (/^(?:cancel|stop|clear)\s+(?:all\s+)?timers?$/.test(normalized)
      || (greek && /^(?:ακυρωσε|σταματα|σταματησε|διαγραψε|διεγραψε)\s+(?:(?:ολα\s+)?τα\s+|το\s+)?χρονομετρ(?:ο|α)$/.test(normalized))) return { type: "cancelTimers" };
  if (/^(?:cancel|stop|clear)\s+(?:all\s+)?reminders?$/.test(normalized)
      || (greek && /^(?:ακυρωσε|σταματα|σταματησε|διαγραψε|διεγραψε)\s+(?:(?:ολεσ\s+)?τισ\s+|την?\s+)?υπενθυμισ(?:η|εισ)$/.test(normalized))) return { type: "cancelReminders" };
  const timer = parseTimer(clean, greek);
  if (timer) return timer;
  const reminder = parseReminder(clean, greek, now);
  if (reminder) return reminder;
  let operator = afterPrefix(clean, /^(?:i\s+am|i['’]m|this\s+is|call\s+me)\s+(?:your\s+)?operator(?=$|[\s,])/);
  if (operator === null && greek) operator = afterPrefix(clean, /^(?:ειμαι|αυτοσ\s+ειναι|αποκαλεσε\s+με)\s+(?:(?:ο|η)\s+)?(?:χειριστησ|χειριστρια|χειριστη)(?:\s+σου)?(?=$|[\s,])/);
  if (operator !== null) {
    operator = operator.replace(/^[,\s]+/, "");
    operator = afterPrefix(operator, greek ? /^(?:name\s+is|με\s+λενε|το\s+ονομα\s+μου\s+ειναι|ονομαζομαι)\s+/ : /^name\s+is\s+/) ?? operator;
    return operator ? { type: "operator", name: operator } : { type: "operator" };
  }
  if (/^(?:disable|stop|turn\s+off|shut\s+off)\s+(?:autonomous\s+mode|autonomy)$/.test(normalized)
      || (greek && /^(?:απενεργοποιησε|σταματα|σταματησε|κλεισε)\s+(?:(?:την|τη)\s+)?(?:αυτονομη\s+λειτουργια|αυτονομια)$/.test(normalized))) return { type: "autonomy", enabled: false };
  if (/^(?:enable|start|turn\s+on)\s+(?:autonomous\s+mode|autonomy)$/.test(normalized)
      || (greek && /^(?:ενεργοποιησε|ξεκινα|ξεκινησε|ανοιξε)\s+(?:(?:την|τη)\s+)?(?:αυτονομη\s+λειτουργια|αυτονομια)$/.test(normalized))) return { type: "autonomy", enabled: true };
  if (/^(?:be\s+quiet|silence|shut\s+up|quiet|pause\s+autonomy|stop\s+talking)$/.test(normalized)
      || (greek && /^(?:σιωπη|ησυχια|κανε\s+ησυχια|μη(?:ν)?\s+μιλασ|σταματα\s+να\s+μιλασ|παυση\s+αυτονομιασ)$/.test(normalized))) return { type: "silence" };
  return parseImages(clean, greek) ?? parseSkill(text.trim(), greek, skills);
}

/* ---------- think-hard prefix ----------
 * "think hard: <request>" (text or voice) routes that SINGLE turn to
 * THINK_HARD_MODEL. The marker is stripped so the agent only ever sees the
 * request, returned verbatim from the original text. This is not a local
 * command: the request itself still goes to the agent.
 *
 * Patterns match the accent-stripped lowercase form (see normalize/afterPrefix),
 * so the Greek spellings need no accented character classes. Greek needs an
 * explicit separator or a trailing adverb, otherwise "σκεψε το πρόβλημα" would
 * swallow the article and hand the agent a mutilated request. The Greek adverb
 * ends in (?![α-ω]) rather than \b because JS \b is ASCII-only and never
 * matches after a Greek letter. */
const THINK_HARD_PREFIX =
  /^\s*(?:think\s+hard|think\s+deeply|think\s+carefully|deeply\s+think)\b\s*(?:[:,\-\u2013\u2014]\s*|\b(?:about|on)\b\s+)?/;

const THINK_HARD_PREFIX_EL =
  /^\s*(?:σκεψου|σκεψε)\s+(?:(?:ας|το|τη|την|αυτο)\s+)?(?:καλα|σοβαρα|αναλυτικα|πολυ)(?![α-ω])\s*(?:[:,\-\u2013\u2014]\s*)?|^\s*(?:σκεψου|σκεψε)\s*[:,\-\u2013\u2014]\s*/;

/** Strip a leading think-hard marker.
 * Returns {message, thinkHard}; when the marker is absent `thinkHard` is false
 * and `message` is the trimmed input. A bare marker with no request after it is
 * NOT an escalation, so an empty turn is never sent to the hard model. */
export function parseThinkHard(text: string, language = "en"): { message: string; thinkHard: boolean } {
  const source = text ?? "";
  const greek = isGreek(language) || /[\u0370-\u03ff]/.test(source);
  const rest = afterPrefix(source, greek ? THINK_HARD_PREFIX_EL : THINK_HARD_PREFIX);
  const message = (rest ?? source).trim();
  if (rest === null || !message) return { message: source.trim(), thinkHard: false };
  return { message, thinkHard: true };
}

export function formatDuration(totalSeconds: number, language = "en"): string {
  const greek = isGreek(language);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const parts: string[] = [];
  if (hours) parts.push(`${hours} ${greek ? hours === 1 ? "ώρα" : "ώρες" : hours === 1 ? "hour" : "hours"}`);
  if (minutes) parts.push(`${minutes} ${greek ? minutes === 1 ? "λεπτό" : "λεπτά" : minutes === 1 ? "minute" : "minutes"}`);
  if (seconds || !parts.length) parts.push(`${seconds} ${greek ? seconds === 1 ? "δευτερόλεπτο" : "δευτερόλεπτα" : seconds === 1 ? "second" : "seconds"}`);
  return parts.join(" ");
}

/* ---------- "<command> on terminal N" ----------
 * "open top on terminal 2", "run ls -la in the second terminal", "άνοιξε το top
 * στο τερματικο 2". This is NOT a window command: the window action is done by
 * focusing that terminal, and the rest of the sentence is a real request that
 * must still reach the agent. The provider uses the number to pick the terminal
 * window, focus it and send its session id as focused_terminal, so
 * terminal_command lands the command in exactly that window.
 *
 * The number may sit before the noun ("in the second terminal"), after it
 * ("in terminal 2", "in terminal one") or both ("in the second terminal 2").
 * The target phrase is a suffix, so the command is the prefix; `message` is
 * returned from the ORIGINAL text (not the accent-stripped form) and the
 * operator's own sentence is returned in `text` for the chat transcript. */
export type TerminalTargeted = { message: string; target: number; text: string };

export function parseTerminalTarget(text: string, language = "en"): TerminalTargeted | null {
  const source = (text ?? "").trim();
  if (!source) return null;
  const greek = isGreek(language) || /[\u0370-\u03ff]/.test(source);
  const norm = normalize(source);
  const dict: Record<string, number> = {
    ...EN_ORDINALS,
    ...(greek ? EL_ORDINALS : EN_NUMBERS),
  };
  const words = Object.keys(dict)
    .sort((a, b) => b.length - a.length)
    .join("|");
  // group 1 = ordinal/number before the noun, groups 2/3 = number after it.
  const pattern = greek
    ? new RegExp(
        `(?:στο|στη|στην|σε|μέσα\\s+σε)\\s+(?:το|τη|την)?\\s*` +
        `(?:(${words})\\s+)?(?:τερματικο|τερματικα|τερματικης|τερματικες|κονσολα)` +
        `(?:\\s+(?:αριθμο(?:ς)?|number))?\\s*(?:#?(\\d{1,2})|#?(${words}))?`)
    : new RegExp(
        `(?:on|in|inside|into|to)\\s+(?:the\\s+)?(?:(${words})\\s+)?` +
        `(?:terminal|console)s?` +
        `(?:\\s+(?:number|no))?\\s*(?:#?(\\d{1,2})|#?(${words}))?`, "i");

  const match = pattern.exec(norm);
  if (!match) return null;

  const named = match[1] ? dict[match[1]] : undefined;
  const target = match[2] ? Number(match[2]) : named ?? (match[3] ? dict[match[3]] : undefined);
  if (target === undefined || !Number.isFinite(target) || target < 1 || target > 99) return null;

  // Nothing before the target: that is "focus terminal 2", a window command.
  const prefix = norm.slice(0, match.index).trim();
  if (!prefix) return null;

  return {
    message: originalSlice(source, 0, match.index).trim() || prefix,
    target,
    text: source,
  };
}
