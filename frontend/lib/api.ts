/* APEX frontend <-> Flask backend client.

   In dev the Next.js server rewrites /be/api/* -> http://127.0.0.1:5001/api/*
   (see next.config.mjs). For a split deployment set NEXT_PUBLIC_API_URL to
   the backend origin + /api (e.g. https://apex.example.com/api).
*/

export const BASE =
  process.env.NEXT_PUBLIC_API_URL ?? (typeof window !== "undefined" ? "/be" : "http://127.0.0.1:5001/api");

const CRED: RequestInit["credentials"] = "include";

/* CSRF ------------------------------------------------------------------
   The session cookie is the only thing identifying the caller, and a browser
   will attach it to a cross-site POST. The backend therefore requires a token
   that only same-origin code can read, sent as X-APEX-CSRF on every
   state-changing request. It is minted by GET /api/auth/status and refreshed
   after login, unlock and lock, because the server rotates the session id at
   those points. */

let csrfToken = "";

export function setCsrfToken(token: string | null | undefined): void {
  csrfToken = typeof token === "string" ? token : "";
}

export function getCsrfToken(): string {
  return csrfToken;
}

/* A session can be locked from another tab or window, and the server is what
   decides. The API layer is the one place that sees the 423, so it notifies
   here instead of every caller having to recognise the status. */
type SessionLockedHandler = () => void;
let sessionLockedHandler: SessionLockedHandler | null = null;

export function onSessionLocked(handler: SessionLockedHandler | null): void {
  sessionLockedHandler = handler;
}

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function requestHeaders(init: RequestInit = {}): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init.headers as Record<string, string>) ?? {}),
  };
  const method = (init.method ?? "GET").toUpperCase();
  if (csrfToken && UNSAFE_METHODS.has(method)) headers["X-APEX-CSRF"] = csrfToken;
  return headers;
}

/* Ask the server for the current token. The server is the only place a valid
   token exists: it is derived from the session id, which rotates on lock,
   unlock, login and on every backend restart. */
let refreshing: Promise<string> | null = null;

export function refreshCsrfToken(): Promise<string> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const resp = await fetch(`${BASE}/api/auth/status`, { credentials: CRED });
        if (!resp.ok) return csrfToken;
        const body: any = await resp.json().catch(() => null);
        if (typeof body?.csrf_token === "string" && body.csrf_token) {
          setCsrfToken(body.csrf_token);
        }
      } catch {
        /* keep whatever we had */
      }
      return csrfToken;
    })().finally(() => {
      refreshing = null;
    });
  }
  return refreshing;
}

/* The one place a state-changing request is allowed to happen.

   Every browser API that mutates something must go through this (or send
   requestHeaders), because the server has no other way to tell it apart from a
   cross-site POST. Hand-rolled fetch() calls that skipped this were rejected
   with invalid_csrf_token - the file manager could not open a file and the
   terminal could not take input.

   A token can go stale between the caller building the request and the server
   checking it: the session id rotates on lock/unlock/login, and it is gone
   after a backend restart, which silently invalidates every token. So one
   403 invalid_csrf_token is retried once with a freshly minted token instead of
   being surfaced as a failure that also leaves the app holding no token at all.
   The retry is not recursive: a second rejection is a real rejection. */
export async function apiFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const send = () =>
    fetch(`${BASE}${url}`, {
      ...init,
      credentials: init.credentials ?? CRED,
      headers: requestHeaders(init),
    });
  const resp = await send();
  if (resp.status !== 403 || !UNSAFE_METHODS.has((init.method ?? "GET").toUpperCase())) {
    return resp;
  }
  const body: any = await resp.clone().json().catch(() => null);
  if (body?.error !== "invalid_csrf_token") return resp;
  const fresh = await refreshCsrfToken();
  if (!fresh || fresh === csrfToken) return resp;
  return send();
}

