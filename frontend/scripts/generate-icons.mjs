#!/usr/bin/env node
/* APEX PWA icon generator.

   The app has no bitmap assets of its own - the orb is SVG/three.js at runtime -
   so the launcher icon is drawn here instead: a small supersampled rasterizer
   plus a hand-rolled PNG encoder. Keeping it dependency-free means the icons can
   be regenerated on any machine with Node, with no native toolchain.

   Usage: node scripts/generate-icons.mjs
   Output: public/icon-*.png, public/apple-touch-icon.png, public/favicon.ico

   Re-run it after changing the palette or the composition below. The service
   worker deliberately does NOT cache these: a launcher icon is fetched once at
   install time and a stale one is worse than a slow one.  */

/* ---------- palette (matches components/ChatUI.tsx + app/globals.css) ---------- */

const BG_INNER = [10, 22, 38];
const BG_OUTER = [2, 5, 10];
const CYAN = [0, 229, 255];
const GOLD = [245, 166, 35];
const WHITE = [236, 252, 255];

/* ---------- PNG encoding (zlib is the only thing we need from Node) ---------- */

import { deflateSync } from "node:zlib";
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const CRC_TABLE = (() => {
  const table = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  return table;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const out = Buffer.alloc(data.length + 12);
  out.writeUInt32BE(data.length, 0);
  out.write(type, 4, "ascii");
  data.copy(out, 8);
  out.writeUInt32BE(crc32(out.subarray(4, 8 + data.length)), 8 + data.length);
  return out;
}

/** rgba: Buffer of width*height*4 bytes. */
function encodePng(rgba, width, height) {
  // One filter byte per scanline; 0 = None. The images are flat gradients, so
  // per-line filtering would cost bytes without buying anything.
  const raw = Buffer.alloc(height * (width * 4 + 1));
  for (let y = 0; y < height; y++) {
    raw[y * (width * 4 + 1)] = 0;
    rgba.copy(raw, y * (width * 4 + 1) + 1, y * width * 4, (y + 1) * width * 4);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // colour type: RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/* ---------- drawing ---------- */

const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
const mix = (a, b, t) => [
  a[0] + (b[0] - a[0]) * t,
  a[1] + (b[1] - a[1]) * t,
  a[2] + (b[2] - a[2]) * t,
];

/**
 * Accumulate one colour into an RGBA buffer with additive-ish blending.
 * `alpha` is coverage; `intensity` scales the colour, which is how the glow is
 * built up from a dozen overlapping passes instead of one expensive blur.
 */
function blend(px, size, x, y, color, alpha, intensity = 1) {
  if (x < 0 || y < 0 || x >= size || y >= size || alpha <= 0) return;
  const i = (y * size + x) * 4;
  const src = [color[0] * intensity, color[1] * intensity, color[2] * intensity];
  const a = clamp01(alpha);
  px[i] = clamp01((px[i] + src[0] * a) / 255) * 255;
  px[i + 1] = clamp01((px[i + 1] + src[1] * a) / 255) * 255;
  px[i + 2] = clamp01((px[i + 2] + src[2] * a) / 255) * 255;
  px[i + 3] = Math.min(255, px[i + 3] + a * 255);
}

/**
 * Draw the APEX orb.
 *
 * `art` is the fraction of the canvas the composition occupies, centred. It is
 * 1 for a plain icon and < 1 for a maskable one: a maskable icon may be cropped
 * to a circle of 80% diameter, so anything outside that must be background.
 */
function drawOrb(size, art, { background }) {
  const px = Buffer.alloc(size * size * 4);
  const c = size / 2;
  const r = (size / 2) * art;

  // Background: a soft radial lift so the icon is not a flat black square on a
  // dark home screen, where a flat fill would make the icon edge invisible.
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const d = Math.hypot(x - c, y - c) / (size / 2);
      const t = clamp01(d / 1.414);
      const col = mix(BG_INNER, BG_OUTER, t * t);
      blend(px, size, x, y, col, 1);
    }
  }

  const ringR = r * 0.78;
  const ringW = r * 0.035;
  const coreR = r * 0.2;

  // Outer halo around the ring. Three passes of a wide, soft falloff read as a
  // bloom that a single pass cannot without looking like a grey disc.
  for (const [spread, alpha] of [[0.16, 0.1], [0.07, 0.13], [0.028, 0.18]]) {
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const d = Math.abs(Math.hypot(x - c, y - c) - ringR);
        const a = clamp01(1 - d / (r * spread)) * alpha;
        blend(px, size, x, y, CYAN, a);
      }
    }
  }

  // The ring itself.
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const d = Math.abs(Math.hypot(x - c, y - c) - ringR);
      const a = clamp01(1 - d / ringW);
      blend(px, size, x, y, CYAN, a * a * (3 - 2 * a));
    }
  }

  // Particle core: a hot white centre bleeding out through cyan.
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const d = Math.hypot(x - c, y - c) / coreR;
      if (d >= 1) continue;
      const falloff = Math.pow(1 - d, 2.1);
      blend(px, size, x, y, CYAN, falloff * 0.95);
      blend(px, size, x, y, WHITE, Math.pow(1 - d, 3.4));
    }
  }

  // Orbit dots, the gold accent from the status bar. Placed at the corners so
  // they do not crowd the core at small sizes.
  for (const deg of [-38, 138]) {
    const a = (deg * Math.PI) / 180;
    const ox = c + Math.cos(a) * ringR;
    const oy = c + Math.sin(a) * ringR;
    const dotR = r * 0.075;
    for (let y = Math.floor(oy - dotR * 2); y <= oy + dotR * 2; y++) {
      for (let x = Math.floor(ox - dotR * 2); x <= ox + dotR * 2; x++) {
        const d = Math.hypot(x - ox, y - oy) / dotR;
        if (d >= 1) continue;
        blend(px, size, x, y, GOLD, Math.pow(1 - d, 0.85));
      }
    }
  }

  if (background) {
    // Opaque: iOS composites a transparent apple-touch-icon over black, which
    // would put a dark halo around the art on a light home screen.
    for (let i = 3; i < px.length; i += 4) px[i] = 255;
  }
  return px;
}

