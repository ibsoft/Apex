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

export function wakePattern(wakeWord: string, language = "en"): RegExp {
  const word = wakeWord.trim();
  if (!word) return /(?!)/;
  const aliases = [wordPattern(word)];
  if (language === "el" && word.toLowerCase() === "apex") aliases.push(wordPattern("απεξ"));
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
  if (language === "el" && aliases[0] === "apex") aliases.push("απεξ");
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
  const greekWakeWord = wakeWord.trim().toLowerCase() === "apex" || /[\u0370-\u03ff]/.test(wakeWord);
  // Default/Greek wake words must be transcribed in Greek to preserve a Greek
  // command spoken in the same utterance. Custom Latin wake words retain the
  // previous English standby + Greek command/follow-up behavior.
  return greekWakeWord || phase === "awake" || (phase === "standby" && armed) ? "el-GR" : "en-US";
}