async function json<T = any>(url: string, init: RequestInit = {}): Promise<T> {
  const resp = await apiFetch(url, init);
  if (resp.status === 401) throw new ApiError("unauthorized", 401);
  if (resp.status === 403) {
    const body: any = await resp.json().catch(() => null);
    if (body?.error === "invalid_csrf_token") {
      // A second rejection after the retry: keep the token empty so the next
      // request re-mints rather than failing forever with a known-bad one.
      setCsrfToken("");
      throw new ApiError("invalid_csrf_token", 403);
    }
    throw new ApiError(body?.error ?? "forbidden", 403, retryAfterFrom(body));
  }
  if (resp.status === 423) {
    // The session is locked server-side. The client may not know yet - another
    // tab or window can have locked it - so the error carries enough detail to
    // show the lock screen instead of a bare failure.
    const body: any = await resp.json().catch(() => null);
    try {
      sessionLockedHandler?.();
    } catch {
      // A notification failure must not replace the real error.
    }
    throw new ApiError(body?.error ?? "session_locked", 423);
  }
  if (!resp.ok) {
    const body: any = await resp.json().catch(() => null);
    throw new ApiError(
      body?.error ?? `${resp.status} ${resp.statusText}`,
      resp.status,
      retryAfterFrom(body),
    );
  }
  return resp.json();
}

/* The throttle's countdown travels in the body, and the UI needs it to tell the
   operator how long to wait. A thrown Error that drops the field means the
   screen can only say "wrong password" and the user retries into the lockout. */
function retryAfterFrom(body: any): number {
  const value = body?.retry_after ?? body?.retryAfter;
  return Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : 0;
}