/* ---------- supersampling ---------- */

/**
 * Render at `ss`x and box-filter down. The ring edge and the core falloff are
 * both sub-pixel-thin at 192px, and a single sample produces visible jaggies
 * and a stair-stepped glow.
 */
function render(size, opts) {
  const ss = size <= 64 ? 8 : 4;
  const big = drawOrb(size * ss, opts.art, opts);
  const out = Buffer.alloc(size * size * 4);
  const n = ss * ss;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let r = 0, g = 0, b = 0, a = 0;
      for (let sy = 0; sy < ss; sy++) {
        for (let sx = 0; sx < ss; sx++) {
          const i = ((y * ss + sy) * size * ss + (x * ss + sx)) * 4;
          r += big[i]; g += big[i + 1]; b += big[i + 2]; a += big[i + 3];
        }
      }
      const o = (y * size + x) * 4;
      out[o] = r / n; out[o + 1] = g / n; out[o + 2] = b / n; out[o + 3] = a / n;
    }
  }
  return out;
}

/* ---------- emit ---------- */

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const pub = join(root, "public");
mkdirSync(pub, { recursive: true });

/* "any" icons fill the frame. A "maskable" one may be cropped to a circle of 80%
   diameter, so the art has to fit inside r = 0.4 - and the ring sits at
   art * 0.78 of the half-width, which caps art at ~0.51. The soft halo is
   deliberately allowed past that line: cropping a glow is invisible, cropping
   the ring would cut the icon in half. */
const SAFE_RING_ART = 0.375 / 0.78;

const targets = [
  { file: "icon-192.png", size: 192, art: 0.86, background: true },
  { file: "icon-512.png", size: 512, art: 0.86, background: true },
  { file: "icon-maskable-192.png", size: 192, art: SAFE_RING_ART, background: true },
  { file: "icon-maskable-512.png", size: 512, art: SAFE_RING_ART, background: true },
  { file: "apple-touch-icon.png", size: 180, art: 0.86, background: true },
  { file: "icon-32.png", size: 32, art: 0.9, background: true },
];

for (const t of targets) {
  const png = encodePng(render(t.size, { art: t.art, background: t.background }), t.size, t.size);
  writeFileSync(join(pub, t.file), png);
  console.log(`${t.file}  ${t.size}x${t.size}  ${(png.length / 1024).toFixed(1)} KB`);
}

/* favicon.ico - a real multi-size ICO rather than a PNG renamed to .ico, because
   Chrome still reads the ICO header for the tab icon on some platforms. */
function encodeIco(images) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0); // reserved
  header.writeUInt16LE(1, 2); // type: icon
  header.writeUInt16LE(images.length, 4);
  let offset = 6 + images.length * 16;
  const dir = [];
  for (const { size, png } of images) {
    const entry = Buffer.alloc(16);
    entry[0] = size >= 256 ? 0 : size;
    entry[1] = size >= 256 ? 0 : size;
    entry[2] = 0; // palette
    entry[3] = 0; // reserved
    entry.writeUInt16LE(1, 4); // colour planes
    entry.writeUInt16LE(32, 6); // bits per pixel
    entry.writeUInt32BE(0, 8);
    entry.writeUInt32LE(png.length, 8);
    entry.writeUInt32LE(offset, 12);
    offset += png.length;
    dir.push(entry);
  }
  return Buffer.concat([header, ...dir, ...images.map((i) => i.png)]);
}

const icoSizes = [16, 32, 48];
const ico = encodeIco(
  icoSizes.map((size) => ({
    size,
    png: encodePng(render(size, { art: 0.9, background: true }), size, size),
  })),
);
writeFileSync(join(pub, "favicon.ico"), ico);
console.log(`favicon.ico  ${icoSizes.join("+")}  ${(ico.length / 1024).toFixed(1)} KB`);