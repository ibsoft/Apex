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
