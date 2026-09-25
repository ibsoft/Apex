"use client";

/* FileManagerWindow - a real filesystem explorer inside a desktop window.
 *
 * Talks to the /api/fm/* endpoints (roots/list/stat/mkdir/rename/trash/jobs/
 * tickets/zip/upload). Supports a cross-window clipboard (localStorage +
 * BroadcastChannel, cut cleared only after a successful move), explicit
 * conflict resolution, cancellable background jobs with progress, trash with
 * restore, drag-and-drop aside, upload progress and Explorer-style shortcuts
 * that never interfere with text inputs.
 *
 * Key ownership: WindowManager stays out of focused files windows (like
 * terminals), so arrow keys / Escape / Delete / F2 / Ctrl+A/C/X/V are handled
 * here and never leak to other consumers. The handler refuses to run while an
 * input/textarea/select is focused.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FmApiError, FM_CHANNEL, FM_CLIPBOARD_KEY, FM_ICON_SIZES, clipboardAfterMove, entryWindowKind, fm, fmUpload, formatFmBytes, formatFmDate, fmJoin, parseClipboard, parseSettings, readStoredClipboard, readStoredSettings, serializeClipboard, serializeSettings, storeStoredClipboard, storeStoredSettings } from "../lib/fm";
import type { FmClipboard, FmConflict, FmEntry, FmJob, FmJobSpec, FmRoot, FmSettings, FmTrashItem } from "../lib/fm";
import { useApex } from "./ApexProvider";

const C = {
  cyan: "#00e5ff",
  gold: "#f5a623",
  bg: "rgba(4,8,16,0.94)",
  line: "rgba(0,229,255,0.16)",
  lineGold: "rgba(245,166,35,0.28)",
  text: "rgba(235,244,255,0.92)",
  dim: "rgba(170,192,215,0.5)",
  danger: "#ff6b6b",
};

type SortKey = "name" | "kind" | "size_bytes" | "mtime_ns";
type ConfirmDelete = { paths: string[]; permanent: boolean };

const KIND_LABEL: Record<string, string> = {
  folder: "FOLDER", image: "IMAGE", pdf: "PDF", docx: "DOCX", xlsx: "XLSX",
  pptx: "PPTX", text: "TEXT", link: "LINK", other: "FILE",
};

function kindIcon(kind: string): string {
  if (kind === "folder") return "📁";
  if (kind === "image") return "🖼️";
  if (kind === "pdf") return "📕";
  if (kind === "text") return "📄";
  if (kind === "link") return "↳";
  return "🗎";
}

function messageOf(error: unknown): string {
  if (error instanceof FmApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

export default function FileManagerWindow({ focused }: { focused: boolean }) {
  const a = useApex();

  /* ---------- state ---------- */

  const [roots, setRoots] = useState<FmRoot[]>([]);
  const [home, setHome] = useState<{ path: string; name: string } | null>(null);
  const [path, setPath] = useState<string | null>(null);
  const [entries, setEntries] = useState<FmEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [clipboard, setClipboard] = useState<FmClipboard>(null);
  const [jobs, setJobs] = useState<Record<string, FmJob>>({});
  const [upload, setUpload] = useState<{ active: boolean; loaded: number; total: number } | null>(null);
  const [toast, setToast] = useState<{ text: string; kind: "ok" | "err" } | null>(null);
  const [showTrash, setShowTrash] = useState(false);
  const [trash, setTrash] = useState<FmTrashItem[]>([]);
  const [menu, setMenu] = useState<{ x: number; y: number; path: string } | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [newFolder, setNewFolder] = useState(false);
  const [newFolderValue, setNewFolderValue] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<ConfirmDelete | null>(null);
  const [conflictState, setConflictState] = useState<{ spec: FmJobSpec; conflicts: FmConflict[] } | null>(null);
  const [conflictChoices, setConflictChoices] = useState<Record<string, "replace" | "skip" | "keep_both">>({});
  const [uploadConflicts, setUploadConflicts] = useState<"replace" | "skip" | "keep_both">("skip");
  const [settings, setSettings] = useState<FmSettings>(() => readStoredSettings());
  const [showSettings, setShowSettings] = useState(false);

  /* navigation history in refs so navigation never double-fires in StrictMode */
  const navStackRef = useRef<string[]>([]);
  const navIndexRef = useRef(-1);
  const [, forceNav] = useState(0);
  const pathRef = useRef<string | null>(null);
  const lastIndexRef = useRef(0);
  const senderRef = useRef<string>(`fm-${Math.random().toString(36).slice(2)}`);
  const channelRef = useRef<BroadcastChannel | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  const setPathRef = (target: string | null) => {
    pathRef.current = target;
    setPath(target);
  };

  /* ---------- helpers ---------- */

  const toastOk = (text: string) => setToast({ text, kind: "ok" });
  const toastErr = (text: string) => setToast({ text, kind: "err" });

  const broadcast = (data: Record<string, unknown>) => {
    channelRef.current?.postMessage({ ...data, sender: senderRef.current });
  };

  const setClipboardBoth = useCallback((cb: FmClipboard) => {
    setClipboard(cb);
    storeStoredClipboard(cb);
    broadcast({ kind: "clipboard", payload: serializeClipboard(cb) });
  }, []);

  const patchSettings = useCallback((patch: Partial<FmSettings>) => {
    setSettings((prev) => {
      const next = { ...prev, ...patch };
      storeStoredSettings(next);
      broadcast({ kind: "settings", payload: serializeSettings(next) });
      return next;
    });
  }, []);

  const load = useCallback(async (target: string) => {
    setLoading(true);
    try {
      const data = await fm.list(target);
      setEntries(data.entries);
      setError(null);
      setSelected([]);
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const refresh = useCallback(() => {
    const target = pathRef.current;
    if (target) void load(target);
  }, [load]);

  const navigate = useCallback((target: string) => {
    navStackRef.current = [...navStackRef.current.slice(0, navIndexRef.current + 1), target];
    navIndexRef.current = navStackRef.current.length - 1;
    forceNav((n) => n + 1);
    setPathRef(target);
    setSelected([]);
  }, []);

  const goBack = useCallback(() => {
    if (navIndexRef.current <= 0) return;
    navIndexRef.current -= 1;
    forceNav((n) => n + 1);
    setPathRef(navStackRef.current[navIndexRef.current]);
  }, []);

  const goForward = useCallback(() => {
    if (navIndexRef.current >= navStackRef.current.length - 1) return;
    navIndexRef.current += 1;
    forceNav((n) => n + 1);
    setPathRef(navStackRef.current[navIndexRef.current]);
  }, []);

  /* ---------- bootstrap + cross-window sync ---------- */

  useEffect(() => {
    let live = true;
    fm.roots()
      .then((data) => {
        if (!live) return;
        setRoots(data.roots);
        setHome(data.home);
        const fallback = data.home?.path ?? data.roots[0]?.path ?? null;
        const remembered = readStoredSettings().lastPath;
        const insideRoots = remembered !== null && data.roots.some((r) =>
          remembered === r.path || remembered.startsWith(r.path === "/" ? "/" : `${r.path}/`));
        const start = insideRoots ? remembered : fallback;
        if (start) {
          navStackRef.current = [start];
          navIndexRef.current = 0;
          setPathRef(start);
        }
      })
      .catch((e) => live && setError(messageOf(e)));
    const stored = readStoredClipboard();
    if (stored) setClipboard(stored);
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    if (!path) return;
    void load(path);
  }, [path, load]);

  useEffect(() => {
    if (!path) return;
    const target = path;
    setSettings((prev) => {
      if (prev.lastPath === target) return prev;
      const next = { ...prev, lastPath: target };
      storeStoredSettings(next);
      return next;
    });
  }, [path]);

  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel(FM_CHANNEL);
    channelRef.current = channel;
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { sender?: string; kind?: string; payload?: string } | null;
      if (!data || data.sender === senderRef.current) return;
      if (data.kind === "clipboard") setClipboard(parseClipboard(data.payload ?? null));
      else if (data.kind === "settings") setSettings(parseSettings(data.payload));
      else if (data.kind === "refresh") {
        const target = pathRef.current;
        if (target) void load(target);
      }
    };
    channel.addEventListener("message", onMessage);
    const storage = (e: StorageEvent) => {
      if (e.key === FM_CLIPBOARD_KEY) setClipboard(parseClipboard(e.newValue));
    };
    window.addEventListener("storage", storage);
    return () => {
      channel.removeEventListener("message", onMessage);
      channel.close();
      channelRef.current = null;
      window.removeEventListener("storage", storage);
    };
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), 3600);
    return () => clearTimeout(id);
  }, [toast]);

  /* ---------- jobs ---------- */

  const pollJob = useCallback(async (id: string) => {
    try {
      const status = await fm.jobStatus(id);
      setJobs((all) => ({ ...all, [id]: status }));
      if (status.status === "running") {
        setTimeout(() => void pollJob(id), 700);
        return;
      }
      if (status.status === "done" && (status.action === "move" || status.action === "copy") && !status.errors.length) {
        // Successful paste: drop the clipboard so the CUT/COPIED strip clears.
        setClipboardBoth(null);
      } else if (status.status === "done" && status.action === "move" && status.errors.length) {
        const moved = status.results.filter((r) => r.ok).map((r) => r.path);
        if (moved.length) {
          const trimmed = clipboardAfterMove(readStoredClipboard(), moved);
          setClipboardBoth(trimmed);
        }
      }
      if (status.status === "done") toastOk(`${status.action} finished (${status.done}/${status.total}).`);
      else if (status.status === "cancelled") toastErr("Operation cancelled.");
      else if (status.errors.length) toastErr(`${status.errors.length} item(s) failed.`);
      refresh();
      broadcast({ kind: "refresh" });
    } catch (e) {
      toastErr(messageOf(e));
    }
  }, [setClipboardBoth, refresh, broadcast]);

  useEffect(() => {
    Object.values(jobs).forEach((job) => {
      if (job.status === "running") setTimeout(() => void pollJob(job.id), 0);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs]);

  const runJob = useCallback(async (spec: FmJobSpec) => {
    try {
      const created = await fm.createJob(spec);
      toastOk(spec.action === "copy" ? "Copying…" : "Moving…");
      void pollJob(created.job_id);
    } catch (e) {
      if (e instanceof FmApiError && e.status === 409 && e.conflicts?.length) {
        setConflictChoices({});
        setConflictState({ spec, conflicts: e.conflicts });
      } else {
        toastErr(messageOf(e));
      }
    }
  }, [pollJob]);

  /* ---------- clipboard actions ---------- */

  const copyCut = useCallback((mode: "copy" | "cut") => {
    if (!selected.length) {
      toastErr("Nothing selected.");
      return;
    }
    setClipboardBoth({ mode, paths: selected, at: Date.now() });
    toastOk(mode === "copy" ? `Copied ${selected.length} item(s).` : `Cut ${selected.length} item(s).`);
  }, [selected, setClipboardBoth]);

  const paste = useCallback(() => {
    if (!clipboard?.paths.length) {
      toastErr("Clipboard is empty.");
      return;
    }
    const destination = pathRef.current;
    if (!destination) return;
    void runJob({
      action: clipboard.mode === "cut" ? "move" : "copy",
      sources: clipboard.paths,
      destination,
    });
  }, [clipboard, runJob]);

  /* ---------- entry actions ---------- */

  const openEntry = useCallback(async (entry: FmEntry) => {
    if (entry.is_dir) {
      navigate(entry.path);
      return;
    }
    if (entry.is_symlink) {
      toastErr("Symlinks cannot be opened.");
      return;
    }
    try {
      const data = await fm.tickets([entry.path]);
      const url = data.tickets[entry.path];
      if (!url) {
        toastErr("Could not build a download link.");
        return;
      }
      a.windowOpen([{ url, title: entry.name, kind: entryWindowKind(entry) }]);
    } catch (e) {
      toastErr(messageOf(e));
    }
  }, [a, navigate]);

  const startRename = useCallback((p: string) => {
    setRenaming(p);
    setRenameValue(p.split("/").pop() ?? "");
    setMenu(null);
  }, []);

  const doRename = useCallback(async () => {
    if (!renaming || !renameValue.trim()) return;
    try {
      await fm.rename(renaming, renameValue.trim());
      toastOk("Renamed.");
      setRenaming(null);
      refresh();
      broadcast({ kind: "refresh" });
    } catch (e) {
      toastErr(messageOf(e));
    }
  }, [renaming, renameValue, refresh, broadcast]);

  const doMkdir = useCallback(async () => {
    const destination = pathRef.current;
    if (!destination || !newFolderValue.trim()) return;
    try {
      await fm.mkdir(fmJoin(destination, newFolderValue.trim()));
      toastOk("Folder created.");
      setNewFolder(false);
      setNewFolderValue("");
      refresh();
      broadcast({ kind: "refresh" });
    } catch (e) {
      toastErr(messageOf(e));
    }
  }, [newFolderValue, refresh, broadcast]);

  const loadTrash = useCallback(async () => {
    try {
      const data = await fm.listTrash();
      setTrash(data.trash);
    } catch (e) {
      setError(messageOf(e));
    }
  }, []);

  useEffect(() => {
    if (showTrash) void loadTrash();
  }, [showTrash, loadTrash]);

  const doTrash = useCallback(async () => {
    if (!confirmDelete || !confirmDelete.paths.length) return;
    try {
      if (confirmDelete.permanent) {
        await fm.cleanTrash(confirmDelete.paths);
        toastOk("Deleted from trash.");
        await loadTrash();
        setConfirmDelete(null);
        return;
      }
      const res = await fm.trash(confirmDelete.paths);
      const failed = res.results.filter((r) => !r.ok);
      if (failed.length) toastErr(`${failed.length} item(s) could not be moved to trash.`);
      else toastOk(`Moved ${res.results.length} item(s) to trash.`);
      setConfirmDelete(null);
      refresh();
      broadcast({ kind: "refresh" });
    } catch (e) {
      toastErr(messageOf(e));
    }
  }, [confirmDelete, loadTrash, refresh, broadcast]);

  const downloadSelected = useCallback(async () => {
    if (!selected.length) return;
    try {
      if (selected.length === 1) {
        const data = await fm.tickets(selected);
        const url = data.tickets[selected[0]];
        if (!url) {
          toastErr("Could not build a download link.");
          return;
        }
        a.windowOpen([{ url, title: selected[0].split("/").pop() ?? "file", kind: "other" }]);
      } else {
        const data = await fm.zip(selected);
        a.windowOpen([{ url: data.download_url, title: data.filename, kind: "other" }]);
      }
    } catch (e) {
      toastErr(messageOf(e));
    }
  }, [selected, a]);

  const restoreItems = useCallback(async (ids: string[]) => {
    try {
      const res = await fm.restore(ids);
      const failed = res.results.filter((r) => !r.ok);
      failed.forEach((r) => toastErr(r.error ?? "Could not restore."));
      if (res.results.some((r) => r.ok)) {
        toastOk("Restored.");
        await loadTrash();
        refresh();
        broadcast({ kind: "refresh" });
      }
    } catch (e) {
      toastErr(messageOf(e));
    }
  }, [loadTrash, refresh, broadcast]);

  const uploadFiles = useCallback(async (files: File[], mode: "replace" | "skip" | "keep_both") => {
    const destination = pathRef.current;
    if (!destination || !files.length) return;
    setUpload({ active: true, loaded: 0, total: 0 });
    try {
      const res = await fmUpload({
        destination,
        onConflict: mode,
        files,
        onProgress: (loaded, total) => setUpload({ active: true, loaded, total }),
      });
      const failed = res.results.filter((r) => !r.ok);
      if (failed.length) toastErr(`${failed.length} file(s) not uploaded.`);
      else toastOk(`Uploaded ${res.results.length} file(s).`);
      refresh();
      broadcast({ kind: "refresh" });
    } catch (e) {
      toastErr(messageOf(e));
    } finally {
      setUpload(null);
    }
  }, [refresh, broadcast]);

  /* ---------- selection + rows ---------- */

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = needle ? entries.filter((e) => e.name.toLowerCase().includes(needle)) : [...entries];
    const visible = settings.showHidden ? filtered : filtered.filter((e) => !e.name.startsWith("."));
    const dirFirst = (x: FmEntry, y: FmEntry) => Number(y.is_dir) - Number(x.is_dir);
    const byKey = (x: FmEntry, y: FmEntry) => {
      if (settings.sortKey === "name") return x.name.localeCompare(y.name, undefined, { sensitivity: "base", numeric: true });
      if (settings.sortKey === "kind") return (x.kind || "").localeCompare(y.kind || "");
      if (settings.sortKey === "size_bytes") return (x.size_bytes ?? -1) - (y.size_bytes ?? -1);
      return x.mtime_ns - y.mtime_ns;
    };
    return visible.sort((x, y) => dirFirst(x, y) || byKey(x, y) * settings.sortDir);
  }, [entries, query, settings]);

  const toggleSort = (key: SortKey) => {
    if (settings.sortKey === key) patchSettings({ sortDir: settings.sortDir === 1 ? -1 : 1 });
    else patchSettings({ sortKey: key, sortDir: key === "size_bytes" || key === "mtime_ns" ? -1 : 1 });
  };

  const selectRow = (entry: FmEntry, event: React.MouseEvent) => {
    const index = rows.findIndex((r) => r.path === entry.path);
    if (event.shiftKey && lastIndexRef.current >= 0) {
      const from = Math.min(lastIndexRef.current, index);
      const to = Math.max(lastIndexRef.current, index);
      setSelected(rows.slice(from, to + 1).map((r) => r.path));
      return;
    }
    if (event.ctrlKey || event.metaKey) {
      setSelected((current) => current.includes(entry.path)
        ? current.filter((p) => p !== entry.path)
        : [...current, entry.path]);
    } else {
      setSelected([entry.path]);
    }
    lastIndexRef.current = index;
  };

  /* ---------- keyboard (window owns keys while focused) ---------- */

  useEffect(() => {
    if (!focused) return;
    const onKey = (e: KeyboardEvent) => {
      const node = document.activeElement as HTMLElement | null;
      if (node && /^(input|textarea|select)$/i.test(node.tagName)) return;
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      const modifier = e.ctrlKey || e.metaKey;
      const key = (e.key || "").toLowerCase();
      if (key === "escape") {
        if (showSettings) return setShowSettings(false);
        if (conflictState) return setConflictState(null);
        if (confirmDelete) return setConfirmDelete(null);
        if (menu) return setMenu(null);
        if (renaming) return setRenaming(null);
        if (newFolder) return setNewFolder(false);
        if (selected.length) return setSelected([]);
        return;
      }
      if (e.key === "F2") {
        e.preventDefault();
        const target = selected[0] ?? rows[0]?.path;
        if (target) startRename(target);
        return;
      }
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        if (showTrash) return;
        const targets = selected.filter((p) => rows.some((r) => r.path === p));
        if (targets.length) setConfirmDelete({ paths: targets, permanent: false });
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        const entry = rows.find((r) => r.path === (selected[0] ?? rows[0]?.path));
        if (entry) void openEntry(entry);
        return;
      }
      if (!modifier && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
        e.preventDefault();
        if (!rows.length) return;
        const current = selected[selected.length - 1];
        const index = Math.max(0, rows.findIndex((r) => r.path === current));
        const next = e.key === "ArrowDown" ? Math.min(rows.length - 1, index + 1) : Math.max(0, index - 1);
        lastIndexRef.current = next;
        setSelected([rows[next].path]);
        return;
      }
      if (modifier && key === "a") { e.preventDefault(); setSelected(rows.map((r) => r.path)); return; }
      if (modifier && key === "c") { e.preventDefault(); copyCut("copy"); return; }
      if (modifier && key === "x") { e.preventDefault(); copyCut("cut"); return; }
      if (modifier && key === "v") { e.preventDefault(); paste(); return; }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focused, conflictState, confirmDelete, menu, renaming, newFolder, selected, rows, showTrash, showSettings, copyCut, paste, openEntry, startRename]);

  /* ---------- breadcrumbs ---------- */

  const crumbs = useMemo(() => {
    if (!path) return [] as { label: string; target: string }[];
    const parts = path.split("/").filter(Boolean);
    const items: { label: string; target: string }[] = [];
    if (!parts.length) return [{ label: "/", target: "/" }];
    items.push({ label: "/", target: "/" });
    let acc = "";
    for (const part of parts) {
      acc += `/${part}`;
      items.push({ label: part, target: acc });
    }
    return items;
  }, [path]);

  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const rootsHome = home ?? roots[0] ?? null;

  /* ---------- render ---------- */

  if (typeof window === "undefined") return null;

  const listRow = (entry: FmEntry) => {
    const isSelected = selectedSet.has(entry.path);
    const isCut = clipboard?.mode === "cut" && clipboard.paths.includes(entry.path);
    const isCopy = clipboard?.mode === "copy" && clipboard.paths.includes(entry.path);
    return (
      <div
        key={entry.path}
        data-path={entry.path}
        onClick={(e) => selectRow(entry, e)}
        onDoubleClick={(e) => { e.preventDefault(); void openEntry(entry); }}
        onContextMenu={(e) => {
          e.preventDefault();
          if (!isSelected) setSelected([entry.path]);
          setMenu({ x: e.clientX, y: e.clientY, path: entry.path });
        }}
        style={{
          display: "grid", gridTemplateColumns: "minmax(0,1fr) 84px 76px 138px",
          alignItems: "center", gap: 8, padding: "5px 10px",
          cursor: "default",
          background: isSelected ? `${C.cyan}1a` : "transparent",
          borderLeft: `2px solid ${isSelected ? C.cyan : "transparent"}`,
          opacity: isCut ? 0.45 : 1,
        }}
        title={entry.path}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <span style={{ fontSize: settings.iconSize, lineHeight: 1 }}>{kindIcon(entry.kind)}</span>
          {isCopy && <span style={{ color: C.gold, fontSize: 9, fontFamily: "var(--font-mono)" }}>⧉</span>}
          {renaming === entry.path ? (
            <input
              autoFocus
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void doRename();
                if (e.key === "Escape") setRenaming(null);
                e.stopPropagation();
              }}
              onClick={(e) => e.stopPropagation()}
              style={{ width: "60%", background: "#0a1526", border: `1px solid ${C.cyan}66`, color: C.text, fontSize: 12, padding: "2px 6px", outline: "none", userSelect: "text" }}
            />
          ) : (
            <span style={{ fontSize: 12, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {entry.name}
            </span>
          )}
        </span>
        <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)", letterSpacing: "0.06em" }}>
          {entry.kind === "link" ? "SYMLINK" : (KIND_LABEL[entry.kind] ?? "FILE")}
        </span>
        <span style={{ fontSize: 11, color: C.dim, fontFamily: "var(--font-mono)", textAlign: "right" }}>
          {entry.is_dir ? "—" : formatFmBytes(entry.size_bytes)}
        </span>
        <span style={{ fontSize: 10, color: C.dim, fontFamily: "var(--font-mono)" }}>
          {formatFmDate(entry.mtime_ns)}
        </span>
      </div>
    );
  };

  const gridRow = (entry: FmEntry) => {
    const isSelected = selectedSet.has(entry.path);
    const isCut = clipboard?.mode === "cut" && clipboard.paths.includes(entry.path);
    const isCopy = clipboard?.mode === "copy" && clipboard.paths.includes(entry.path);
    return (
      <div
        key={entry.path}
        data-path={entry.path}
        onClick={(e) => selectRow(entry, e)}
        onDoubleClick={(e) => { e.preventDefault(); void openEntry(entry); }}
        onContextMenu={(e) => {
          e.preventDefault();
          if (!isSelected) setSelected([entry.path]);
          setMenu({ x: e.clientX, y: e.clientY, path: entry.path });
        }}
        title={entry.path}
        style={{
          width: 96, padding: "8px 4px", borderRadius: 8, cursor: "default",
          display: "flex", flexDirection: "column", alignItems: "center", gap: 6,
          background: isSelected ? `${C.cyan}1a` : "transparent",
          border: `1px solid ${isSelected ? `${C.cyan}88` : "transparent"}`,
          opacity: isCut ? 0.45 : 1,
        }}
      >
        <span style={{ position: "relative", fontSize: Math.round(settings.iconSize * 2.2), lineHeight: 1 }}>
          {kindIcon(entry.kind)}
          {isCopy && <span style={{ position: "absolute", top: -6, right: -12, color: C.gold, fontSize: 10, fontFamily: "var(--font-mono)" }}>⧉</span>}
        </span>
        {renaming !== entry.path && (
          <span style={{ fontSize: 10, color: C.text, textAlign: "center", width: "100%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {entry.name}
          </span>
        )}
        {renaming === entry.path && (() => {
          const commit = () => { void doRename(); };
          return (
            <input autoFocus value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commit();
                if (e.key === "Escape") setRenaming(null);
                e.stopPropagation();
              }}
              onClick={(e) => e.stopPropagation()}
              style={{ width: "100%", background: "#0a1526", border: `1px solid ${C.cyan}66`, color: C.text, fontSize: 10, padding: "2px 6px", outline: "none", userSelect: "text", textAlign: "center" }} />
          );
        })()}
      </div>
    );
  };

  const SortHeader = ({ label, sortKey: key }: { label: string; sortKey: SortKey }) => (
    <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
      <button onClick={() => toggleSort(key)} style={{
        background: "none", border: "none", color: settings.sortKey === key ? C.cyan : C.dim,
        cursor: "pointer", fontSize: 9, letterSpacing: "0.1em", fontFamily: "var(--font-mono)", padding: 0,
      }}>
        {label}{settings.sortKey === key ? (settings.sortDir === 1 ? " ↑" : " ↓") : ""}
      </button>
    </div>
  );

  const formatProgress = (job: FmJob) => {
    const pct = job.total > 0 ? Math.min(100, Math.round((job.done / job.total) * 100)) : 0;
    return { pct, label: `${job.done}/${job.total}` };
  };

  const toolButton = (label: string, onClick: () => void, opts: { disabled?: boolean; accent?: boolean; danger?: boolean } = {}) => (
    <button onClick={onClick} disabled={opts.disabled}
      style={{
        padding: "4px 9px", borderRadius: 7, cursor: opts.disabled ? "default" : "pointer",
        fontFamily: "var(--font-mono)", fontSize: 9, letterSpacing: "0.08em",
        background: opts.accent ? `${C.cyan}14` : "transparent",
        border: `1px solid ${opts.danger ? C.danger : opts.accent ? C.cyan : C.line}`,
        color: opts.disabled ? C.dim : opts.danger ? C.danger : opts.accent ? C.cyan : C.text,
        opacity: opts.disabled ? 0.45 : 1,
      }}>
      {label}
    </button>
  );

  return (
    <div className="fm-root" style={{ flex: 1, minWidth: 0, minHeight: 0, width: "100%", display: "flex", flexDirection: "column",
      background: C.bg, position: "relative", overflow: "hidden", userSelect: "none", WebkitUserSelect: "none" }}>
      {/* toolbar */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 10px", borderBottom: `1px solid ${C.line}`, flexWrap: "wrap" }}>
        {toolButton("⟵", goBack, { disabled: navIndexRef.current <= 0 })}
        {toolButton("⟶", goForward, { disabled: navIndexRef.current >= navStackRef.current.length - 1 })}
        {toolButton("⟰", () => {
          const p = pathRef.current;
          if (!p || p === "/") return;
          const parent = (p.endsWith("/") ? p.slice(0, -1) : p.split("/").slice(0, -1).join("/")) || "/";
          navigate(parent);
        }, { disabled: !path || path === "/" })}
        {toolButton("⟳", refresh)}
        {toolButton("+ FOLDER", () => setNewFolder(true), { accent: true })}
        {toolButton(showTrash ? "← FILES" : "TRASH", () => setShowTrash((s) => !s), { accent: showTrash })}
        {!showTrash && (selected.length > 0) && (
          <>
            <span style={{ width: 1, height: 18, background: C.line }} />
            {toolButton("DOWNLOAD", () => void downloadSelected())}
            {toolButton("COPY", () => copyCut("copy"))}
            {toolButton("CUT", () => copyCut("cut"))}
            {toolButton("DELETE", () => setConfirmDelete({ paths: selected, permanent: false }), { danger: true })}
          </>
        )}
        <div style={{ flex: 1 }} />
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="filter…"
            style={{
              width: 110, background: "#0a1526", border: `1px solid ${C.line}`,
              color: C.text, fontSize: 10, padding: "4px 7px", outline: "none", fontFamily: "var(--font-mono)", userSelect: "text",
            }}
          />
          <button onClick={() => patchSettings({ view: "list" })} title="List view"
            style={{ padding: "4px 7px", borderRadius: 7, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 10, lineHeight: 1, letterSpacing: "0.08em", background: settings.view === "list" ? `${C.cyan}14` : "transparent", border: `1px solid ${settings.view === "list" ? C.cyan : C.line}`, color: settings.view === "list" ? C.cyan : C.text }}>
            ≡
          </button>
          <button onClick={() => patchSettings({ view: "grid" })} title="Grid view"
            style={{ padding: "4px 7px", borderRadius: 7, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 10, lineHeight: 1, letterSpacing: "0.08em", background: settings.view === "grid" ? `${C.cyan}14` : "transparent", border: `1px solid ${settings.view === "grid" ? C.cyan : C.line}`, color: settings.view === "grid" ? C.cyan : C.text }}>
            ⊞
          </button>
          <button onClick={() => patchSettings({ showHidden: !settings.showHidden })} title="Show/hide hidden (dot) files"
            style={{ padding: "4px 9px", borderRadius: 7, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 9, letterSpacing: "0.08em", background: settings.showHidden ? `${C.cyan}14` : "transparent", border: `1px solid ${settings.showHidden ? C.cyan : C.line}`, color: settings.showHidden ? C.cyan : C.text }}>
            {settings.showHidden ? "HIDDEN ON" : "HIDDEN OFF"}
          </button>
          <button onClick={() => setShowSettings((s) => !s)} title="Settings: icon size, hidden files"
            style={{ padding: "4px 8px", borderRadius: 7, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 10, letterSpacing: "0.08em", background: showSettings ? `${C.cyan}14` : "transparent", border: `1px solid ${showSettings ? C.cyan : C.line}`, color: showSettings ? C.cyan : C.text }}>
            ⚙
          </button>
          {!showTrash && (
            <>
              <select
                value={uploadConflicts}
                onChange={(e) => setUploadConflicts(e.target.value as "replace" | "skip" | "keep_both")}
                style={{ background: "#0a1526", border: `1px solid ${C.line}`, color: C.dim, fontSize: 9, fontFamily: "var(--font-mono)", padding: "3px 4px" }}
                title="What to do when an upload hits an existing name"
              >
                <option value="skip">skip existing</option>
                <option value="replace">replace existing</option>
                <option value="keep_both">keep both</option>
              </select>
              <button onClick={() => fileInputRef.current?.click()}
                style={{ padding: "4px 9px", borderRadius: 7, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 9, letterSpacing: "0.08em", background: `${C.gold}14`, border: `1px solid ${C.lineGold}`, color: C.gold }}>
                UPLOAD FILES
              </button>
              <button onClick={() => folderInputRef.current?.click()} title="Upload whole folders"
                style={{ padding: "4px 9px", borderRadius: 7, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 9, letterSpacing: "0.08em", background: "transparent", border: `1px solid ${C.line}`, color: C.text }}>
                FOLDER
              </button>
              <input ref={fileInputRef} type="file" multiple style={{ display: "none" }}
                onChange={(e) => { if (e.target.files?.length) void uploadFiles([...e.target.files], uploadConflicts); e.target.value = ""; }} />
              <input ref={folderInputRef} type="file" multiple {...({ webkitdirectory: "" } as any)} style={{ display: "none" }}
                onChange={(e) => { if (e.target.files?.length) void uploadFiles([...e.target.files], uploadConflicts); e.target.value = ""; }} />
            </>
          )}
        </div>
      </div>

      {/* settings dropdown */}
      {showSettings && (
        <>
          <div style={{ position: "fixed", inset: 0, zIndex: 90 }} onClick={() => setShowSettings(false)} />
          <div style={{ position: "absolute", right: 10, top: 46, zIndex: 91, width: 230, padding: 10, borderRadius: 10,
            background: "rgba(10,16,30,0.97)", border: `1px solid ${C.line}`, boxShadow: "0 14px 40px rgba(0,0,0,0.5)",
            display: "flex", flexDirection: "column", gap: 10 }}>
            <span style={{ fontSize: 9, fontFamily: "var(--font-mono)", letterSpacing: "0.1em", color: C.dim }}>ICON SIZE</span>
            <div style={{ display: "flex", gap: 4 }}>
              {FM_ICON_SIZES.map((size) => (
                <button key={size} onClick={() => patchSettings({ iconSize: size })} title={`${size}px icons`}
                  style={{ flex: 1, padding: "4px 0", borderRadius: 6, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 9,
                    background: settings.iconSize === size ? `${C.cyan}16` : "transparent",
                    border: `1px solid ${settings.iconSize === size ? C.cyan : C.line}`,
                    color: settings.iconSize === size ? C.cyan : C.text }}>
                  {size}px
                </button>
              ))}
            </div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
              <span style={{ fontSize: 9, fontFamily: "var(--font-mono)", letterSpacing: "0.1em", color: C.dim }}>HIDDEN FILES</span>
              <button onClick={() => patchSettings({ showHidden: !settings.showHidden })}
                style={{ padding: "3px 9px", borderRadius: 6, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 9, letterSpacing: "0.08em",
                  background: settings.showHidden ? `${C.cyan}16` : "transparent",
                  border: `1px solid ${settings.showHidden ? C.cyan : C.line}`,
                  color: settings.showHidden ? C.cyan : C.text }}>
                {settings.showHidden ? "SHOW" : "HIDE"}
              </button>
            </div>
          </div>
        </>
      )}

      {/* breadcrumbs + root shortcuts */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "6px 10px", borderBottom: `1px solid ${C.line}`, background: "rgba(255,255,255,0.02)" }}>
        <button onClick={() => rootsHome && navigate(rootsHome.path)}
          style={{ padding: "2px 8px", borderRadius: 6, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 9, border: `1px solid ${C.line}`, background: "transparent", color: C.dim }}
          title={rootsHome?.path}>
          {home ? "HOME" : roots.length === 1 ? "ROOT" : "FILES"}
        </button>
        {roots.length > 1 && roots.map((root) => (
          <button key={root.path} onClick={() => navigate(root.path)}
            style={{ padding: "2px 7px", borderRadius: 6, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 9, border: `1px solid ${C.line}`, background: "transparent", color: C.dim }}
            title={root.path}>
            {root.name}
          </button>
        ))}
        <span style={{ color: C.dim, fontSize: 11 }}>/</span>
        {crumbs.map((crumb, i) => (
          <span key={crumb.target} style={{ display: "flex", alignItems: "center", gap: 2 }}>
            {i > 0 && <span style={{ color: C.dim, fontSize: 10 }}>›</span>}
            <button onClick={() => navigate(crumb.target)}
              style={{ background: "none", border: "none", color: i === crumbs.length - 1 ? C.cyan : C.dim, cursor: "pointer", fontSize: 10, fontFamily: "var(--font-mono)", letterSpacing: "0.04em", padding: 0, maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {crumb.label}
            </button>
          </span>
        ))}
      </div>

      {/* clipboard strip */}
      {clipboard && clipboard.paths.length > 0 && !showTrash && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 10px", borderBottom: `1px solid ${C.lineGold}`, background: `${C.gold}0a` }}>
          <span style={{ fontSize: 9, fontFamily: "var(--font-mono)", letterSpacing: "0.1em", color: C.gold }}>
            {clipboard.mode === "cut" ? "CUT" : "COPIED"} · {clipboard.paths.length} item{clipboard.paths.length === 1 ? "" : "s"}
          </span>
          <button onClick={paste} style={{ padding: "2px 9px", borderRadius: 6, cursor: "pointer", fontSize: 9, fontFamily: "var(--font-mono)", letterSpacing: "0.1em", background: `${C.cyan}16`, border: `1px solid ${C.cyan}`, color: C.cyan }}>
            PASTE HERE
          </button>
          <button onClick={() => setClipboardBoth(null)} style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 9, fontFamily: "var(--font-mono)" }}>
            CLEAR
          </button>
        </div>
      )}

      {/* jobs */}
      {Object.values(jobs).some((job) => job.status === "running") && (
        <div style={{ padding: "5px 10px", borderBottom: `1px solid ${C.line}`, display: "flex", flexDirection: "column", gap: 4 }}>
          {Object.values(jobs).filter((job) => job.status === "running").map((job) => {
            const progress = formatProgress(job);
            return (
              <div key={job.id} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontSize: 9, fontFamily: "var(--font-mono)", letterSpacing: "0.08em", color: C.cyan, minWidth: 46 }}>
                  {job.action.toUpperCase()}
                </span>
                <div style={{ flex: 1, height: 5, borderRadius: 3, background: "rgba(255,255,255,0.06)", overflow: "hidden" }}>
                  <div style={{ height: "100%", width: `${progress.pct}%`, background: C.cyan, transition: "width 0.3s" }} />
                </div>
                <span style={{ fontSize: 9, fontFamily: "var(--font-mono)", color: C.dim }}>{progress.label}</span>
                <button onClick={() => void fm.cancelJob(job.id).catch(() => undefined)}
                  style={{ background: "none", border: "none", color: C.danger, cursor: "pointer", fontSize: 10, fontFamily: "var(--font-mono)" }}>
                  ✕
                </button>
              </div>
            );
          })}
        </div>
      )}

      {/* upload progress */}
      {upload && (
        <div style={{ padding: "5px 10px", borderBottom: `1px solid ${C.line}`, display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 9, fontFamily: "var(--font-mono)", letterSpacing: "0.08em", color: C.gold }}>UPLOAD</span>
          <div style={{ flex: 1, height: 5, borderRadius: 3, background: "rgba(255,255,255,0.06)", overflow: "hidden" }}>
            <div style={{ height: "100%", width: `${upload.total > 0 ? Math.min(100, (upload.loaded / upload.total) * 100) : 4}%`, background: C.gold, transition: "width 0.2s" }} />
          </div>
          <span style={{ fontSize: 9, fontFamily: "var(--font-mono)", color: C.dim }}>
            {upload.total > 0 ? formatFmBytes(upload.loaded) : "…"}
          </span>
        </div>
      )}

      {/* error */}
      {error && (
        <div style={{ padding: "5px 10px", borderBottom: `1px solid ${C.line}`, display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 10, color: C.danger, fontFamily: "var(--font-mono)", flex: 1 }}>{error}</span>
          <button onClick={() => setError(null)} style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 12 }}>×</button>
        </div>
      )}

      {/* main area */}
      <div style={{ flex: 1, minHeight: 0, overflowY: "auto", position: "relative" }}>
        {showTrash ? (
          <div>
            <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 1.2fr 90px 70px", gap: 8, padding: "6px 10px", borderBottom: `1px solid ${C.line}`, color: C.dim, fontSize: 9, fontFamily: "var(--font-mono)", letterSpacing: "0.1em" }}>
              <span>NAME</span><span>ORIGINAL</span><span>SIZE</span><span>DELETED</span>
            </div>
            {trash.length === 0 && (
              <div style={{ padding: 22, textAlign: "center", color: C.dim, fontSize: 10, fontFamily: "var(--font-mono)", letterSpacing: "0.1em" }}>TRASH IS EMPTY</div>
            )}
            {trash.map((item) => (
              <div key={item.id} style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 1.2fr 90px 70px", alignItems: "center", gap: 8, padding: "5px 10px", opacity: item.exists ? 1 : 0.4 }}>
                <span style={{ fontSize: 12, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.name}</span>
                <span style={{ fontSize: 10, color: C.dim, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.original}</span>
                <span style={{ fontSize: 10, color: C.dim, fontFamily: "var(--font-mono)", textAlign: "right" }}>{formatFmBytes(item.size_bytes)}</span>
                <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)" }}>{formatFmDate(item.at * 1000)}</span>
                <span style={{ display: "flex", gap: 5, gridColumn: "1 / -1" }}>
                  <button onClick={() => void restoreItems([item.id])} disabled={!item.exists}
                    style={{ padding: "2px 8px", borderRadius: 6, cursor: item.exists ? "pointer" : "default", fontSize: 9, fontFamily: "var(--font-mono)", background: `${C.cyan}12`, border: `1px solid ${C.line}`, color: C.cyan, opacity: item.exists ? 1 : 0.4 }}>
                    RESTORE
                  </button>
                  <button onClick={() => setConfirmDelete({ paths: [item.id], permanent: true })}
                    style={{ padding: "2px 8px", borderRadius: 6, cursor: "pointer", fontSize: 9, fontFamily: "var(--font-mono)", background: "transparent", border: `1px solid ${C.line}`, color: C.danger }}>
                    DELETE FOREVER
                  </button>
                </span>
              </div>
            ))}
            {trash.length > 0 && (
              <div style={{ padding: "6px 10px", display: "flex", gap: 8 }}>
                <button onClick={() => setConfirmDelete({ paths: trash.filter((t) => t.exists).map((t) => t.id), permanent: true })}
                  style={{ padding: "3px 10px", borderRadius: 6, cursor: "pointer", fontSize: 9, fontFamily: "var(--font-mono)", border: `1px solid ${C.danger}`, background: "transparent", color: C.danger }}>
                  EMPTY TRASH
                </button>
              </div>
            )}
          </div>
        ) : (
          <>
            {settings.view === "grid" ? (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, padding: 10, alignItems: "flex-start", alignContent: "flex-start" }}>
                {rows.map(gridRow)}
              </div>
            ) : (
              <>
                {/* header */}
                <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 84px 76px 138px", gap: 8, padding: "6px 10px", borderBottom: `1px solid ${C.line}`, color: C.dim }}>
                  <SortHeader label="NAME" sortKey="name" />
                  <SortHeader label="KIND" sortKey="kind" />
                  <div style={{ textAlign: "right" }}><SortHeader label="SIZE" sortKey="size_bytes" /></div>
                  <SortHeader label="MODIFIED" sortKey="mtime_ns" />
                </div>
                {rows.map(listRow)}
              </>
            )}
            {newFolder && (
              <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "5px 10px", borderBottom: `1px solid ${C.line}`, background: `${C.cyan}0a` }}>
                <span style={{ fontSize: 12 }}>📁</span>
                <input autoFocus value={newFolderValue}
                  onChange={(e) => setNewFolderValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void doMkdir();
                    if (e.key === "Escape") { setNewFolder(false); setNewFolderValue(""); }
                    e.stopPropagation();
                  }}
                  placeholder="new folder name…"
                  style={{ flex: 1, background: "#0a1526", border: `1px solid ${C.cyan}66`, color: C.text, fontSize: 12, padding: "3px 8px", outline: "none", userSelect: "text" }} />
                <button onClick={() => void doMkdir()} disabled={!newFolderValue.trim()}
                  style={{ padding: "3px 10px", borderRadius: 6, cursor: "pointer", fontSize: 9, fontFamily: "var(--font-mono)", background: `${C.cyan}16`, border: `1px solid ${C.cyan}`, color: C.cyan }}>
                  CREATE
                </button>
              </div>
            )}
            {loading ? (
              <div style={{ padding: 26, textAlign: "center", color: C.dim, fontSize: 10, fontFamily: "var(--font-mono)", letterSpacing: "0.14em" }}>LOADING…</div>
            ) : rows.length === 0 ? (
              <div style={{ padding: 26, textAlign: "center", color: C.dim, fontSize: 10, fontFamily: "var(--font-mono)", letterSpacing: "0.1em" }}>
                {query ? "NO MATCHES" : "EMPTY FOLDER"}
              </div>
            ) : rows.map(settings.view === "grid" ? gridRow : listRow)}
          </>
        )}
      </div>

      {/* status bar */}
      <div style={{ padding: "4px 10px", borderTop: `1px solid ${C.line}`, display: "flex", alignItems: "center", gap: 10, fontSize: 9, fontFamily: "var(--font-mono)", color: C.dim, letterSpacing: "0.06em" }}>
        <span>{path ?? ""}</span>
        {!showTrash && <span>{selected.length} of {rows.length} selected</span>}
        {entries.length !== rows.length && !loading && <span>filtered from {entries.length}</span>}
        <span style={{ marginLeft: "auto" }}>
          ⌘C COPY · ⌘X CUT · ⌘V PASTE · ⌘A ALL · F2 RENAME · DEL TRASH
        </span>
      </div>

      {/* context menu */}
      {menu && (
        <div style={{
          position: "fixed", zIndex: 80,
          left: Math.min(menu.x, (typeof window !== "undefined" ? window.innerWidth : 1200) - 150),
          top: Math.min(menu.y, (typeof window !== "undefined" ? window.innerHeight : 800) - 210),
          minWidth: 150, padding: 4, borderRadius: 10,
          background: "rgba(10,16,30,0.97)", border: `1px solid ${C.line}`, boxShadow: "0 14px 40px rgba(0,0,0,0.5)",
          display: "flex", flexDirection: "column",
        }}
          onMouseLeave={() => setMenu(null)}>
          {[
            { label: "OPEN", fn: () => { const entry = rows.find((r) => r.path === menu.path); setMenu(null); if (entry) void openEntry(entry); } },
            { label: "RENAME", fn: () => startRename(menu.path) },
            { label: "COPY", fn: () => { setSelected([menu.path]); copyCut("copy"); setMenu(null); } },
            { label: "CUT", fn: () => { setSelected([menu.path]); copyCut("cut"); setMenu(null); } },
            { label: "DOWNLOAD", fn: () => { setSelected([menu.path]); void downloadSelected(); setMenu(null); } },
            { label: "MOVE TO TRASH", fn: () => { setSelected([menu.path]); setConfirmDelete({ paths: [menu.path], permanent: false }); setMenu(null); }, danger: true },
          ].map((item) => (
            <button key={item.label} onClick={item.fn}
              style={{ textAlign: "left", padding: "6px 10px", borderRadius: 6, cursor: "pointer", fontSize: 10, fontFamily: "var(--font-mono)", letterSpacing: "0.06em", background: "none", border: "none", color: (item as { danger?: boolean }).danger ? C.danger : C.text }}>
              {item.label}
            </button>
          ))}
        </div>
      )}

      {/* confirm delete */}
      {confirmDelete && (
        <Modal title={confirmDelete.permanent ? "DELETE FOREVER" : "MOVE TO TRASH"} onClose={() => setConfirmDelete(null)}>
          <div style={{ fontSize: 11.5, color: C.text, lineHeight: 1.5, marginBottom: 14 }}>
            {confirmDelete.permanent
              ? `Permanently delete ${confirmDelete.paths.length} item(s)? This cannot be undone.`
              : `Move ${confirmDelete.paths.length} item(s) to trash? You will be able to restore them.`}
          </div>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button onClick={() => setConfirmDelete(null)} style={modalButton("cancel")}>CANCEL</button>
            <button onClick={() => void doTrash()} style={modalButton(confirmDelete.permanent ? "danger" : "ok")}>
              {confirmDelete.permanent ? "DELETE" : "TRASH"}
            </button>
          </div>
        </Modal>
      )}

      {/* conflicts */}
      {conflictState && (
        <Modal title="NAME CONFLICT" onClose={() => setConflictState(null)}>
          <div style={{ fontSize: 11, color: C.dim, lineHeight: 1.5, marginBottom: 10 }}>
            Some names already exist in the destination. Choose what to do with each.
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: "55%", overflowY: "auto", marginBottom: 12 }}>
            {conflictState.conflicts.map((conflict) => {
              const choice = conflictChoices[conflict.source] ?? "replace";
              return (
                <div key={conflict.source} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ flex: 1, fontSize: 11, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {conflict.source.split("/").pop()}
                  </span>
                  {(["replace", "keep_both", "skip"] as const).map((option) => (
                    <button key={option} onClick={() => setConflictChoices((m) => ({ ...m, [conflict.source]: option }))}
                      style={{
                        padding: "3px 8px", borderRadius: 6, cursor: "pointer", fontSize: 9, fontFamily: "var(--font-mono)",
                        background: choice === option ? `${C.gold}1c` : "transparent",
                        border: `1px solid ${choice === option ? C.lineGold : C.line}`,
                        color: choice === option ? C.gold : C.dim,
                      }}>
                      {option.replace("_", " ")}
                    </button>
                  ))}
                </div>
              );
            })}
          </div>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button onClick={() => setConflictState(null)} style={modalButton("cancel")}>CANCEL</button>
            <button onClick={() => {
              const spec = conflictState.spec;
              void runJob({ ...spec, conflicts: conflictChoices });
              setConflictState(null);
            }} style={modalButton("ok")}>PROCEED</button>
          </div>
        </Modal>
      )}

      {/* toast */}
      {toast && (
        <div style={{
          position: "absolute", left: "50%", bottom: 26, transform: "translateX(-50%)", zIndex: 90,
          padding: "7px 14px", borderRadius: 10, fontSize: 10.5, fontFamily: "var(--font-mono)", letterSpacing: "0.06em",
          background: toast.kind === "ok" ? "rgba(0,229,255,0.14)" : "rgba(255,107,107,0.16)",
          border: `1px solid ${toast.kind === "ok" ? C.cyan : C.danger}`,
          color: toast.kind === "ok" ? C.cyan : C.danger,
          boxShadow: "0 10px 30px rgba(0,0,0,0.5)", maxWidth: "80%",
        }}>
          {toast.text}
        </div>
      )}
    </div>
  );
}

