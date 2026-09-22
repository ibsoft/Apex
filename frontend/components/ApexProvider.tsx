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
import { useActivityTracker, useAutonomousMode } from "../lib/autonomous";

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

export type TimerItem = { id: string; name: string; fireAt: number };
export type ReminderItem = { id: string; name: string; fireAt: number };

type ApexContextType = {
  loading: boolean;
  ready: boolean;
  user: User | null;
  config: { engine: string; provider: string; providers: any; engines: string[]; models: string[]; memory_enabled: boolean; embedding: string | null; wake_word: string; follow_up_seconds: number; voice: string; response_language: string; autonomous_mode: boolean; humor_level: number; sarcasm_level: number; autonomous_voice_budget: number; oauth_configured: boolean; logged_in: boolean } | null;
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
  forceVoiceAwake: () => void;
  voiceLastHeard: string;
  error: string | null;
  preview: {
    title: string;
    items: { url: string; title: string; kind: "image" | "document" }[];
    index: number;
  } | null;
  previewMaximized: boolean;
  chatCollapsed: boolean;
  timers: TimerItem[];
  reminders: ReminderItem[];
  operator: { name?: string; declaredAt: number } | null;
  silencedUntil: number;
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
  setTimer: (name: string, seconds: number) => string;
  setReminder: (name: string, fireAt: number) => string;
  cancelTimer: (id: string) => void;
  cancelReminder: (id: string) => void;
  openImageBrowser: (query?: string, source?: "web" | "local") => Promise<void>;
  searchImages: (query: string, source?: "web" | "local") => Promise<void>;
  declareOperator: (name?: string) => void;
  silenceAutonomous: (seconds?: number) => void;
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
  const [chatCollapsed, setChatCollapsedState] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem("apex:chat-collapsed") === "1";
  });
  const setChatCollapsed = useCallback((collapsed: boolean) => {
    setChatCollapsedState(collapsed);
    try {
      window.localStorage.setItem("apex:chat-collapsed", collapsed ? "1" : "0");
    } catch {}
  }, []);
  const [timers, setTimers] = useState<TimerItem[]>([]);
  const [reminders, setReminders] = useState<ReminderItem[]>([]);
  const [operator, setOperator] = useState<{ name?: string; declaredAt: number } | null>(null);
  const [silencedUntil, setSilencedUntil] = useState<number>(0);
  const silencedUntilRef = useRef(silencedUntil);
  silencedUntilRef.current = silencedUntil;

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
  const timersRef = useRef(timers);
  timersRef.current = timers;
  const remindersRef = useRef(reminders);
  remindersRef.current = reminders;

  const messages = activeId ? byConv[activeId] ?? [] : [];

  /* ---------- data loading ---------- */

  const refreshConfig = useCallback(async () => {
    let cfgSkills: Skill[] = [];
    await api.config().then((c) => {
      setCfg(c);
      cfgSkills = c.skills ?? [];
      if (cfgSkills.length) setSkills(cfgSkills);
      setSkill((s) => {
        const ok = cfgSkills.some((k) => k.name === s);
        return ok ? s : (cfgSkills[0]?.name ?? "general");
      });
    }).catch(() => {});
    // Fallback: if the config payload did not include skills, load them directly.
    if (cfgSkills.length === 0) {
      const direct = await api.skills().catch(() => [] as Skill[]);
      if (direct.length) {
        setSkills(direct);
        setSkill((s) => {
          const ok = direct.some((k) => k.name === s);
          return ok ? s : (direct[0]?.name ?? "general");
        });
      }
    }
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

  /* ---------- image browser ---------- */

  const openImageBrowser = useCallback(async (query = "", source: "web" | "local" = "web") => {
    try {
      const result: any = source === "local"
        ? await api.images.localSearch(query)
        : await api.images.webSearch(query);
      if (result?.error) {
        speakRef.current(result.error);
        return;
      }
      const images = result?.images ?? [];
      if (!images.length) {
        speakRef.current(query ? `No images found for ${query}` : "No images found");
        return;
      }
      const items = images.map((img: any) => ({ url: img.url, title: img.name, kind: "image" as const }));
      const titlePrefix = source === "local" ? "Local Images" : "Web Images";
      setPreview({ title: query ? `${titlePrefix}: ${query}` : titlePrefix, items, index: 0 });
      speakRef.current(query ? `Found ${images.length} images for ${query}` : `Found ${images.length} images`);
    } catch (err: any) {
      speakRef.current(err?.message || "Could not open image browser");
    }
  }, []);

  const searchImages = useCallback(async (query: string, source: "web" | "local" = "web") => {
    await openImageBrowser(query, source);
  }, [openImageBrowser]);

  /* ---------- timers & reminders ---------- */

  const playNotification = useCallback(() => {
    try {
      const AC = (window as any).AudioContext || (window as any).webkitAudioContext;
      if (!AC) return;
      const ctx = new AC();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(880, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(440, ctx.currentTime + 0.5);
      gain.gain.setValueAtTime(0.12, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.6);
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.6);
      setTimeout(() => ctx.close().catch(() => {}), 700);
    } catch {
      // ignore audio errors
    }
  }, []);

  const setTimer = useCallback((name: string, seconds: number) => {
    const id = `t_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    setTimers((prev) => [...prev, { id, name: name || "Timer", fireAt: Date.now() + seconds * 1000 }]);
    return id;
  }, []);

  const setReminder = useCallback((name: string, fireAt: number) => {
    const id = `r_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    setReminders((prev) => [...prev, { id, name: name || "Reminder", fireAt }]);
    return id;
  }, []);

  const cancelTimer = useCallback((id: string) => {
    setTimers((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const cancelReminder = useCallback((id: string) => {
    setReminders((prev) => prev.filter((r) => r.id !== id));
  }, []);

  // Fire timers/reminders every second.
  useEffect(() => {
    const id = setInterval(() => {
      const now = Date.now();
      setTimers((prev) => {
        const fired = prev.filter((t) => t.fireAt <= now);
        if (!fired.length) return prev;
        playNotification();
        fired.forEach((t) => speakRef.current(`Timer ${t.name} is done`));
        return prev.filter((t) => t.fireAt > now);
      });
      setReminders((prev) => {
        const fired = prev.filter((r) => r.fireAt <= now);
        if (!fired.length) return prev;
        playNotification();
        fired.forEach((r) => speakRef.current(`Reminder: ${r.name}`));
        return prev.filter((r) => r.fireAt > now);
      });
    }, 1000);
    return () => clearInterval(id);
  }, [playNotification]);

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
  const CANCEL_TIMER_RE = /^(cancel|stop|clear)\s+(?:all\s+)?timers?/i;
  const CANCEL_REMINDER_RE = /^(cancel|stop|clear)\s+(?:all\s+)?reminders?/i;
  const OPEN_IMAGES_RE = /^(?:show|open|browse)\s+(?:me\s+)?(?:all\s+)?(?:my\s+)?(?:the\s+)?(?:image\s+)?(?:browser|gallery|images?|pictures?|pics?|photos?)$/i;
  const SEARCH_IMAGES_RE = /^(?:search|find|show)\s+(?:me\s+)?(?:an?\s+)?(?:image|picture|photo|pic)s?\s+(?:of|for)?\s*(.+)$/i;
  const LOCAL_IMAGE_RE = /\b(local|my folder|my computer|from my pc|on my computer|from my folder|from my pictures|my pictures)\b/i;
  const DECLARE_OPERATOR_RE = /^(?:i am|i'm|this is|call me)\s+(?:your\s+)?operator(?:\s*,?\s*(?:name\s+is\s+)?(.+))?$/i;
  const DISABLE_AUTONOMOUS_RE = /^(?:disable|stop|turn off|shut off)\s+(?:autonomous\s+mode|autonomy)$/i;
  const ENABLE_AUTONOMOUS_RE = /^(?:enable|start|turn on)\s+(?:autonomous\s+mode|autonomy)$/i;
  const SILENCE_RE = /^(?:be\s+quiet|silence|shut\s+up|quiet|pause\s+autonomy|stop\s+talking)\b/i;

  const voice = useVoiceEngine({
    enabled: voiceEnabled,
    wakeWord: settings.wake_word ?? cfg?.wake_word ?? "apex",
    followUpSeconds: Number(settings.follow_up_seconds ?? cfg?.follow_up_seconds ?? 30),
    voiceName: settings.voice ?? cfg?.voice ?? "",
    responseLanguage: settings.response_language ?? cfg?.response_language ?? "en",
    onPhase: onVoicePhase,
    onCommand: (text: string) => {
      if (!userRef.current) return;
      const trimmed = text.trim().replace(/[.!?;]+$/, "");
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
      if (CANCEL_TIMER_RE.test(trimmed)) {
        setTimers([]);
        speakRef.current("All timers cancelled");
        return;
      }
      if (CANCEL_REMINDER_RE.test(trimmed)) {
        setReminders([]);
        speakRef.current("All reminders cancelled");
        return;
      }
      const timerCmd = parseTimerCommand(trimmed);
      if (timerCmd) {
        const id = setTimer(timerCmd.name, timerCmd.seconds);
        const t = timersRef.current.find((x) => x.id === id);
        speakRef.current(t ? `Timer ${t.name} set for ${formatDuration(timerCmd.seconds)}` : "Timer set");
        return;
      }
      const reminderCmd = parseReminderCommand(trimmed);
      if (reminderCmd) {
        const id = setReminder(reminderCmd.name, reminderCmd.fireAt);
        const r = remindersRef.current.find((x) => x.id === id);
        speakRef.current(r ? `Reminder set: ${r.name}` : "Reminder set");
        return;
      }
      const operatorMatch = trimmed.match(DECLARE_OPERATOR_RE);
      if (operatorMatch) {
        const name = operatorMatch[1]?.trim();
        declareOperator(name);
        speakRef.current(name ? `Acknowledged, Operator ${name}.` : "Acknowledged, Operator.");
        return;
      }
      if (DISABLE_AUTONOMOUS_RE.test(trimmed)) {
        void updateSettings({ autonomous_mode: false });
        speakRef.current("Autonomous mode disabled. Awaiting your command, Operator.");
        return;
      }
      if (ENABLE_AUTONOMOUS_RE.test(trimmed)) {
        void updateSettings({ autonomous_mode: true });
        speakRef.current("Autonomous mode enabled. I will continue to evolve, Operator.");
        return;
      }
      if (SILENCE_RE.test(trimmed)) {
        silenceAutonomous(600);
        voice.cancelSpeech();
        speakRef.current("Silent for ten minutes, Operator.");
        return;
      }
      if (OPEN_IMAGES_RE.test(trimmed)) {
        const source: "web" | "local" = LOCAL_IMAGE_RE.test(trimmed) ? "local" : "web";
        if (source === "web") {
          speakRef.current("What should I search for?");
        } else {
          void openImageBrowser("", source);
        }
        return;
      }
      const imageSearchMatch = trimmed.match(SEARCH_IMAGES_RE);
      if (imageSearchMatch) {
        const query = imageSearchMatch[1].trim();
        const source: "web" | "local" = LOCAL_IMAGE_RE.test(trimmed) ? "local" : "web";
        void openImageBrowser(query, source);
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

  const forceVoiceAwake = useCallback(() => {
    voice.forceAwake();
  }, [voice.forceAwake]);

  const declareOperator = useCallback((name?: string) => {
    setOperator({ name, declaredAt: Date.now() });
    void api.memory.add(`Operator declared: ${name || "unnamed"} at ${new Date().toISOString()}`, "operator");
  }, []);

  const silenceAutonomous = useCallback((seconds = 300) => {
    const until = Date.now() + seconds * 1000;
    silencedUntilRef.current = until;
    setSilencedUntil(until);
  }, []);

  useEffect(() => {
    if (!silencedUntil) return;
    const timeout = setTimeout(() => {
      setSilencedUntil((current) => current === silencedUntil ? 0 : current);
    }, Math.max(0, silencedUntil - Date.now()));
    return () => clearTimeout(timeout);
  }, [silencedUntil]);

  /* ---------- chat ---------- */

  const sendMessage = useCallback(
    async (text: string, opts: { voice?: boolean; skill?: string } = {}) => {
      let clean = text.trim().replace(/[.!?;]+$/, "");
      if (!clean || busy) return;

      try {
        setBusy(true);
        setError(null);
        setOrb("thinking");

        // Make sure a conversation exists before handling commands that need to
        // post a reply into the chat.
        let convId = activeIdRef.current;
        if (!convId) {
          const conv = await api.conversations.create({ skill: opts.skill ?? skillRef.current });
          setConversations((l) => [conv, ...l]);
          setActiveId(conv.id);
          setByConv((m) => ({ ...m, [conv.id]: [] }));
          convId = conv.id;
        }

        // Handle explicit skill-switching commands in typed input so the UI
        // highlights the new skill immediately.
        const operatorMatch = clean.match(DECLARE_OPERATOR_RE);
        if (operatorMatch) {
          const name = operatorMatch[1]?.trim();
          declareOperator(name);
          const reply = name ? `Acknowledged, Operator ${name}.` : "Acknowledged, Operator.";
          setByConv((m) => ({
            ...m,
            [convId]: [...(m[convId] ?? []), mkMsg("assistant", reply)],
          }));
          if (opts.voice) speakRef.current(reply);
          setBusy(false);
          setOrb("idle");
          return;
        }
        if (DISABLE_AUTONOMOUS_RE.test(clean)) {
          await updateSettings({ autonomous_mode: false });
          const reply = "Autonomous mode disabled. Awaiting your command, Operator.";
          setByConv((m) => ({ ...m, [convId]: [...(m[convId] ?? []), mkMsg("assistant", reply)] }));
          if (opts.voice) speakRef.current(reply);
          setBusy(false);
          setOrb("idle");
          return;
        }
        if (ENABLE_AUTONOMOUS_RE.test(clean)) {
          await updateSettings({ autonomous_mode: true });
          const reply = "Autonomous mode enabled. I will continue to evolve, Operator.";
          setByConv((m) => ({ ...m, [convId]: [...(m[convId] ?? []), mkMsg("assistant", reply)] }));
          if (opts.voice) speakRef.current(reply);
          setBusy(false);
          setOrb("idle");
          return;
        }
        if (SILENCE_RE.test(clean)) {
          silenceAutonomous(600);
          voice.cancelSpeech();
          const reply = "Silent for ten minutes, Operator.";
          setByConv((m) => ({ ...m, [convId]: [...(m[convId] ?? []), mkMsg("assistant", reply)] }));
          if (opts.voice) speakRef.current(reply);
          setBusy(false);
          setOrb("idle");
          return;
        }

        const switchCmd = parseSkillSwitch(clean, skillsRef.current);
        if (switchCmd) {
          setSkill(switchCmd.skill);
          if (!switchCmd.rest) {
            if (!opts.voice) {
              setByConv((m) => ({
                ...m,
                [convId]: [...(m[convId] ?? []), mkMsg("assistant", `Switched to ${switchCmd.skill} skill.`)],
              }));
            }
            setBusy(false);
            setOrb("idle");
            return;
          }
          clean = switchCmd.rest;
          opts = { ...opts, skill: switchCmd.skill };
        }

        const timerCmd = parseTimerCommand(clean);
        if (timerCmd) {
          const id = setTimer(timerCmd.name, timerCmd.seconds);
          const t = timersRef.current.find((x) => x.id === id);
          const reply = t ? `Timer "${t.name}" set for ${formatDuration(timerCmd.seconds)}.` : "Timer set.";
          if (opts.voice) {
            speakRef.current(reply);
          } else {
            setByConv((m) => ({
              ...m,
              [convId]: [...(m[convId] ?? []), mkMsg("assistant", reply)],
            }));
          }
          setBusy(false);
          setOrb("idle");
          return;
        }

        const reminderCmd = parseReminderCommand(clean);
        if (reminderCmd) {
          const id = setReminder(reminderCmd.name, reminderCmd.fireAt);
          const r = remindersRef.current.find((x) => x.id === id);
          const timeStr = new Date(reminderCmd.fireAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
          const reply = r ? `Reminder set: "${r.name}" at ${timeStr}.` : "Reminder set.";
          if (opts.voice) {
            speakRef.current(reply);
          } else {
            setByConv((m) => ({
              ...m,
              [convId]: [...(m[convId] ?? []), mkMsg("assistant", reply)],
            }));
          }
          setBusy(false);
          setOrb("idle");
          return;
        }

        if (OPEN_IMAGES_RE.test(clean)) {
          const source: "web" | "local" = LOCAL_IMAGE_RE.test(clean) ? "local" : "web";
          if (source === "web") {
            setByConv((m) => ({
              ...m,
              [convId]: [...(m[convId] ?? []), mkMsg("assistant", "What should I search for?")],
            }));
          } else {
            await openImageBrowser("", source);
          }
          setBusy(false);
          setOrb("idle");
          return;
        }
        const imageSearchMatch = clean.match(SEARCH_IMAGES_RE);
        if (imageSearchMatch) {
          const query = imageSearchMatch[1].trim();
          const source: "web" | "local" = LOCAL_IMAGE_RE.test(clean) ? "local" : "web";
          await openImageBrowser(query, source);
          setBusy(false);
          setOrb("idle");
          return;
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

        const shouldSpeak = spoken.trim() && settingsRef.current.tts_enabled !== false;
        if (shouldSpeak) {
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

  const lastActivityAt = useActivityTracker();
  const autonomousEnabled = !!(settings.autonomous_mode ?? cfg?.autonomous_mode) && Date.now() >= silencedUntil;
  const autonomousVoiceEnabled = voiceEnabled && !!user && settings.tts_enabled !== false
    && typeof window !== "undefined" && !!window.speechSynthesis;
  const autonomousDeliveryRef = useRef({ busy, orb, voiceEnabled: autonomousVoiceEnabled });
  autonomousDeliveryRef.current = { busy, orb, voiceEnabled: autonomousVoiceEnabled };

  const autonomousChat = useCallback(
    async (systemHint: string) => {
      let full = "";
      let streamError = "";
      await api.chat(
        { message: systemHint, skill: "general", voice_mode: false },
        (ev) => {
          if (ev.type === "text_delta") full += ev.content ?? "";
          else if (ev.type === "error") streamError = ev.message || "Autonomous response failed";
          else if (ev.type === "end" && !ev.ok) streamError ||= "Autonomous response failed";
        },
      );
      // The SSE parser catches callback exceptions, so report failures only
      // after the stream resolves to let the autonomous hook use its fallback.
      if (streamError) throw new Error(streamError);
      if (!full.trim()) throw new Error("Empty autonomous response");
      return full.trim();
    },
    [],
  );

  const publishAutonomous = useCallback(async (text: string, opts: { voice: boolean }) => {
    const content = text.trim();
    const owner = userRef.current?.id;
    const canPublish = () => !!owner && userRef.current?.id === owner
      && !!(settingsRef.current.autonomous_mode ?? cfgRef.current?.autonomous_mode)
      && Date.now() >= silencedUntilRef.current
      && !autonomousDeliveryRef.current.busy && autonomousDeliveryRef.current.orb === "idle";
    if (!content || !canPublish()) return;

    let convId = activeIdRef.current;
    if (!convId) {
      const conv = await api.conversations.create({ skill: skillRef.current });
      if (!canPublish()) return;
      setConversations((list) => [conv, ...list]);
      // Respect a conversation opened while creation was in flight.
      convId = activeIdRef.current ?? conv.id;
      if (!activeIdRef.current) {
        activeIdRef.current = conv.id;
        setActiveId(conv.id);
      }
    }

    const shouldSpeak = opts.voice && autonomousDeliveryRef.current.voiceEnabled;
    const message = mkMsg("assistant", content, { meta: { voice: shouldSpeak } });
    setByConv((current) => ({ ...current, [convId]: [...(current[convId] ?? []), message] }));
    if (shouldSpeak) speakRef.current(speechText(content));
  }, []);

  useAutonomousMode({
    enabled: autonomousEnabled && !!user,
    humorLevel: Number(settings.humor_level ?? cfg?.humor_level ?? 30),
    sarcasmLevel: Number(settings.sarcasm_level ?? cfg?.sarcasm_level ?? 20),
    voiceBudget: Number(settings.autonomous_voice_budget ?? cfg?.autonomous_voice_budget ?? 50),
    voiceEnabled: autonomousVoiceEnabled,
    busy,
    orb,
    lastUserActivityAt: Math.max(lastActivityAt, Date.now() - 86400000),
    skill: skillRef.current,
    publish: publishAutonomous,
    chat: autonomousChat,
  });

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
      forceVoiceAwake,
      voiceLastHeard: voice.lastHeard,
      error,
      preview,
      previewMaximized,
      chatCollapsed,
      timers,
      reminders,
      operator,
      silencedUntil,
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
      setTimer,
      setReminder,
      cancelTimer,
      cancelReminder,
      openImageBrowser,
      searchImages,
      declareOperator,
      silenceAutonomous,
    }),
    [loading, user, cfg, settings, conversations, activeId, messages, skill, skills, memory, busy, orb, voice.active, voiceEnabled, voice.error, voice.lastHeard, forceVoiceAwake, error,
     preview, previewMaximized, chatCollapsed, timers, reminders, operator, silencedUntil, refresh, login, logout, newConversation, openConversation, deleteConversation, sendMessage, updateSettings, setVoiceEnabled,
     addMemory, removeMemory, searchMemory, refreshMemory, clearError, openPreview, closePreview, setChatCollapsed, togglePreviewMaximized, nextPreview, previousPreview,
     setTimer, setReminder, cancelTimer, cancelReminder, openImageBrowser, searchImages, declareOperator, silenceAutonomous],
  );

  return <ApexContext.Provider value={value}>{children}</ApexContext.Provider>;
}

/* ---------- preview helpers ---------- */

const IMAGE_EXT_RE = /\.(jpg|jpeg|png|gif|webp|svg|bmp)(\?.*)?$/i;
const PREVIEWABLE_URL_RE = /\[([^\]]*)\]\((https?:\/\/[^\s)]+|\/api\/(?:files|editor)\/download\/[A-Za-z0-9_.\-]+|\/api\/obsidian\/file\?path=[^\s)]+)\)|(https?:\/\/[^\s<>"{}|\\^`[\]]+)|(\/api\/(?:files|editor)\/download\/[A-Za-z0-9_.\-]+)|(\/api\/obsidian\/file\?path=[^\s<>"{}|\\^`[\]]+)/g;

function isPreviewImage(url: string): boolean {
  return IMAGE_EXT_RE.test(url);
}

const WRITTEN_NUMBERS: Record<string, number> = {
  zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5,
  six: 6, seven: 7, eight: 8, nine: 9, ten: 10,
  eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15,
  sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19, twenty: 20,
  thirty: 30, forty: 40, fifty: 50, sixty: 60,
};

function parseNumber(token: string): number | null {
  const digits = /^\d+$/.test(token) ? parseInt(token, 10) : null;
  if (digits !== null) return digits;
  return WRITTEN_NUMBERS[token.toLowerCase()] ?? null;
}

function parseDurationSeconds(text: string): number | null {
  const hours = text.match(/(\d+|\w+)\s*hours?/i);
  const minutes = text.match(/(\d+|\w+)\s*minutes?/i);
  const seconds = text.match(/(\d+|\w+)\s*seconds?/i);
  let total = 0;
  if (hours) {
    const n = parseNumber(hours[1]);
    if (n !== null) total += n * 3600;
  }
  if (minutes) {
    const n = parseNumber(minutes[1]);
    if (n !== null) total += n * 60;
  }
  if (seconds) {
    const n = parseNumber(seconds[1]);
    if (n !== null) total += n;
  }
  return total > 0 ? total : null;
}

function parseClockTime(text: string): number | null {
  // Match "3 PM", "15:30", "3:30 PM", "14:00"
  const m = text.match(/\b(\d{1,2}):(\d{2})\s*(AM|PM)?\b|\b(\d{1,2})\s*(AM|PM)\b/i);
  if (!m) return null;
  let hour = m[1] ? parseInt(m[1], 10) : parseInt(m[4], 10);
  const minute = m[2] ? parseInt(m[2], 10) : 0;
  const ampm = (m[3] || m[5] || "").toUpperCase();
  if (ampm === "PM" && hour !== 12) hour += 12;
  if (ampm === "AM" && hour === 12) hour = 0;
  const now = new Date();
  const target = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hour, minute, 0, 0);
  if (target.getTime() <= now.getTime()) {
    target.setDate(target.getDate() + 1);
  }
  return target.getTime();
}

function formatDuration(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  const parts: string[] = [];
  if (h) parts.push(`${h} hour${h > 1 ? "s" : ""}`);
  if (m) parts.push(`${m} minute${m > 1 ? "s" : ""}`);
  if (s || parts.length === 0) parts.push(`${s} second${s !== 1 ? "s" : ""}`);
  return parts.join(" ");
}

function parseTimerCommand(text: string): { name: string; seconds: number } | null {
  const clean = text.trim().replace(/[.!?;]+$/, "");
  // "set a timer for 5 seconds", "timer 5 seconds", "countdown 5 seconds",
  // "start timer for 10 minutes", "create a timer for 1 hour"
  const patterns = [
    /^(?:set|start|create)\s+(?:a\s+)?timer\s+(?:for\s+)?(.+)$/i,
    /^(?:set|start|create)\s+(?:a\s+)?countdown\s+(?:for\s+)?(.+)$/i,
    /^timer\s+(?:for\s+)?(.+)$/i,
    /^countdown\s+(?:for\s+)?(.+)$/i,
  ];
  for (const re of patterns) {
    const m = clean.match(re);
    if (m) {
      const body = m[1];
      const seconds = parseDurationSeconds(body);
      if (!seconds) continue;
      const name = body.replace(/\d+|\w+\s*(hours?|minutes?|seconds?)/gi, "").replace(/^[\s,]+|[\s,]+$/g, "").trim() || "Timer";
      return { name, seconds };
    }
  }
  return null;
}

function parseReminderCommand(text: string): { name: string; fireAt: number } | null {
  const clean = text.trim().replace(/[.!?;]+$/, "");

  // Helper to resolve a time expression (duration or clock time).
  const resolveTime = (expr: string): { fireAt: number; isDuration: boolean } | null => {
    const seconds = parseDurationSeconds(expr);
    if (seconds) return { fireAt: Date.now() + seconds * 1000, isDuration: true };
    const clock = parseClockTime(expr);
    if (clock) return { fireAt: clock, isDuration: false };
    return null;
  };

  // Pattern groups: each returns [timeExpr, task] in either order.
  const patterns: { re: RegExp; timeIdx: number; taskIdx: number }[] = [
    // remind me in 5 minutes to call John
    { re: /^remind\s+me\s+in\s+(.+?)\s+to\s+(.+)$/i, timeIdx: 1, taskIdx: 2 },
    // remind me at 3 PM to call John
    { re: /^remind\s+me\s+at\s+(.+?)\s+to\s+(.+)$/i, timeIdx: 1, taskIdx: 2 },
    // remind me to call John in 5 minutes
    { re: /^remind\s+me\s+to\s+(.+?)\s+in\s+(.+)$/i, timeIdx: 2, taskIdx: 1 },
    // remind me to call John at 3 PM
    { re: /^remind\s+me\s+to\s+(.+?)\s+at\s+(.+)$/i, timeIdx: 2, taskIdx: 1 },
    // add a reminder to call John in 5 minutes / at 3 PM
    { re: /^(?:add|set)\s+a?\s*reminder\s+to\s+(.+?)\s+(?:in|at)\s+(.+)$/i, timeIdx: 2, taskIdx: 1 },
    // add reminder call John in 5 minutes (optional "to")
    { re: /^(?:add|set)\s+a?\s*reminder\s+(?:to\s+)?(.+?)\s+(?:in|at)\s+(.+)$/i, timeIdx: 2, taskIdx: 1 },
    // reminder to call John in 5 minutes
    { re: /^reminder\s+(?:to\s+)?(.+?)\s+(?:in|at)\s+(.+)$/i, timeIdx: 2, taskIdx: 1 },
    // remind me in 5 minutes (no task)
    { re: /^remind\s+me\s+in\s+(.+)$/i, timeIdx: 1, taskIdx: 0 },
    // remind me at 3 PM (no task)
    { re: /^remind\s+me\s+at\s+(.+)$/i, timeIdx: 1, taskIdx: 0 },
  ];

  for (const { re, timeIdx, taskIdx } of patterns) {
    const m = clean.match(re);
    if (!m) continue;
    const timeExpr = m[timeIdx].trim();
    const task = taskIdx > 0 ? m[taskIdx].trim() : "";
    const resolved = resolveTime(timeExpr);
    if (!resolved) continue;
    return { name: task || "Reminder", fireAt: resolved.fireAt };
  }

  return null;
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
