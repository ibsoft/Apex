/** The context a voice turn is interpreted against: the recent exchange and
 *  the open player, as one block of text for the command router.
 *
 *  Voice arrives at the reasoning layer *first* (see `handleVoiceCommand`), so
 *  an instruction that does not name its target - "bigger", "again", "that one",
 *  "put it back" - can only be read against what the operator was just doing.
 *  The keyword parsers are the fallback for the commands no catalogue can
 *  express (a reminder's text, a skill name), not the first opinion.
 *
 *  DOM-free and pure like `media.ts`: the caller owns the refs, this only shapes
 *  what the model reads.
 */

import { mediaContextBlock, type MediaState } from "./media";

export type ContextMessage = { role?: string; content?: string };

/** How many of the tail of the conversation are carried. Six exchanges is
 *  enough for "do that again" and pronouns without rebuilding the thread. */
export const CONTEXT_MESSAGES = 6;

/** One line of chat, clipped: a pasted log would otherwise spend the whole
 *  context budget on a single message. */
const LINE_CHARS = 400;

/** Total budget for the block. The route clips again (`MAX_CONTEXT_CHARS`) on
 *  the server; clipping here keeps the request itself honest. */
const BLOCK_CHARS = 4000;

const clip = (text: string, limit: number): string => {
  const one = text.replace(/\s+/g, " ").trim();
  return one.length > limit ? `${one.slice(0, limit - 1)}…` : one;
};

/** Build the block, or "" when there is nothing to say - the caller omits an
 *  empty field rather than sending a section with no content. */
export function commandContextBlock(input: {
  media?: MediaState | null;
  messages?: ContextMessage[] | null;
  skill?: string | null;
}): string {
  const parts: string[] = [];
  const messages = (input.messages ?? [])
    .slice(-CONTEXT_MESSAGES)
    .filter((entry) => (entry?.role === "user" || entry?.role === "assistant") && (entry.content ?? "").trim());
  if (messages.length) {
    const lines = messages.map((entry) => `${entry.role}: ${clip(entry.content ?? "", LINE_CHARS)}`);
    parts.push(`Recent conversation:\n${lines.join("\n")}`);
  }
  const player = mediaContextBlock(input.media);
  if (player) parts.push(player);
  const skill = (input.skill ?? "").trim();
  if (skill) parts.push(`[Active skill: ${skill}]`);
  const block = parts.join("\n");
  return block.length > BLOCK_CHARS ? `${block.slice(0, BLOCK_CHARS - 1)}…` : block;
}
