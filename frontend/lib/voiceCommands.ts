/** Command matching shared by the voice listener and its browser-free tests. */

function normalize(text: string): string {
  return text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/ς/g, "σ");
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function wordPattern(word: string): string {
  const vowels: Record<string, string> = {
    α: "[αά]", ε: "[εέ]", η: "[ηή]", ι: "[ιίϊΐ]",
    ο: "[οό]", υ: "[υύϋΰ]", ω: "[ωώ]", σ: "[σς]",
  };
  return Array.from(word.normalize("NFC").toLowerCase()).map((letter) => {
    // Accept both precomposed and decomposed accents in Greek transcripts.
    const greekLetter = normalize(letter);
    if (vowels[greekLetter]) return `${vowels[greekLetter]}[\\u0300-\\u036f]*`;
    return /\s/.test(letter) ? "\\s+" : escapeRegExp(letter);
  }).join("");
}

/* Greek phonetic spellings of the default English "apex" wake word. The el-GR
 * recognizer transcribes the same sound several ways (άπεξ, έιπεξ, αϊπεξ,
 * απεχς …); accepting all of them keeps Greek wake detection as responsive as
 * the English one instead of depending on the spelling Chrome happens to pick. */
const GREEK_APEX_ALIASES = ["απεξ", "ειπεξ", "αιπεξ", "απεχς"];

function greekApexAliases(wakeWord: string): string[] {
  const word = normalize(wakeWord.trim());
  return word === "apex" || word === "απεξ" ? GREEK_APEX_ALIASES : [];
}

export function wakePattern(wakeWord: string, language = "en"): RegExp {
  const word = wakeWord.trim();
  if (!word) return /(?!)/;
  const words = [word];
  if (language === "el") {
    // Greek aliases are normalized (accent-insensitive, final sigma folded),
    // so dedupe against their normalized form before adding.
    for (const greek of greekApexAliases(wakeWord)) {
      if (!words.some((w) => normalize(w) === greek)) words.push(greek);
    }
  }
  const aliases = words.map(wordPattern);
  // JavaScript's \b treats Greek letters as non-word characters.
  return new RegExp(`(?<![\\p{L}\\p{N}_])(?:${aliases.join("|")})(?![\\p{L}\\p{N}_])`, "iu");
}

export function isWakeOnlyText(text: string, wakeWord: string, language = "en"): boolean {
  const match = text.match(wakePattern(wakeWord, language));
  if (!match || match.index === undefined) return false;
  const remainder = text.slice(0, match.index) + text.slice(match.index + match[0].length);
  return !/[\p{L}\p{N}]/u.test(remainder);
}

export function isWakeWordFragment(text: string, wakeWord: string, language = "en"): boolean {
  const fragment = normalize(text).replace(/[.!?,;:··;]+/g, "").trim();
  const aliases = [normalize(wakeWord.trim())];
  if (language === "el") {
    for (const greek of greekApexAliases(wakeWord)) {
      if (!aliases.includes(greek)) aliases.push(greek);
    }
  }
  return fragment.length < 2 || aliases.some((word) => word.startsWith(fragment));
}

