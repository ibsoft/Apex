"use client";

/* ApexProvider - single source of truth for the assistant UI.

   Owns: auth/config, settings, conversations + message streaming, skills,
   memory, the orb state machine and the always-on voice engine.

   Orb mapping (voice engine phase → orb):
     standby  -> idle
     awake    -> listening  (wake word heard, awaiting command)
     thinking -> thinking
     speaking -> speaking
*/

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  api,
  ApiError,
  Conversation,
  MemoryEntry,
  Skill,
  User,
  ChatEvent,
} from "../lib/api";
import { useVoiceEngine, VoicePhase } from "../lib/voice";
import { speechText } from "./speechText";

export type OrbState = "idle" | "listening" | "thinking" | "speaking";

export type Message = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  meta?: {
    tools?: { name: string; args?: any; output?: string; running?: boolean }[];
    voice?: boolean;
    usage?: any;
    error?: boolean;
  };
  streaming?: boolean;
};

export type Settings = Record<string, any>;

type ApexContextType = {
  loading: boolean;
  ready: boolean;
  user: User | null;
  config: { engine: string; provider: string; providers: any; engines: string[]; models: string[]; memory_enabled: boolean; embedding: string | null; wake_word: string; follow_up_seconds: number; voice: string; response_language: string; oauth_configured: boolean; logged_in: boolean } | null;
  settings: Settings;
  conversations: Conversation[];
  activeId: string | null;
  messages: Message[];
  skill: string;
  skills: Skill[];
  memory: MemoryEntry[];
  busy: boolean;
  orb: OrbState;
  voiceActive: boolean;
  voiceEnabled: boolean;
  voiceError: string | null;
  error: string | null;
  preview: {
    title: string;
    items: { url: string; title: string; kind: "image" | "document" }[];
    index: number;
  } | null;
  previewMaximized: boolean;
  chatCollapsed: boolean;
  /* actions */
  refresh: () => Promise<void>;
  login: () => void;
  logout: () => Promise<void>;
  newConversation: () => Promise<void>;
  openConversation: (id: string) => Promise<void>;
  deleteConversation: (id: string) => Promise<void>;
  sendMessage: (text: string, opts?: { voice?: boolean; skill?: string }) => Promise<void>;
  setSkill: (name: string) => void;
  updateSettings: (patch: Settings) => Promise<void>;
  setVoiceEnabled: (on: boolean) => void;
  addMemory: (text: string, category?: string) => Promise<void>;
  removeMemory: (ids: string[], all?: boolean) => Promise<void>;
  searchMemory: (q: string) => Promise<MemoryEntry[]>;
  refreshMemory: () => Promise<void>;
  clearError: () => void;
  openPreview: (items: { url: string; title: string; kind: "image" | "document" }[], title?: string, startIndex?: number) => void;
  closePreview: () => void;
  setChatCollapsed: (collapsed: boolean) => void;
  togglePreviewMaximized: () => void;
  nextPreview: () => void;
  previousPreview: () => void;
};

const ApexContext = createContext<ApexContextType | null>(null);
export const useApex = () => {
  const ctx = useContext(ApexContext);
  if (!ctx) throw new Error("useApex must be used inside <ApexProvider>");
  return ctx;
};

let msgSeq = 0;
const mkMsg = (role: Message["role"], content: string, extra: Partial<Message> = {}): Message => ({
  id: `m${Date.now().toString(36)}_${msgSeq++}`,
  role,
  content,
  ...extra,
});

