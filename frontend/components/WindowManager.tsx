"use client";

/* WindowManager - the APEX desktop media layer.
 *
 * Renders up to MAX_WINDOWS floating windows (images, PDFs, rendered
 * Word/Excel/text documents, or generic download cards), each with a
 * draggable title bar, bottom-right resize handle and an optional
 * right-hand notes panel. A bottom taskbar shows the open windows and
 * the active arrangement. Everything is driven by ApexProvider state so
 * voice/text commands can focus, arrange, close or write to a window.
 *
 * Visual language matches the rest of the UI: dark glass, cyan + gold,
 * monospace caps, thin borders, no external icon library.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BASE } from "../lib/api";
import {
  AppWindow,
  DESKTOPS,
  MAX_WINDOWS,
  WindowArrangement,
  WindowItem,
  isFilesWindow,
  isNotepadWindow,
  isTerminalWindow,
  itemTitle,
  kindForName,
  layoutRects,
  onDesktop,
  terminalSessionId,
  windowDownload,
} from "../lib/windows";
import { useApex } from "./ApexProvider";
import { backendFileHref } from "./FileDownloads";
import TerminalWindow from "./TerminalWindow";
import FileManagerWindow from "./FileManagerWindow";
import NotepadWindow from "./NotepadWindow";

const C = {
  cyan: "#00e5ff",
  gold: "#f5a623",
  bg: "rgba(6,10,20,0.82)",
  line: "rgba(0,229,255,0.16)",
  lineGold: "rgba(245,166,35,0.28)",
  text: "rgba(235,244,255,0.92)",
  dim: "rgba(170,192,215,0.5)",
};

const TASKBAR_H = 44;

/* The renderer endpoint accepts the signed URL shape (relative or absolute);
 * the browser-facing href from backendFileHref is NOT the backend shape (in dev
 * it is /be-prefixed), so pass the original backend URL through. Anything else
 * (external web images) is used directly. */
function windowSource(url: string): { src: string; proxy: boolean } {
  if (backendFileHref(url)) {
    return { src: `${BASE.replace(/\/$/, "")}/api/preview/render?url=${encodeURIComponent(url)}`, proxy: true };
  }
  return { src: url, proxy: false };
}

function KindTag({ kind }: { kind: string }) {
  return (
    <span style={{
      fontSize: 8.5, letterSpacing: "0.14em", textTransform: "uppercase",
      color: kind === "image" ? C.gold : C.cyan,
      border: `1px solid ${kind === "image" ? C.lineGold : C.line}`,
      background: kind === "image" ? `${C.gold}0d` : `${C.cyan}0a`,
      padding: "1px 6px", borderRadius: 9, whiteSpace: "nowrap",
    }}>
      {kind}
    </span>
  );
}

/* Title-bar download: every window gets one. Signed backend links hit the
 * download endpoint; external links open the original in a new tab. */
function DownloadButton({ item }: { item: WindowItem }) {
  const link = windowDownload(item);
  if (!link) return null;
  return (
    <a href={link.href} download={link.download} target="_blank" rel="noreferrer" aria-label="Download file"
      title="Download file"
      style={{ background: "none", border: "none", color: C.cyan, cursor: "pointer", fontSize: 13, lineHeight: 1, fontFamily: "var(--font-mono)", textDecoration: "none" }}>
      ⭳
    </a>
  );
}

/* ---------- document bodies ---------- */

type HtmlDocBodyProps = { item: WindowItem; htmlSrc: string };

