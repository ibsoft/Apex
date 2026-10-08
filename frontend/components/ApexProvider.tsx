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
  apiFetch,
  auth,
  BASE,
  Conversation,
  MemoryEntry,
  onSessionLocked,
  Skill,
  User,
  ChatEvent,
} from "../lib/api";
import { useVoiceEngine, VoicePhase } from "../lib/voice";
import { speechText } from "./speechText";
import { useActivityTracker, useAutonomousMode } from "../lib/autonomous";
import { sendNotepadCommand, notepadContext, requestsNotepadOutput } from "../lib/notepad";
import type { NotepadCommand } from "../lib/notepad";
import { sendChatInputCommand, sendPanelCommand, PANEL_TAB_NAMES } from "../lib/panelBridge";
import {
  formatDuration,
  parseLocalCommand,
  parseTerminalTarget,
  parseThinkHard,
  type LocalCommand,
  type PanelTabName,
} from "../lib/commands";
import {
  buildUiState,
  renderActionCatalogue,
  renderUiState,
  sanitizeActions,
} from "../lib/commandSpec";
import {
  describeOneTask,
  describeTasks,
  filterTasks,
  resolveTarget,
  sortTasks,
  taskNotification,
  type Task,
  type TaskDraft,
  type TaskPatch,
} from "../lib/tasks";
import {
  AppWindow,
  MAX_WINDOWS,
  WindowArrangement,
  WindowItem,
  WindowKind,
  collectPreviewableItems,
  groupItemsByKind,
  itemTitle,
  isFilesWindow,
  isNotepadWindow,
  isTerminalWindow,
  kindForItems,
  layoutRects,
  onDesktop,
  resolvePreviewKinds,
  terminalSessionId,
  terminalUrl,
  terminalWindows,
  windowContextBlock,
} from "../lib/windows";

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
  config: { engine: string; provider: string; providers: any; engines: string[]; models: string[]; think_hard_model: string; think_hard_model_enabled: boolean; visio_enabled: boolean; visio_provider: string; visio_model: string; visio_camera: string; sip_enabled: boolean; sip_server: string; sip_user: string; sip_password_set: boolean; sip_transport: string; sip_port: string; sip_display_name: string; sip_domain: string; sip_outbound_proxy: string; sip_notify_to: string; sip_tts_engine: string; sip_tts_voice: string; sip_configured: boolean; memory_enabled: boolean; embedding: string | null; wake_word: string; follow_up_seconds: number; voice: string; response_language: string; soul: string; soul_max_chars: number; autonomous_mode: boolean; humor_level: number; sarcasm_level: number; autonomous_voice_budget: number; oauth_configured: boolean; logged_in: boolean } | null;
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
  /* Auth state consumed by AppShell to decide which screen to mount. */
  locked: boolean;
  lockEnabled: boolean;
  systemLoginEnabled: boolean;
  oauthAvailable: boolean;
  lock: () => Promise<void>;
  unlock: (password: string) => Promise<boolean>;
  afterAuth: () => Promise<void>;
  forceVoiceAwake: () => void;
  voiceLastHeard: string;
  error: string | null;
  windows: AppWindow[];
  focusedWindowId: string | null;
  activeDesktop: number;
  desktopSet: (n: number) => void;
  desktopNext: () => void;
  desktopPrev: () => void;
  windowMoveToDesktop: (id: string, n: number) => void;
  chatCollapsed: boolean;
  timers: TimerItem[];
  reminders: ReminderItem[];
  /* Scheduled tasks. The provider owns the list, the poll and the completion
     notice; the panel is a view over them plus the manual editor. */
  tasks: Task[];
  tasksError: string | null;
  tasksLoading: boolean;
  loadTasks: () => Promise<void>;
  createTask: (draft: TaskDraft) => Promise<Task>;
  updateTask: (id: number, patch: TaskPatch) => Promise<Task>;
  deleteTask: (id: number) => Promise<void>;
  runTaskNow: (id: number) => Promise<void>;
  clearTaskNotice: (id: number) => void;
  operator: { name?: string; declaredAt: number } | null;
  silencedUntil: number;
  sudoPrompt: { reason?: string } | null;
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
  windowOpen: (items: WindowItem[], opts?: { title?: string; kind?: WindowKind; maximize?: boolean }) => void;
  windowOpenNew: (items: WindowItem[], opts?: { title?: string; kind?: WindowKind; maximize?: boolean }) => void;
  openTerminal: () => Promise<string | null>;
  windowClose: (id: string) => void;
  windowCloseAll: () => void;
  windowFocus: (id: string) => void;
  windowToggleMaximize: (id: string) => void;
  windowToggleMinimize: (id: string) => void;
  windowArrange: (arrangement: WindowArrangement) => void;
  windowNext: () => void;
  windowPrevious: () => void;
  windowSetNote: (id: string, note: string) => void;
  windowToggleNotes: (id: string) => void;
  windowUpdate: (id: string, patch: Partial<Pick<AppWindow, "rect" | "maximized" | "minimized">>) => void;
  setChatCollapsed: (collapsed: boolean) => void;
  setTimer: (name: string, seconds: number) => string;
  setReminder: (name: string, fireAt: number) => string;
  cancelTimer: (id: string) => void;
  cancelReminder: (id: string) => void;
  openImageBrowser: (query?: string, source?: "web" | "local") => Promise<void>;
  searchImages: (query: string, source?: "web" | "local") => Promise<void>;
  declareOperator: (name?: string) => void;
  silenceAutonomous: (seconds?: number) => void;
  setSudoPassword: (password: string, save: boolean) => Promise<void>;
  closeSudoPrompt: () => void;
  githubPrompt: { reason?: string } | null;
  setGithubToken: (token: string) => Promise<void>;
  closeGithubPrompt: () => void;
};

const ApexContext = createContext<ApexContextType | null>(null);
export const useApex = () => {
  const ctx = useContext(ApexContext);
  if (!ctx) throw new Error("useApex must be used inside <ApexProvider>");
  return ctx;
};

let msgSeq = 0;
/* How often the TASKS tab asks the backend what changed. Kept in step with
   TASKS_TICK_SECONDS (20s) so the tab shows a run that just became due at
   roughly the moment it fired, rather than up to a full tick later. */
const TASKS_POLL_MS = 20000;
const mkMsg = (role: Message["role"], content: string, extra: Partial<Message> = {}): Message => ({
  id: `m${Date.now().toString(36)}_${msgSeq++}`,
  role,
  content,
  ...extra,
});