/* ---------- modal frame used for confirms / conflicts ---------- */

function modalButton(kind: "ok" | "cancel" | "danger"): React.CSSProperties {
  const C_local = {
    cyan: "#00e5ff",
    danger: "#ff6b6b",
    line: "rgba(0,229,255,0.16)",
  };
  const colors: Record<string, string> = { ok: C_local.cyan, cancel: "rgba(170,192,215,0.5)", danger: C_local.danger };
  return {
    padding: "7px 14px", borderRadius: 8, cursor: "pointer", fontSize: 10,
    fontFamily: "var(--font-mono)", letterSpacing: "0.1em",
    background: "transparent", border: `1px solid ${colors[kind]}`,
    color: colors[kind],
  };
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const C_local = {
    cyan: "#00e5ff",
    bg: "rgba(6,10,20,0.97)",
    line: "rgba(0,229,255,0.16)",
    text: "rgba(235,244,255,0.92)",
    dim: "rgba(170,192,215,0.5)",
  };
  return (
    <div style={{ position: "absolute", inset: 0, zIndex: 70, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.45)" }}
      onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={title}
        style={{
          width: "min(440px, 88%)", padding: 14, borderRadius: 12,
          background: C_local.bg, border: `1px solid ${C_local.line}`,
          boxShadow: "0 20px 60px rgba(0,0,0,0.6)", display: "flex", flexDirection: "column",
        }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
          <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", letterSpacing: "0.14em", color: C_local.cyan }}>{title}</span>
          <button onClick={onClose} aria-label="Close dialog" style={{ background: "none", border: "none", color: C_local.dim, cursor: "pointer", fontSize: 14 }}>×</button>
        </div>
        {children}
      </div>
    </div>
  );
}