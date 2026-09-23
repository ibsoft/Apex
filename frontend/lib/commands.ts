/** Shared parser for typed and spoken local commands. Greek is opt-in; English
 * remains available in every language. Captures always retain the user's text. */
export type LocalCommand =
  | { type: "preview"; action: "close" | "maximize" | "restore" | "next" | "previous" }
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

export function parseLocalCommand(text: string, language: string, skills: Array<{ name: string }>, now = Date.now()): LocalCommand | null {
  const clean = text.trim().replace(/[.!?;·;]+$/, "").trim();
  if (!clean) return null;
  const greek = isGreek(language);
  const normalized = normalize(clean);
  const previewTarget = "(?:\\s+(?:the\\s+)?(?:preview|it|window|image|document|that))?";
  const greekPreviewTarget = "(?:\\s+(?:(?:τη|την|το)\\s+)?(?:προεπισκοπηση|παραθυρο|εικονα|εγγραφο|αυτο))?";
  const previewPatterns: Array<["close" | "maximize" | "restore" | "next" | "previous", string, string]> = [
    ["close", `(?:close|hide|dismiss|shut)${previewTarget}`, `(?:κλεισε|κρυψε|αποκρυψε)${greekPreviewTarget}`],
    ["maximize", `(?:maxim(?:ize|ise)|full[-\\s]?screen|enlarge|expand)${previewTarget}`, `(?:(?:μεγιστοποιησε|μεγεθυνε|επεκτεινε)${greekPreviewTarget}|πληρησ?\\s+οθονη)`],
    ["restore", `(?:normali(?:ze|ise)|minimize|shrink|restore|small(?:er)?\\s+window)${previewTarget}`, `(?:επαναφερε|ελαχιστοποιησε|μικρυνε)${greekPreviewTarget}`],
    ["next", "(?:next|forward)(?:\\s+(?:image|one|photo|picture|page))?", "(?:επομενο|επομενη|μπροστα)(?:\\s+(?:εικονα|φωτογραφια|σελιδα|εγγραφο))?"],
    ["previous", "(?:previous|back|last|prev|earlier)(?:\\s+(?:image|one|photo|picture|page))?", "(?:προηγουμενο|προηγουμενη|πισω)(?:\\s+(?:εικονα|φωτογραφια|σελιδα|εγγραφο))?"],
  ];
  for (const [action, en, el] of previewPatterns) {
    if (new RegExp(`^(?:${en}${greek ? `|${el}` : ""})$`).test(normalized)) return { type: "preview", action };
  }
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
