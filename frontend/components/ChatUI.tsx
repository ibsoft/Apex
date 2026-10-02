"use client";

/* ChatUI - the assistant panel: chat stream, conversation history, skills,
   runtime settings (engine/provider/model/voice/wake word) and memory.

   Styled to sit on the APEX world: glassy dark, cyan + gold, monospace caps.
*/

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useApex, Message } from "./ApexProvider";
import { api } from "../lib/api";
import { CHAT_INPUT_EVENT, PANEL_EVENT, type PanelTabName } from "../lib/panelBridge";
import FileDownloads, { backendFileHref } from "./FileDownloads";
import AppsPanel from "./AppsPanel";
import TasksPanel from "./TasksPanel";
import VisioSettings from "./VisioSettings";
import SipSettings from "./SipSettings";
import SettingsCard from "./SettingsCard";

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

/* SOUL.md - the operator-authored persona. Unlike every other setting, a
   keystroke must not hit the database, so the textarea keeps a local draft and
   saves it once typing stops (and on blur, and on unmount). A draft in flight is
   never overwritten by the value coming back from the server. */
const SOUL_SAVE_DEBOUNCE_MS = 800;

function SoulEditor() {
  const a = useApex();
  const saved = String(a.settings.soul ?? a.config?.soul ?? "");
  const limit = Number(a.config?.soul_max_chars ?? 8000);
  const [draft, setDraft] = useState(saved);
  const [status, setStatus] = useState<"idle" | "saving" | "saved">("idle");
  const draftRef = useRef(saved);
  const dirtyRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const save = useCallback((text: string) => {
    dirtyRef.current = false;
    setStatus("saving");
    void a.updateSettings({ soul: text }).then(() => {
      setStatus((s) => (s === "saving" ? "saved" : s));
    }).catch(() => {
      dirtyRef.current = true; // let blur or the next edit retry
      setStatus("idle");
    });
  }, [a.updateSettings]);

  // Adopt a value that changed outside this box (first load, another tab).
  useEffect(() => {
    if (!dirtyRef.current) setDraft(saved);
  }, [saved]);

  // Never lose the tail of an in-flight draft when the settings tab unmounts.
  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (dirtyRef.current) save(draftRef.current);
    };
  }, [save]);

  const edit = (text: string) => {
    draftRef.current = text;
    dirtyRef.current = true;
    setDraft(text);
    setStatus("idle");
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      if (dirtyRef.current) save(draftRef.current);
    }, SOUL_SAVE_DEBOUNCE_MS);
  };

  const flush = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (dirtyRef.current) save(draftRef.current);
  };

  const over = Math.max(0, draft.length - limit);
  return (
    <>
      <textarea
        value={draft}
        onChange={(e) => edit(e.target.value)}
        onBlur={flush}
        rows={7}
        spellCheck={false}
        placeholder={"Speak in first person, stay dry and precise, push back when the request is a bad idea.\nExample:\n\nYou are calm, exact and slightly dry. You never pad an answer. You disagree when the user is wrong, and you say why in one sentence."}
        style={{ ...inputBase, width: "100%", resize: "vertical", minHeight: 130, lineHeight: 1.5 }}
      />
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, fontSize: 9, fontFamily: "var(--font-mono)", color: over ? C.gold : C.dim }}>
        <span>{over ? `over limit by ${over} — the end will be cut off` : "added to every system prompt · trusted, and sent with every request"}</span>
        <span>{status === "saving" ? "SAVING…" : status === "saved" ? "SAVED" : `${draft.length}/${limit}`}</span>
      </div>
    </>
  );
}

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
    <div ref={ref} className="apex-scroll" style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: 12, padding: "12px 14px" }}>
      <div aria-hidden style={{ height: 4 }} />
      {messages.map((m) => <MessageBubble key={m.id} msg={m} />)}
      <div aria-hidden style={{ height: 6 }} />
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

/* The command layer speaks the tab names shown in the UI; this component's own
   state uses short keys. Keeping the mapping in one place means adding a tab
   later cannot silently make one spelling work and the other not. */