export function ApexProvider({ children }: { children: React.ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [user, setUser] = useState<User | null>(null);
  const [cfg, setCfg] = useState<ApexContextType["config"] | null>(null);
  const [settings, setSettings] = useState<Settings>({});
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [byConv, setByConv] = useState<Record<string, Message[]>>({});
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skill, setSkill] = useState("general");
  const [memory, setMemory] = useState<MemoryEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [orb, setOrb] = useState<OrbState>("idle");
  const [voiceEnabled, setVoiceEnabledState] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<{
    title: string;
    items: { url: string; title: string; kind: "image" | "document" }[];
    index: number;
  } | null>(null);
  const [previewMaximized, setPreviewMaximized] = useState(false);
  const [chatCollapsed, setChatCollapsed] = useState(false);

  const activeIdRef = useRef(activeId);
  activeIdRef.current = activeId;
  const byConvRef = useRef(byConv);
  byConvRef.current = byConv;
  const skillRef = useRef(skill);
  skillRef.current = skill;
  const skillsRef = useRef(skills);
  skillsRef.current = skills;
  const cfgRef = useRef(cfg);
  cfgRef.current = cfg;
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const userRef = useRef(user);
  userRef.current = user;
  const previewRef = useRef(preview);
  previewRef.current = preview;
  const previewMaximizedRef = useRef(previewMaximized);
  previewMaximizedRef.current = previewMaximized;

  const messages = activeId ? byConv[activeId] ?? [] : [];

  /* ---------- data loading ---------- */

  const refreshConfig = useCallback(async () => {
    await api.config().then((c) => {
      setCfg(c);
      if (c.skills?.length) setSkills(c.skills);
      setSkill((s) => {
        const ok = c.skills?.some((k: Skill) => k.name === s);
        return ok ? s : (c.skills?.[0]?.name ?? "general");
      });
    }).catch(() => {});
  }, []);

  const refreshConvos = useCallback(async (knownUser = userRef.current) => {
    if (!knownUser) return;
    const list = await api.conversations.list().catch(() => []);
    setConversations(list);
  }, []);

  const refreshMemory = useCallback(async (knownUser = userRef.current) => {
    if (!knownUser) return;
    const m = await api.memory.list().catch(() => ({ entries: [] as MemoryEntry[] }));
    setMemory(m.entries ?? []);
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      await refreshConfig();
      const me = await api.me().catch(() => null);
      if (me?.ok && me.user) {
        setUser(me.user);
        setSettings(me.settings ?? {});
      } else {
        setUser(null);
      }
      // The shell should not be blocked by optional history or memory data.
      setLoading(false);
      if (me?.ok && me.user) {
        void Promise.all([refreshConvos(me.user), refreshMemory(me.user)]);
      }
    } catch {
      setLoading(false);
    }
  }, [refreshConfig, refreshConvos, refreshMemory]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  /* ---------- auth ---------- */

  const login = useCallback(() => api.login(), []);
  const logout = useCallback(async () => {
    await api.logout().catch(() => {});
    setUser(null);
    setConversations([]);
    setByConv({});
    setActiveId(null);
    setMemory([]);
    if (voice) voice.cancelSpeech();
    await refresh();
  }, []);

  /* ---------- conversations ---------- */

  const newConversation = useCallback(async () => {
    const conv = await api.conversations.create({ skill: skillRef.current });
    setConversations((l) => [conv, ...l]);
    setActiveId(conv.id);
    setByConv((m) => ({ ...m, [conv.id]: [] }));
  }, []);

  const openConversation = useCallback(
    async (id: string) => {
      if (id === activeIdRef.current) return;
      setActiveId(id);
      if (byConvRef.current[id] === undefined) {
        const { messages: msgs } = await api.conversations.messages(id).catch(() => ({ messages: [] }));
        setByConv((m) => ({
          ...m,
          [id]: msgs.map((msg: any) =>
            mkMsg(msg.role === "user" ? "user" : "assistant", msg.content || "", { meta: msg.meta ?? {} }),
          ),
        }));
      }
    },
    [],
  );

  const deleteConversation = useCallback(async (id: string) => {
    await api.conversations.remove(id).catch(() => {});
    setConversations((l) => l.filter((c) => c.id !== id));
    setByConv((m) => {
      const n = { ...m };
      delete n[id];
      return n;
    });
    if (activeIdRef.current === id) {
      const first = conversations.find((c) => c.id !== id);
      setActiveId(first?.id ?? null);
    }
  }, [conversations]);

  /* ---------- preview window ---------- */

  const openPreview = useCallback((items: { url: string; title: string; kind: "image" | "document" }[], title = "Preview", startIndex = 0) => {
    if (!items.length) return;
    setPreview({ title, items, index: Math.max(0, Math.min(startIndex, items.length - 1)) });
  }, []);

  const closePreview = useCallback(() => {
    setPreview(null);
    setPreviewMaximized(false);
  }, []);

  const togglePreviewMaximized = useCallback(() => {
    setPreviewMaximized((m) => !m);
  }, []);

  const nextPreview = useCallback(() => {
    setPreview((p) => (p ? { ...p, index: (p.index + 1) % p.items.length } : p));
  }, []);

  const previousPreview = useCallback(() => {
    setPreview((p) => (p ? { ...p, index: (p.index - 1 + p.items.length) % p.items.length } : p));
  }, []);

  // Auto-open/update preview for voice assistant messages that contain images
  // or backend file/document links. Only pop up when the chat panel is
  // collapsed so the inline chat view is not duplicated.
  useEffect(() => {
    if (!chatCollapsed) return;
    const last = messages[messages.length - 1];
    if (!last || last.role !== "assistant" || last.streaming || !last.meta?.voice || !last.content) return;
    const items = collectPreviewableItems(last.content);
    if (items.length) {
      setPreview({ title: "Voice Preview", items, index: 0 });
    }
  }, [messages, chatCollapsed]);

  /* ---------- voice + orb ---------- */

  const onVoicePhase = useCallback((p: VoicePhase) => {
    setOrb(p === "standby" ? "idle" : p === "awake" ? "listening" : p);
  }, []);

  const sendRef = useRef<any>(null);
  const speakRef = useRef<(text: string) => void>(() => {});

  const CLOSE_PREVIEW_RE = /^(close|hide|dismiss|shut)\b.*(preview|it|window|image|document|that)?/i;
  const MAXIMIZE_PREVIEW_RE = /^(maxim(?:ize|ise)|full[-\s]?screen|enlarge|expand)\b/i;
  const NORMALIZE_PREVIEW_RE = /^(normali(?:ze|ise)|minimize|shrink|restore|small(er)?\s+window)\b/i;
  const NEXT_PREVIEW_RE = /^(next|forward|next\s+(image|one|photo|picture|page))\b/i;
  const PREV_PREVIEW_RE = /^(previous|back|last|prev|earlier\s+(image|one|photo|picture|page))\b/i;

  const voice = useVoiceEngine({
    enabled: voiceEnabled,
    wakeWord: settings.wake_word ?? cfg?.wake_word ?? "apex",
    followUpSeconds: Number(settings.follow_up_seconds ?? cfg?.follow_up_seconds ?? 30),
    voiceName: settings.voice ?? cfg?.voice ?? "",
    responseLanguage: settings.response_language ?? cfg?.response_language ?? "en",
    onPhase: onVoicePhase,
    onCommand: (text: string) => {
      if (!userRef.current) return;
      const trimmed = text.trim();
      if (previewRef.current && CLOSE_PREVIEW_RE.test(trimmed)) {
        closePreview();
        speakRef.current("Preview closed");
        return;
      }
      if (previewRef.current && MAXIMIZE_PREVIEW_RE.test(trimmed)) {
        if (!previewMaximizedRef.current) togglePreviewMaximized();
        speakRef.current(previewMaximizedRef.current ? "Already maximized" : "Preview maximized");
        return;
      }
      if (previewRef.current && NORMALIZE_PREVIEW_RE.test(trimmed)) {
        if (previewMaximizedRef.current) togglePreviewMaximized();
        speakRef.current(previewMaximizedRef.current ? "Preview normalized" : "Already normalized");
        return;
      }
      if (previewRef.current && (previewRef.current?.items.length ?? 0) > 1 && NEXT_PREVIEW_RE.test(trimmed)) {
        nextPreview();
        const item = previewRef.current?.items[previewRef.current?.index ?? 0];
        speakRef.current(item ? `Showing ${item.title}` : "Next");
        return;
      }
      if (previewRef.current && (previewRef.current?.items.length ?? 0) > 1 && PREV_PREVIEW_RE.test(trimmed)) {
        previousPreview();
        const item = previewRef.current?.items[previewRef.current?.index ?? 0];
        speakRef.current(item ? `Showing ${item.title}` : "Previous");
        return;
      }
      const switchCmd = parseSkillSwitch(trimmed, skillsRef.current);
      if (switchCmd) {
        setSkill(switchCmd.skill);
        if (switchCmd.rest) {
          void sendRef.current(switchCmd.rest, { voice: true, skill: switchCmd.skill });
        } else {
          speakRef.current(`Switched to ${switchCmd.skill} skill`);
        }
        return;
      }
      void sendRef.current(text, { voice: true, skill: skillRef.current });
    },
  });

  speakRef.current = voice.speak;

  const setVoiceEnabled = useCallback((on: boolean) => {
    setVoiceEnabledState(on);
    if (!on) {
      setOrb((o) => (o === "speaking" || o === "listening" ? "idle" : o));
    }
  }, []);

  /* ---------- chat ---------- */

  const sendMessage = useCallback(
    async (text: string, opts: { voice?: boolean; skill?: string } = {}) => {
      let clean = text.trim();
      if (!clean || busy) return;

      // Handle explicit skill-switching commands in typed input so the UI
      // highlights the new skill immediately.
      const switchCmd = parseSkillSwitch(clean, skillsRef.current);
      if (switchCmd) {
        setSkill(switchCmd.skill);
        if (!switchCmd.rest) {
          setBusy(false);
          setOrb("idle");
          return;
        }
        clean = switchCmd.rest;
        opts = { ...opts, skill: switchCmd.skill };
      }

      const fallbackId = activeIdRef.current;

      try {
        setBusy(true);
        setError(null);
        setOrb("thinking");

        let convId = fallbackId;
        if (!convId) {
          const conv = await api.conversations.create({ skill: opts.skill ?? skillRef.current });
          setConversations((l) => [conv, ...l]);
          setActiveId(conv.id);
          setByConv((m) => ({ ...m, [conv.id]: [] }));
          convId = conv.id;
        }

        // append the user message optimistically
        setByConv((m) => ({
          ...m,
          [convId]: [...(m[convId] ?? []), mkMsg("user", clean, { meta: { voice: !!opts.voice } })],
        }));

        const asstId = `asst_${Date.now().toString(36)}_${msgSeq++}`;
        const pushAssistant = (patch: Partial<Message>) =>
          setByConv((m) => {
            const list = [...(m[convId] ?? [])];
            const existing = list.find((msg) => msg.id === asstId);
            const next: Message =
              existing ? { ...existing, ...patch } : { ...mkMsg("assistant", "", {}), id: asstId, ...patch };
            if (existing) {
              const i = list.findIndex((msg) => msg.id === asstId);
              list[i] = next;
            } else {
              list.push(next);
            }
            return { ...m, [convId]: list };
          });

        pushAssistant({ streaming: true, content: "", meta: { voice: !!opts.voice, tools: [] } });

        let spoken = "";
        let streamError = "";
        let streamedTools: NonNullable<NonNullable<Message["meta"]>["tools"]> = [];
        const payload: any = {
          message: clean,
          conversation_id: convId,
          skill: opts.skill ?? skillRef.current,
          voice_mode: !!opts.voice,
        };
        const model = settingsRef.current.model;
        if (model && stripSystemModel(model)) payload.model = model;

        await api.chat(payload, (ev: ChatEvent) => {
          if (ev.type === "text_delta") {
            spoken += ev.content ?? "";
            pushAssistant({ streaming: true, content: spoken });
          } else if (ev.type === "tool_call") {
            streamedTools = [...streamedTools, { name: ev.name, args: ev.arguments, running: true }];
            pushAssistant({
              streaming: true,
              content: spoken,
              meta: {
                voice: !!opts.voice,
                tools: streamedTools,
              },
            });
          } else if (ev.type === "tool_result") {
            const pendingIndex = streamedTools.findIndex((t) => t.name === ev.name && t.running);
            streamedTools = pendingIndex >= 0
              ? streamedTools.map((t, i) => i === pendingIndex ? { ...t, output: ev.output, running: false } : t)
              : [...streamedTools, { name: ev.name, output: ev.output, running: false }];
            pushAssistant({ streaming: true, content: spoken, meta: { voice: !!opts.voice, tools: streamedTools } });
          } else if (ev.type === "memory") {
            void refreshMemory();
          } else if (ev.type === "error") {
            streamError = ev.message;
            setError(ev.message);
            pushAssistant({
              streaming: false,
              content: spoken || streamError,
              meta: { voice: !!opts.voice, tools: streamedTools, error: true },
            });
          } else if (ev.type === "done") {
            pushAssistant({
              streaming: false,
              content: spoken,
              meta: { voice: !!opts.voice,
                tools: streamedTools, usage: ev.usage },
            });
          } else if (ev.type === "end") {
            pushAssistant({ streaming: false, content: spoken });
            void refreshMemory();
          }
        });

        pushAssistant({ streaming: false, content: spoken || streamError });
        setByConv((m) => {
          const list = (m[convId] ?? []).map((msg) =>
            msg.id === asstId && msg.streaming ? { ...msg, streaming: false, content: msg.content } : msg,
          );
          return { ...m, [convId]: list };
        });

        if (opts.voice && spoken.trim() && settingsRef.current.tts_enabled !== false) {
          voice.speak(speechText(spoken));
        } else if (!opts.voice) {
          setOrb("idle");
        }

        void refreshConvos();
      } catch (err: any) {
        if (err instanceof ApiError && err.status === 401) {
          setUser(null);
        } else {
          setError(err?.message ?? String(err));
        }
        setOrb("idle");
      } finally {
        setBusy(false);
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [busy, voice],
  );

  sendRef.current = sendMessage;

  /* ---------- settings ---------- */

  const updateSettings = useCallback(async (patch: Settings) => {
    const res = await api.settings.set(patch).catch(() => null);
    if (res?.settings) {
      setSettings(res.settings);
      const s = res.settings;
      if (s.engine || s.provider || s.model) await refreshConfig();
    }
  }, [refreshConfig]);

  /* ---------- memory ---------- */

  const addMemory = useCallback(async (text: string, category?: string) => {
    const ids = category ? undefined : undefined;
    void ids;
    const m = await api.memory.add(text, category).catch(() => null);
    if (m?.id) void refreshMemory();
  }, [refreshMemory]);

  const removeMemory = useCallback(
    async (ids: string[], all?: boolean) => {
      await api.memory.remove(ids, all).catch(() => {});
      if (all) setMemory([]);
      else setMemory((m) => m.filter((e) => !ids.includes(e.id)));
    },
    [],
  );

  const searchMemory = useCallback(async (q: string) => {
    const res = await api.memory.search(q).catch(() => [] as MemoryEntry[]);
    return res ?? [];
  }, []);

  const clearError = useCallback(() => setError(null), []);

  // voice disabled when not logged in
  useEffect(() => {
    setVoiceEnabledState((on) => (user ? on : false));
  }, [user]);

  const value: ApexContextType = useMemo(
    () => ({
      loading,
      ready: !!user,
      user,
      config: cfg,
      settings,
      conversations,
      activeId,
      messages,
      skill,
      skills,
      memory,
      busy,
      orb,
      voiceActive: voice.active,
      voiceEnabled,
      voiceError: voice.error,
      error,
      preview,
      previewMaximized,
      chatCollapsed,
      refresh,
      login,
      logout,
      newConversation,
      openConversation,
      deleteConversation,
      sendMessage,
      setSkill,
      updateSettings,
      setVoiceEnabled,
      addMemory,
      removeMemory,
      searchMemory,
      refreshMemory,
      clearError,
      openPreview,
      closePreview,
      setChatCollapsed,
      togglePreviewMaximized,
      nextPreview,
      previousPreview,
    }),
    [loading, user, cfg, settings, conversations, activeId, messages, skill, skills, memory, busy, orb, voice.active, voiceEnabled, error,
     preview, previewMaximized, chatCollapsed, refresh, login, logout, newConversation, openConversation, deleteConversation, sendMessage, updateSettings, setVoiceEnabled,
     addMemory, removeMemory, searchMemory, refreshMemory, clearError, openPreview, closePreview, setChatCollapsed, togglePreviewMaximized, nextPreview, previousPreview],
  );

  return <ApexContext.Provider value={value}>{children}</ApexContext.Provider>;
}

/* ---------- preview helpers ---------- */

const IMAGE_EXT_RE = /\.(jpg|jpeg|png|gif|webp|svg|bmp)(\?.*)?$/i;
const PREVIEWABLE_URL_RE = /\[([^\]]*)\]\((https?:\/\/[^\s)]+|\/api\/(?:files|editor)\/download\/[A-Za-z0-9_.\-]+|\/api\/obsidian\/file\?path=[^\s)]+)\)|(https?:\/\/[^\s<>"{}|\\^`[\]]+)|(\/api\/(?:files|editor)\/download\/[A-Za-z0-9_.\-]+)|(\/api\/obsidian\/file\?path=[^\s<>"{}|\\^`[\]]+)/g;

function isPreviewImage(url: string): boolean {
  return IMAGE_EXT_RE.test(url);
}

function parseSkillSwitch(text: string, skills: Skill[]): { skill: string; rest: string } | null {
  const prefixRe = /^(?:use|switch\s+to|activate|enable)\s+(?:the\s+)?(?:skill\s+)?/i;
  const prefixMatch = text.match(prefixRe);
  if (!prefixMatch) return null;
  const afterPrefix = text.slice(prefixMatch[0].length);
  // Try longest skill name first so multi-word names win over single-word prefixes.
  const sorted = [...skills].sort((a, b) => b.name.length - a.name.length);
  const lowerAfter = afterPrefix.toLowerCase();
  for (const skill of sorted) {
    const name = skill.name.toLowerCase();
    if (lowerAfter.startsWith(name)) {
      const rest = afterPrefix.slice(skill.name.length).replace(/^[,.\s]+/, "").trim();
      return { skill: skill.name, rest };
    }
  }
  return null;
}

function titleFromUrl(url: string): string {
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

function collectPreviewableItems(content: string): { url: string; title: string; kind: "image" | "document" }[] {
  const seen = new Set<string>();
  const items: { url: string; title: string; kind: "image" | "document" }[] = [];
  PREVIEWABLE_URL_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = PREVIEWABLE_URL_RE.exec(content)) !== null) {
    const label = m[1];
    const url = m[2] || m[3] || m[4] || m[5];
    if (!url || seen.has(url)) continue;
    seen.add(url);
    const isImage = isPreviewImage(url);
    if (isImage || /\/api\/(?:files|editor)\/download\//.test(url) || /\/api\/obsidian\/file\?path=/.test(url)) {
      const title = (label && label.trim()) || titleFromUrl(url);
      items.push({ url, title, kind: isImage ? "image" : "document" });
    }
  }
  return items;
}

/* drop the "gpt-5-codex"-style backend model aliases when a user-set model came
   from a different provider; keep simple here - the backend validates anyway */
function stripSystemModel(m: string): string {
  return m;
}