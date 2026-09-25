/** Shared parser for typed and spoken local commands. Greek is opt-in; English
 * remains available in every language. Captures always retain the user's text. */
import type { WindowArrangement } from "./windows";

export type LocalCommand =
  | {
      type: "window";
      action: "open" | "close" | "close_all" | "focus" | "maximize" | "minimize"
        | "restore" | "arrange" | "next" | "previous" | "list" | "note";
      target?: number;
      arrangement?: WindowArrangement;
      note?: string;
    }
  | { type: "terminal"; action: "open" | "close" | "focus"; target?: number; create?: boolean }
  | { type: "cancelTimers" }
  | { type: "cancelReminders" }
  | { type: "timer"; name: string; seconds: number }
  | { type: "reminder"; name: string; fireAt: number }
  | { type: "operator"; name?: string }
  | { type: "autonomy"; enabled: boolean }
  | { type: "silence" }
  | { type: "images"; query: string; source: "web" | "local" }
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
      || (greek && /^(?:κλεισε|κρυψε|αποκρυψε)\s+(?:(?:ολα|ολα\s+τα|τα)\s+)?παραθυρα$/.test(normalized)))
    return { type: "window", action: "close_all" };

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
  const finish = (rest: string, ordinal: number | undefined, action: "open" | "close" | "focus", createOpt: {}): LocalCommand | null => {
    const target = ordinal ?? (rest ? trailingOf(rest) : undefined);
    if (target !== undefined && !Number.isNaN(target)) return { type: "terminal", action, target, ...createOpt };
    if (rest.trim() !== "") return null;
    return { type: "terminal", action, ...createOpt };
  };

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

  return null;
}

export function parseLocalCommand(text: string, language: string, skills: Array<{ name: string }>, now = Date.now()): LocalCommand | null {
  const clean = text.trim().replace(/[.!?;·;]+$/, "").trim();
  if (!clean) return null;
  const greek = isGreek(language);
  const normalized = normalize(clean);
  const terminal = parseTerminalCommand(clean, greek);
  if (terminal) return terminal;
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