/* The avatar menu. Sign out and Lock live here because they are the two actions
   an operator reaches for when someone else is at the machine, and burying
   logout in settings means the previous user's session stays open on a shared
   desktop. Rendered as a real menu so Escape and outside clicks close it. */
function ProfileMenu({
  name, email, onClose, onLock, onLogout,
}: {
  name: string;
  email: string;
  onClose: () => void;
  onLock: () => void;
  onLogout: () => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const away = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) onClose();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [onClose]);

  const item: React.CSSProperties = {
    display: "block", width: "100%", textAlign: "left", padding: "7px 10px",
    background: "none", border: "none", cursor: "pointer",
    fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase",
    fontFamily: "var(--font-mono)", color: C.text,
  };

  return (
    <div
      ref={ref}
      role="menu"
      style={{
        position: "absolute", right: 14, top: 46, zIndex: 60, minWidth: 190,
        background: C.bg, border: `1px solid ${C.line}`,
        boxShadow: "0 18px 40px rgba(0,0,0,0.55)", padding: 4,
      }}
    >
      <div style={{ padding: "7px 10px 8px", borderBottom: `1px solid ${C.line}`, marginBottom: 3 }}>
        <div style={{ fontSize: 11, color: C.text, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</div>
        <div style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{email}</div>
      </div>
      <button role="menuitem" onClick={onLock} style={item}>Lock screen</button>
      <button
        role="menuitem"
        onClick={onLogout}
        style={{ ...item, color: C.gold, borderTop: `1px solid ${C.line}`, marginTop: 3 }}
      >
        Sign out
      </button>
    </div>
  );
}

type Tab = "chat" | "hist" | "settings" | "memory" | "apps" | "tasks";

const TAB_FROM_COMMAND: Record<PanelTabName, Tab> = {
  chat: "chat",
  history: "hist",
  settings: "settings",
  memory: "memory",
  apps: "apps",
  tasks: "tasks",
};

export default function ChatUI() {
  const a = useApex();
  const [tab, setTab] = useState<Tab>("chat");
  const [menuOpen, setMenuOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [collapsed, setCollapsed] = useState(a.chatCollapsed);
  useEffect(() => {
    a.setChatCollapsed(collapsed);
  }, [collapsed, a.setChatCollapsed]);
  const [histOpen, toggleHist] = useState(false);
  const [memSearch, setMemSearch] = useState("");
  const unreadTasks = a.tasks.filter((t) => t.unread).length;
  const [memNote, setMemNote] = useState("");
  const [memResults, setMemResults] = useState<null | any[]>(null);
  const [memFiles, setMemFiles] = useState<FileList | null>(null);
  const [memUploading, setMemUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [sendDisabled, setSendDisabled] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const hasPreview = a.windows.length > 0;

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

  /* Panel and chat-input command consumers.

     A window listener, not an effect on shared state: the command arrives
     whenever it arrives, and `preventDefault` is what marks it accepted so the
     bridge stops retrying. Reading the panel state through functional updates
     keeps a close followed by a "go to chat" in the same tick correct.
   */
  useEffect(() => {
    const onPanel = (event: Event) => {
      const detail = (event as CustomEvent).detail as {
        action: "open" | "close" | "toggle";
        tab?: PanelTabName;
        respond: (message: string) => void;
      };
      if (detail.action === "close") setCollapsed(true);
      else setCollapsed(false);
      if (detail.tab) setTab(TAB_FROM_COMMAND[detail.tab] ?? "chat");
      event.preventDefault();
      detail.respond(
        detail.tab
          ? `Opening ${detail.tab}.`
          : detail.action === "close" ? "Closing the panel." : "Opening the panel.",
      );
    };
    window.addEventListener(PANEL_EVENT, onPanel);
    return () => window.removeEventListener(PANEL_EVENT, onPanel);
  }, []);

  const draftRef = useRef(draft);
  const busyRef = useRef(a.busy);
  useEffect(() => { draftRef.current = draft; }, [draft]);
  useEffect(() => { busyRef.current = a.busy; }, [a.busy]);

  useEffect(() => {
    const onChatInput = (event: Event) => {
      const detail = (event as CustomEvent).detail as {
        action: "write" | "send";
        text?: string;
        respond: (message: string) => void;
      };
      // Both commands imply the chat tab: writing anywhere else is not
      // something the user can see, and neither is the box they are filling.
      setCollapsed(false);
      setTab("chat");

      if (detail.action === "write") {
        const text = (detail.text ?? "").trim();
        if (!text) {
          detail.respond("Nothing to write. Say it like: write in chat, then the words.");
          return;
        }
        setDraft(text);
        event.preventDefault();
        // Focus only after the textarea exists and the panel is open.
        requestAnimationFrame(() => inputRef.current?.focus({ preventScroll: true }));
        detail.respond("Written in the chat box.");
        return;
      }

      // "send chat" delivers the pending text, or whatever is already typed.
      const body = (detail.text || draftRef.current).trim();
      if (!body) {
        detail.respond("The chat box is empty.");
        return;
      }
      if (busyRef.current) {
        // Not accepted: the caller is told to try again rather than the
        // message being silently dropped.
        detail.respond("Still working on the previous message. Try again in a moment.");
        return;
      }
      event.preventDefault();
      setDraft("");
      void send(body);
      detail.respond("Sending.");
    };
    window.addEventListener(CHAT_INPUT_EVENT, onChatInput);
    return () => window.removeEventListener(CHAT_INPUT_EVENT, onChatInput);
  }, [send]);

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
  const thinkHardModel = a.settings.think_hard_model ?? a.config?.think_hard_model ?? "";
  const thinkHardEnabled = !!(a.settings.think_hard_model_enabled ?? a.config?.think_hard_model_enabled);
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
                <>
                  <button
                    onClick={() => setMenuOpen((v) => !v)}
                    aria-haspopup="menu"
                    aria-expanded={menuOpen}
                    title={a.user.email}
                    style={{
                      width: 24, height: 24, borderRadius: "50%", overflow: "hidden", padding: 0,
                      cursor: "pointer", border: `1px solid ${menuOpen ? C.cyan : C.line}`,
                      background: "none",
                    }}
                  >
                    {a.user.picture ? <img src={a.user.picture} alt="" width={24} height={24} style={{ display: "block" }} /> : (
                      <span style={{ display: "flex", alignItems: "center", justifyContent: "center", width: 24, height: 24, background: `${C.cyan}22`, color: C.cyan, fontSize: 11 }}>{a.user.name?.[0] ?? "A"}</span>
                    )}
                  </button>
                  {menuOpen && (
                    <ProfileMenu
                      name={a.user.name ?? a.user.email}
                      email={a.user.email}
                      onClose={() => setMenuOpen(false)}
                      onLock={() => { setMenuOpen(false); void a.lock(); }}
                      onLogout={() => { setMenuOpen(false); void a.logout(); }}
                    />
                  )}
                </>
              )}
              <button onClick={() => setCollapsed(true)} aria-label="Collapse"
                style={{ background: "none", border: "none", color: C.dim, cursor: "pointer", fontSize: 16, lineHeight: 1 }}>
                ›
              </button>
            </span>
          </header>

          {/* tabs */}
          <nav style={{ display: "flex", borderBottom: `1px solid ${C.line}` }}>
            {(["chat", "hist", "tasks", "settings", "memory", "apps"] as const).map((t) => (
              <button key={t} onClick={() => setTab(t)}
                style={{
                  flex: 1, padding: "9px 4px", fontSize: 9.5, letterSpacing: "0.14em", cursor: "pointer",
                  fontFamily: "var(--font-mono)", textTransform: "uppercase",
                  background: tab === t ? `${C.cyan}12` : "transparent",
                  color: tab === t ? C.cyan : C.dim,
                  border: "none", borderBottom: tab === t ? `2px solid ${C.cyan}` : "2px solid transparent",
                }}>
                {t === "hist" ? "history" : t}
                {/* An unread count on the tab itself: a run that finished at
                    03:00 has to be visible before the panel is even opened. */}
                {t === "tasks" && unreadTasks > 0 && (
                  <span style={{ marginLeft: 4, color: C.gold }}>({unreadTasks})</span>
                )}
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
                    <span style={{ marginLeft: "auto" }} />
                  </div>

                  {/* input */}
                  <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
                    <textarea
                      ref={inputRef}
                      className="apex-scroll-slim"
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
              <div className="apex-scroll" style={{ padding: 10, overflowY: "auto", flex: 1 }}>
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
              <div className="apex-scroll" style={{ padding: 12, overflowY: "auto", flex: 1, display: "flex", flexDirection: "column", gap: 11 }}>
                <SettingsCard title="Model & engine">
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
                </SettingsCard>
                <SettingsCard title="Think hard">
                  <Row label="Think hard model">
                    <input style={inputBase} list="apex-think-hard-model-list"
                      placeholder="off (uses the main model)"
                      value={thinkHardModel === "<auto>" ? "" : thinkHardModel}
                      onChange={(e) => { const v = e.target.value; void a.updateSettings({ think_hard_model: v || null }); }}
                    />
                    <datalist id="apex-think-hard-model-list">
                      {models.concat(a.config?.models ?? []).filter((m, i, arr) => m && arr.indexOf(m) === i).map((m) => <option key={m} value={m} />)}
                    </datalist>
                  </Row>
                  <Row label="Think hard">
                    <label style={{ fontSize: 11.5, color: C.text, display: "flex", alignItems: "center", gap: 8 }}>
                      <input type="checkbox" checked={!!thinkHardEnabled}
                        disabled={!thinkHardModel}
                        onChange={(e) => void a.updateSettings({ think_hard_model_enabled: e.target.checked })} />
                      Answer “think hard” turns with that model
                    </label>
                    <span style={{ fontSize: 9, color: C.dim, lineHeight: 1.5 }}>
                      {thinkHardModel
                        ? `One turn only: “think hard: …” (text or voice) is answered by ${thinkHardModel}.`
                        : "Set a model above (or THINK_HARD_MODEL) to enable it."}
                    </span>
                  </Row>
                </SettingsCard>
                <VisioSettings />
                <SipSettings />
                <SettingsCard title="Voice & language">
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
                </SettingsCard>
                <SettingsCard title="Personality">
                  <Row label="SOUL.md · personality">
                    <SoulEditor />
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
                </SettingsCard>
                <SettingsCard title="Autonomous behavior">
                  <Row label="Autonomous mode">
                    <label style={{ fontSize: 11.5, color: C.text, display: "flex", alignItems: "center", gap: 8 }}>
                      <input type="checkbox" checked={!!(a.settings.autonomous_mode ?? a.config?.autonomous_mode)}
                        onChange={(e) => void a.updateSettings({ autonomous_mode: e.target.checked })} />
                      Let APEX initiate, evolve and play
                    </label>
                  </Row>
                  <Row label="Daily voice budget">
                    <input type="range" min={0} max={100}
                      value={Number(a.settings.autonomous_voice_budget ?? a.config?.autonomous_voice_budget ?? 50)}
                      onChange={(e) => void a.updateSettings({ autonomous_voice_budget: Number(e.target.value) })} />
                    <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)" }}>{Number(a.settings.autonomous_voice_budget ?? a.config?.autonomous_voice_budget ?? 50)}%</span>
                  </Row>
                </SettingsCard>
              </div>
            )}

            {tab === "memory" && (
              <div className="apex-scroll" style={{ padding: 12, overflowY: "auto", flex: 1, display: "flex", flexDirection: "column", gap: 10 }}>
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

            {tab === "apps" && <AppsPanel />}

            {tab === "tasks" && <TasksPanel />}
          </div>
        </aside>
      )}
    </>
  );
}
