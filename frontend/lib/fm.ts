/* File-manager client and pure helpers.

 * Talks to the /api/fm/* endpoints (same origin /be proxy as the rest of the
 * app). Pure helpers for the shared clipboard and formatting live here so the
 * node test suite can exercise them without the DOM.
 */

import { BASE } from "./api";
import { kindForName } from "./windows";
import type { WindowKind } from "./windows";

export const FM_CLIPBOARD_KEY = "apex:fm-clipboard";
export const FM_CHANNEL = "apex-fm-clipboard";

/* ---------- types ---------- */

export type FmEntry = {
  name: string;
  path: string;
  is_dir: boolean;
  is_symlink: boolean;
  size_bytes: number | null;
  mtime_ns: number;
  kind: string;
};

export type FmList = {
  path: string;
  name: string;
  parent: string | null;
  entries: FmEntry[];
  skipped_inaccessible: number;
};

export type FmRoot = { path: string; name: string; is_root: boolean };
export type FmHome = { path: string; name: string } | null;

export type FmTrashItem = {
  id: string;
  original: string;
  name: string;
  at: number;
  exists: boolean;
  size_bytes: number | null;
};

export type FmJobResult = { path: string; ok: boolean; skipped?: boolean };
export type FmJobError = { path: string; error: string };

export type FmJob = {
  id: string;
  action: "copy" | "move";
  status: "running" | "done" | "failed" | "cancelled";
  message: string;
  total: number;
  done: number;
  current: string;
  cancelled: boolean;
  errors: FmJobError[];
  results: FmJobResult[];
};

export type FmConflict = { source: string; target: string; is_dir: boolean };

export type FmJobSpec = {
  action: "copy" | "move";
  sources: string[];
  destination: string;
  conflicts?: Record<string, "replace" | "skip" | "keep_both">;
};

export type UploadResult = {
  name: string;
  ok: boolean;
  size_bytes?: number;
  error?: string;
  skipped?: boolean;
};

export type FmClipboard = { mode: "copy" | "cut"; paths: string[]; at: number } | null;

/* ---------- errors ---------- */

export class FmApiError extends Error {
  status: number;
  conflicts?: FmConflict[];
  constructor(message: string, status: number, conflicts?: FmConflict[]) {
    super(message);
    this.status = status;
    this.conflicts = conflicts;
  }
}

/* ---------- http ---------- */

async function fmJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${BASE}${url}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
  if (resp.status === 401) throw new FmApiError("unauthorized", 401);
  if (!resp.ok) {
    const body: any = await resp.json().catch(() => null);
    throw new FmApiError(body?.error ?? `${resp.status} ${resp.statusText}`, resp.status, body?.conflicts);
  }
  return resp.json() as Promise<T>;
}

/* ---------- endpoints ---------- */

