/** Shared model + helpers for the APEX desktop window manager.

 * Window state lives only in the frontend session: opening files, galleries
 * and notes from voice/text commands; arranged near the chat via WindowManager.
 */

export const MAX_WINDOWS = 10;

export type WindowKind = "image" | "pdf" | "docx" | "xlsx" | "text" | "other";
export type WindowArrangement = "cascade" | "grid" | "tile-h" | "tile-v" | "center";

export type WindowItem = { url: string; title: string };
export type WindowRect = { x: number; y: number; w: number; h: number };

export type AppWindow = {
  id: string;
  items: WindowItem[];
  index: number;
  kind: WindowKind;
  rect: WindowRect;
  maximized: boolean;
  minimized: boolean;
  note: string;
  showNotes: boolean;
};

const IMAGE_EXT_RE = /\.(jpg|jpeg|png|gif|webp|svg|bmp)(\?.*)?$/i;

const TEXT_EXTS = [
  "txt", "md", "markdown", "csv", "tsv", "json", "log",
  "ini", "cfg", "conf", "toml", "yaml", "yml", "sql",
  "py", "sh", "js", "jsx", "ts", "tsx", "css", "html", "htm", "xml",
];

/** Guess the render kind from a filename or URL. */
export function kindForName(name: string, url = ""): WindowKind {
  const source = (name || url || "").split("?")[0].split("#")[0];
  if (IMAGE_EXT_RE.test(source) || IMAGE_EXT_RE.test(url)) return "image";
  if (/\/api\/images\/file\/[A-Za-z0-9_.\-]+/.test(url)) return "image";
  const ext = (source.match(/\.([a-z0-9]+)$/i)?.[1] ?? "").toLowerCase();
  if (ext === "pdf") return "pdf";
  if (ext === "docx") return "docx";
  if (ext === "xlsx") return "xlsx";
  if (TEXT_EXTS.includes(ext)) return "text";
  return "other";
}

/** Determine the kind a whole window should use for its collection. */
export function kindForItems(items: WindowItem[]): WindowKind {
  if (!items.length) return "other";
  if (items.every((i) => kindForName(i.title, i.url) === "image")) return "image";
  return kindForName(items[0].title, items[0].url);
}

export function titleFromUrl(url: string): string {
  try {
    const parsed = new URL(url, typeof window !== "undefined" ? window.location.href : "http://localhost:3000");
    const params = parsed.searchParams.get("path");
    if (params) {
      const parts = params.split("/");
      return decodeURIComponent(parts[parts.length - 1]) || "Preview";
    }
    const parts = parsed.pathname.split("/");
    return decodeURIComponent(parts[parts.length - 1]) || "Preview";
  } catch {
    return "Preview";
  }
}

export function itemTitle(item: WindowItem): string {
  return (item.title && item.title.trim()) || titleFromUrl(item.url);
}

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));

