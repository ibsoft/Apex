/* Math segmentation for assistant replies.

   A reply is split into plain-text and math segments so each piece can be
   rendered by the right component: text through the URL/image scanner, math
   through KaTeX. `splitMath` is pure and dependency-free so it can be unit
   tested in Node; the actual KaTeX call lives in the component.

   Supported delimiters: `$...$` and `\(...\)` for inline, `$$...$$` and
   `\[...\]` for display. A `\` escapes the next character, so a literal
   dollar sign survives. */

export type MathSegment = {
  type: "text" | "math";
  text: string;
  /** Block (display) math is centred; inline stays in the line of prose. */
  display: boolean;
};

/** An inline pair is math only when the content is tight: no surrounding
 *  whitespace, no newline. "costs $5 and $7" is money, not an equation. */
function isInlineMath(tex: string): boolean {
  return tex.length > 0 && tex === tex.trim() && !tex.includes("\n");
}

export function splitMath(input: string): MathSegment[] {
  const segments: MathSegment[] = [];
  let text = "";
  let i = 0;

  const pushText = () => {
    if (text) {
      segments.push({ type: "text", text, display: false });
      text = "";
    }
  };
  const pushMath = (tex: string, display: boolean) => {
    pushText();
    segments.push({ type: "math", text: tex.trim(), display });
  };

  while (i < input.length) {
    const ch = input[i];

    if (ch === "\\" && (input[i + 1] === "(" || input[i + 1] === "[")) {
      const display = input[i + 1] === "[";
      const close = input.indexOf(display ? "\\]" : "\\)", i + 2);
      if (close !== -1) {
        pushMath(input.slice(i + 2, close), display);
        i = close + 2;
        continue;
      }
    }

    if (ch === "$") {
      const display = input[i + 1] === "$";
      const delimiter = display ? "$$" : "$";
      const close = input.indexOf(delimiter, i + delimiter.length);
      if (close !== -1) {
        const tex = input.slice(i + delimiter.length, close);
        if (display ? tex.trim().length > 0 : isInlineMath(tex)) {
          pushMath(tex, display);
          i = close + delimiter.length;
          continue;
        }
      }
    }

    if (ch === "\\") {
      // Keep the escape and the escaped character verbatim so the text
      // renderer sees the same string the model wrote.
      text += input.slice(i, i + 2);
      i += 2;
      continue;
    }

    text += ch;
    i += 1;
  }

  pushText();
  return segments;
}