export const fm = {
  roots: () => fmJson<{ roots: FmRoot[]; home: FmHome }>("/api/fm/roots"),

  list: (path: string) =>
    fmJson<FmList>(`/api/fm/list?${new URLSearchParams({ path }).toString()}`),

  stat: (path: string) =>
    fmJson<{ entry: FmEntry; permissions: string }>(`/api/fm/stat?${new URLSearchParams({ path }).toString()}`),

  mkdir: (path: string) =>
    fmJson<{ entry: FmEntry }>("/api/fm/mkdir", { method: "POST", body: JSON.stringify({ path }) }),

  rename: (path: string, newName: string) =>
    fmJson<{ entry: FmEntry }>("/api/fm/rename", { method: "POST", body: JSON.stringify({ path, new_name: newName }) }),

  trash: (paths: string[]) =>
    fmJson<{ ok: boolean; results: { path: string; ok: boolean; error?: string }[] }>("/api/fm/trash", {
      method: "POST",
      body: JSON.stringify({ paths }),
    }),

  listTrash: () => fmJson<{ trash: FmTrashItem[] }>("/api/fm/trash"),

  restore: (ids: string[]) =>
    fmJson<{ ok: boolean; results: { id: string; ok: boolean; path?: string; error?: string }[] }>("/api/fm/restore", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),

  cleanTrash: (ids: string[] | null) =>
    fmJson<{ ok: boolean; results: { id: string; ok: boolean; error?: string }[] }>("/api/fm/trash/cleanup", {
      method: "POST",
      body: JSON.stringify(ids ? { ids } : { all: true }),
    }),

  createJob: (spec: FmJobSpec) =>
    fmJson<{ job_id: string; total: number }>("/api/fm/jobs", {
      method: "POST",
      body: JSON.stringify(spec),
    }),

  jobStatus: (jobId: string) => fmJson<FmJob>(`/api/fm/jobs/${jobId}`),

  cancelJob: (jobId: string) =>
    fmJson<{ ok: boolean; status: string }>(`/api/fm/jobs/${jobId}/cancel`, { method: "POST" }),

  tickets: (paths: string[]) =>
    fmJson<{ tickets: Record<string, string>; expires_in_seconds: number }>("/api/fm/tickets", {
      method: "POST",
      body: JSON.stringify({ paths }),
    }),

  zip: (paths: string[]) =>
    fmJson<{ filename: string; download_url: string; expires_in_seconds: number }>("/api/fm/zip", {
      method: "POST",
      body: JSON.stringify({ paths }),
    }),
};

