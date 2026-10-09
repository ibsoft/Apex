/* Message-body parsing that the chat renderer needs but a URL regex cannot do.

   `renderRichText` in ChatUI walks a single URL-matching regex and knows
   nothing about block structure, so a fenced code block arrived as literal
   backticks inside a `white-space: pre-wrap` span. This module pulls the
   fenced blocks out first, leaving the renderer to handle prose and links
   exactly as it did; the blocks are then shown in their own bubble with a
   language label and a copy button.

   It is a pure function on purpose: the renderer is a component and cannot be
   exercised by the node test harness, so the part with the off-by-one risk
   (fence state, an unclosed fence, a tilde fence) lives here and is tested
   directly. */

export type ContentSegment =
  | { type: "text"; text: string }
  | { type: "code"; language: string; code: string };

/** Split text into prose and fenced code blocks, preserving everything else.
 *
 *  A fence opens on a line that, after optional indentation, is three or more
 *  backticks or tildes optionally followed by a language word; it closes on
 *  the next line that is the same fence character (three or more) and nothing
 *  else. Closing fence length does not have to match the opener - CommonMark
 *  only requires it to be at least as long, and being lax here is harmless.
 *
 *  An unclosed fence runs to the end of the text: that is exactly what a
 *  streaming reply looks like in the moment before its terminator arrives, and
 *  treating it as literal backticks would flicker the block in and out. */
export function splitFencedCode(text: string): ContentSegment[] {
  const segments: ContentSegment[] = [];
  const lines = text.split("\n");
  let prose: string[] = [];

  const flushProse = () => {
    if (prose.length) {
      segments.push({ type: "text", text: prose.join("\n") });
      prose = [];
    }
  };

  let i = 0;
  while (i < lines.length) {
    const open = /^[ \t]*(`{3,}|~{3,})[ \t]*([^\s`~]*)[ \t]*$/.exec(lines[i]);
    if (!open) {
      prose.push(lines[i]);
      i += 1;
      continue;
    }
    const fenceChar = open[1][0];
    const language = open[2] || "";
    flushProse();
    i += 1;
    const body: string[] = [];
    while (i < lines.length) {
      const close = /^[ \t]*(`{3,}|~{3,})[ \t]*$/.exec(lines[i]);
      if (close && close[1][0] === fenceChar) {
        i += 1;
        break;
      }
      body.push(lines[i]);
      i += 1;
    }
    segments.push({ type: "code", language, code: body.join("\n") });
  }

  flushProse();
  return segments;
}
