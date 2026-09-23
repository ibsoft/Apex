"use client";

/* ChatUI - the assistant panel: chat stream, conversation history, skills,
   runtime settings (engine/provider/model/voice/wake word) and memory.

   Styled to sit on the APEX world: glassy dark, cyan + gold, monospace caps.
*/

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useApex, Message } from "./ApexProvider";
import { api } from "../lib/api";
import FileDownloads, { backendFileHref } from "./FileDownloads";

const C = {
  cyan: "#00e5ff",
  gold: "#f5a623",
  bg: "rgba(6,10,20,0.82)",
  line: "rgba(0,229,255,0.16)",
  lineGold: "rgba(245,166,35,0.28)",
  text: "rgba(235,244,255,0.92)",
  dim: "rgba(170,192,215,0.5)",
};

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span style={{ fontSize: 9, letterSpacing: "0.16em", color: C.dim, fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>{label}</span>
      {children}
    </div>
  );
}

const inputBase: React.CSSProperties = {
  background: "rgba(8,14,26,0.9)",
  border: `1px solid ${C.line}`,
  borderRadius: 8,
  color: C.text,
  padding: "7px 10px",
  fontSize: 12,
  fontFamily: "var(--font-mono)",
  outline: "none",
};
const selectBase: React.CSSProperties = { ...inputBase, width: "100%" };

function ToolChips({ tools }: { tools?: NonNullable<Message["meta"]>["tools"] }) {
  if (!tools || tools.length === 0) return null;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>
      {tools.map((t, i) => (
        <span key={`${t.name}-${i}`} style={{
          fontSize: 9, letterSpacing: "0.08em", fontFamily: "var(--font-mono)", textTransform: "uppercase",
          padding: "2px 7px", borderRadius: 10,
          background: t.running ? `${C.gold}1d` : `${C.cyan}0e`,
          border: `1px solid ${t.running ? C.lineGold : C.line}`,
          color: t.running ? C.gold : C.cyan,
        }}>
          {t.running ? "▶" : "✓"} {t.name}
        </span>
      ))}
    </div>
  );
}

function isImageUrl(url: string): boolean {
  return /\.(jpg|jpeg|png|gif|webp|svg|bmp)(\?.*)?$/i.test(url);
}