/** Compute desktop rects for `total` windows in the chosen arrangement. */
export function layoutRects(arrangement: WindowArrangement, total: number, vw: number, vh: number): WindowRect[] {
  total = Math.max(1, total);
  const availW = Math.max(240, vw);
  const availH = Math.max(200, vh - 52); // leave room for the taskbar
  const pad = 20;

  if (arrangement === "tile-h") {
    const h = availH / total;
    return Array.from({ length: total }, (_, i) => ({ x: 0, y: i * h, w: availW, h }));
  }
  if (arrangement === "tile-v") {
    const w = availW / total;
    return Array.from({ length: total }, (_, i) => ({ x: i * w, y: 0, w, h: availH }));
  }
  if (arrangement === "center") {
    const w = Math.min(760, availW - 160);
    const h = Math.min(560, availH - 90);
    const cx = (availW - w) / 2;
    const cy = (availH - h) / 2;
    const step = Math.min(30, (availW - w - 40) / Math.max(1, total - 1));
    const yStep = Math.min(26, (availH - h - 50) / Math.max(1, total - 1));
    return Array.from({ length: total }, (_, i) => ({
      x: clamp(cx + i * step, 0, availW - w),
      y: clamp(cy + i * yStep, 0, availH - h),
      w,
      h,
    }));
  }
  if (arrangement === "grid") {
    const cols = Math.ceil(Math.sqrt(total));
    const rows = Math.ceil(total / cols);
    const gap = 18;
    const w = (availW - pad * 2 - gap * (cols - 1)) / cols;
    const h = (availH - pad - gap * (rows - 1)) / rows;
    return Array.from({ length: total }, (_, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      return { x: pad + col * (w + gap), y: pad + row * (h + gap), w, h };
    });
  }
  // cascade (default)
  const w = Math.min(700, availW - 200);
  const h = Math.min(520, availH - 110);
  const startX = 40;
  const startY = 26;
  const step = total > 1 ? Math.min(26, (availW - w - startX - 60) / (total - 1)) : 0;
  return Array.from({ length: total }, (_, i) => ({
    x: clamp(startX + i * step, 0, availW - w),
    y: clamp(startY + i * 26, 0, availH - h),
    w,
    h,
  }));
}

/* ---------- extraction from assistant messages ---------- */

const PREVIEWABLE_URL_RE =
  /\[([^\]]*)\]\((https?:\/\/[^\s)]+|\/api\/(?:files|editor)\/download\/[A-Za-z0-9_.\-]+|\/api\/images\/file\/[A-Za-z0-9_.\-]+|\/api\/obsidian\/file\?path=[^\s)]+)\)|(https?:\/\/[^\s<>"{}|\\^`[\]]+)|(\/api\/(?:files|editor)\/download\/[A-Za-z0-9_.\-]+)|(\/api\/images\/file\/[A-Za-z0-9_.\-]+)|(\/api\/obsidian\/file\?path=[^\s<>"{}|\\^`[\]]+)/g;

function isPreviewableUrl(url: string): boolean {
  if (/\/api\/(?:files|editor)\/download\/[A-Za-z0-9_.\-]+$/.test(url)) return true;
  if (/\/api\/images\/file\/[A-Za-z0-9_.\-]+$/.test(url)) return true;
  if (/\/api\/obsidian\/file\?path=/.test(url)) return true;
  if (/\.(jpg|jpeg|png|gif|webp|svg|bmp|pdf)(\?.*)?$/i.test(url)) return true;
  return false;
}

/** Collect previewable file/image links from a text blob (deduplicated). */
export function collectPreviewableItems(content: string): WindowItem[] {
  const seen = new Set<string>();
  const items: WindowItem[] = [];
  PREVIEWABLE_URL_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = PREVIEWABLE_URL_RE.exec(content)) !== null) {
    const label = match[1];
    let url = match[2] || match[3] || match[4] || match[5] || match[6];
    if (!url || seen.has(url) || !isPreviewableUrl(url)) continue;
    // raw http(s) captures may swallow trailing sentence punctuation
    if (match[3]) url = url.replace(/[\.,;:!?'")\]]+$/, "");
    if (!url || seen.has(url) || !isPreviewableUrl(url)) continue;
    seen.add(url);
    const title = (label && label.trim()) || titleFromUrl(url);
    items.push({ url, title });
  }
  return items;
}

/** Apex-awareness block appended to the model prompt, e.g.
 *  [Open windows: #1 "Budget.xlsx" (xlsx, focused) · #2 "chart.png" (image)] */
export function windowContextBlock(windows: AppWindow[], focusedId: string | null | undefined): string {
  if (!windows.length) return "";
  const parts = windows.map((w, i) => {
    const focused = w.id === focusedId ? ", focused" : "";
    const item = w.items[w.index] ?? w.items[0];
    const title = itemTitle(item ?? { url: "", title: "" });
    return `#${i + 1} "${title}" (${w.kind}${focused})`;
  });
  return `[Open windows: ${parts.join(" · ")}]`;
}