const ENGLISH_SLEEP = /^(?:stop(?:\s+listening)?|sleep|good\s*bye|good\s*night|never\s*mind|that['’]?s\s*all|dismiss|quiet|go\s*to\s*sleep|stand\s*down)(?:[\s,]+(?:now|please|thank\s+you|thanks))?$/i;
const GREEK_SLEEP = /^(?:σταματα(?: να ακουσ)?|σταματησε(?: να ακουσ)?|κοιμησου|πηγαινε για υπνο|μπεσ σε αναμονη|πηγαινε σε αναμονη|καληνυχτα|αντιο|αστο|ασ['’]?\s+το|αυτο ηταν|αυτα ηταν|τελοσ|ακυρο)(?:[\s,]+(?:σε παρακαλω|παρακαλω|ευχαριστω))?$/;

export function isSleepCommand(text: string, language = "en"): boolean {
  const phrase = text.trim().replace(/[.!?,;:··;]+$/g, "").trim();
  if (ENGLISH_SLEEP.test(phrase)) return true;
  if (language !== "el") return false;
  // Whole phrases avoid swallowing actions such as "σταμάτα το χρονόμετρο"
  // and "stop timers", or the autonomous-silence shortcut "stop talking".
  const clean = normalize(phrase).replace(/\s+/g, " ");
  return GREEK_SLEEP.test(clean);
}

/* ---------- wake acknowledgement ---------- */

/* Short spoken confirmations played right after the wake word. They tell the
 * user the mic caught them without covering the command that usually follows,
 * so they stay under ~1.5 words. */
const WAKE_ACKS_EN = [
  "Go ahead.",
  "I'm listening.",
  "Yes?",
  "Ready.",
  "I'm here.",
  "What do you need?",
  "Go for it.",
  "Tell me.",
  "I'm ready.",
  "Standing by.",
  "At your service.",
  "How can I help?",
  "Listening.",
  "Proceed.",
  "What's next?",
  "APEX online.",
  "APEX ready.",
  "I'm with you.",
  "Say the word.",
];

const WAKE_ACKS_EL = [
  "Πες μου.",
  "Σε ακούω.",
  "Ναι;",
  "Έτοιμος.",
  "Είμαι εδώ.",
  "Τι χρειάζεσαι;",
  "Πάμε.",
  "Πες το.",
  "Είμαι έτοιμος.",
  "Σε ετοιμότητα.",
  "Στη διάθεσή σου.",
  "Πώς μπορώ να βοηθήσω;",
  "Ακούω.",
  "Συνέχεισε.",
  "Τι ακολουθεί;",
  "Το APEX είναι έτοιμο.",
  "APEX σε ετοιμότητα.",
  "Είμαι μαζί σου.",
  "Πες τον λόγο σου.",
];

export function wakeAcks(language = "en"): readonly string[] {
  return /^el(?:-|$)/i.test((language || "en").trim()) ? WAKE_ACKS_EL : WAKE_ACKS_EN;
}

/**
 * Pick the acknowledgement to speak after the wake word, without repeating the
 * previous one back to back. Falls back to a uniform pick when the list is
 * empty or `previous` is not one of its entries (start-up, language switch).
 */
export function pickWakeAck(
  language = "en",
  previous?: string | null,
  rand: () => number = Math.random,
): string {
  const acks = wakeAcks(language);
  if (!acks.length) return "";
  const pool = previous && acks.includes(previous) ? acks.filter((a) => a !== previous) : acks;
  const pick = pool[Math.min(pool.length - 1, Math.max(0, Math.floor(rand() * pool.length)))];
  return pick ?? acks[0];
}

/**
 * Drop an acknowledgement the recognizer heard back from the speaker while the
 * command was collected, so a command is never prefixed or replaced by it.
 * Matching is accent/punctuation-insensitive, and the whole utterance is dropped
 * when the echo is all that was heard.
 */
export function stripAckEcho(text: string, ack: string | null | undefined): string {
  if (!ack) return text;
  // Per-token normalisation, so punctuation, accents and capitalisation from
  // the recognizer never stop the echo from matching.
  const clean = (value: string) =>
    normalize(value).replace(/[.!?,;:··;'"’]+/g, "").toLowerCase();
  const words = clean(ack).split(/\s+/).filter(Boolean);
  if (!words.length) return text;
  const original = text.split(/\s+/).filter(Boolean);
  const parts = original.map(clean);
  const kept: string[] = [];
  for (let i = 0; i < parts.length; i++) {
    const end = i + words.length;
    if (end <= parts.length && parts.slice(i, end).join(" ") === words.join(" ")) {
      i = end - 1;
      continue;
    }
    kept.push(original[i]);
  }
  return kept.join(" ").trim();
}

/* ---------- resume after an accidental interruption ---------- */

/* The user interrupts APEX by talking, gets cut off, then realises it was an
 * accident and says "continue". The voice engine only consumes this phrase when
 * it actually has a saved reply remainder (see voice.ts); a plain "continue"
 * with nothing to resume still reaches the agent as a normal request. */
const ENGLISH_RESUME = /^(?:continue|go\s*on|carry\s*on|keep\s*going|resume|proceed|pick\s*(?:it|things)?\s*up)(?:[\s,]+(?:please|now|thanks|thank\s*you))?$/i;
/* Written in normalized space (final sigma folded: "πες" -> "πεσ"). */
const GREEK_RESUME = /^(?:συνεχισ(?:ε)?|συνεχεια|πεσ\s+το\s+υπολοιπο|πεσ\s+τα\s+υπολοιπα)(?:[\s,]+(?:σε\s+παρακαλω|παρακαλω|ευχαριστω))?$/;

export function isResumeCommand(text: string, language = "en"): boolean {
  const phrase = text.trim().replace(/[.!?,;:··;]+$/g, "").trim();
  if (ENGLISH_RESUME.test(phrase)) return true;
  if (language !== "el") return false;
  return GREEK_RESUME.test(normalize(phrase).replace(/\s+/g, " "));
}

/* ---------- voice-activity interruption (barge-in) ---------- */

/* While the assistant is speaking, a separate mic analyser measures loudness.
 * The user talking at the same time raises the RMS level; a run of loud frames
 * cuts the speech off. These pure helpers map the 1..10 sensitivity slider onto
 * an RMS threshold and make the sustained-loudness decision with hysteresis, so
 * browser tests can pin the exact behaviour. */

export type VadState = { loud: number; quiet: number; fired: boolean };

export function initialVadState(): VadState {
  return { loud: 0, quiet: 0, fired: false };
}

/** Consecutive loud frames (at ~60 fps one is ~17 ms) that cut speech. */
export const VAD_LOUD_FRAMES = 4;
/** Quiet frames below the hysteresis band needed before firing again. */
export const VAD_REARM_FRAMES = 4;

export function clampSensitivity(sensitivity: number, min = 1, max = 10): number {
  if (!Number.isFinite(sensitivity)) return 6;
  return Math.max(min, Math.min(max, Math.round(Number(sensitivity))));
}

/** Map the 1..10 slider onto an RMS threshold (1 = hardest to interrupt). */
export function sensitivityToThreshold(sensitivity: number): number {
  const s = clampSensitivity(sensitivity);
  return Number((0.11 - (s - 1) * 0.009).toFixed(4));
}

/**
 * Feed one measured RMS frame into the state machine.
 *
 * - rms >= threshold accumulates a loud run; a run of `VAD_LOUD_FRAMES` fires
 *   once, and stays fired (no re-fire) until the level drops below
 *   `threshold * 0.6` for `VAD_REARM_FRAMES` quiet frames.
 * - levels inside the hysteresis band (threshold*0.6 .. threshold) neither add
 *   loudness nor reset the quiet counter, so a short dip in a loud burst does
 *   not re-arm mid-interruption.
 */
export function vadStep(
  state: VadState,
  rms: number,
  threshold: number,
  loudFrames = VAD_LOUD_FRAMES,
  rearmFrames = VAD_REARM_FRAMES,
): { state: VadState; fire: boolean } {
  let { loud, quiet, fired } = state;
  if (rms >= threshold) {
    quiet = 0;
    loud += 1;
    if (!fired && loud >= loudFrames) {
      return { state: { loud, quiet, fired: true }, fire: true };
    }
  } else if (rms < threshold * 0.6) {
    loud = 0;
    quiet += 1;
    if (fired && quiet >= rearmFrames) fired = false;
  } else {
    quiet = 0; // hysteresis band: hold position
  }
  return { state: { loud, quiet, fired }, fire: false };
}

/* ---------- utterance accumulation (pause-safe endpointing) ---------- */

export type ResultSnapshot = {
  /** First result index that belongs to the current command. */
  from: number;
  /** First result index not yet absorbed into `finals`. */
  committed: number;
  /** Text from finalized results, each joined with a trailing space. */
  finals: string;
  /** Live text of the currently-interim (still being spoken) result. */
  interim: string;
};

export function emptyResultSnapshot(from = 0): ResultSnapshot {
  return { from, committed: from, finals: "", interim: "" };
}

/**
 * Merge a fresh `SpeechRecognition` result list into the snapshot. Web Speech
 * finalizes an in-progress chunk after a pause and then appends a new result
 * for the rest of the sentence; this merges every finalized chunk plus the live
 * interim so a mid-sentence pause never drops the leading part of a command.
 * Results below `snap.from` (pre-wake noise, assistant TTS echo) are ignored.
 */
function resultTranscript(result: any): string {
  const first = result?.[0];
  if (first && typeof first.transcript === "string") return first.transcript;
  return typeof result?.transcript === "string" ? result.transcript : "";
}

export function accumulateResults(
  results: readonly { isFinal?: boolean }[],
  snap: ResultSnapshot,
): ResultSnapshot {
  const n = results?.length ?? 0;
  if (n <= snap.from) return snap;
  const out: ResultSnapshot = { ...snap, committed: snap.committed };
  const lastIdx = n - 1;
  for (let i = Math.max(snap.from, snap.committed); i < lastIdx; i++) {
    const result = results[i];
    const transcript = resultTranscript(result);
    if (result?.isFinal && transcript) {
      out.finals += transcript + " ";
    }
  }
  out.committed = lastIdx;
  const last = results[lastIdx];
  const lastText = resultTranscript(last);
  if (last && last.isFinal) {
    if (lastText) out.finals += lastText + " ";
    out.interim = "";
    out.committed = n;
  } else {
    out.interim = lastText;
  }
  return out;
}

export function commandText(snap: ResultSnapshot): string {
  const joined = [snap.finals.trim(), snap.interim].filter(Boolean).join(" ");
  return joined.replace(/\s+/g, " ").trim();
}

/**
 * Strip the wake word and any leading noise/punctuation from accumulated text,
 * sliced at the LAST wake-word occurrence so speech before and after a pause
 * that surrounds the wake word is all preserved.
 */
export function sliceAfterLastWake(text: string, wakeWord: string, language = "en"): string {
  const input = text.trim();
  if (!input) return "";
  const base = wakePattern(wakeWord, language);
  const global = base.global ? base : new RegExp(base.source, base.flags + "g");
  let last: RegExpExecArray | null = null;
  for (let m = global.exec(input); m; m = global.exec(input)) {
    last = m;
  }
  if (!last || last.index === undefined) return input;
  return input
    .slice(last.index + last[0].length)
    .replace(/^[\s,.;:!?··;]+/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function recognitionLanguage(
  responseLanguage: string,
  wakeWord: string,
  phase: string,
  armed: boolean,
): string {
  if (responseLanguage !== "el") return "en-US";
  const greekWakeWord = /[\u0370-\u03ff]/.test(wakeWord);
  // Standby listens for the wake word, so an English wake word (including the
  // default "apex") needs en-US to stay responsive. Once the wake word is heard
  // (awake) or a follow-up window arms, the session switches to el-GR so the
  // command is transcribed in Greek. Only wake words spelled with Greek letters
  // keep the standby recognizer on el-GR.
  return greekWakeWord || phase === "awake" || (phase === "standby" && armed) ? "el-GR" : "en-US";
}

/**
 * The text the TTS engine should speak. The assistant's reply is spoken
 * verbatim, so raw command output pasted into the message must never reach the
 * voice: fenced code blocks (and tables, which are the shape of `df -h`, `ps`,
 * `ls`) are removed, and heading/list markers are stripped. What remains is the
 * prose summary. Pure and unit-tested so the guarantee does not depend on the
 * model obeying a prompt.
 */
export function speechSummary(text: string): string {
  if (!text) return "";
  let s = text.replace(/\r\n?/g, "\n");
  s = s.replace(/```[\s\S]*?```/g, " ");
  s = s.replace(/~~~[\s\S]*?~~~/g, " ");
  s = s.replace(/```[\s\S]*$/g, " ").replace(/~~~[\s\S]*$/g, " ");
  s = s
    .split("\n")
    .filter((ln) => ((ln.match(/\|/g) || []).length < 2))
    .join("\n");
  s = s.replace(/^\s{0,3}#{1,6}\s+/gm, "");
  s = s.replace(/^\s{0,3}[-*+]\s+/gm, "");
  s = s.replace(/^\s{0,3}\d+[.)]\s+/gm, "");
  s = s.replace(/\*\*/g, "").replace(/__/g, "");
  s = s.replace(/`([^`]*)`/g, "$1");
  return s.replace(/[ \t]+/g, " ").replace(/\n{2,}/g, "\n").trim();
}