export function ApexProvider({ children }: { children: React.ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [user, setUser] = useState<User | null>(null);
  /* Lock screen. `locked` is a *client* mirror of the server's session flag:
     the server is what actually refuses work, this is what decides whether the
     overlay is shown. `voiceAllowed` is the single gate every voice code path
     checks, so an unauthenticated or locked machine can never listen. */
  const [locked, setLocked] = useState(false);
  const [lockEnabled, setLockEnabled] = useState(true);
  /* Assume the machine's own sign-in until the server says otherwise. This flag
     decides which screen an unauthenticated user gets, so defaulting it to
     false means one failed /auth/status hands them the OAuth screen instead -
     a dead end when OAuth is disabled, and the one screen that cannot work
     without a network round trip that has already failed. */
  const [systemLoginEnabled, setSystemLoginEnabled] = useState(true);
  const [oauthAvailable, setOauthAvailable] = useState(false);
  /* The user's microphone preference, kept separately from the effective state
     so locking can force the mic off and unlocking can put it back exactly as it
     was, rather than guessing from settings. */
  const voiceWantedRef = useRef(true);
  /* The voice engine itself, reachable from the auth callbacks that are
     defined before it. Only cancelSpeech is used, and it is safe to call when
     the engine is idle. */
  const voiceRef = useRef<{ cancelSpeech: () => void } | null>(null);
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
  const [windows, setWindows] = useState<AppWindow[]>([]);
  const [focusedWindowId, setFocusedWindowId] = useState<string | null>(null);
  const [activeDesktop, setActiveDesktop] = useState(0);
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
  /* Scheduled tasks. `tasksRef` is the mirror the local command handler reads:
     executeLocalCommand is called from the voice path and a ref read there must
     not be a stale closure over a list the last render had. */
  const [tasks, setTasks] = useState<Task[]>([]);
  const tasksRef = useRef<Task[]>([]);
  tasksRef.current = tasks;
  const [tasksError, setTasksError] = useState<string | null>(null);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [operator, setOperator] = useState<{ name?: string; declaredAt: number } | null>(null);
  const [silencedUntil, setSilencedUntil] = useState<number>(0);
  const silencedUntilRef = useRef(silencedUntil);
  silencedUntilRef.current = silencedUntil;
  const [sudoPrompt, setSudoPrompt] = useState<{ reason?: string } | null>(null);
  const [githubPrompt, setGithubPrompt] = useState<{ reason?: string } | null>(null);

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
  const windowsRef = useRef(windows);
  windowsRef.current = windows;
  const focusedWindowIdRef = useRef(focusedWindowId);
  focusedWindowIdRef.current = focusedWindowId;
  const activeDesktopRef = useRef(activeDesktop);
  activeDesktopRef.current = activeDesktop;
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

  /* ---------- scheduled tasks ----------
   *
   * A run happens on a backend thread with no browser attached, so the tab
   * cannot learn about it from the chat stream. It polls, and the poll is the
   * only thing that turns a finished run into something the operator sees. That
   * makes the poll load-bearing rather than a nicety: without it a task could
   * succeed at 03:00 and the only way to know would be to open the tab.
   *
   * Completion is announced once and then acknowledged server-side. `seenRef`
   * holds the run stamps already reported in this session, so a poll that
   * overlaps another poll (React re-render, StrictMode double-mount) cannot
   * post the same result into chat twice - the user would get a duplicate
   * message and the ack would race the second one. */
  const seenRunsRef = useRef<Set<number>>(new Set());

  /* Post a finished run into the active conversation. Client-side only: the
     task's real output is already persisted in the task's own conversation, and
     writing a second copy into the operator's chat thread would make the
     database disagree with what is on screen. */
  const pushSystemMessage = useCallback((content: string) => {
    const convId = activeIdRef.current;
    if (!convId) return;
    setByConv((m) => ({
      ...m,
      [convId]: [...(m[convId] ?? []), mkMsg("system", content)],
    }));
  }, []);

  const refreshTasks = useCallback(async (knownUser = userRef.current) => {
    if (!knownUser) return;
    const data = await api.tasks.list().catch(() => null);
    if (!data) return;
    setTasksError(null);
    setTasks(data.tasks ?? []);
  }, []);

  /* Report runs the operator has not seen yet, then ack them. The ack is sent
     after the message is in the conversation, never before: acking first and
     crashing would lose the result permanently, while acking late at worst
     repeats a message on the next reload. */
  const announceFinishedRuns = useCallback((list: Task[]) => {
    const fresh = list.filter(
      (t) => t.unread && t.last_run && !seenRunsRef.current.has(t.last_run * 1000 + t.id),
    );
    if (!fresh.length) return;
    for (const task of fresh) seenRunsRef.current.add(task.last_run! * 1000 + task.id);
    for (const task of fresh) {
      pushSystemMessage(taskNotification(task, commandLanguage()));
    }
    void api.tasks.ack(fresh.map((t) => t.id)).catch(() => {});
  }, []);

  const loadTasks = useCallback(async () => {
    setTasksLoading(true);
    try {
      const data = await api.tasks.list();
      setTasksError(null);
      setTasks(data.tasks ?? []);
      announceFinishedRuns(data.tasks ?? []);
    } catch (err) {
      setTasksError(err instanceof Error ? err.message : "Could not load tasks.");
    } finally {
      setTasksLoading(false);
    }
  }, [announceFinishedRuns]);

  const createTask = useCallback(async (draft: TaskDraft) => {
    const task = await api.tasks.create(draft);
    setTasks((prev) => [...prev, task]);
    return task;
  }, []);

  const updateTask = useCallback(async (id: number, patch: TaskPatch) => {
    const task = await api.tasks.update(id, patch);
    setTasks((prev) => prev.map((t) => (t.id === id ? task : t)));
    return task;
  }, []);

  const deleteTask = useCallback(async (id: number) => {
    await api.tasks.remove(id);
    setTasks((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const runTaskNow = useCallback(async (id: number) => {
    await api.tasks.runNow(id);
    // The result arrives on the next poll, not in this response: the run is a
    // whole agent turn and the request returns as soon as it is claimed.
    return loadTasks();
  }, [loadTasks]);

  const clearTaskNotice = useCallback((id: number) => {
    setTasks((prev) => prev.map((t) => (t.id === id ? { ...t, unread: false } : t)));
    void api.tasks.ack([id]).catch(() => {});
  }, []);

  /* The poll. Only while signed in, and it pauses while the tab is hidden: a
     backgrounded tab is not a place to spend a request every 20 seconds, and
     the unread flag is persisted precisely so a result waiting for hours is
     still there when the tab comes back. */
  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    const poll = async () => {
      if (cancelled || document.hidden) return;
      const data = await api.tasks.list().catch(() => null);
      if (cancelled || !data) return;
      setTasksError(null);
      setTasks(data.tasks ?? []);
      announceFinishedRuns(data.tasks ?? []);
    };
    const timer = setInterval(() => void poll(), TASKS_POLL_MS);
    const onVisible = () => {
      if (!document.hidden) void poll();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [user, announceFinishedRuns]);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      await refreshConfig();
      // Read auth state first: it carries the CSRF token every later
      // state-changing request needs, and it says which sign-in screen to show.
      let status = await auth.status().catch(() => null);
      if (!status) {
        // The backend may still be coming up. One retry costs nothing and
        // avoids stranding the user on the wrong sign-in screen.
        await new Promise((resolve) => setTimeout(resolve, 600));
        status = await auth.status().catch(() => null);
      }
      if (status) {
        setSystemLoginEnabled(status.system_login_enabled);
        setLockEnabled(status.lock_enabled);
        setOauthAvailable(status.oauth_available);
        setLocked(status.locked);
      }
      const me = await api.me().catch(() => null);
      if (me?.ok && me.user) {
        setUser(me.user);
        setSettings(me.settings ?? {});
        /* Re-read the auth status *after* /api/me. A dev session (and any
           session this request just established) gets its session id from
           that call, so the first status read had no token to hand back.
           Without this the app would look signed in and then fail every write
           with a 403 until the next page load. */
        const after = await auth.status().catch(() => null);
        if (after) {
          setSystemLoginEnabled(after.system_login_enabled);
          setLockEnabled(after.lock_enabled);
          setOauthAvailable(after.oauth_available);
          setLocked(after.locked);
        }
      } else {
        setUser(null);
      }
      // The shell should not be blocked by optional history or memory data.
      setLoading(false);
      if (me?.ok && me.user) {
        void Promise.all([refreshConvos(me.user), refreshMemory(me.user), refreshTasks(me.user)]);
      }
    } catch {
      setLoading(false);
    }
  }, [refreshConfig, refreshConvos, refreshMemory, refreshTasks]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  /* A PWA manifest shortcut deep-links straight into a panel tab
     ("/?panel=tasks"). The query is stripped on the way past: left in place,
     every reload would re-issue the command and fight whatever the operator did
     next.

     The command waits for `user`. Sending it on mount would work - the bridge
     retries until ChatUI mounts - but on a signed-out launch nothing ever
     mounts, so it would dispatch a custom event every 50ms for 30 seconds
     against a login screen that cannot answer. Gating on the session costs one
     effect and turns that into a no-op. */
  const [deepLinkTab, setDeepLinkTab] = useState<PanelTabName | null>(null);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const requested = params.get("panel") as PanelTabName | null;
    if (!requested || !PANEL_TAB_NAMES.includes(requested)) return;
    setDeepLinkTab(requested);
    params.delete("panel");
    const rest = params.toString();
    window.history.replaceState(null, "", `${window.location.pathname}${rest ? `?${rest}` : ""}`);
  }, []);
  useEffect(() => {
    if (!deepLinkTab || !user) return;
    // Same ordering as the spoken "show tasks" path: a tab change is
    // meaningless while the panel is collapsed, so opening is part of the
    // intent.
    setChatCollapsedState(false);
    void sendPanelCommand({ action: "open", tab: deepLinkTab }).catch(() => {});
  }, [deepLinkTab, user]);

  /* ---------- auth ---------- */

  const login = useCallback(() => api.login(), []);

  /* Everything a lock has to do locally: remember the microphone preference,
     cover the screen, and stop listening *before* any network call, so there is
     no window in which a locked screen is still recording. */
  const applyLocked = useCallback(() => {
    voiceWantedRef.current = voiceEnabled;
    setLocked(true);
    setVoiceEnabledState(false);
    if (voiceRef.current) voiceRef.current.cancelSpeech();
  }, [voiceEnabled]);

  const lock = useCallback(async () => {
    applyLocked();
    // The server call then marks the session locked; if it fails we still stay
    // locked locally, because a lock that depends on a round trip is not a lock.
    await auth.lock().catch(() => {});
  }, [applyLocked]);

  /* Another tab or window can lock the same session. The server answers the next
     request with 423 and the API layer reports it here, so this tab locks too
     instead of quietly carrying on. */
  useEffect(() => {
    onSessionLocked(applyLocked);
    return () => onSessionLocked(null);
  }, [applyLocked]);

  /* Called by the login and lock screens once the server has accepted the
     password. Re-reads identity and settings rather than reloading the page:
     a full reload would tear down the voice engine and lose the desktop. */
  const afterAuth = useCallback(async () => {
    const me = await api.me().catch(() => null);
    if (me?.ok && me.user) {
      setUser(me.user);
      setSettings(me.settings ?? {});
      void Promise.all([refreshConvos(me.user), refreshMemory(me.user), refreshTasks(me.user)]);
    } else {
      setUser(null);
    }
  }, [refreshConvos, refreshMemory, refreshTasks]);

  const unlock = useCallback(async (password: string) => {
    await auth.unlock(password);
    setLocked(false);
    // Restore the microphone only to the state the user had chosen, and only
    // if they are still signed in; the gate in voiceAllowed still applies.
    setVoiceEnabledState(voiceWantedRef.current);
    return true;
  }, []);

  const logout = useCallback(async () => {
    // Clear locally first: a failed logout must never leave the previous user's
    // conversation, memory and windows on screen.
    setUser(null);
    setLocked(false);
    setConversations([]);
    setByConv({});
    setActiveId(null);
    setMemory([]);
    setSudoPrompt(null);
    setWindows([]);
    setTimers([]);
    setReminders([]);
    setSkill("general");
    setRoutedSkill(null);
    if (voiceRef.current) voiceRef.current.cancelSpeech();
    /* Drop the cached sudo/VAPT secret *before* the session ends. Afterwards the
       server has no user to attribute it to and the call is rejected, which
       would leave a privileged credential cached on a machine someone else is
       about to sign in to. */
    await api.vapt.clear().catch(() => {});
    await auth.logout().catch(() => {});
    await refresh();
  }, [refresh]);

  const setSudoPassword = useCallback(async (password: string, save: boolean) => {
    await api.vapt.password({ password, save });
  }, []);

  const closeSudoPrompt = useCallback(() => setSudoPrompt(null), []);

  const setGithubToken = useCallback(async (token: string) => {
    await api.code.githubToken(token);
  }, []);

  const closeGithubPrompt = useCallback(() => setGithubPrompt(null), []);

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

  // Fire-and-forget: ask the backend to summarize the thread we're leaving so
  // Apex can recall it from memory later. Never blocks the switch.
  const summarizeLeftConversation = (id: string | null) => {
    if (!id) return;
    void api.conversations.summarize(id).catch(() => {});
  };

  const newConversation = useCallback(async () => {
    summarizeLeftConversation(activeIdRef.current);
    const conv = await api.conversations.create({ skill: skillRef.current });
    setConversations((l) => [conv, ...l]);
    setActiveId(conv.id);
    setByConv((m) => ({ ...m, [conv.id]: [] }));
  }, []);

  const openConversation = useCallback(
    async (id: string) => {
      if (id === activeIdRef.current) return;
      summarizeLeftConversation(activeIdRef.current);
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

  /* ---------- desktop windows ---------- */

  const findWindow = (id: string) => windowsRef.current.find((w) => w.id === id);

  const signature = (items: WindowItem[]) => items.map((i) => i.url).sort().join("|");
  const createWindow = useCallback((items: WindowItem[], opts: { title?: string; kind?: WindowKind; maximize?: boolean } = {}) => {
    const list = windowsRef.current;
    if (list.length >= MAX_WINDOWS) {
      speakRef.current(localize(
        `Maximum ${MAX_WINDOWS} windows are open. Close one to open another.`,
        `Έχουν ανοίξει το μέγιστο των ${MAX_WINDOWS} παραθύρων. Κλείστε ένα για να ανοίξετε άλλο.`,
      ));
      return;
    }
    const rects = layoutRects("cascade", list.length + 1, window.innerWidth, window.innerHeight);
    const win: AppWindow = {
      id: `win_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`,
      items,
      index: 0,
      kind: opts.kind ?? kindForItems(items),
      rect: rects[list.length],
      maximized: !!opts.maximize,
      minimized: false,
      desktop: activeDesktopRef.current,
      note: "",
      showNotes: false,
    };
    windowsRef.current = [...windowsRef.current, win];
    setWindows(windowsRef.current);
    setFocusedWindowId(win.id);
    return win.id;
  }, []);

  const windowOpen = useCallback((items: WindowItem[], opts: { title?: string; kind?: WindowKind; maximize?: boolean } = {}) => {
    if (!items.length) return;
    const list = windowsRef.current;
    const target = signature(items);
    const existing = list.find((w) => signature(w.items) === target);
    if (existing) {
      setFocusedWindowId(existing.id);
      if (existing.minimized) {
        setWindows((prev) => prev.map((w) => (w.id === existing.id ? { ...w, minimized: false } : w)));
      }
      return;
    }
    createWindow(items, opts);
  }, [createWindow]);

  const windowOpenNew = useCallback((items: WindowItem[], opts: { title?: string; kind?: WindowKind; maximize?: boolean } = {}) => {
    if (!items.length) return;
    createWindow(items, opts);
  }, [createWindow]);

  const windowClose = useCallback((id: string) => {
    if (!window.dispatchEvent(new CustomEvent("apex:window-before-close", { cancelable: true, detail: { id } }))) return false;
    const closed = windowsRef.current.find((w) => w.id === id);
    const next = windowsRef.current.filter((w) => w.id !== id);
    setWindows(next);
    if (focusedWindowIdRef.current === id) {
      // Focus the last remaining window on the same desktop, not just any window.
      const fallback = [...next].reverse().find((w) => w.desktop === (closed?.desktop ?? activeDesktopRef.current));
      setFocusedWindowId(fallback ? fallback.id : null);
    }
    return true;
  }, []);

  const windowCloseAll = useCallback(() => {
    if (!window.dispatchEvent(new CustomEvent("apex:window-before-close", { cancelable: true, detail: {} }))) return;
    setWindows([]);
    setFocusedWindowId(null);
  }, []);

  const windowFocus = useCallback((id: string) => {
    const target = findWindow(id);
    if (!target) return;
    // Linux-style: activating a window on another desktop switches to it.
    if (target.desktop !== activeDesktopRef.current) setActiveDesktop(target.desktop);
    if (target.minimized) {
      setWindows((prev) => prev.map((w) => (w.id === id ? { ...w, minimized: false } : w)));
    }
    setFocusedWindowId(id);
  }, []);

  const windowToggleMaximize = useCallback((id: string) => {
    setWindows((prev) => prev.map((w) => (w.id === id ? { ...w, maximized: !w.maximized } : w)));
    setFocusedWindowId(id);
  }, []);

  const windowToggleMinimize = useCallback((id: string) => {
    setWindows((prev) => {
      const target = prev.find((w) => w.id === id);
      if (!target) return prev;
      const wasMinimized = target.minimized;
      const next = prev.map((w) => (w.id === id ? { ...w, minimized: !w.minimized } : w));
      const focusId = focusedWindowIdRef.current;
      if (!wasMinimized && focusId === id) {
        const fallback = next.filter((w) => !w.minimized && w.desktop === target.desktop);
        setFocusedWindowId(fallback.length ? fallback[fallback.length - 1].id : null);
      } else if (wasMinimized) {
        setFocusedWindowId(id);
      }
      return next;
    });
  }, []);

  const windowArrange = useCallback((arrangement: WindowArrangement) => {
    // Arrange only the windows of the active desktop, like a workspace-aware
    // window manager; windows on other desktops keep their own layout.
    const list = onDesktop(windowsRef.current, activeDesktopRef.current);
    if (!list.length) return;
    const rects = layoutRects(arrangement, list.length, window.innerWidth, window.innerHeight);
    setWindows((prev) => prev.map((w) => {
      const i = list.findIndex((l) => l.id === w.id);
      return i < 0 ? w : { ...w, rect: rects[i], maximized: false, minimized: false };
    }));
  }, []);

  /* ---------- virtual desktops ---------- */

  const desktopSet = useCallback((n: number) => {
    const target = Math.max(0, Math.min(3, Math.round(n || 0)));
    if (target === activeDesktopRef.current) return;
    setActiveDesktop(target);
    // Focus the top visible window on the target desktop (Linux behavior).
    const fallback = [...windowsRef.current].reverse().find((w) => w.desktop === target && !w.minimized);
    setFocusedWindowId(fallback ? fallback.id : null);
  }, []);

  const desktopNext = useCallback(() => {
    desktopSet((activeDesktopRef.current + 1) % 4);
  }, [desktopSet]);

  const desktopPrev = useCallback(() => {
    desktopSet((activeDesktopRef.current + 3) % 4);
  }, [desktopSet]);

  const windowMoveToDesktop = useCallback((id: string, n: number) => {
    const desktop = Math.max(0, Math.min(3, Math.round(n || 0)));
    setWindows((prev) => prev.map((w) => (w.id === id ? { ...w, desktop } : w)));
  }, []);

  const windowNext = useCallback(() => {
    const id = focusedWindowIdRef.current;
    if (!id) return;
    const current = windowsRef.current.find((w) => w.id === id);
    if (current && current.items.length > 1 && isNotepadWindow(current)
        && !window.dispatchEvent(new CustomEvent("apex:window-before-close", { cancelable: true, detail: { id } }))) return;
    setWindows((prev) => prev.map((w) =>
      w.id === id && w.items.length > 1 ? { ...w, index: (w.index + 1) % w.items.length } : w,
    ));
  }, []);

  const windowPrevious = useCallback(() => {
    const id = focusedWindowIdRef.current;
    if (!id) return;
    const current = windowsRef.current.find((w) => w.id === id);
    if (current && current.items.length > 1 && isNotepadWindow(current)
        && !window.dispatchEvent(new CustomEvent("apex:window-before-close", { cancelable: true, detail: { id } }))) return;
    setWindows((prev) => prev.map((w) =>
      w.id === id && w.items.length > 1 ? { ...w, index: (w.index - 1 + w.items.length) % w.items.length } : w,
    ));
  }, []);

  const windowSetNote = useCallback((id: string, note: string) => {
    setWindows((prev) => prev.map((w) => (w.id === id ? { ...w, note } : w)));
  }, []);

  const windowToggleNotes = useCallback((id: string) => {
    setWindows((prev) => prev.map((w) => (w.id === id ? { ...w, showNotes: !w.showNotes } : w)));
  }, []);

  const windowUpdate = useCallback((id: string, patch: Partial<Pick<AppWindow, "rect" | "maximized" | "minimized">>) => {
    setWindows((prev) => prev.map((w) => (w.id === id ? { ...w, ...patch } : w)));
  }, []);

  /* Show a terminal that already exists server-side (the model opened one for
   * itself) in a desktop window bound to that PTY session. */
  const attachTerminalWindow = useCallback((sessionId: string) => {
    if (!sessionId) return;
    windowOpen([{ url: terminalUrl(sessionId), title: "Terminal" }], { kind: "terminal" });
  }, [windowOpen]);

  /* Create host terminal session(s) and open them in desktop windows. Returns
   * an error message on failure so executeLocalCommand can speak/echo it.
   * `count` > 1 backs "open 4 terminals": the sessions are created first, so a
   * failure part-way through does not leave stray windows behind. */
  const openTerminalWindow = useCallback(async (count = 1): Promise<string | null> => {
    /* Path only: apiFetch prepends BASE and owns the CSRF token. */
    const url = "/api/terminal/session";
    const wanted = Math.max(1, Math.min(Math.floor(count) || 1, 10));
    const sessions: string[] = [];
    try {
      for (let i = 0; i < wanted; i++) {
        const res = await apiFetch(url, {
          method: "POST",
          body: JSON.stringify({ rows: 24, cols: 80 }),
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          const message = (data?.error as string) ?? `Could not open a terminal (${res.status}).`;
          if (!sessions.length) return message;
          break;   // keep the terminals that did open
        }
        const data = await res.json();
        if (data?.terminal_id) sessions.push(data.terminal_id);
      }
      for (const sessionId of sessions) attachTerminalWindow(sessionId);
      return null;
    } catch (err: any) {
      return String(err?.message ?? err);
    }
  }, [attachTerminalWindow]);

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
      windowOpen(images.map((img: any) => ({ url: img.url, title: img.name })), { kind: "image", maximize: false });
      speakRef.current(query
        ? localize(`Found ${images.length} images for ${query}`, `Βρέθηκαν ${images.length} εικόνες για ${query}`)
        : localize(`Found ${images.length} images`, `Βρέθηκαν ${images.length} εικόνες`));
    } catch (err: any) {
      speakRef.current(err?.message || localize("Could not open image browser", "Δεν ήταν δυνατό το άνοιγμα των εικόνων"));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

  // Auto-open desktop windows for file/image links in assistant replies — for
  // both voice and typed turns. Only reacts to brand-new messages so opening
  // an old conversation does not pop windows from history. Signed editor/files/
  // shell tokens hide their real filename behind the token, so the backend is
  // asked to classify them (docx/xlsx/pptx/pdf/text/image) before the kind is
  // pinned — otherwise Word/Excel/shell output would fall back to a "download
  // only" card instead of an inline preview.
  const notepadReplyIds = useRef(new Set<string>());
  /* The reply whose camera frame was already opened from a `visio_frame` event.
     The frame URL is signed and has no image extension, so the link scanner
     below cannot find it in the text - and if the model does write the URL out,
     this stops the scanner opening the same snapshot a second time. */
  const visioFrameMsgRef = useRef<string | null>(null);
  const autoOpenedMsgRef = useRef<string | null>(null);
  useEffect(() => {
    const last = messages[messages.length - 1];
    if (!last || last.role !== "assistant" || last.streaming || !last.content) return;
    if (autoOpenedMsgRef.current === last.id || notepadReplyIds.current.has(last.id)) return;
    // A camera frame for this reply is already open, from its own event.
    if (visioFrameMsgRef.current === last.id) return;
    const items = collectPreviewableItems(last.content);
    if (items.length) {
      autoOpenedMsgRef.current = last.id;
      void resolvePreviewKinds(items, fetch, BASE).then((resolved) => {
        /* One window per kind, not one window for everything: a photo, a PDF
           and a Word file in a single window would all render as the first
           item's kind. Images still share one gallery window. */
        for (const group of groupItemsByKind(resolved)) {
          windowOpen(group, { kind: kindForItems(group) });
        }
      });
    }
  }, [messages, windowOpen]);

  /* ---------- voice + orb ---------- */

  const onVoicePhase = useCallback((p: VoicePhase) => {
    setOrb(p === "standby" ? "idle" : p === "awake" ? "listening" : p);
  }, []);

  const sendRef = useRef<any>(null);
  const speakRef = useRef<(text: string) => void>(() => {});

  /* One gate for every voice path. Voice is a microphone that is open at all
     times, so it must be off whenever the app is not actually in a trusted,
     unlocked, signed-in state - not just when the user toggled it off. */
  const voiceAllowed = voiceEnabled && !!user && !locked;

  const voice = useVoiceEngine({
    enabled: voiceAllowed,
    wakeWord: settings.wake_word ?? cfg?.wake_word ?? "apex",
    followUpSeconds: Number(settings.follow_up_seconds ?? cfg?.follow_up_seconds ?? 30),
    voiceName: settings.voice ?? cfg?.voice ?? "",
    responseLanguage: settings.response_language ?? cfg?.response_language ?? "en",
    onPhase: onVoicePhase,
    onCommand: (text: string) => {
      if (!userRef.current) return;
      return handleVoiceCommand(text);
    },
  });

  speakRef.current = voice.speak;
  voiceRef.current = voice;

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
  // null means a context-dependent command (such as window navigation) is not
  // applicable, so the original request can still be handled by the model.
  async function executeLocalCommand(command: NonNullable<ReturnType<typeof parseLocalCommand>>): Promise<string | null> {
    switch (command.type) {
      case "window": {
        const list = windowsRef.current;
        if (!list.length) return null;
        const pick = (): AppWindow | null => {
          if (command.target != null) return list[command.target - 1] ?? null;
          return list.find((w) => w.id === focusedWindowIdRef.current) ?? list[list.length - 1];
        };
        const nameOf = (w: AppWindow) => itemTitle(w.items[w.index] ?? w.items[0]);
        switch (command.action) {
          case "close_all":
            windowCloseAll();
            return localize("All windows closed.", "Έκλεισαν όλα τα παράθυρα.");
          case "minimize_all":
            setWindows((prev) => prev.map((w) => ({ ...w, minimized: true })));
            setFocusedWindowId(null);
            return localize("All windows minimized.", "Ελαχιστοποιήθηκαν όλα τα παράθυρα.");
          case "maximize_all":
            setWindows((prev) => prev.map((w) => ({ ...w, maximized: true, minimized: false })));
            {
              const top = [...windowsRef.current].reverse().find((w) => w.desktop === activeDesktopRef.current);
              setFocusedWindowId(top ? top.id : null);
            }
            return localize("All windows maximized.", "Μεγιστοποιήθηκαν όλα τα παράθυρα.");
          case "restore_all":
            setWindows((prev) => prev.map((w) => ({ ...w, minimized: false })));
            {
              const top = [...windowsRef.current].reverse().find((w) => w.desktop === activeDesktopRef.current);
              setFocusedWindowId(top ? top.id : null);
            }
            return localize("All windows restored.", "Επαναφέρθηκαν όλα τα παράθυρα.");
          case "close": {
            const w = pick();
            if (!w) return localize("That window is not open.", "Αυτό το παράθυρο δεν είναι ανοιχτό.");
            windowClose(w.id);
            return localize(`Closed "${nameOf(w)}".`, `Έκλεισε το «${nameOf(w)}».`);
          }
          case "focus": {
            const w = pick();
            if (!w) return localize("That window is not open.", "Αυτό το παράθυρο δεν είναι ανοιχτό.");
            windowFocus(w.id);
            return localize(`Focused "${nameOf(w)}".`, `Επιλέχθηκε το «${nameOf(w)}».`);
          }
          case "maximize": {
            const w = pick();
            if (!w) return localize("That window is not open.", "Αυτό το παράθυρο δεν είναι ανοιχτό.");
            if (!w.maximized) windowToggleMaximize(w.id);
            return localize(`Maximized "${nameOf(w)}".`, `Μεγιστοποιήθηκε το «${nameOf(w)}».`);
          }
          case "minimize": {
            const w = pick();
            if (!w) return localize("That window is not open.", "Αυτό το παράθυρο δεν είναι ανοιχτό.");
            if (!w.minimized) windowToggleMinimize(w.id);
            return localize(`Minimized "${nameOf(w)}".`, `Ελαχιστοποιήθηκε το «${nameOf(w)}».`);
          }
          case "restore": {
            const w = pick();
            if (!w) return localize("That window is not open.", "Αυτό το παράθυρο δεν είναι ανοιχτό.");
            if (w.minimized) windowToggleMinimize(w.id);
            if (w.maximized) windowToggleMaximize(w.id);
            windowFocus(w.id);
            return localize(`Restored "${nameOf(w)}".`, `Επανήλθε το «${nameOf(w)}».`);
          }
          case "arrange": {
            const arranged = onDesktop(windowsRef.current, activeDesktopRef.current).length;
            windowArrange(command.arrangement ?? "cascade");
            const names = localize(
              `arranged ${arranged} windows in ${command.arrangement ?? "cascade"} style.`,
              `διάταξα ${arranged} παράθυρα σε στυλ ${(command.arrangement ?? "cascade") === "cascade" ? "καταρράκτη" : command.arrangement}.`,
            );
            return names.replace(/^./, (c) => c.toUpperCase());
          }
          case "next":
          case "previous": {
            const w = pick();
            if (!w) return null;
            if (w.items.length < 2) return localize("There is only one item.", "Υπάρχει μόνο ένα στοιχείο.");
            if (command.action === "next") windowNext();
            else windowPrevious();
            return localize(`Showing ${nameOf(w)}.`, `Προβάλλεται: ${nameOf(w)}.`);
          }
          case "list":
            return localize(
              `Open windows: ${list.map((w, i) => `#${i + 1} ${nameOf(w)}`).join(", ")}.`,
              `Ανοιχτά παράθυρα: ${list.map((w, i) => `#${i + 1} ${nameOf(w)}`).join(", ")}.`,
            );
          case "note": {
            const w = pick();
            if (!w) return null;
            if (!command.note) return localize("What should I write in the note?", "Τι θέλετε να σημειώσω;");
            windowSetNote(w.id, command.note);
            windowToggleNotes(w.id);
            return localize(`Note added to "${nameOf(w)}".`, `Προστέθηκε σημείωση στο «${nameOf(w)}».`);
          }
          case "open":
            return null;
        }
      }
      case "terminal": {
        const terminals = windowsRef.current.filter(isTerminalWindow);
        const termWord = () => localize("Terminal", "Τερματικό");
        const nth = (n: number | undefined): AppWindow | null => {
          if (n == null) return terminals[terminals.length - 1] ?? null;
          return terminals[n - 1] ?? null;
        };
        switch (command.action) {
          /* Bulk terminal actions. Each one names the kind explicitly because
           * "restore all" is ambiguous between kinds, and the acknowledgement
           * says how many windows it touched so a voice reply is verifiable.
           * Every window of the kind is affected, on every desktop: a terminal
           * number is its position among terminals, which does not depend on
           * which desktop the window happens to be on. */
          case "close_all":
          case "minimize_all":
          case "maximize_all":
          case "restore_all": {
            if (!terminals.length) return localize("No terminal window is open.", "Δεν είναι ανοιχτό παράθυρο τερματικού.");
            const ids = terminals.map((w) => w.id);
            const count = ids.length;
            if (command.action === "close_all") ids.forEach((id) => windowClose(id));
            else if (command.action === "minimize_all") {
              setWindows((prev) => prev.map((w) => (ids.includes(w.id) ? { ...w, minimized: true } : w)));
            } else {
              // maximize and restore both clear minimized; restore also clears
              // maximized, which is the only difference between the two.
              setWindows((prev) => prev.map((w) => (ids.includes(w.id)
                ? { ...w, maximized: command.action === "maximize_all", minimized: false }
                : w)));
            }
            const plural = count === 1 ? "Terminal" : `${count} Terminals`;
            const pluralEl = count === 1 ? "Τερματικό" : `${count} Τερματικά`;
            if (command.action === "close_all") return localize(`Closed ${plural}.`, `Έκλεισε ${pluralEl}.`);
            if (command.action === "minimize_all") return localize(`Minimized ${plural}.`, `Ελαχιστοποιήθηκε ${pluralEl}.`);
            if (command.action === "maximize_all") return localize(`Maximized ${plural}.`, `Μεγιστοποιήθηκε ${pluralEl}.`);
            return localize(`Restored ${plural}.`, `Επαναφέρθηκε ${pluralEl}.`);
          }
          case "open": {
            const wanted = command.count ?? 1;
            /* Asking for more than one terminal *is* asking for them to be
               created, so `count` implies `create`. The deterministic parser
               spells both out ("open two terminals" sets create + count), but a
               count the operator gave and a create flag the model forgot is the
               same request - and reading it as "focus one" is how "show me two
               terminals" opened a single window. */
            if ((command.create || wanted > 1) && command.target == null) {
              const error = await openTerminalWindow(wanted);
              if (error) return error;
              return wanted > 1
                ? localize(`Opened ${wanted} Terminals.`, `Άνοιξα ${wanted} Τερματικά.`)
                : localize("Opened Terminal.", "Άνοιξε το Τερματικό.");
            }
            const existing = nth(command.target);
            if (existing) {
              windowFocus(existing.id);
              return localize(`Focused ${termWord()}${command.target != null ? ` ${command.target}` : ""}.`, `Επιλέχθηκε ${termWord()}${command.target != null ? ` ${command.target}` : ""}.`);
            }
            const error = await openTerminalWindow();
            if (error) return error;
            return localize("Opened Terminal.", "Άνοιξε το Τερματικό.");
          }
          case "focus": {
            if (!terminals.length) {
              const error = await openTerminalWindow();
              if (error) return error;
              return localize("Opened Terminal.", "Άνοιξε το Τερματικό.");
            }
            let w: AppWindow;
            if (command.target != null) {
              w = nth(command.target) as AppWindow;
            } else {
              const focused = windowsRef.current.find((t) => t.id === focusedWindowIdRef.current);
              w = (focused && isTerminalWindow(focused) ? focused : terminals[terminals.length - 1]) as AppWindow;
            }
            if (!w) {
              const error = await openTerminalWindow();
              if (error) return error;
              return localize("Opened Terminal.", "Άνοιξε το Τερματικό.");
            }
            windowFocus(w.id);
            return localize(`Focused ${termWord()}${command.target != null ? ` ${command.target}` : ""}.`, `Επιλέχθηκε ${termWord()}${command.target != null ? ` ${command.target}` : ""}.`);
          }
          case "close": {
            const w = nth(command.target);
            if (!w) return localize("No terminal window is open.", "Δεν είναι ανοιχτό παράθυρο τερματικού.");
            windowClose(w.id);
            return localize(`Closed ${termWord()}${command.target != null ? ` ${command.target}` : ""}.`, `Έκλεισε ${termWord()}${command.target != null ? ` ${command.target}` : ""}.`);
          }
          case "minimize":
          case "maximize":
          case "restore": {
            const w = nth(command.target);
            if (!w) return localize("No terminal window is open.", "Δεν είναι ανοιχτό παράθυρο τερματικού.");
            const which = `${termWord()}${command.target != null ? ` ${command.target}` : ""}`;
            if (command.action === "minimize" && !w.minimized) windowToggleMinimize(w.id);
            else if (command.action === "maximize" && !w.maximized) windowToggleMaximize(w.id);
            else {
              if (w.minimized) windowToggleMinimize(w.id);
              if (w.maximized) windowToggleMaximize(w.id);
              windowFocus(w.id);
            }
            if (command.action === "minimize") return localize(`Minimized ${which}.`, `Ελαχιστοποιήθηκε ${which}.`);
            if (command.action === "maximize") return localize(`Maximized ${which}.`, `Μεγιστοποιήθηκε ${which}.`);
            return localize(`Restored ${which}.`, `Επαναφέρθηκε ${which}.`);
          }
        }
        return null;
      }
      case "files": {
        const files = windowsRef.current.filter(isFilesWindow);
        const fileWord = () => localize("File Manager", "Διαχειριστής Αρχείων");
        switch (command.action) {
          case "open": {
            if (command.create) {
              // A fresh window: unique url keeps windowOpen from colliding with
              // the existing "files:" signature (it would focus it otherwise).
              windowOpen([{ url: `files:${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`, title: fileWord() }], { kind: "files" });
              return localize(`Opened a new ${fileWord()} window.`, `Ανοίχτηκε νέο παράθυρο Διαχειριστή Αρχείων.`);
            }
            const existing = files[files.length - 1];
            if (existing) {
              if (existing.minimized) windowToggleMinimize(existing.id);
              windowFocus(existing.id);
              return localize(`Focused ${fileWord()}.`, `Επιλέχθηκε ο ${fileWord()}.`);
            }
            windowOpen([{ url: "files:", title: fileWord() }], { kind: "files" });
            return localize(`Opened ${fileWord()}.`, `Ανοίχτηκε ο ${fileWord()}.`);
          }
          case "focus": {
            const w = files[files.length - 1];
            if (!w) return localize("The File Manager is not open.", "Ο Διαχειριστής Αρχείων δεν είναι ανοιχτός.");
            if (w.minimized) windowToggleMinimize(w.id);
            windowFocus(w.id);
            return localize(`Focused ${fileWord()}.`, `Επιλέχθηκε ο ${fileWord()}.`);
          }
          case "close": {
            const w = files[files.length - 1];
            if (!w) return localize("The File Manager is not open.", "Ο Διαχειριστής Αρχείων δεν είναι ανοιχτός.");
            windowClose(w.id);
            return localize(`Closed ${fileWord()}.`, `Έκλεισε ο ${fileWord()}.`);
          }
          case "minimize":
          case "maximize":
          case "restore": {
            const w = files[files.length - 1];
            if (!w) return localize("The File Manager is not open.", "Ο Διαχειριστής Αρχείων δεν είναι ανοιχτός.");
            if (command.action === "minimize" && !w.minimized) windowToggleMinimize(w.id);
            else if (command.action === "maximize" && !w.maximized) windowToggleMaximize(w.id);
            else {
              if (w.minimized) windowToggleMinimize(w.id);
              if (w.maximized) windowToggleMaximize(w.id);
              windowFocus(w.id);
            }
            if (command.action === "minimize") return localize(`Minimized ${fileWord()}.`, `Ελαχιστοποιήθηκε ο ${fileWord()}.`);
            if (command.action === "maximize") return localize(`Maximized ${fileWord()}.`, `Μεγιστοποιήθηκε ο ${fileWord()}.`);
            return localize(`Restored ${fileWord()}.`, `Επαναφέρθηκε ο ${fileWord()}.`);
          }
          /* Bulk forms, so "restore all file managers" has the same vocabulary as
           * "restore all terminals". There is normally one files window, but the
           * open action can make more, so this must not assume a single one. */
          case "close_all":
          case "minimize_all":
          case "maximize_all":
          case "restore_all": {
            if (!files.length) return localize("The File Manager is not open.", "Ο Διαχειριστής Αρχείων δεν είναι ανοιχτός.");
            const ids = files.map((w) => w.id);
            if (command.action === "close_all") ids.forEach((id) => windowClose(id));
            else if (command.action === "minimize_all") {
              setWindows((prev) => prev.map((w) => (ids.includes(w.id) ? { ...w, minimized: true } : w)));
            } else {
              setWindows((prev) => prev.map((w) => (ids.includes(w.id)
                ? { ...w, maximized: command.action === "maximize_all", minimized: false }
                : w)));
              windowFocus(ids[ids.length - 1]);
            }
            const count = ids.length;
            const plural = count === 1 ? fileWord() : `${count} File Manager windows`;
            const pluralEl = count === 1 ? "Διαχειριστές Αρχείων" : `${count} παράθυρα Διαχειριστή Αρχείων`;
            if (command.action === "close_all") return localize(`Closed ${plural}.`, `Έκλεισε ${pluralEl}.`);
            if (command.action === "minimize_all") return localize(`Minimized ${plural}.`, `Ελαχιστοποιήθηκε ${pluralEl}.`);
            if (command.action === "maximize_all") return localize(`Maximized ${plural}.`, `Μεγιστοποιήθηκε ${pluralEl}.`);
            return localize(`Restored ${plural}.`, `Επαναφέρθηκε ${pluralEl}.`);
          }
        }
        return null;
      }
      case "notepad": {
        const pads = windowsRef.current.filter(isNotepadWindow);
        const pad = pads.find((item) => item.id === focusedWindowIdRef.current)
          ?? [...pads].reverse().find((item) => item.desktop === activeDesktopRef.current) ?? pads[pads.length - 1];
        /* The bulk forms are handled here rather than falling through to the editor
         * below: an unknown NotepadAction is forwarded verbatim as an editing
         * command, so "restore all notepads" would otherwise be typed into the
         * document. They are also in the no-window guard, because opening a
         * notepad just to restore it is the opposite of what was asked. */
        if (command.action.endsWith("_all")) {
          if (!pads.length) return localize("Notepad is not open.", "Το Σημειωμάριο δεν είναι ανοιχτό.");
          const ids = pads.map((p) => p.id);
          if (command.action === "close_all") ids.forEach((padId) => windowClose(padId));
          else if (command.action === "minimize_all") {
            setWindows((prev) => prev.map((w) => (ids.includes(w.id) ? { ...w, minimized: true } : w)));
          } else {
            setWindows((prev) => prev.map((w) => (ids.includes(w.id)
              ? { ...w, maximized: command.action === "maximize_all", minimized: false }
              : w)));
            windowFocus(ids[ids.length - 1]);
          }
          const count = ids.length;
          const plural = count === 1
            ? localize("Notepad", "το Σημειωμάριο")
            : localize(`${count} Notepad windows`, `${count} παράθυρα Σημειωμάτων`);
          if (command.action === "close_all") return localize(`Closed ${plural}.`, `Κλείστηκε ${plural}.`);
          if (command.action === "minimize_all") return localize(`Minimized ${plural}.`, `Ελαχιστοποιήθηκε ${plural}.`);
          if (command.action === "maximize_all") return localize(`Maximized ${plural}.`, `Μεγεθυνθεί ${plural}.`);
          return localize(`Restored ${plural}.`, `Επαναφέρθηκε ${plural}.`);
        }
        let request: NotepadCommand = command;
        if (command.action === "command_output") {
          const messages = byConvRef.current[activeIdRef.current ?? ""] ?? [];
          const outputs = messages.flatMap((message) => message.meta?.tools ?? []).filter((tool) => !tool.running && tool.output && /terminal_command|run_command|shell|exec|vapt_run|code_run/.test(tool.name));
          const output = outputs[outputs.length - 1]?.output;
          if (!output) return "No command output is available in this conversation. Run a command first, or name the command to run and send to Notepad.";
          request = { type: "notepad", action: "write", content: output };
        }
        let id: string | undefined = command.action === "open" && command.create ? undefined : pad?.id;
        if (!id) {
          if (["close", "minimize", "maximize", "restore"].includes(request.action)) return "Notepad is not open.";
          id = createWindow([{ url: "notepad:" + Date.now(), title: "Notepad", kind: "notepad" }], { kind: "notepad" });
          if (!id) return "Could not open Notepad: close another window first.";
        }
        switch (request.action) {
          case "open": case "focus": windowFocus(id); return "Opened Notepad.";
          case "close": return windowClose(id) ? "Closed Notepad." : "Notepad remains open; closing was canceled or a save is in progress.";
          case "minimize": if (!pad?.minimized) windowToggleMinimize(id); return "Minimized Notepad.";
          case "maximize": if (!pad?.maximized) windowToggleMaximize(id); windowFocus(id); return "Maximized Notepad.";
          case "restore":
            if (pad?.minimized) windowToggleMinimize(id);
            if (pad?.maximized) windowToggleMaximize(id);
            windowFocus(id); return "Restored Notepad.";
          default:
            windowFocus(id);
            return await sendNotepadCommand(request, id);
        }
      }
      case "desktop": {
        const nameOf = (w: AppWindow) => itemTitle(w.items[w.index] ?? w.items[0]);
        switch (command.action) {
          case "switch":
            desktopSet(command.desktop);
            return localize(
              `Switched to virtual desktop ${command.desktop + 1}.`,
              `Μετάβαση στην εικονική επιφάνεια εργασίας ${command.desktop + 1}.`,
            );
          case "next": {
            const target = (activeDesktopRef.current + 1) % 4;
            desktopSet(target);
            return localize(
              `Moved to virtual desktop ${target + 1}.`,
              `Μετακίνηση στην εικονική επιφάνεια εργασίας ${target + 1}.`,
            );
          }
          case "previous": {
            const target = (activeDesktopRef.current + 3) % 4;
            desktopSet(target);
            return localize(
              `Moved to virtual desktop ${target + 1}.`,
              `Μετακίνηση στην εικονική επιφάνεια εργασίας ${target + 1}.`,
            );
          }
          case "move": {
            const list = windowsRef.current;
            if (!list.length) return localize("No windows are open.", "Δεν είναι ανοιχτό κανένα παράθυρο.");
            // Terminal moves address terminals by their own number ("move
            // terminal 2 to desktop 3"), plain window moves by window position.
            const wanted = command.terminals
              ? (command.targets ?? (command.target != null ? [command.target] : []))
                  .map((n) => terminalWindows(list)[n - 1])
                  .filter((w): w is AppWindow => !!w)
              : command.targets?.length
                ? command.targets.map((n) => list[n - 1]).filter((w): w is AppWindow => !!w)
                : [
                    (command.target != null
                      ? list[command.target - 1] ?? null
                      : list.find((x) => x.id === focusedWindowIdRef.current) ?? list[list.length - 1]) as AppWindow,
                  ];
            const targets = wanted.filter(Boolean);
            if (!targets.length) return localize("That window is not open.", "Αυτό το παράθυρο δεν είναι ανοιχτό.");
            for (const w of targets) {
              const movedFocused = w.id === focusedWindowIdRef.current;
              windowMoveToDesktop(w.id, command.desktop);
              if (movedFocused) windowFocus(w.id);
            }
            if (targets.length > 1) {
              return localize(
                `Moved ${targets.length} windows to virtual desktop ${command.desktop + 1}.`,
                `Μετακινήθηκαν ${targets.length} παράθυρα στην εικονική επιφάνεια εργασίας ${command.desktop + 1}.`,
              );
            }
            return localize(
              `Moved "${nameOf(targets[0])}" to virtual desktop ${command.desktop + 1}.`,
              `Μετακινήθηκε το «${nameOf(targets[0])}» στην εικονική επιφάνεια εργασίας ${command.desktop + 1}.`,
            );
          }
        }
        return null;
      }
      case "task": {
        const list = sortTasks(tasksRef.current);
        const noTasks = localize("You have no scheduled tasks.", "Δεν έχεις εργασίες σε αναμονή.");
        // "show tasks" both answers the question and reveals the tab, because
        // the spoken answer names rows the operator cannot otherwise see.
        if (command.action === "open") {
          setChatCollapsedState(false);
          return await sendPanelCommand({ action: "open", tab: "tasks" });
        }
        if (command.action === "list") {
          const shown = filterTasks(list, command.filter);
          setChatCollapsedState(false);
          await sendPanelCommand({ action: "open", tab: "tasks" });
          return shown.length ? describeTasks(shown, commandLanguage()) : noTasks;
        }
        const target = resolveTarget(list, command.target);
        if (!target) {
          const which = command.target === -1
            ? localize("There are no tasks to do that to.", "Δεν υπάρχουν εργασίες για να το κάνω.")
            : localize(`There is no task #${command.target}. You have ${list.length}.`,
              `Δεν υπάρχει εργασία #${command.target}. Έχεις ${list.length}.`);
          return list.length ? which : noTasks;
        }
        const name = target.title;
        switch (command.action) {
          case "show":
            return describeOneTask(target, commandLanguage());
          case "run": {
            await runTaskNow(target.id).catch(() => {});
            return localize(`Running "${name}" now. I will report back when it finishes.`,
              `Εκτελώ τώρα την «${name}». Θα σας ενημερώσω όταν τελειώσει.`);
          }
          case "pause": {
            if (!target.enabled) {
              return localize(`"${name}" is already paused.`, `Η «${name}» είναι ήδη σε παύση.`);
            }
            await updateTask(target.id, { enabled: false }).catch(() => {});
            return localize(`Paused "${name}".`, `Η «${name}» σε παύση.`);
          }
          case "resume": {
            if (target.enabled) {
              return localize(`"${name}" is already scheduled.`, `Η «${name}» είναι ήδη προγραμματισμένη.`);
            }
            await updateTask(target.id, { enabled: true }).catch(() => {});
            return localize(`Resumed "${name}".`, `Η «${name}» συνεχίστηκε.`);
          }
          case "delete": {
            await deleteTask(target.id).catch(() => {});
            return localize(`Deleted "${name}".`, `Διεγράφη η «${name}».`);
          }
        }
        return null;
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
        return localize(`Switched to ${command.skill} skill.`, `Ενεργοποιήθηκη η δεξιότητα ${command.skill}.`);

      /* Panel and chat-input commands are executed by ChatUI, which owns the
         visible state (which tab is showing, what is in the box). The provider
         only records the intent in a ref; ChatUI's effect below performs it and
         acknowledges. Keeping it out of the provider is what lets the command
         drive real UI state instead of a second, divergent copy of it. */
      case "panel": {
        // A tab change is meaningless while the panel is collapsed, so opening
        // is part of the intent, not a separate step the caller has to know.
        if (command.action === "open") setChatCollapsedState(false);
        return await sendPanelCommand({ action: command.action, tab: command.tab });
      }

      case "chatinput": {
        if (command.action === "write") setChatCollapsedState(false);
        return await sendChatInputCommand({
          action: command.action,
          text: command.text ?? "",
        });
      }

      case "lock":
        await lock();
        // No spoken confirmation: the mic is already off by the time the lock
        // screen appears, and speaking here would leak the last words through.
        return "";

      case "signout":
        await logout();
        // The mic is still on here, so this confirms the words were heard as a
        // command. The session id is revoked server-side, so a second tab cannot
        // put the old cookie back and undo this.
        return localize("Signed out.", "Αποσυνδεθήκατε.");
    }
  }

  /* ---------- the reasoning fallback ---------- */

  /* What the screen looks like, for the model to read. Rebuilt per request
   * rather than cached: a model that is told about a window which has since
   * closed will confidently address a window that is not there. */
  const uiStateSnapshot = useCallback(
    () =>
      buildUiState({
        windows: windowsRef.current,
        focusedWindowId: focusedWindowIdRef.current,
        activeDesktop: activeDesktopRef.current,
        tasks: tasksRef.current.map((task) => ({
          id: task.id,
          description: task.title,
          status: task.enabled ? "enabled" : "paused",
        })),
      }),
    [],
  );

  /* Ask the model to read an utterance the parsers did not recognise.
   *
   * Returns [] for every failure, including "the provider is down" and "that
   * was a question for the agent". Both mean the same thing to the caller -
   * send it to the agent, which is what happened before this existed - so they
   * must not be distinguishable here.
   *
   * The reply is treated as a proposal. `sanitizeActions` is the only thing that
   * decides it may run, and it checks every action against the catalogue the
   * browser just sent, so a hallucinated action is dropped instead of executed
   * and a number the screen does not show is discarded rather than aimed at
   * whatever happens to be in that slot. */
  const resolveCommandWithModel = useCallback(async (text: string): Promise<LocalCommand[]> => {
    try {
      const { actions } = await api.resolveCommand({
        text,
        language: commandLanguage(),
        catalogue: renderActionCatalogue(),
        state: renderUiState(uiStateSnapshot()),
      });
      return sanitizeActions(actions, {
        state: uiStateSnapshot(),
        utterance: text,
        language: commandLanguage(),
        skills: skillsRef.current,
      });
    } catch {
      // An offline backend, a 423 from another window locking the session, a
      // router that is switched off. None of them may stop the turn: it just
      // goes to the agent.
      return [];
    }
  }, [commandLanguage, uiStateSnapshot]);

  /* Run a resolved chain in order and answer with one sentence.
   *
   * Sequential rather than parallel because the steps of a chain interact:
   * "close the window and restore terminal 2" is wrong if the second step reads
   * the window list before the first step has changed it. A step that returns
   * null is not applicable - the window it names is not open - so it is dropped
   * and the rest still run; only an empty result means "not a local command",
   * which is the signal to send the original text to the agent.
   *
   * The reply joins the parts that said something, which is what makes a chain
   * legible out loud: two actions, one spoken sentence, in the order asked. */
  const runResolvedCommands = useCallback(
    async (commands: LocalCommand[]): Promise<string | null> => {
      if (!commands.length) return null;
      const replies: string[] = [];
      for (const command of commands) {
        const reply = await executeLocalCommand(command);
        if (reply) replies.push(reply);
      }
      if (!replies.length) return null;
      // One full stop per step, so a chain of four actions reads as four clauses
      // instead of one run-on. Trailing punctuation is trimmed first so a part
      // that already ended in a full stop does not collect two.
      return replies
        .map((reply) => reply.trim().replace(/[.\s]+$/, ""))
        .filter(Boolean)
        .map((part) => `${part}.`)
        .join(" ");
    },
    // executeLocalCommand is re-created every render on purpose: it closes over
    // live window state. Depending on it would rebuild this on every frame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

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
      // The parsers did not recognise it and the local handler could not carry
      // it out. Ask the model what it meant against the state of the screen -
      // this is where "restore all terminals" and "bring back the one I closed"
      // stop being nothing at all.
      const resolved = await resolveCommandWithModel(text);
      if (resolved.length) {
        const reply = await runResolvedCommands(resolved);
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
      let clean = text.trim();
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

        // A leading "think hard:" / "σκέψου καλά:" marker routes this single
        // turn to THINK_HARD_MODEL. Strip it before anything else so the agent
        // only ever sees the request itself.
        const thinkHard = parseThinkHard(clean, commandLanguage());
        clean = thinkHard.message;

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

        /* Nothing recognised it and no local handler could carry it out, which
         * used to mean the turn went straight to the agent. The agent has no
         * tool that can un-minimize a window, so "restore all terminals" was a
         * reply and no change. Ask the model what the utterance meant, against
         * the list of actions the browser supports and the state of the screen,
         * and run what comes back. */
        const resolved = await resolveCommandWithModel(clean);
        if (resolved.length) {
          const reply = await runResolvedCommands(resolved);
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

        // "open top on terminal 2" — not a window command: focus the named
        // terminal so the operator watches it, and hand the rest of the
        // sentence to the agent with that session pinned as focused_terminal.
        // The user's own sentence is kept in the transcript.
        let targetedTerminalId: string | null = null;
        let targetedTerminalNumber = 0;
        const target = parseTerminalTarget(clean, commandLanguage());
        if (target) {
          targetedTerminalNumber = target.target;
          const win = terminalWindows(windowsRef.current)[target.target - 1];
          if (win) {
            targetedTerminalId = win.id;
            clean = target.message;
            windowFocus(win.id);
            if (win.desktop !== activeDesktopRef.current) desktopSet(win.desktop);
          } else {
            setByConv((m) => ({
              ...m,
              [convId]: [
                ...(m[convId] ?? []),
                mkMsg("assistant", localize(`Terminal ${target.target} is not open.`, `Το τερματικό ${target.target} δεν είναι ανοιχτό.`), {
                  meta: { voice: !!opts.voice },
                }),
              ],
            }));
            setOrb("idle");
            if (opts.voice) speakRef.current(localize(`Terminal ${target.target} is not open.`, `Το τερματικό ${target.target} δεν είναι ανοιχτό.`));
            return;
          }
        }

        const asstId = `asst_${Date.now().toString(36)}_${msgSeq++}`;
        const outputInNotepad = requestsNotepadOutput(clean);
        if (outputInNotepad) notepadReplyIds.current.add(asstId);
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
        const windowBlock = windowContextBlock(windowsRef.current, focusedWindowIdRef.current, activeDesktopRef.current) + notepadContext();
        // Tell the backend which terminal window is focused so terminal_command
        // can default to it ("run this on the focused terminal").
        const focusedWin = windowsRef.current.find((w) => w.id === focusedWindowIdRef.current);
        // "open top on terminal 2": the spoken number wins over whatever is
        // focused. terminal_command then uses this session id, so the command
        // lands in the exact window the operator named.
        const targetWin = targetedTerminalId ? windowsRef.current.find((w) => w.id === targetedTerminalId) : focusedWin;
        const focusedTerminalId = (targetWin && terminalSessionId(targetWin.items[targetWin.index] ?? targetWin.items[0])) || "";
        const terminalMap = terminalWindows(windowsRef.current)
          .map((w) => terminalSessionId(w.items[w.index] ?? w.items[0]))
          .filter((id): id is string => !!id);
        const payload: any = {
          message: clean,
          ...(outputInNotepad ? { output_destination: "notepad" } : {}),
          conversation_id: convId,
          skill: opts.skill ?? skillRef.current,
          voice_mode: !!opts.voice,
          ...(windowBlock ? { window_context: windowBlock } : {}),
          ...(focusedTerminalId ? { focused_terminal: focusedTerminalId } : {}),
          // Set only when the operator named a window ("… on terminal 4"): it
          // tells the backend the request is already pinned to one session, so
          // "open" must not be read as "use a new terminal".
          ...(targetedTerminalNumber ? { terminal_target: targetedTerminalNumber } : {}),
          // The number painted in a terminal's title bar is its position in this
          // list, so the model resolves `terminal=N` to the same window the
          // operator is looking at instead of the backend's own session order.
          ...(terminalMap.length ? { terminal_map: terminalMap } : {}),
          ...(thinkHard.thinkHard ? { think_hard: true } : {}),
        };
        const model = settingsRef.current.model;
        // An explicit think-hard turn always uses THINK_HARD_MODEL, so the main
        // model is deliberately not sent for it.
        if (model && stripSystemModel(model) && !thinkHard.thinkHard) payload.model = model;

        let notepadWork = Promise.resolve();
        const notepadResults: string[] = [];
        const notepadWrites = new Set<string>();
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
            if (ev.name === "notepad_control" || (outputInNotepad && ev.name === "run_shell")) {
              try {
                const result = JSON.parse(ev.output);
                if (result.notepad_command) {
                  const command = result.notepad_command as NotepadCommand;
                  notepadReplyIds.current.add(asstId);
                  const duplicate = command.action === "write" && notepadWrites.has(command.content ?? "");
                  if (command.action === "write") notepadWrites.add(command.content ?? "");
                  if (!duplicate) notepadWork = notepadWork.then(async () => {
                    const reply = await executeLocalCommand({ ...command, type: "notepad" });
                    if (reply) notepadResults.push(reply);
                  });
                }
              } catch { /* A normal tool error has no browser command. */ }
            }
            const pendingIndex = streamedTools.findIndex((t) => t.name === ev.name && t.running);
            streamedTools = pendingIndex >= 0
              ? streamedTools.map((t, i) => i === pendingIndex ? { ...t, output: ev.output, running: false } : t)
              : [...streamedTools, { name: ev.name, output: ev.output, running: false }];
            pushAssistant({ streaming: true, content: spoken, meta: { voice: !!opts.voice, tools: streamedTools } });
          } else if (ev.type === "terminal_opened") {
            // The backend opened a PTY session for the model; show it so the
            // operator sees the commands and can answer sudo/passphrase
            // prompts by hand.
            attachTerminalWindow(ev.terminal_id);
          } else if (ev.type === "visio_frame") {
            /* "Show me what you see" is two requests: an explanation and the
               picture. The model only writes the explanation, so the frame
               arrives as its own event and is opened here. Done on the event
               rather than by scraping the reply for the URL, because the model
               paraphrasing the sentence must not be able to drop the window. */
            if (ev.url) {
              visioFrameMsgRef.current = asstId;
              windowOpen([{ url: ev.url, title: ev.title || "Camera snapshot" }], { kind: "image" });
            }
          } else if (ev.type === "task_changed") {
            // The model created, edited or deleted a task during this turn.
            // Re-read instead of patching from the event: the event says *that*
            // something changed, not the row, and a stale cache would show the
            // tab disagreeing with what the assistant just said it did.
            void refreshTasks();
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
          } else if (ev.type === "sudo_password") {
            setSudoPrompt({ reason: ev.reason });
          } else if (ev.type === "github_token") {
            setGithubPrompt({ reason: ev.reason });
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
        await notepadWork;
        if (notepadResults.length) {
          spoken += "\n\n" + notepadResults.join("\n");
          pushAssistant({ streaming: false, content: spoken });
        }


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
  const autonomousVoiceEnabled = voiceAllowed && !!user && settings.tts_enabled !== false
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
      windows,
      focusedWindowId,
      activeDesktop,
      desktopSet,
      desktopNext,
      desktopPrev,
      windowMoveToDesktop,
      chatCollapsed,
      timers,
      reminders,
      tasks,
      tasksError,
      tasksLoading,
      loadTasks,
      createTask,
      updateTask,
      deleteTask,
      runTaskNow,
      clearTaskNotice,
      operator,
      silencedUntil,
      sudoPrompt,
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
      locked,
      lockEnabled,
      systemLoginEnabled,
      oauthAvailable,
      lock,
      unlock,
      afterAuth,
      addMemory,
      removeMemory,
      searchMemory,
      refreshMemory,
      clearError,
      windowOpen,
      windowOpenNew,
      openTerminal: openTerminalWindow,
      windowClose,
      windowCloseAll,
      windowFocus,
      windowToggleMaximize,
      windowToggleMinimize,
      windowArrange,
      windowNext,
      windowPrevious,
      windowSetNote,
      windowToggleNotes,
      windowUpdate,
      setChatCollapsed,
      setTimer,
      setReminder,
      cancelTimer,
      cancelReminder,
      openImageBrowser,
      searchImages,
      declareOperator,
      silenceAutonomous,
      setSudoPassword,
      closeSudoPrompt,
      githubPrompt,
      setGithubToken,
      closeGithubPrompt,
    }),
    [loading, user, cfg, settings, conversations, activeId, messages, skill, routedSkill, skills, memory, busy, orb, voice.active, voiceEnabled, voice.error, voice.lastHeard, forceVoiceAwake, error,
     windows, focusedWindowId, activeDesktop, desktopSet, desktopNext, desktopPrev, windowMoveToDesktop, chatCollapsed, timers, reminders, operator, silencedUntil, sudoPrompt, githubPrompt, refresh, login, logout, newConversation, openConversation, deleteConversation,      sendMessage, updateSettings, setVoiceEnabled, deleteSkill,
     addMemory, removeMemory, searchMemory, refreshMemory, clearError, windowOpen, windowClose, windowCloseAll, windowFocus, windowToggleMaximize, windowToggleMinimize, windowArrange, windowNext, windowPrevious,
     windowSetNote, windowToggleNotes, windowUpdate, setChatCollapsed,
     setTimer, setReminder, cancelTimer, cancelReminder, openImageBrowser, searchImages, declareOperator, silenceAutonomous, setSudoPassword, closeSudoPrompt, setGithubToken, closeGithubPrompt],
  );

  return <ApexContext.Provider value={value}>{children}</ApexContext.Provider>;
}

/* drop the "gpt-5-codex"-style backend model aliases when a user-set model came
   from a different provider; keep simple here - the backend validates anyway */
function stripSystemModel(m: string): string {
  return m;
}
