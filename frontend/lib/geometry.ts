/* A tiny, dependency-free geometry language for assistant replies.

   A fenced ```geometry block is parsed here into SVG primitives. The parser is
   pure (no React, no DOM) so it is unit tested in Node; `GeometryBlock` in the
   chat panel only paints the result.

   Coordinates live in a fixed 0..100 box (viewBox "-5 -5 110 110", so strokes
   at the edges are not clipped). One directive per line; `#` starts a comment.

     line x1 y1 x2 y2
     rect x y w h
     circle cx cy r
     polygon x1 y1 x2 y2 ...
     polyline x1 y1 x2 y2 ...
     point x y [label]
     text x y label
     angle vx vy ax ay bx by [r]   # arc at vertex between two arms

   Unknown commands and malformed numbers are reported as an `error` rather
   than silently dropped, so a bad diagram is visible to the operator. Whatever
   parsed before the bad line is still returned and rendered. */

export type GeoElement =
  | { kind: "line"; x1: number; y1: number; x2: number; y2: number }
  | { kind: "rect"; x: number; y: number; w: number; h: number }
  | { kind: "circle"; cx: number; cy: number; r: number }
  | { kind: "poly"; points: [number, number][]; closed: boolean }
  | { kind: "point"; x: number; y: number; label: string }
  | { kind: "text"; x: number; y: number; label: string }
  | { kind: "angle"; vertex: [number, number]; a: [number, number]; b: [number, number]; r: number };

export type Geometry = {
  viewBox: string;
  elements: GeoElement[];
  error: string;
};

const VIEW_BOX = "-5 -5 110 110";

function num(token: string): number | null {
  if (!/^[-+]?(\d+\.?\d*|\.\d+)$/.test(token)) return null;
  const value = Number(token);
  return Number.isFinite(value) ? value : null;
}

function nums(tokens: string[], count: number): number[] | null {
  if (tokens.length !== count) return null;
  const out: number[] = [];
  for (const token of tokens) {
    const value = num(token);
    if (value === null) return null;
    out.push(value);
  }
  return out;
}

function label(rest: string[]): string {
  let text = rest.join(" ").trim();
  if (text.length >= 2 && ((text[0] === '"' && text.endsWith('"')) || (text[0] === "'" && text.endsWith("'")))) {
    text = text.slice(1, -1);
  }
  return text;
}

function dist(a: [number, number], b: [number, number]): number {
  return Math.hypot(a[0] - b[0], a[1] - b[1]);
}

export function parseGeometry(source: string): Geometry {
  const elements: GeoElement[] = [];
  const lines = source.split(/\r?\n/);
  const result = (error: string): Geometry => ({ viewBox: VIEW_BOX, elements, error });

  for (let index = 0; index < lines.length; index++) {
    const raw = lines[index].split("#")[0].trim();
    if (!raw) continue;
    const tokens = raw.split(/\s+/);
    const command = tokens[0].toLowerCase();
    const rest = tokens.slice(1);
    const at = `line ${index + 1}: `;

    let el: GeoElement | null = null;
    switch (command) {
      case "line": {
        const v = nums(rest, 4);
        if (!v) return result(`${at}line needs 4 numbers, got "${rest.join(" ")}"`);
        el = { kind: "line", x1: v[0], y1: v[1], x2: v[2], y2: v[3] };
        break;
      }
      case "rect": {
        const v = nums(rest, 4);
        if (!v) return result(`${at}rect needs 4 numbers (x y w h), got "${rest.join(" ")}"`);
        el = { kind: "rect", x: v[0], y: v[1], w: v[2], h: v[3] };
        break;
      }
      case "circle": {
        const v = nums(rest, 3);
        if (!v || v[2] <= 0) return result(`${at}circle needs cx cy r with r > 0`);
        el = { kind: "circle", cx: v[0], cy: v[1], r: v[2] };
        break;
      }
      case "polygon":
      case "polyline": {
        if (rest.length < 4 || rest.length % 2 !== 0) {
          return result(`${at}${command} needs x y pairs`);
        }
        const v = nums(rest, rest.length);
        if (!v) return result(`${at}${command} coordinates must be numbers`);
        const points: [number, number][] = [];
        for (let i = 0; i < v.length; i += 2) points.push([v[i], v[i + 1]]);
        el = { kind: "poly", points, closed: command === "polygon" };
        break;
      }
      case "point": {
        if (rest.length < 2) return result(`${at}point needs x y`);
        const v = nums(rest.slice(0, 2), 2);
        if (!v) return result(`${at}point coordinates must be numbers`);
        el = { kind: "point", x: v[0], y: v[1], label: label(rest.slice(2)) };
        break;
      }
      case "text": {
        if (rest.length < 3) return result(`${at}text needs x y label`);
        const v = nums(rest.slice(0, 2), 2);
        if (!v) return result(`${at}text coordinates must be numbers`);
        el = { kind: "text", x: v[0], y: v[1], label: label(rest.slice(2)) };
        break;
      }
      case "angle": {
        if (rest.length !== 6 && rest.length !== 7) {
          return result(`${at}angle needs vx vy ax ay bx by [r]`);
        }
        const v = nums(rest, rest.length);
        if (!v) return result(`${at}angle coordinates must be numbers`);
        const vertex: [number, number] = [v[0], v[1]];
        const a: [number, number] = [v[2], v[3]];
        const b: [number, number] = [v[4], v[5]];
        const r = rest.length === 7 ? v[6] : 0.3 * Math.min(dist(vertex, a), dist(vertex, b));
        if (r <= 0) return result(`${at}angle radius must be positive`);
        el = { kind: "angle", vertex, a, b, r };
        break;
      }
      default:
        return result(`${at}unknown command "${command}"`);
    }
    if (el) elements.push(el);
  }

  return { viewBox: VIEW_BOX, elements, error: "" };
}
