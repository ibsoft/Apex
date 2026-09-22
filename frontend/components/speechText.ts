/** Keep useful search descriptions in speech, without reading link destinations. */
export function speechText(text: string): string {
  const clean = (text || "")
    // Reference-link definitions are metadata, not part of the spoken answer.
    .replace(/^\s{0,3}\[[^\]\n]+\]:\s*\S+[^\n]*$/gm, "")
    // Speak the label/alt text of Markdown links and images. URL parentheses
    // (common in article names) and optional Markdown titles are supported.
    .replace(/!?\[((?:\\.|[^\]\\])*)\]\(\s*(?:<[^>\n]*>|(?:[^()\s]|\([^()]*\))+)(?:\s+["'][^\n]*?["'])?\s*\)/g, "$1")
    .replace(/!?\[([^\]]+)\]\[[^\]]*\]/g, "$1")
    .replace(/<(?:https?:\/\/|www\.)[^>\s]+>/gi, "")
    .replace(/\b(?:https?:\/\/|www\.)[^\s<>"`]+/gi, (url) => url.match(/[.,!?;:]+$/)?.[0] || "")
    .replace(/\bdata:image\/[^\s<>]+/gi, "")
    .replace(/\\([\\\[\]])/g, "$1")
    .replace(/\(\s*\)|\[\s*\]/g, "")
    .replace(/\s+([,.;:!?])/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
  return /[\p{L}\p{N}]/u.test(clean) ? clean : "";
}