/* Upload with progress events (XHR so upload.onprogress is available). */
export function fmUpload(opts: {
  destination: string;
  onConflict: "replace" | "skip" | "keep_both";
  files: File[];
  onProgress?: (loaded: number, total: number) => void;
}): Promise<{ ok: boolean; results: UploadResult[] }> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("destination", opts.destination);
    form.append("on_conflict", opts.onConflict);
    for (const file of opts.files) form.append("files", file, file.webkitRelativePath || file.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/api/fm/upload`);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) opts.onProgress?.(event.loaded, event.total);
    };
    xhr.onload = () => {
      let body: any = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* non-JSON error page */
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as { ok: boolean; results: UploadResult[] });
      } else {
        reject(new FmApiError(body?.error ?? `upload failed (${xhr.status})`, xhr.status));
      }
    };
    xhr.onerror = () => reject(new FmApiError("Network error during upload.", 0));
    xhr.send(form);
  });
}

/* ---------- clipboard (pure) ---------- */

export function serializeClipboard(clipboard: FmClipboard): string {
  return clipboard ? JSON.stringify(clipboard) : "";
}

export function parseClipboard(raw: string | null | undefined): FmClipboard {
  if (!raw) return null;
  try {
    const data = JSON.parse(raw);
    if (data && (data.mode === "copy" || data.mode === "cut") && Array.isArray(data.paths)) {
      const paths = (data.paths as unknown[]).filter((p): p is string => typeof p === "string");
      if (paths.length) return { mode: data.mode, paths, at: data.at ?? 0 };
    }
  } catch {
    /* invalid stored clipboard */
  }
  return null;
}

export function readStoredClipboard(storage?: Pick<Storage, "getItem"> | null): FmClipboard {
  if (!storage && typeof localStorage === "undefined") return null;
  return parseClipboard((storage ?? localStorage).getItem(FM_CLIPBOARD_KEY));
}

export function storeStoredClipboard(clipboard: FmClipboard, storage?: Pick<Storage, "setItem" | "removeItem"> | null): void {
  if (!storage && typeof localStorage === "undefined") return;
  const raw = serializeClipboard(clipboard);
  if (raw) (storage ?? localStorage).setItem(FM_CLIPBOARD_KEY, raw);
  else (storage ?? localStorage).removeItem(FM_CLIPBOARD_KEY);
}

/* After a move completes, cut items that were moved are gone: drop them from
 * the clipboard. Copied items stay so the user can paste the same set again. */
export function clipboardAfterMove(clipboard: FmClipboard, movedPaths: string[]): FmClipboard {
  if (!clipboard || clipboard.mode !== "cut") return clipboard;
  const moved = new Set(movedPaths);
  const remaining = clipboard.paths.filter((p) => !moved.has(p));
  if (!remaining.length) return null;
  return { mode: "cut", paths: remaining, at: Date.now() };
}

/* ---------- window settings (pure) ---------- */

export const FM_SETTINGS_KEY = "apex:fm-settings";
export const FM_ICON_SIZES = [14, 19, 26, 33] as const;

export type FmView = "list" | "grid";
export type FmSortKey = "name" | "kind" | "size_bytes" | "mtime_ns";
export type FmSortDir = 1 | -1;

export interface FmSettings {
  iconSize: number;
  showHidden: boolean;
  view: FmView;
  sortKey: FmSortKey;
  sortDir: FmSortDir;
  lastPath: string | null;
}

export const DEFAULT_FM_SETTINGS: FmSettings = {
  iconSize: 19, showHidden: false, view: "list", sortKey: "name", sortDir: 1, lastPath: null,
};

export const FM_SORT_KEYS: FmSortKey[] = ["name", "kind", "size_bytes", "mtime_ns"];

export function serializeSettings(settings: FmSettings): string {
  return JSON.stringify(settings);
}

export function parseSettings(raw: string | null | undefined): FmSettings {
  if (!raw) return { ...DEFAULT_FM_SETTINGS };
  try {
    const data = JSON.parse(raw);
    const iconSize = typeof data.iconSize === "number" && FM_ICON_SIZES.includes(data.iconSize as never)
      ? data.iconSize
      : DEFAULT_FM_SETTINGS.iconSize;
    const showHidden = typeof data.showHidden === "boolean" ? data.showHidden : DEFAULT_FM_SETTINGS.showHidden;
    const view = data.view === "grid" ? "grid" : DEFAULT_FM_SETTINGS.view;
    const sortKey = FM_SORT_KEYS.includes(data.sortKey as never) ? data.sortKey : DEFAULT_FM_SETTINGS.sortKey;
    const sortDir = data.sortDir === -1 ? -1 : 1;
    const lastPath = typeof data.lastPath === "string" && data.lastPath ? data.lastPath : null;
    return { iconSize, showHidden, view, sortKey, sortDir, lastPath };
  } catch {
    return { ...DEFAULT_FM_SETTINGS };
  }
}

export function readStoredSettings(storage?: Pick<Storage, "getItem"> | null): FmSettings {
  if (!storage && typeof localStorage === "undefined") return { ...DEFAULT_FM_SETTINGS };
  return parseSettings((storage ?? localStorage).getItem(FM_SETTINGS_KEY));
}

export function storeStoredSettings(settings: FmSettings, storage?: Pick<Storage, "setItem"> | null): void {
  if (!storage && typeof localStorage === "undefined") return;
  (storage ?? localStorage).setItem(FM_SETTINGS_KEY, serializeSettings(settings));
}

/* ---------- formatting (pure) ---------- */

export function fmJoin(base: string, name: string): string {
  return base.endsWith("/") ? `${base}${name}` : `${base}/${name}`;
}

export function formatFmBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = units[0];
  for (let i = 1; i < units.length && value >= 1024; i++) {
    value /= 1024;
    unit = units[i];
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${unit}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function formatFmDate(mtimeNs: number): string {
  if (!mtimeNs) return "—";
  const date = new Date(Math.floor(mtimeNs / 1e6));
  return `${MONTHS[date.getMonth()]} ${date.getDate()}, ${date.getFullYear()} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/** Map a backend entry onto the window-manager render kind (folders and
 *  symlinks have no inline preview and fall back to "other"). */
export function entryWindowKind(entry: { name: string; kind: string }): WindowKind {
  if (entry.kind === "link" || entry.kind === "folder") return "other";
  if (entry.kind === "image" || entry.kind === "pdf" || entry.kind === "docx"
      || entry.kind === "xlsx" || entry.kind === "pptx" || entry.kind === "text") {
    return entry.kind as WindowKind;
  }
  return kindForName(entry.name);
}