function InlineImage({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return (
    <img
      src={src}
      alt={alt}
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
      style={{ maxWidth: "100%", maxHeight: 320, borderRadius: 8, display: "block", margin: "6px 0" }}
    />
  );
}

function renderRichText(text: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const regex = /(!?)\[((?:\\.|[^\]\\])*)\]\((https?:\/\/[^\s)]+|\/api\/files\/download\/[A-Za-z0-9_.-]+|\/api\/editor\/download\/[A-Za-z0-9_.-]+|\/api\/images\/file\/[A-Za-z0-9_.-]+|\/api\/obsidian\/file\?path=[^\s)]+)\)|(https?:\/\/[^\s<>"{}|\\^`[\]]+)|(\/api\/files\/download\/[A-Za-z0-9_.-]+)|(\/api\/editor\/download\/[A-Za-z0-9_.-]+)|(\/api\/images\/file\/[A-Za-z0-9_.-]+)|(\/api\/obsidian\/file\?path=[^\s<>"{}|\\^`[\]]+)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(
        <span key={`t-${lastIndex}`} style={{ whiteSpace: "pre-wrap" }}>
          {text.slice(lastIndex, match.index)}
        </span>
      );
    }
    const full = match[0];
    const label = match[2]?.replace(/\\([\\\[\]])/g, "$1");
    const url = match[3] || match[4] || match[5] || match[6];
    const fileHref = backendFileHref(url);
    const isBackendFile = fileHref !== null;
    const isImage = match[1] === "!" || (!match[3] && isImageUrl(url));
    if (isImage) {
      nodes.push(<InlineImage key={`img-${match.index}`} src={fileHref || url} alt={label || "image"} />);
    } else if (isBackendFile) {
      nodes.push(
        <a
          key={`a-${match.index}`}
          href={fileHref}
          download
          style={{ color: C.cyan, textDecoration: "underline", overflowWrap: "anywhere" }}
        >
          {label || "Download file"}
        </a>
      );
    } else {
      nodes.push(
        <a
          key={`a-${match.index}`}
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: C.cyan, textDecoration: "underline", overflowWrap: "anywhere" }}
        >
          {label || url}
        </a>
      );
    }
    lastIndex = match.index + full.length;
  }
  if (lastIndex < text.length) {
    nodes.push(
      <span key={`t-${lastIndex}`} style={{ whiteSpace: "pre-wrap" }}>
        {text.slice(lastIndex)}
      </span>
    );
  }
  return nodes;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore clipboard errors
    }
  }, [text]);
  return (
    <button
      onClick={handleCopy}
      style={{
        marginTop: 4,
        padding: "2px 8px",
        fontSize: 10,
        fontFamily: "var(--font-mono)",
        letterSpacing: "0.05em",
        color: copied ? C.gold : C.dim,
        background: "transparent",
        border: "none",
        cursor: "pointer",
        opacity: copied ? 1 : 0.7,
        transition: "opacity 0.15s",
      }}
      onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
      onMouseLeave={(e) => (e.currentTarget.style.opacity = copied ? "1" : "0.7")}
      aria-label="Copy message"
    >
      {copied ? "COPIED" : "COPY"}
    </button>
  );
}

function MessageBubble({ msg }: { msg: Message }) {
  const isUser = msg.role === "user";
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: isUser ? "flex-end" : "flex-start" }}>
      <div style={{
        maxWidth: "92%", padding: "8px 11px", borderRadius: 12,
        fontSize: 12.5, lineHeight: 1.5, wordBreak: "break-word",
        background: isUser ? `${C.cyan}14` : "rgba(255,255,255,0.04)",
        border: isUser ? `1px solid ${C.cyan}33` : "1px solid rgba(255,255,255,0.08)",
        color: C.text,
      }}>
        {msg.content ? renderRichText(msg.content) : (msg.streaming ? "…" : "")}
        {msg.streaming && <span className="apex-blink" style={{ color: C.cyan }}>▊</span>}
      </div>
      {!isUser && <FileDownloads tools={msg.meta?.tools} />}
      <ToolChips tools={msg.meta?.tools} />
      {msg.content && !msg.streaming && <CopyButton text={msg.content} />}
    </div>
  );
}

function MensajeList({ messages }: { messages: Message[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight, behavior: "smooth" });
  }, [messages]);
  if (messages.length === 0) {
    return (
      <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", textAlign: "center", padding: 24 }}>
        <div>
          <div style={{ fontSize: 22, color: C.cyan, letterSpacing: "0.2em", fontFamily: "var(--font-mono)" }}>APEX</div>
          <div style={{ fontSize: 10, color: C.dim, marginTop: 8, lineHeight: 1.7, fontFamily: "var(--font-mono)" }}>
            TAP THE CORE, OR SAY<br />
            &ldquo;{""}APEX{""}&hellip;&rdquo; AND ASK
          </div>
        </div>
      </div>
    );
  }
  return (
    <div ref={ref} style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: 12, padding: "12px 14px" }}>
      <div aria-hidden style={{ height: 4 }} />
      {messages.map((m) => <MessageBubble key={m.id} msg={m} />)}
      <div aria-hidden style={{ height: 6 }} />
    </div>
  );
}

function PreviewModal() {
  const a = useApex();
  const preview = a.preview;
  useEffect(() => {
    if (!preview) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") a.closePreview();
      if (e.key === "ArrowRight") a.nextPreview();
      if (e.key === "ArrowLeft") a.previousPreview();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [preview, a]);
  if (!preview || !preview.items.length) return null;
  const item = preview.items[preview.index];
  const isImage = item.kind === "image";
  const isPdf = /\.pdf(\?.*)?$/i.test(item.title) || /\.pdf(\?.*)?$/i.test(item.url);
  const displayUrl = backendFileHref(item.url) || item.url;
  const maximized = a.previewMaximized;
  const hasNav = preview.items.length > 1;
  return (
    <div onClick={a.closePreview} style={{
      position: "fixed", inset: 0, zIndex: 100,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: maximized ? C.bg : "rgba(6,10,20,0.88)",
      backdropFilter: maximized ? undefined : "blur(14px)",
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        position: "relative",
        width: maximized ? "100vw" : (isPdf ? "min(900px, 92vw)" : undefined),
        height: maximized ? "100vh" : undefined,
        maxWidth: maximized ? "100vw" : "min(900px, 92vw)",
        maxHeight: maximized ? "100vh" : "min(85vh, 720px)",
        display: "flex", flexDirection: "column",
        background: C.bg,
        border: maximized ? "none" : `1px solid ${C.line}`,
        borderRadius: maximized ? 0 : 16,
        boxShadow: maximized ? "none" : "0 24px 80px rgba(0,0,0,0.6)",
        overflow: "hidden",
      }}>
        {/* header */}
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "10px 14px", borderBottom: `1px solid ${C.line}`,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: C.text, fontFamily: "var(--font-mono)", letterSpacing: "0.08em", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 360 }}>
              {item.title}
            </div>
            {hasNav && (
              <span style={{ fontSize: 10, color: C.dim, fontFamily: "var(--font-mono)", whiteSpace: "nowrap" }}>
                {preview.index + 1} / {preview.items.length}
              </span>
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {hasNav && (
              <>
                <button onClick={a.previousPreview} aria-label="Previous"
                  style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 16 }}>‹</button>
                <button onClick={a.nextPreview} aria-label="Next"
                  style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 16 }}>›</button>
              </>
            )}
            <button onClick={a.togglePreviewMaximized} aria-label={maximized ? "Normalize preview" : "Maximize preview"}
              style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 13, fontFamily: "var(--font-mono)", letterSpacing: "0.05em" }}>
              {maximized ? "⊡" : "□"}
            </button>
            <a href={displayUrl} download={item.title}
              style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: C.cyan, textDecoration: "none", letterSpacing: "0.08em" }}>
              DOWNLOAD
            </a>
            <button onClick={a.closePreview} aria-label="Close preview"
              style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 18, lineHeight: 1 }}>
              ×
            </button>
          </div>
        </div>
        {/* body */}
        <div style={{
          flex: 1, minHeight: 0, display: "flex", alignItems: "center", justifyContent: "center",
          padding: isImage || isPdf ? 0 : 24,
        }}>
          {isImage ? (
            <img src={displayUrl} alt={item.title} referrerPolicy="no-referrer"
              style={{ maxWidth: "100%", maxHeight: maximized ? "calc(100vh - 44px)" : "min(70vh, 600px)", objectFit: "contain", display: "block" }} />
          ) : isPdf ? (
            <embed src={displayUrl} type="application/pdf"
              style={{ width: maximized ? "100vw" : "min(900px, 92vw)", height: maximized ? "calc(100vh - 44px)" : "min(75vh, 640px)", border: "none", display: "block" }} />
          ) : (
            <div style={{ textAlign: "center" }}>
              <div style={{ fontSize: 48, marginBottom: 16 }}>📄</div>
              <div style={{ fontSize: 14, color: C.text, marginBottom: 8, fontWeight: 600 }}>{item.title}</div>
              <div style={{ fontSize: 11, color: C.dim, marginBottom: 18, fontFamily: "var(--font-mono)" }}>
                Voice preview window
              </div>
              <a href={displayUrl} download={item.title}
                style={{
                  display: "inline-block", padding: "8px 18px", borderRadius: 8,
                  background: `${C.cyan}18`, border: `1px solid ${C.line}`,
                  color: C.cyan, fontSize: 11, fontFamily: "var(--font-mono)",
                  textDecoration: "none", letterSpacing: "0.08em",
                }}>
                DOWNLOAD FILE
              </a>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function useNow() {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

function formatTimeLeft(ms: number): string {
  if (ms <= 0) return "now";
  const totalSeconds = Math.ceil(ms / 1000);
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function TimersPanel() {
  const a = useApex();
  const now = useNow();
  const items = [
    ...a.timers.map((t) => ({ ...t, type: "timer" as const })),
    ...a.reminders.map((r) => ({ ...r, type: "reminder" as const })),
  ].sort((a, b) => a.fireAt - b.fireAt);
  if (!items.length) return null;
  return (
    <div style={{ padding: "8px 12px", borderTop: `1px solid ${C.line}`, display: "flex", flexDirection: "column", gap: 6 }}>
      {items.map((item) => (
        <div key={item.id} style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "6px 10px", borderRadius: 8,
          background: "rgba(255,255,255,0.03)", border: `1px solid ${C.line}`,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
            <span style={{ fontSize: 10, color: C.cyan, fontFamily: "var(--font-mono)", letterSpacing: "0.08em" }}>
              {item.type === "timer" ? "TIMER" : "REMINDER"}
            </span>
            <span style={{ fontSize: 11, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {item.name}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
            <span style={{ fontSize: 11, color: C.gold, fontFamily: "var(--font-mono)" }}>
              {formatTimeLeft(item.fireAt - now)}
            </span>
            <button onClick={() => item.type === "timer" ? a.cancelTimer(item.id) : a.cancelReminder(item.id)}
              aria-label="Cancel"
              style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 14, lineHeight: 1 }}>
              ×
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function Empty({ label }: { label: string }) {
  return <div style={{ color: C.dim, fontSize: 10, fontFamily: "var(--font-mono)", letterSpacing: "0.1em", textAlign: "center", padding: "18px 8px" }}>{label}</div>;
}

export default function ChatUI() {
  const a = useApex();
  const [tab, setTab] = useState<"chat" | "hist" | "settings" | "memory">("chat");
  const [draft, setDraft] = useState("");
  const [collapsed, setCollapsed] = useState(a.chatCollapsed);
  useEffect(() => {
    a.setChatCollapsed(collapsed);
  }, [collapsed, a.setChatCollapsed]);
  const [histOpen, toggleHist] = useState(false);
  const [memSearch, setMemSearch] = useState("");
  const [memNote, setMemNote] = useState("");
  const [memResults, setMemResults] = useState<null | any[]>(null);
  const [memFiles, setMemFiles] = useState<FileList | null>(null);
  const [memUploading, setMemUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [sendDisabled, setSendDisabled] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const hasPreview = !!a.preview;

  useEffect(() => {
    if (collapsed || tab !== "chat" || !a.user || a.busy || hasPreview) return;
    const focusInput = () => {
      if (document.hidden || document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      inputRef.current?.focus({ preventScroll: true });
    };
    // Wait for the visible textarea to be mounted and re-enabled after a reply.
    const frame = requestAnimationFrame(focusInput);
    window.addEventListener("focus", focusInput);
    document.addEventListener("visibilitychange", focusInput);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("focus", focusInput);
      document.removeEventListener("visibilitychange", focusInput);
    };
  }, [collapsed, tab, a.user?.id, a.busy, a.activeId, a.skill, hasPreview]);

  const send = useCallback(async (text?: string) => {
    const body = (text ?? draft).trim();
    if (!body || a.busy) return;
    setDraft("");
    setSendDisabled(true);
    try {
      await a.sendMessage(body, { voice: false, skill: a.skill });
    } finally {
      setSendDisabled(false);
    }
  }, [a, draft]);

  const uploadMemoryFiles = useCallback(async () => {
    if (!memFiles || memFiles.length === 0) return;
    setMemUploading(true);
    try {
      const res = await api.memory.upload(memFiles);
      if (res.ok) {
        setMemFiles(null);
        if (fileInputRef.current) fileInputRef.current.value = "";
        await a.refreshMemory();
      }
    } finally {
      setMemUploading(false);
    }
  }, [memFiles, a]);

  const engine = a.settings.engine ?? a.config?.engine ?? "";
  const provider = a.settings.provider ?? a.config?.provider ?? "";
  const model = a.settings.model ?? "<auto>";
  const wake = a.settings.wake_word ?? a.config?.wake_word ?? "apex";
  const responseLanguage = a.settings.response_language ?? a.config?.response_language ?? "en";

  const providers = (a.config?.providers ?? {}) as Record<string, { available?: boolean; models?: string[] }>;
  const providerNames = Object.keys(providers);
  const engines = a.config?.engines ?? ["responses"];

  /* models for the selected provider - refetched when the provider changes */
  const [models, setModels] = useState<string[]>(a.config?.models ?? []);
  useEffect(() => {
    let live = true;
    setModels([]);
    if (provider) {
      api.models(provider).then((r) => { if (live) setModels(r.models ?? []); }).catch(() => { if (live) setModels(a.config?.models ?? []); });
    }
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider]);

  const provAvail = (p: string) => !!(providers[p]?.available);
  const needHint = (p: string) =>
    p === "openai" ? (provAvail(p) ? null : "set OPENAI_API_KEY in backend/.env or sign in with OpenAI") :
    p === "codex" ? (provAvail(p) ? null : "Sign in with ChatGPT using backend/setup_provider.py") :
    p === "kimi" ? (provAvail(p) ? null : "set KIMI_API_KEY in backend/.env (Moonshot AI)") :
    p === "ollama" ? null :
    p === "torch" ? (provAvail(p) ? null : "pip install torch transformers") :
    null;

  return (
    <>
      <PreviewModal />
      {/* reopen tab when collapsed */}
      {collapsed ? (
        <button onClick={() => setCollapsed(false)} aria-label="Open assistant"
          style={{
            position: "fixed", right: 14, top: "50%", transform: "translateY(-50%)", zIndex: 55,
            width: 40, height: 40, borderRadius: "50%", cursor: "pointer",
            background: C.bg, border: `1px solid ${C.line}`,
            color: C.cyan, fontSize: 18, fontFamily: "var(--font-mono)",
          }}>
          ◂
        </button>
      ) : (
        <aside style={{
          position: "fixed", right: 0, top: 0, bottom: 0, width: "min(392px, 100vw)", zIndex: 50,
          display: "flex", flexDirection: "column",
          background: C.bg, backdropFilter: "blur(22px)",
          borderLeft: `1px solid ${C.line}`,
          boxShadow: "-24px 0 60px rgba(0,0,0,0.45)",
          fontFamily: "var(--font-body)",
        }}>
          {/* header */}
          <header style={{
            display: "flex", alignItems: "center", gap: 10, padding: "12px 14px",
            borderBottom: `1px solid ${C.line}`,
          }}>
            <span style={{ width: 9, height: 9, borderRadius: "50%", background: a.orb === "speaking" ? C.gold : a.orb === "thinking" || a.orb === "listening" ? C.cyan : "rgba(255,255,255,0.25)", boxShadow: `0 0 8px ${a.orb === "speaking" ? C.gold : C.cyan}` }} />
            <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: "0.18em", color: C.text }}>APEX</div>
            <div style={{ fontSize: 9, fontFamily: "var(--font-mono)", color: C.dim, letterSpacing: "0.06em", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {engine} · {provider} · {model}
            </div>
            <span style={{ marginLeft: "auto", display: "flex", gap: 6, alignItems: "center" }}>
              {a.user && (
                <span title={a.user.email} style={{
                  width: 24, height: 24, borderRadius: "50%", overflow: "hidden",
                  border: `1px solid ${C.line}`,
                }}>
                  {a.user.picture ? <img src={a.user.picture} alt="" width={24} height={24} style={{ display: "block" }} /> : (
                    <span style={{ display: "flex", alignItems: "center", justifyContent: "center", width: 24, height: 24, background: `${C.cyan}22`, color: C.cyan, fontSize: 11 }}>{a.user.name?.[0] ?? "A"}</span>
                  )}
                </span>
              )}
              <button onClick={() => setCollapsed(true)} aria-label="Collapse"
                style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 16, lineHeight: 1 }}>
                ›
              </button>
            </span>
          </header>

          {/* tabs */}
          <nav style={{ display: "flex", borderBottom: `1px solid ${C.line}` }}>
            {(["chat", "hist", "settings", "memory"] as const).map((t) => (
              <button key={t} onClick={() => setTab(t)}
                style={{
                  flex: 1, padding: "9px 4px", fontSize: 9.5, letterSpacing: "0.14em", cursor: "pointer",
                  fontFamily: "var(--font-mono)", textTransform: "uppercase",
                  background: tab === t ? `${C.cyan}12` : "transparent",
                  color: tab === t ? C.cyan : C.dim,
                  border: "none", borderBottom: tab === t ? `2px solid ${C.cyan}` : "2px solid transparent",
                }}>
                {t === "hist" ? "history" : t}
              </button>
            ))}
          </nav>

          {/* body */}
          <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
            {tab === "chat" && (
              <>
                <MensajeList messages={a.messages} />
                <TimersPanel />
                <div style={{ padding: "10px 12px", borderTop: `1px solid ${C.line}`, display: "flex", flexDirection: "column", gap: 8 }}>
                  {/* skills */}
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
                    {a.skills.map((s) => {
                      const isActive = a.skill === s.name;
                      const isRouted = a.routedSkill === s.name;
                      return (
                        <div key={s.name} style={{ display: "flex", alignItems: "center", gap: 2 }}>
                          <button onClick={() => a.setSkill(s.name)} title={s.description}
                            style={{
                              padding: "3px 9px", borderRadius: 12, cursor: "pointer",
                              fontSize: 9, letterSpacing: "0.08em", fontFamily: "var(--font-mono)", textTransform: "uppercase",
                              background: isActive ? `${C.gold}1f` : isRouted ? `${C.cyan}1f` : "transparent",
                              border: `1px solid ${isActive ? C.lineGold : isRouted ? C.cyan : C.line}`,
                              color: isActive ? C.gold : isRouted ? C.cyan : C.dim,
                              boxShadow: isRouted ? `0 0 8px ${C.cyan}44` : undefined,
                              animation: isRouted ? "apex-pulse 1.2s infinite" : undefined,
                            }}>
                            {s.name}
                          </button>
                          {!s.builtin && (
                            <button
                              onClick={() => a.deleteSkill(s.name)}
                              title="Delete custom skill"
                              style={{
                                padding: "0 4px",
                                borderRadius: 8,
                                cursor: "pointer",
                                fontSize: 10,
                                lineHeight: 1,
                                background: "transparent",
                                border: "none",
                                color: C.dim,
                              }}
                              onMouseEnter={(e) => (e.currentTarget.style.color = "#ff4d4d")}
                              onMouseLeave={(e) => (e.currentTarget.style.color = C.dim)}
                            >
                              ×
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>

                  {/* voice status */}
                  <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 9.5, fontFamily: "var(--font-mono)", letterSpacing: "0.06em", color: C.dim }}>
                    <button onClick={() => a.setVoiceEnabled(!a.voiceEnabled)} aria-pressed={a.voiceEnabled}
                      style={{
                        padding: "3px 10px", borderRadius: 12, cursor: "pointer", letterSpacing: "0.1em",
                        background: a.voiceEnabled ? `${C.cyan}18` : "transparent",
                        border: `1px solid ${a.voiceEnabled ? C.cyan : C.line}`,
                        color: a.voiceEnabled ? C.cyan : C.dim,
                      }}>
                      {a.voiceEnabled ? "MIC ON" : "MIC OFF"}
                    </button>
                    {a.voiceEnabled && !a.voiceActive && <span className="apex-blink">PENDING PERMISSION…</span>}
                    {a.voiceError && <span style={{ color: C.gold }}>{a.voiceError}</span>}
                    {a.voiceEnabled && a.voiceActive && <span>{a.orb === "listening" ? "AWAITING COMMAND…" : `SAY "${wake}"…`}</span>}
                  </div>

                  {/* input */}
                  <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
                    <textarea
                      ref={inputRef}
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); }
                      }}
                      placeholder={a.user ? "Type & press Enter, or tap the core & speak…" : "Sign in with OpenAI to start"}
                      rows={1}
                      disabled={!a.user || a.busy}
                      style={{ ...inputBase, flex: 1, resize: "none", lineHeight: 1.5, maxHeight: 90 }}
                    />
                    <button onClick={() => void send()} disabled={!a.user || a.busy || !draft.trim()}
                      style={{
                        padding: "8px 14px", borderRadius: 8, cursor: "pointer", letterSpacing: "0.1em",
                        fontFamily: "var(--font-mono)", fontSize: 11,
                        background: a.busy ? "transparent" : `${C.cyan}16`,
                        border: `1px solid ${a.busy ? C.dim : C.cyan}`,
                        color: a.busy ? C.dim : C.cyan,
                      }}>
                      {a.busy ? "⟳" : "SEND"}
                    </button>
                  </div>
                </div>
              </>
            )}

            {tab === "hist" && (
              <div style={{ padding: 10, overflowY: "auto", flex: 1 }}>
                <button onClick={() => void a.newConversation()} disabled={a.busy}
                  style={{ width: "100%", marginBottom: 8, padding: 8, borderRadius: 8, cursor: "pointer",
                    background: `${C.gold}14`, border: `1px solid ${C.lineGold}`, color: C.gold,
                    fontFamily: "var(--font-mono)", fontSize: 10, letterSpacing: "0.14em" }}>
                  + NEW THREAD
                </button>
                {a.conversations.length === 0 && <Empty label="NO THREADS YET" />}
                <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                  {a.conversations.map((c) => (
                    <div key={c.id} style={{
                      display: "flex", alignItems: "center", gap: 6,
                      padding: "7px 9px", borderRadius: 8, cursor: c.id === a.activeId ? "default" : "pointer",
                      background: c.id === a.activeId ? `${C.cyan}10` : "transparent",
                      border: `1px solid ${c.id === a.activeId ? C.cyan : "rgba(255,255,255,0.05)"}`,
                    }} onClick={() => { if (c.id !== a.activeId) void a.openConversation(c.id); }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 11, color: C.text, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{c.title || "Untitled"}</div>
                        <div style={{ fontSize: 8.5, color: C.dim, fontFamily: "var(--font-mono)" }}>
                          {new Date(c.updated_at * 1000).toLocaleString()} · {c.messages} msgs
                        </div>
                      </div>
                      <button onClick={(e) => { e.stopPropagation(); void a.deleteConversation(c.id); }} aria-label="Delete"
                        style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 13 }}>×</button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {tab === "settings" && (
              <div style={{ padding: 12, overflowY: "auto", flex: 1, display: "flex", flexDirection: "column", gap: 11 }}>
                <Row label="Engine">
                  <select style={selectBase} value={engine} onChange={(e) => void a.updateSettings({ engine: e.target.value })}>
                    {engines.map((x) => <option key={x} value={x}>{x}</option>)}
                  </select>
                </Row>
                <Row label="Provider">
                  <select style={selectBase} value={provider} onChange={(e) => void a.updateSettings({ provider: e.target.value })}>
                    {providerNames.length === 0 ? <option value="">none</option> : providerNames.map((x) => <option key={x} value={x}>{x}{provAvail(x) ? "" : " (needs setup)"}</option>)}
                  </select>
                  {(() => {
                    const hint = needHint(provider);
                    if (!hint) return null;
                    return (
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span style={{ fontSize: 9, color: C.gold, lineHeight: 1.5, flex: 1 }}>{hint}</span>
                        {provider === "openai" && a.config?.oauth_configured && (
                          <button onClick={() => a.login()} style={{ ...inputBase, color: C.cyan, cursor: "pointer", flexShrink: 0, padding: "5px 10px" }}>SIGN IN</button>
                        )}
                      </div>
                    );
                  })()}
                </Row>
                <Row label="Model">
                  <input style={inputBase} list="apex-model-list" placeholder="auto (provider default)"
                    value={model === "<auto>" ? "" : model}
                    onChange={(e) => { const v = e.target.value; void a.updateSettings({ model: v || null }); }}
                    disabled={!!a.settings.model && a.settings.model !== "<auto>" && false}
                  />
                  <datalist id="apex-model-list">
                    {models.concat(a.config?.models ?? []).filter((m, i, arr) => m && arr.indexOf(m) === i).map((m) => <option key={m} value={m} />)}
                  </datalist>
                </Row>
                <Row label="Temperature">
                  <input type="range" min={0} max={2} step={0.05}
                    value={Number(a.settings.temperature ?? 0.7)}
                    onChange={(e) => void a.updateSettings({ temperature: Number(e.target.value) })} />
                  <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)" }}>{Number(a.settings.temperature ?? 0.7).toFixed(2)}</span>
                </Row>
                <Row label="Wake word">
                  <input style={inputBase} value={wake} onChange={(e) => void a.updateSettings({ wake_word: e.target.value })} />
                </Row>
                <Row label="Default response language">
                  <select style={selectBase} value={responseLanguage}
                    onChange={(e) => void a.updateSettings({ response_language: e.target.value })}>
                    <option value="en">English</option>
                    <option value="el">Greek</option>
                  </select>
                </Row>
                <Row label="Follow-up window (sec)">
                  <input style={inputBase} type="number" min={0} max={120}
                    value={Number(a.settings.follow_up_seconds ?? 30)}
                    onChange={(e) => void a.updateSettings({ follow_up_seconds: Number(e.target.value) })} />
                </Row>
                <Row label="TTS voice name">
                  <input style={inputBase} value={a.settings.voice ?? a.config?.voice ?? ""} placeholder="e.g. Google UK English Female"
                    onChange={(e) => void a.updateSettings({ voice: e.target.value })} />
                </Row>
                <Row label="Spoken replies">
                  <label style={{ fontSize: 11.5, color: C.text, display: "flex", alignItems: "center", gap: 8 }}>
                    <input type="checkbox" checked={!!a.settings.tts_enabled}
                      onChange={(e) => void a.updateSettings({ tts_enabled: e.target.checked })} />
                    Read responses aloud
                  </label>
                </Row>
                <Row label="Autonomous mode">
                  <label style={{ fontSize: 11.5, color: C.text, display: "flex", alignItems: "center", gap: 8 }}>
                    <input type="checkbox" checked={!!(a.settings.autonomous_mode ?? a.config?.autonomous_mode)}
                      onChange={(e) => void a.updateSettings({ autonomous_mode: e.target.checked })} />
                    Let APEX initiate, evolve and play
                  </label>
                </Row>
                <Row label="Humor level">
                  <input type="range" min={1} max={100}
                    value={Number(a.settings.humor_level ?? a.config?.humor_level ?? 30)}
                    onChange={(e) => void a.updateSettings({ humor_level: Number(e.target.value) })} />
                  <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)" }}>{Number(a.settings.humor_level ?? a.config?.humor_level ?? 30)}</span>
                </Row>
                <Row label="Sarcasm level">
                  <input type="range" min={1} max={100}
                    value={Number(a.settings.sarcasm_level ?? a.config?.sarcasm_level ?? 20)}
                    onChange={(e) => void a.updateSettings({ sarcasm_level: Number(e.target.value) })} />
                  <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)" }}>{Number(a.settings.sarcasm_level ?? a.config?.sarcasm_level ?? 20)}</span>
                </Row>
                <Row label="Daily voice budget">
                  <input type="range" min={0} max={100}
                    value={Number(a.settings.autonomous_voice_budget ?? a.config?.autonomous_voice_budget ?? 50)}
                    onChange={(e) => void a.updateSettings({ autonomous_voice_budget: Number(e.target.value) })} />
                  <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)" }}>{Number(a.settings.autonomous_voice_budget ?? a.config?.autonomous_voice_budget ?? 50)}%</span>
                </Row>
                <Row label="Account">
                  <div style={{ display: "flex", gap: 8 }}>
                    {a.user
                      ? <button style={{ ...inputBase, color: C.gold, cursor: "pointer", borderColor: C.lineGold }} onClick={() => void a.logout()}>SIGN OUT</button>
                      : <button style={{ ...inputBase, color: C.cyan, cursor: "pointer" }} onClick={() => a.login()}>SIGN IN WITH OPENAI</button>}
                  </div>
                </Row>
              </div>
            )}

            {tab === "memory" && (
              <div style={{ padding: 12, overflowY: "auto", flex: 1, display: "flex", flexDirection: "column", gap: 10 }}>
                <div style={{ display: "flex", gap: 6 }}>
                  <input style={inputBase} placeholder="Add a memory note…" value={memNote}
                    onChange={(e) => setMemNote(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && memNote.trim()) { void a.addMemory(memNote.trim()); setMemNote(""); } }} />
                  <button disabled={!memNote.trim()} onClick={() => { void a.addMemory(memNote.trim()); setMemNote(""); }}
                    style={{ ...inputBase, color: C.cyan, cursor: "pointer", flexShrink: 0 }}>SAVE</button>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  <input style={inputBase} placeholder="Search memories…" value={memSearch}
                    onChange={(e) => { setMemSearch(e.target.value); if (!e.target.value) setMemResults(null); }}
                    onKeyDown={async (e) => { if (e.key === "Enter" && memSearch.trim()) setMemResults(await a.searchMemory(memSearch)); }} />
                  <button disabled={!memSearch.trim()} onClick={async () => setMemResults(await a.searchMemory(memSearch))}
                    style={{ ...inputBase, color: C.gold, cursor: "pointer", flexShrink: 0 }}>SEARCH</button>
                </div>
                <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    accept=".txt,.md,.pdf,.json,.csv,.py,.js,.ts,.html,.yaml,.yml"
                    style={{ display: "none" }}
                    onChange={(e) => setMemFiles(e.target.files)}
                  />
                  <button onClick={() => fileInputRef.current?.click()}
                    style={{ ...inputBase, color: C.cyan, cursor: "pointer", flexShrink: 0 }}>
                    CHOOSE FILES
                  </button>
                  <span style={{ fontSize: 10, color: C.dim, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {memFiles ? `${memFiles.length} file${memFiles.length === 1 ? "" : "s"} selected` : "upload documents as memory chunks"}
                  </span>
                  <button disabled={!memFiles || memUploading} onClick={() => void uploadMemoryFiles()}
                    style={{ ...inputBase, color: C.gold, cursor: "pointer", flexShrink: 0 }}>
                    {memUploading ? "UPLOADING…" : "UPLOAD"}
                  </button>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)", letterSpacing: "0.1em" }}>{a.memory.length} ENTRIES</span>
                  {a.memory.length > 0 && (
                    <button onClick={() => void a.removeMemory([], true)} style={{ background: "none", border: "none", color: C.gold, cursor: "pointer", fontSize: 9, letterSpacing: "0.1em", fontFamily: "var(--font-mono)" }}>CLEAR ALL</button>
                  )}
                </div>
                {(memResults ?? a.memory).length === 0 && <Empty label="NOTHING STORED YET" />}
                <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                  {(memResults ?? a.memory).map((m) => (
                    <div key={m.id} style={{
                      padding: "7px 9px", borderRadius: 8,
                      background: "rgba(255,255,255,0.03)", border: `1px solid ${C.line}`,
                    }}>
                      <div style={{ fontSize: 10.5, color: C.text, lineHeight: 1.45 }}>{m.text}</div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
                        {m.score != null && <span style={{ fontSize: 8.5, color: C.gold, fontFamily: "var(--font-mono)" }}>{m.score.toFixed(3)}</span>}
                        <span style={{ fontSize: 8.5, color: C.dim, fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>{m.meta?.category ?? ""}</span>
                        <span style={{ marginLeft: "auto" }}>
                          <button onClick={() => void a.removeMemory([m.id])} aria-label="Delete" style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 12 }}>×</button>
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </aside>
      )}
    </>
  );
}