// Fetches the renderer HTML once per resource and keeps it in an srcDoc iframe.
function HtmlDocBody({ item, htmlSrc }: HtmlDocBodyProps) {
  const [cache, setCache] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let live = true;
    if (cache[htmlSrc] !== undefined) return;
    setLoading(true);
    fetch(htmlSrc, { credentials: "include" })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`render ${r.status}`))))
      .then((text) => { if (live) setCache((c) => ({ ...c, [htmlSrc]: text })); })
      .catch(() => { if (live) setCache((c) => ({ ...c, [htmlSrc]: "" })); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [htmlSrc]);

  if (loading || cache[htmlSrc] === undefined) {
    return (
      <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 10 }}>
        <span className="apex-blink" style={{ color: C.dim, fontSize: 10, letterSpacing: "0.2em", fontFamily: "var(--font-mono)" }}>LOADING…</span>
      </div>
    );
  }
  if (!cache[htmlSrc]) {
    return (
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 12, alignItems: "center", justifyContent: "center", padding: 20, textAlign: "center" }}>
        <div style={{ fontSize: 34 }}>📄</div>
        <div style={{ fontSize: 11, color: C.dim, fontFamily: "var(--font-mono)", letterSpacing: "0.06em" }}>{item.title}</div>
        <a href={item.url} download={item.title} style={{
          display: "inline-block", padding: "8px 16px", borderRadius: 8,
          background: `${C.cyan}18`, border: `1px solid ${C.line}`,
          color: C.cyan, fontSize: 10, fontFamily: "var(--font-mono)",
          textDecoration: "none", letterSpacing: "0.08em",
        }}>
          DOWNLOAD FILE
        </a>
      </div>
    );
  }
  return (
    <iframe title={item.title} srcDoc={cache[htmlSrc]}
      style={{ width: "100%", height: "100%", border: "none", background: "#eceff3", display: "block" }} />
  );
}

function ImageBody({ items, index, onNext, onPrevious }: {
  items: WindowItem[]; index: number; onNext: () => void; onPrevious: () => void;
}) {
  const item = items[index] ?? items[0];
  const hasNav = items.length > 1;
  const { src } = windowSource(item.url);
  const [broken, setBroken] = useState(false);
  useEffect(() => setBroken(false), [item.url]);
  const arrow = (dir: "‹" | "›", action: () => void, label: string) => (
    <button onClick={(e) => { e.stopPropagation(); action(); }} aria-label={label}
      style={{
        position: "absolute", top: "50%", transform: "translateY(-50%)",
        left: dir === "‹" ? 8 : undefined, right: dir === "›" ? 8 : undefined,
        width: 34, height: 34, borderRadius: "50%", cursor: "pointer",
        background: C.bg, border: `1px solid ${C.line}`, color: C.cyan,
        fontSize: 17, lineHeight: 1, fontFamily: "var(--font-mono)",
        boxShadow: "0 4px 18px rgba(0,0,0,0.4)",
      }}>
      {dir}
    </button>
  );
  return (
    <div style={{ flex: 1, minHeight: 0, position: "relative", display: "flex", alignItems: "center", justifyContent: "center", background: "#04070d" }}>
      {broken ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 12, alignItems: "center", padding: 20, textAlign: "center" }}>
          <div style={{ fontSize: 34 }}>🖼️</div>
          <div style={{ fontSize: 12, color: C.text, fontWeight: 600, wordBreak: "break-word", maxWidth: "80%" }}>{itemTitle(item)}</div>
          <div style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)", letterSpacing: "0.06em" }}>
            IMAGE UNAVAILABLE — APPLY FOR THE SAVED LINK
          </div>
          <a href={item.url} target="_blank" rel="noreferrer"
            style={{ fontSize: 10, color: C.cyan, fontFamily: "var(--font-mono)", letterSpacing: "0.06em", textDecoration: "none" }}>
            OPEN ORIGINAL
          </a>
        </div>
      ) : (
        <img src={src} alt={itemTitle(item)} referrerPolicy="no-referrer" onError={() => setBroken(true)}
          style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", display: "block" }} />
      )}
      {hasNav && arrow("‹", onPrevious, "Previous image")}
      {hasNav && arrow("›", onNext, "Next image")}
    </div>
  );
}

function PdfBody({ item }: { item: WindowItem }) {
  const { src } = windowSource(item.url);
  return (
    <iframe title={item.title} src={src} style={{ width: "100%", height: "100%", border: "none", background: "#eceff3", display: "block" }} />
  );
}

function OtherBody({ item }: { item: WindowItem }) {
  const link = windowDownload(item);
  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 14, alignItems: "center", justifyContent: "center", padding: 22, textAlign: "center" }}>
      <div style={{ fontSize: 44 }}>📄</div>
      <div style={{ fontSize: 13, color: C.text, fontWeight: 600, wordBreak: "break-word" }}>{itemTitle(item)}</div>
      <div style={{ fontSize: 10, color: C.dim, fontFamily: "var(--font-mono)", letterSpacing: "0.06em" }}>
        NO INLINE PREVIEW — DOWNLOAD INSTEAD
      </div>
      {link ? (
        <a href={link.href} download={link.download}
          style={{ display: "inline-block", padding: "9px 18px", borderRadius: 9,
            background: `${C.cyan}18`, border: `1px solid ${C.line}`,
            color: C.cyan, fontSize: 10, fontFamily: "var(--font-mono)",
            textDecoration: "none", letterSpacing: "0.1em" }}>
          DOWNLOAD FILE
        </a>
      ) : (
        <div style={{ fontSize: 10, color: C.dim, fontFamily: "var(--font-mono)" }}>LINK NOT AVAILABLE</div>
      )}
    </div>
  );
}