export class ApiError extends Error {
  status: number;
  retryAfter: number;
  constructor(message: string, status: number, retryAfter = 0) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

/* ---------- payload types ---------- */
export type User = {
  id: string;
  name: string;
  email: string;
  picture: string;
  oauth: boolean;
  uses_oauth_token: boolean;
  scopes: string[];
  refresh: boolean;
};

export type Skill = {
  name: string;
  description: string;
  builtin: boolean;
  tools: string[];
  model: string;
};

export type ProviderStatus = {
  codex: { available: boolean };
  openai: { available: boolean };
  ollama: { available: boolean; models: string[] };
  torch: { available: boolean };
};

export type AppConfig = {
  ok: boolean;
  oauth_configured: boolean;
  logged_in: boolean;
  engine: string;
  provider: string;
  providers: ProviderStatus;
  engines: string[];
  models: string[];
  /** Model APEX uses for one turn when the user asks it to think hard. */
  think_hard_model: string;
  /** Whether the think-hard switch is armed (env THINK_HARD_MODEL_ENABLED). */
  think_hard_model_enabled: boolean;
  memory_enabled: boolean;
  embedding: string | null;
  wake_word: string;
  follow_up_seconds: number;
  voice: string;
  response_language: string;
  /** Operator-authored persona appended to every system prompt. */
  soul: string;
  /** Server-side cap for `soul`; longer text is truncated on save. */
  soul_max_chars: number;
  autonomous_mode: boolean;
  humor_level: number;
  sarcasm_level: number;
  autonomous_voice_budget: number;
  skills: Skill[];
};

export type ChatEventMeta = {
  type: "meta";
  conversation_id: string;
  engine: string;
  provider: string;
  model: string;
  skill: string;
  voice: boolean;
};
export type ChatEvent =
  | ChatEventMeta
  | { type: "text_delta"; content: string }
  | { type: "tool_call"; name: string; id: string; arguments: any }
  | { type: "tool_result"; name: string; output: string }
  | { type: "memory"; action: string; detail: any }
  | { type: "terminal_opened"; terminal_id: string }
  | { type: "skills_changed" }
  | { type: "sudo_password"; reason?: string }
  | { type: "github_token"; reason?: string }
  | { type: "done"; usage?: any }
  | { type: "error"; message: string }
  | { type: "end"; ok: boolean };

export type Conversation = {
  id: string;
  title: string;
  skill: string;
  engine: string;
  created_at: number;
  updated_at: number;
  messages: number;
};

export type MemoryEntry = {
  id: string;
  text: string;
  meta: { category?: string; created_at?: number; conversation_id?: string };
  score?: number | null;
};

/* ---------- endpoints ---------- */
/* ---------- system-user auth ---------- */
export type SystemUserLite = { username: string; name: string; initials: string; shell: string };

export type AuthStatus = {
  ok: boolean;
  system_login_enabled: boolean;
  lock_enabled: boolean;
  authenticated: boolean;
  locked: boolean;
  oauth_available: boolean;
  csrf_token: string;
};

export type LoginResult = { ok: boolean; user: User; csrf_token: string };
export type LoginError = { error: string; retry_after?: number };

/* The token is cached by the module, so a page that never calls status()
   (impossible in practice, but cheap to be safe about) still fails closed
   rather than sending a request with no protection and no explanation. */
function adoptToken(token: string | undefined): void {
  if (token) setCsrfToken(token);
}

export const auth = {
  status: async (): Promise<AuthStatus> => {
    const data = await json<AuthStatus>("/api/auth/status");
    adoptToken(data.csrf_token);
    return data;
  },

  systemUsers: () =>
    json<{ ok: boolean; enabled: boolean; users: SystemUserLite[] }>("/api/auth/system-users"),

  login: async (username: string, password: string): Promise<LoginResult> => {
    const data = await json<LoginResult>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    adoptToken(data.csrf_token);
    return data;
  },

  unlock: async (password: string): Promise<{ ok: boolean; csrf_token: string }> => {
    const data = await json<{ ok: boolean; csrf_token: string }>("/api/auth/unlock", {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    adoptToken(data.csrf_token);
    return data;
  },

  lock: async (): Promise<{ ok: boolean; csrf_token: string }> => {
    /* The cached token has to travel *with* this request: the server checks it
       before it will lock anything. The response carries the replacement token
       because the server rotates the session id, and the very next call the
       lock screen makes is the unlock POST, which is itself protected. Clearing
       the token first, or not adopting the new one, makes unlock impossible. */
    const data = await json<{ ok: boolean; csrf_token: string }>("/api/auth/lock", { method: "POST" });
    adoptToken(data.csrf_token);
    return data;
  },

  logout: async () => {
    /* Same ordering rule as lock: send the token, then forget it. Dropping it
       first turns every logout into a 403 and leaves the server session alive,
       so a refresh would sign the operator straight back in. */
    try {
      return await json<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
    } finally {
      setCsrfToken("");
    }
  },
};

export const api = {
  me: () => json<{ ok: boolean; user: User | null; settings: Record<string, any>; engine: string; provider: string }>("/api/me"),

  config: () => json<AppConfig>("/api/config"),

  login: () => {
    window.location.href = `${BASE}/api/oauth/start?next=${encodeURIComponent(window.location.href)}`;
  },
  logout: () => json("/api/logout"),

  skills: {
    list: () => json<Skill[]>("/api/skills"),
    delete: (name: string) => json<{ ok: boolean }>(`/api/skills/${encodeURIComponent(name)}`, { method: "DELETE" }),
  },

  vapt: {
    password: (payload: { password: string; save: boolean }) =>
      json<{ ok: boolean; saved: boolean; ttl_minutes: number }>("/api/vapt/password", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    clear: () => json<{ ok: boolean }>("/api/vapt/password", { method: "DELETE" }),
    status: () =>
      json<{ enabled: boolean; projects_root: string; password: string; saved: boolean }>("/api/vapt/status"),
  },

  code: {
    githubToken: (token: string) =>
      json<{ ok: boolean; login: string }>("/api/code/github-token", {
        method: "POST",
        body: JSON.stringify({ token }),
      }),
    clearGithubToken: () => json<{ ok: boolean }>("/api/code/github-token", { method: "DELETE" }),
    githubStatus: () =>
      json<{ token: string; login: string }>("/api/code/github/status"),
  },

  conversations: {
    list: () => json<Conversation[]>("/api/conversations"),
    create: (data?: { title?: string; skill?: string }) =>
      json<Conversation>("/api/conversations", { method: "POST", body: JSON.stringify(data ?? {}) }),
    messages: (id: string) =>
      json<{ conversation: Conversation; messages: { role: string; content: string; meta: any }[] }>(
        `/api/conversations/${id}/messages`,
      ),
    remove: (id: string) => json(`/api/conversations/${id}`, { method: "DELETE" }),
    summarize: (id: string) =>
      json<{ ok: boolean; status: string }>(`/api/conversations/${id}/summarize`, { method: "POST" }),
  },

  settings: {
    get: () => json("/api/settings"),
    set: (patch: Record<string, any>) =>
      json<{ ok: boolean; settings: Record<string, any> }>("/api/settings", {
        method: "POST",
        body: JSON.stringify(patch),
      }),
  },

  models: (provider?: string) =>
    json<{ models: string[] }>(`/api/models${provider ? `?provider=${encodeURIComponent(provider)}` : ""}`),

  self: {
    health: (run?: boolean) =>
      json<{ ok: boolean; health: { overall: string; checks: { label: string; ok: boolean; returncode?: number; error?: string; stdout?: string; stderr?: string }[] } }>(
        `/api/self/health${run ? "?run=1" : ""}`
      ),
  },

  notepad: {
    list: () => json<{ documents: { name: string; modified_at: number; size_bytes: number }[]; directory: string }>("/api/notepad/documents"),
    get: (name: string) => json<{ name: string; title: string; content: string; modified_at: number }>(`/api/notepad/documents/${encodeURIComponent(name)}`),
    save: (payload: { name?: string; title: string; content: string }) =>
      json<{ ok: boolean; name: string; title: string; modified_at: number; download_url: string; directory: string }>("/api/notepad/documents", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
  },

  images: {
    webSearch: (query: string, limit?: number) =>
      json<{ images: { name: string; path: string; url: string }[]; query: string; count: number; source: string }>(
        `/api/images/web_search?${new URLSearchParams({ query, limit: String(limit ?? 8) }).toString()}`
      ),
    localSearch: (query?: string, limit?: number) =>
      json<{ images: { name: string; path: string; url: string }[]; query: string; count: number; source: string }>(
        `/api/images/local_search?${new URLSearchParams({ query: query ?? "", limit: String(limit ?? 50) }).toString()}`
      ),
  },

  memory: {
    list: () => json<{ count: number; entries: MemoryEntry[] }>("/api/memory"),
    add: (text: string, category?: string) =>
      json<{ ok: boolean; id: string }>("/api/memory", { method: "POST", body: JSON.stringify({ text, category }) }),
    search: (query: string) =>
      json<MemoryEntry[]>("/api/memory/search", { method: "POST", body: JSON.stringify({ query }) }),
    remove: (ids: string[], all?: boolean) =>
      json("/api/memory", { method: "DELETE", body: JSON.stringify(all ? { all: true } : { ids }) }),
    upload: async (files: FileList | File[]) => {
      const form = new FormData();
      for (const f of files) form.append("files", f);
      const resp = await fetch(`${BASE}/api/memory/upload`, {
        method: "POST",
        credentials: CRED,
        // FormData sets its own multipart Content-Type boundary, so only the
        // CSRF header is added here.
        headers: csrfToken ? { "X-APEX-CSRF": csrfToken } : {},
        body: form,
      });
      if (resp.status === 401) throw new ApiError("unauthorized", 401);
      if (!resp.ok) {
        const body: any = await resp.json().catch(() => null);
        throw new ApiError(body?.error ?? `${resp.status} ${resp.statusText}`, resp.status);
      }
      return resp.json() as Promise<{ ok: boolean; total: number; files: { filename: string; chunks?: number; error?: string }[] }>;
    },
  },

  /** Stream a chat turn over SSE. onEvent receives parsed events. Returns the meta/conversation info. */
  async chat(
    payload: {
      message: string;
      conversation_id?: string;
      skill?: string;
      model?: string;
      voice_mode?: boolean;
      store_messages?: boolean;
    },
    onEvent: (ev: ChatEvent) => void,
  ): Promise<void> {
    const resp = await fetch(`${BASE}/api/chat`, {
      method: "POST",
      credentials: CRED,
      headers: requestHeaders({ method: "POST" }),
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const body: any = await resp.json().catch(() => null);
      throw new ApiError(body?.error ?? `chat failed (${resp.status})`, resp.status);
    }
    if (!resp.body) throw new ApiError("empty response body", 502);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const line = block.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        try {
          onEvent(JSON.parse(line.slice(6)) as ChatEvent);
        } catch {
          /* swallow partial/invalid frames */
        }
      }
    }
  },
};