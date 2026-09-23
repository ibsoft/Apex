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
import { formatDuration, parseLocalCommand } from "../lib/commands";

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
  routedSkill: string | null;
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
  deleteSkill: (name: string) => Promise<void>;
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
  const [routedSkill, setRoutedSkill] = useState<string | null>(null);
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

  const commandLanguage = () => settingsRef.current.response_language ?? cfgRef.current?.response_language ?? "en";
  const localize = (english: string, greek: string) => /^el(?:-|$)/i.test(commandLanguage()) ? greek : english;

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
      const direct = await api.skills.list().catch(() => [] as Skill[]);
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

  const deleteSkill = useCallback(async (name: string) => {
    const target = skillsRef.current.find((s) => s.name === name);
    if (!target || target.builtin) return;
    try {
      await api.skills.delete(name);
    } catch (err: any) {
      // 404 means the file is already gone; drop it from the UI as well.
      if (err?.status === 404) {
        setSkills((prev) => prev.filter((s) => s.name !== name));
        if (skillRef.current === name) {
          setSkill("general");
        }
        return;
      }
      setError(err?.message ?? `Could not delete skill "${name}".`);
      return;
    }
    setSkills((prev) => prev.filter((s) => s.name !== name));
    if (skillRef.current === name) {
      setSkill("general");
    }
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
        speakRef.current(query
          ? localize(`No images found for ${query}`, `Δεν βρέθηκαν εικόνες για ${query}`)
          : localize("No images found", "Δεν βρέθηκαν εικόνες"));
        return;
      }
      const items = images.map((img: any) => ({ url: img.url, title: img.name, kind: "image" as const }));
      const titlePrefix = source === "local" ? localize("Local Images", "Τοπικές εικόνες") : localize("Web Images", "Εικόνες ιστού");
      setPreview({ title: query ? `${titlePrefix}: ${query}` : titlePrefix, items, index: 0 });
      speakRef.current(query
        ? localize(`Found ${images.length} images for ${query}`, `Βρέθηκαν ${images.length} εικόνες για ${query}`)
        : localize(`Found ${images.length} images`, `Βρέθηκαν ${images.length} εικόνες`));
    } catch (err: any) {
      speakRef.current(err?.message || localize("Could not open image browser", "Δεν ήταν δυνατό το άνοιγμα των εικόνων"));
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
    setTimers((prev) => [...prev, { id, name: name || localize("Timer", "Χρονόμετρο"), fireAt: Date.now() + seconds * 1000 }]);
    return id;
  }, []);

  const setReminder = useCallback((name: string, fireAt: number) => {
    const id = `r_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    setReminders((prev) => [...prev, { id, name: name || localize("Reminder", "Υπενθύμιση"), fireAt }]);
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
        fired.forEach((t) => speakRef.current(localize(`Timer ${t.name} is done`, `Το χρονόμετρο ${t.name} ολοκληρώθηκε`)));
        return prev.filter((t) => t.fireAt > now);
      });
      setReminders((prev) => {
        const fired = prev.filter((r) => r.fireAt <= now);
        if (!fired.length) return prev;
        playNotification();
        fired.forEach((r) => speakRef.current(localize(`Reminder: ${r.name}`, `Υπενθύμιση: ${r.name}`)));
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

  const voice = useVoiceEngine({
    enabled: voiceEnabled,
    wakeWord: settings.wake_word ?? cfg?.wake_word ?? "apex",
    followUpSeconds: Number(settings.follow_up_seconds ?? cfg?.follow_up_seconds ?? 30),
    voiceName: settings.voice ?? cfg?.voice ?? "",
    responseLanguage: settings.response_language ?? cfg?.response_language ?? "en",
    onPhase: onVoicePhase,
    onCommand: (text: string) => {
      if (!userRef.current) return;
      void handleVoiceCommand(text);
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

  // Both voice and typed input execute the same commands and acknowledgements.
  // null means a context-dependent command (such as preview navigation) is not
  // applicable, so the original request can still be handled by the model.
  async function executeLocalCommand(command: NonNullable<ReturnType<typeof parseLocalCommand>>): Promise<string | null> {
    switch (command.type) {
      case "preview": {
        const current = previewRef.current;
        if (!current) return null;
        if (command.action === "close") {
          closePreview();
          return localize("Preview closed.", "Η προεπισκόπηση έκλεισε.");
        }
        if (command.action === "maximize") {
          if (!previewMaximizedRef.current) togglePreviewMaximized();
          return localize("Preview maximized.", "Η προεπισκόπηση μεγιστοποιήθηκε.");
        }
        if (command.action === "restore") {
          if (previewMaximizedRef.current) togglePreviewMaximized();
          return localize("Preview restored.", "Η προεπισκόπηση επανήλθε στο κανονικό μέγεθος.");
        }
        if (current.items.length < 2) return localize("There is only one item.", "Υπάρχει μόνο ένα στοιχείο.");
        const direction = command.action === "next" ? 1 : -1;
        const item = current.items[(current.index + direction + current.items.length) % current.items.length];
        if (direction === 1) nextPreview();
        else previousPreview();
        return localize(`Showing ${item.title}.`, `Προβάλλεται: ${item.title}.`);
      }
      case "cancelTimers":
        setTimers([]);
        return localize("All timers cancelled.", "Ακυρώθηκαν όλα τα χρονόμετρα.");
      case "cancelReminders":
        setReminders([]);
        return localize("All reminders cancelled.", "Ακυρώθηκαν όλες οι υπενθυμίσεις.");
      case "timer":
        setTimer(command.name, command.seconds);
        return localize(
          `Timer "${command.name}" set for ${formatDuration(command.seconds)}.`,
          `Ορίστηκε χρονόμετρο «${command.name}» για ${formatDuration(command.seconds, "el")}.`,
        );
      case "reminder": {
        setReminder(command.name, command.fireAt);
        const time = new Date(command.fireAt).toLocaleTimeString(commandLanguage(), { hour: "2-digit", minute: "2-digit" });
        return localize(`Reminder set: "${command.name}" at ${time}.`, `Ορίστηκε υπενθύμιση: «${command.name}» στις ${time}.`);
      }
      case "operator":
        declareOperator(command.name);
        return command.name
          ? localize(`Acknowledged, Operator ${command.name}.`, `Έγινε, χειριστή ${command.name}.`)
          : localize("Acknowledged, Operator.", "Έγινε, χειριστή.");
      case "autonomy":
        await updateSettings({ autonomous_mode: command.enabled });
        return command.enabled
          ? localize("Autonomous mode enabled.", "Η αυτόνομη λειτουργία ενεργοποιήθηκε.")
          : localize("Autonomous mode disabled.", "Η αυτόνομη λειτουργία απενεργοποιήθηκε.");
      case "silence":
        silenceAutonomous(600);
        voice.cancelSpeech();
        return localize("Silent for ten minutes, Operator.", "Θα παραμείνω σιωπηλός για δέκα λεπτά, χειριστή.");
      case "images":
        if (!command.query && command.source === "web") return localize("What should I search for?", "Τι να αναζητήσω;");
        await openImageBrowser(command.query, command.source);
        return ""; // The browser reports the search result itself.
      case "skill":
        setSkill(command.skill);
        return localize(`Switched to ${command.skill} skill.`, `Ενεργοποιήθηκε η δεξιότητα ${command.skill}.`);
    }
  }

  async function handleVoiceCommand(text: string) {
    try {
      const command = parseLocalCommand(text, commandLanguage(), skillsRef.current);
      if (command?.type === "skill" && command.rest) {
        setSkill(command.skill);
        await sendRef.current(command.rest, { voice: true, skill: command.skill });
        return;
      }
      if (command) {
        const reply = await executeLocalCommand(command);
        if (reply !== null) {
          if (reply) speakRef.current(reply);
          return;
        }
      }
      await sendRef.current(text, { voice: true, skill: skillRef.current });
    } catch (err: any) {
      setError(err?.message ?? String(err));
      setOrb("idle");
    }
  }

  /* ---------- chat ---------- */

  const sendMessage = useCallback(
    async (text: string, opts: { voice?: boolean; skill?: string } = {}) => {
      let clean = text.trim().replace(/[.!?;]+$/, "");
      if (!clean || busy) return;

      try {
        setBusy(true);
        setError(null);
        setOrb("thinking");
        setRoutedSkill(null);

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

        let command = parseLocalCommand(clean, commandLanguage(), skillsRef.current);
        if (command?.type === "skill" && command.rest) {
          setSkill(command.skill);
          clean = command.rest;
          opts = { ...opts, skill: command.skill };
          command = parseLocalCommand(clean, commandLanguage(), skillsRef.current);
        }
        if (command) {
          const reply = await executeLocalCommand(command);
          if (reply !== null) {
            if (reply) {
              setByConv((m) => ({
                ...m,
                [convId]: [...(m[convId] ?? []), mkMsg("assistant", reply, { meta: { voice: !!opts.voice } })],
              }));
            }
            setOrb("idle");
            if (opts.voice && reply) speakRef.current(reply);
            return;
          }
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
          if (ev.type === "meta") {
            if (ev.skill && ev.skill !== skillRef.current) {
              setRoutedSkill(ev.skill);
            }
          } else if (ev.type === "text_delta") {
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
          } else if (ev.type === "skills_changed") {
            void api.skills.list().then((list) => {
              if (list.length) {
                setSkills(list);
                setSkill((s) => {
                  const ok = list.some((k) => k.name === s);
                  return ok ? s : (list[0]?.name ?? "general");
                });
              }
            }).catch(() => {});
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
            // Keep the routed-skill highlight visible briefly after the turn.
            setTimeout(() => setRoutedSkill((current) => (current ? null : current)), 2000);
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
        {
          message: systemHint,
          skill: "general",
          voice_mode: false,
          store_messages: false,
          conversation_id: activeIdRef.current || undefined,
        },
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

    // Autonomous nudges are ephemeral: they use the current conversation for
    // context but never create new history entries. If no conversation is open
    // the message is only spoken, not persisted visually.
    const convId = activeIdRef.current;
    const shouldSpeak = opts.voice && autonomousDeliveryRef.current.voiceEnabled;
    if (convId) {
      const message = mkMsg("assistant", content, { meta: { voice: shouldSpeak } });
      setByConv((current) => ({ ...current, [convId]: [...(current[convId] ?? []), message] }));
    }
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
      routedSkill,
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
      deleteSkill,
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
    [loading, user, cfg, settings, conversations, activeId, messages, skill, routedSkill, skills, memory, busy, orb, voice.active, voiceEnabled, voice.error, voice.lastHeard, forceVoiceAwake, error,
     preview, previewMaximized, chatCollapsed, timers, reminders, operator, silencedUntil, refresh, login, logout, newConversation, openConversation, deleteConversation,      sendMessage, updateSettings, setVoiceEnabled, deleteSkill,
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