function WindowBody({ w, focused, onNext, onPrevious }: {
  w: AppWindow; focused: boolean; onNext: () => void; onPrevious: () => void;
}) {
  const a = useApex();
  const item = w.items[w.index] ?? w.items[0];
  if (isTerminalWindow(w)) {
    const sessionId = terminalSessionId(item);
    if (!sessionId) return <OtherBody item={item} />;
    return (
      <TerminalWindow
        key={sessionId}
        sessionId={sessionId}
        base={BASE}
        focused={focused}
        onClosed={() => a.windowClose(w.id)}
      />
    );
  }
  if (isFilesWindow(w)) {
    return (
      <FileManagerWindow
        key={w.id}
        focused={focused}
      />
    );
  }
  if (isNotepadWindow(w)) {
    return <NotepadWindow key={w.id} windowId={w.id} focused={focused} />;
  }
  const kind = w.kind === "image" && w.items.length > 1 ? "image" : w.kind;
  if (kind === "image") return <ImageBody items={w.items} index={w.index} onNext={onNext} onPrevious={onPrevious} />;
  if (kind === "pdf") return <PdfBody item={item} />;
  if (kind === "docx" || kind === "xlsx" || kind === "pptx" || kind === "text") {
    return <HtmlDocBody item={item} htmlSrc={windowSource(item.url).src} />;
  }
  return <OtherBody item={item} />;
}

/* ---------- notes panel ---------- */

function NotesPanel({ w, onNote }: { w: AppWindow; onNote: (note: string) => void }) {
  const a = useApex();
  return (
    <div style={{
      width: 210, minWidth: 210, borderLeft: `1px solid ${C.line}`,
      display: "flex", flexDirection: "column", background: "rgba(8,14,26,0.55)",
    }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "8px 10px", borderBottom: `1px solid ${C.line}`,
      }}>
        <span style={{ fontSize: 9, letterSpacing: "0.14em", color: C.dim, fontFamily: "var(--font-mono)" }}>NOTES</span>
        <button onClick={() => a.windowToggleNotes(w.id)} aria-label="Hide notes"
          style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 13, lineHeight: 1, fontFamily: "var(--font-mono)" }}>
          ×
        </button>
      </div>
      <textarea
        value={w.note}
        onChange={(e) => onNote(e.target.value)}
        placeholder="notes…"
        style={{
          flex: 1, resize: "none", border: "none", outline: "none", padding: 10,
          background: "transparent", color: C.text, fontSize: 11.5, lineHeight: 1.5,
          fontFamily: "var(--font-body)",
        }}
      />
    </div>
  );
}

/* ---------- window frame ---------- */

const frameBase: React.CSSProperties = {
  position: "fixed",
  display: "flex", flexDirection: "column",
  background: C.bg,
  border: `1px solid ${C.line}`,
  borderRadius: 12,
  overflow: "hidden",
  boxShadow: "0 18px 60px rgba(0,0,0,0.55)",
  backdropFilter: "blur(18px)",
};

export default function WindowManager() {
  const a = useApex();
  const windows = a.windows;
  const focusedId = a.focusedWindowId;
  const [arrangement, setArrangement] = useState<WindowArrangement>("cascade");
  const [interacting, setInteracting] = useState(false);
  const dragRef = useRef<{ id: string; kind: "move" | "resize"; startX: number; startY: number; rect: { x: number; y: number; w: number; h: number } } | null>(null);

  const viewport = useMemo(
    () => typeof window !== "undefined"
      ? { vw: window.innerWidth, vh: window.innerHeight }
      : { vw: 1200, vh: 800 },
    [],
  );

  // Global keys: Ctrl+Alt+1..4 switch virtual desktops, Ctrl+Alt arrows step
  // between them. Escape closes the focused window, arrows navigate its gallery.
  // A focused terminal keeps its own keys (captured at the host element), but
  // the guard also refuses window-level handling so a terminal can never be
  // dismissed by an Escape the terminal did not consume. Files windows own
  // their shortcuts too (rename/delete/navigate), so they get the same pass.
  useEffect(() => {
    const onDesktopKey = (e: KeyboardEvent) => {
      if (!e.ctrlKey || !e.altKey) return;
      const num = Number(e.key) - 1;
      if (num >= 0 && num < DESKTOPS) {
        e.preventDefault();
        a.desktopSet(num);
        return;
      }
      if (e.key === "ArrowRight") { e.preventDefault(); a.desktopNext(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); a.desktopPrev(); }
    };
    window.addEventListener("keydown", onDesktopKey);
    return () => window.removeEventListener("keydown", onDesktopKey);
  }, [a]);

  useEffect(() => {
    if (!windows.length) return;
    const focused = byId(focusedId);
    if (!focused || focused.desktop !== a.activeDesktop) return;
    if (isTerminalWindow(focused) || isFilesWindow(focused) || isNotepadWindow(focused)) return;
    const node = document.activeElement as HTMLElement | null;
    if (node && /^(input|textarea|select)$/i.test(node.tagName)) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { a.windowClose(focused.id); }
      else if (e.key === "ArrowRight") a.windowNext();
      else if (e.key === "ArrowLeft") a.windowPrevious();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [a, a.activeDesktop, focusedId, windows.length]);

  if (!windows.length) return null;

  const byId = (id: string | null | undefined) => (id ? windows.find((w) => w.id === id) : undefined);
  // Only the active virtual desktop is on screen; focus falls back to the most
  // recently opened visible window so the highlight never lands on a hidden one.
  const visible = onDesktop(windows, a.activeDesktop);
  const focusedVisible = byId(focusedId);
  const focused = (focusedVisible && focusedVisible.desktop === a.activeDesktop ? focusedVisible : undefined)
    ?? visible[visible.length - 1];

  const focusWindow = (id: string) => { a.windowFocus(id); };

  const arrange = (arr: WindowArrangement) => {
    setArrangement(arr);
    a.windowArrange(arr);
  };

  const onPointerDown = (id: string, kind: "move" | "resize") => (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    // Titles-bar buttons (close/minimize/maximize/notes) must keep their click;
    // capturing the pointer here would swallow the click and retarget it.
    if (kind === "move" && (e.target as HTMLElement).closest("button, a, input, textarea, select")) return;
    const w = byId(id);
    if (!w || (kind === "move" && w.maximized)) return;
    focusWindow(id);
    dragRef.current = {
      id,
      kind,
      startX: e.clientX,
      startY: e.clientY,
      rect: { x: w.rect.x, y: w.rect.y, w: w.rect.w, h: w.rect.h },
    };
    setInteracting(true);
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const drag = dragRef.current;
    if (!drag) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    if (drag.kind === "move") {
      a.windowUpdate(drag.id, {
        rect: {
          x: Math.max(-drag.rect.w + 90, Math.min(drag.rect.x + dx, vw - 90)),
          y: Math.max(0, Math.min(drag.rect.y + dy, vh - TASKBAR_H - 42)),
          w: drag.rect.w,
          h: drag.rect.h,
        },
      });
    } else {
      a.windowUpdate(drag.id, {
        rect: {
          x: drag.rect.x,
          y: drag.rect.y,
          w: Math.max(300, Math.min(drag.rect.w + dx, vw - drag.rect.x)),
          h: Math.max(180, Math.min(drag.rect.h + dy, vh - TASKBAR_H - drag.rect.y)),
        },
      });
    }
  };

  const endPointer = () => {
    dragRef.current = null;
    setInteracting(false);
  };

  // All windows stay mounted; minimized ones are only hidden (kept out of view
  // but never unmounted) so live terminals keep their PTY session and previews
  // keep their stream. Restoring is then instant and lossless.
  const order = [...windows];

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 40, pointerEvents: "none" }}>
      {order.map((w) => {
        const focusedWin = w.id === focusedId;
        const rect = w.maximized
          ? { x: 0, y: 0, w: viewport.vw, h: viewport.vh - TASKBAR_H }
          : w.rect;
        const item = w.items[w.index] ?? w.items[0];
        const hasNav = w.items.length > 1;
        return (
          <div key={w.id} role="dialog" aria-label={itemTitle(item)}
            onPointerDown={() => focusWindow(w.id)}
            style={{
              ...frameBase,
              left: rect.x, top: rect.y, width: rect.w, height: rect.h,
              zIndex: focusedWin ? 60 : 5 + order.indexOf(w),
              visibility: w.minimized || w.desktop !== a.activeDesktop ? "hidden" : "visible",
              pointerEvents: w.minimized || w.desktop !== a.activeDesktop ? "none" : "auto",
              cursor: interacting ? "default" : undefined,
              transition: interacting ? "none" : "left .28s cubic-bezier(.22,.9,.3,1), top .28s cubic-bezier(.22,.9,.3,1), width .28s cubic-bezier(.22,.9,.3,1), height .28s cubic-bezier(.22,.9,.3,1)",
              borderColor: focusedWin ? `${C.cyan}55` : C.line,
              boxShadow: focusedWin ? "0 20px 70px rgba(0,0,0,0.6), 0 0 34px rgba(0,229,255,0.08)" : "0 18px 60px rgba(0,0,0,0.55)",
            }}>
            {/* title bar */}
            <div
              onPointerDown={onPointerDown(w.id, "move")}
              onPointerMove={onPointerMove}
              onPointerUp={endPointer}
              onPointerCancel={endPointer}
              style={{
                display: "flex", alignItems: "center", gap: 9,
                padding: "7px 10px", borderBottom: `1px solid ${C.line}`,
                background: focusedWin ? "linear-gradient(180deg, rgba(0,229,255,0.10), rgba(0,229,255,0.02))" : undefined,
                cursor: w.maximized ? "default" : "grab", touchAction: "none", userSelect: "none",
              }}>
              <span style={{ flex: 1, minWidth: 0, fontSize: 10, letterSpacing: "0.1em", color: focusedWin ? C.text : C.dim,
                fontFamily: "var(--font-mono)", textTransform: "uppercase", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {itemTitle(item)}
              </span>
              {hasNav && (
                <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)", whiteSpace: "nowrap" }}>
                  {w.index + 1}/{w.items.length}
                </span>
              )}
              <KindTag kind={w.kind} />
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <DownloadButton item={w.items[w.index] ?? w.items[0]} />
                <button onClick={() => a.windowToggleNotes(w.id)} aria-label={w.showNotes ? "Hide notes" : "Show notes"}
                  style={{ background: "none", border: "none", color: w.showNotes ? C.gold : C.dim, cursor: "pointer", fontSize: 12, lineHeight: 1, fontFamily: "var(--font-mono)" }}>
                  ✎
                </button>
                <button onClick={() => a.windowToggleMinimize(w.id)} aria-label="Minimize window"
                  style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 12, lineHeight: 1, fontFamily: "var(--font-mono)" }}>
                  –
                </button>
                <button onClick={() => a.windowToggleMaximize(w.id)} aria-label={w.maximized ? "Restore window" : "Maximize window"}
                  style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 11, lineHeight: 1, fontFamily: "var(--font-mono)" }}>
                  {w.maximized ? "⊡" : "□"}
                </button>
                <button onClick={() => a.windowClose(w.id)} aria-label="Close window"
                  style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 14, lineHeight: 1 }}>×</button>
              </div>
            </div>

            {/* body + optional notes */}
            <div style={{ flex: 1, minHeight: 0, display: "flex" }}>
              <div style={{ flex: 1, minWidth: 0, display: "flex" }}>
                <WindowBody w={w} focused={focusedWin}
                  onNext={() => a.windowNext()}
                  onPrevious={() => a.windowPrevious()} />
              </div>
              {w.showNotes && <NotesPanel w={w} onNote={(note) => a.windowSetNote(w.id, note)} />}
            </div>

            {/* resize handle hidden when maximized */}
            {!w.maximized && (
              <div
                onPointerDown={onPointerDown(w.id, "resize")}
                onPointerMove={onPointerMove}
                onPointerUp={endPointer}
                onPointerCancel={endPointer}
                style={{ position: "absolute", right: 0, bottom: 0, width: 18, height: 18,
                  cursor: "nwse-resize", touchAction: "none",
                  background: "linear-gradient(135deg, transparent 50%, rgba(0,229,255,0.35) 50%)",
                  borderBottomRightRadius: 12 }} />
            )}
          </div>
        );
      })}

      {/* taskbar */}
      <div style={{
        position: "fixed", left: 0, right: 0, bottom: 0, height: TASKBAR_H, zIndex: 41,
        display: "flex", alignItems: "center", gap: 8, padding: "0 12px",
        background: "rgba(6,10,20,0.35)", borderTop: `1px solid ${C.line}`, pointerEvents: "auto",
      }}>
        <button onClick={() => a.windowCloseAll()} aria-label="Close all windows"
          style={{ fontSize: 9, letterSpacing: "0.12em", fontFamily: "var(--font-mono)", color: C.dim,
            background: "none", border: `1px solid ${C.line}`, borderRadius: 8, padding: "5px 9px", cursor: "pointer" }}>
          ✕ ALL
        </button>
        <button onClick={() => arrange(arrangement === "cascade" ? "grid" : arrangement === "grid" ? "tile-h" : arrangement === "tile-h" ? "tile-v" : arrangement === "tile-v" ? "center" : "cascade")}
          aria-label="Arrange windows"
          style={{ fontSize: 9, letterSpacing: "0.12em", fontFamily: "var(--font-mono)", color: C.cyan,
            background: `${C.cyan}0d`, border: `1px solid ${C.line}`, borderRadius: 8, padding: "5px 9px", cursor: "pointer" }}>
          ▦ {arrangement.toUpperCase()}
        </button>
        <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)", letterSpacing: "0.1em", margin: "0 6px" }}>
          {visible.length}/{MAX_WINDOWS}
        </span>
        <div style={{ flex: 1, display: "flex", gap: 6, overflowX: "auto" }}>
          {windows.map((w, i) => {
            const title = itemTitle(w.items[w.index] ?? w.items[0]);
            // Linux-style: every window stays in the taskbar; clicking one on
            // another desktop switches there, unminimizes and focuses it.
            return (
              <button key={w.id}
                onClick={() => a.windowFocus(w.id)}
                style={{
                  display: "flex", alignItems: "center", gap: 6, maxWidth: 200,
                  padding: "5px 10px", borderRadius: 9, cursor: "pointer", whiteSpace: "nowrap", overflow: "hidden",
                  fontSize: 9.5, letterSpacing: "0.06em", fontFamily: "var(--font-mono)",
                  color: w.id === focusedId && !w.minimized ? C.cyan : C.dim,
                  background: w.id === focusedId && !w.minimized ? `${C.cyan}12` : "rgba(255,255,255,0.02)",
                  border: w.minimized ? `1px dashed ${C.line}` : `1px solid ${C.line}`,
                  opacity: w.desktop !== a.activeDesktop && w.id !== focusedId ? 0.55 : 1,
                }}>
                <span>{i + 1}</span>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{title}</span>
                {w.minimized && <span style={{ fontSize: 8, color: C.gold }}>▾</span>}
              </button>
            );
          })}
        </div>
        {/* virtual-desktop switcher (right side, like a Linux panel) */}
        <div style={{ display: "flex", alignItems: "center", gap: 4, marginLeft: "auto" }}>
          <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)", letterSpacing: "0.12em", marginRight: 3 }}>
            ▦
          </span>
          {Array.from({ length: DESKTOPS }, (_, d) => {
            const count = onDesktop(windows, d).length;
            const active = d === a.activeDesktop;
            return (
              <button key={d}
                onClick={() => a.desktopSet(d)}
                aria-label={`Virtual desktop ${d + 1}`}
                title={`Virtual desktop ${d + 1}`}
                style={{
                  minWidth: 28, height: 28, borderRadius: 8, cursor: "pointer",
                  display: "flex", alignItems: "center", justifyContent: "center", gap: 3,
                  padding: "0 4px", fontFamily: "var(--font-mono)", fontSize: 10,
                  color: active ? C.cyan : C.dim,
                  background: active ? `${C.cyan}14` : "rgba(255,255,255,0.02)",
                  border: active ? `1px solid ${C.cyan}66` : `1px solid ${C.line}`,
                }}>
                <span>{d + 1}</span>
                {count > 0 && <span style={{ fontSize: 8, opacity: 0.75 }}>{count}</span>}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}