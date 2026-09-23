/* APEX frontend <-> Flask backend client.

   In dev the Next.js server rewrites /be/api/* -> http://127.0.0.1:5001/api/*
   (see next.config.mjs). For a split deployment set NEXT_PUBLIC_API_URL to
   the backend origin + /api (e.g. https://apex.example.com/api).
*/

export const BASE =
  process.env.NEXT_PUBLIC_API_URL ?? (typeof window !== "undefined" ? "/be" : "http://127.0.0.1:5001/api");

const CRED: RequestInit["credentials"] = "include";

async function json<T = any>(url: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${BASE}${url}`, {
    ...init,
    credentials: CRED,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
  if (resp.status === 401) throw new ApiError("unauthorized", 401);
  if (!resp.ok) {
    const body: any = await resp.json().catch(() => null);
    throw new ApiError(body?.error ?? `${resp.status} ${resp.statusText}`, resp.status);
  }
  return resp.json();
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
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
  memory_enabled: boolean;
  embedding: string | null;
  wake_word: string;
  follow_up_seconds: number;
  voice: string;
  response_language: string;
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
  | { type: "skills_changed" }
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

  conversations: {
    list: () => json<Conversation[]>("/api/conversations"),
    create: (data?: { title?: string; skill?: string }) =>
      json<Conversation>("/api/conversations", { method: "POST", body: JSON.stringify(data ?? {}) }),
    messages: (id: string) =>
      json<{ conversation: Conversation; messages: { role: string; content: string; meta: any }[] }>(
        `/api/conversations/${id}/messages`,
      ),
    remove: (id: string) => json(`/api/conversations/${id}`, { method: "DELETE" }),
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
      const resp = await fetch(`${BASE}/api/memory/upload`, { method: "POST", credentials: CRED, body: form });
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
      headers: { "Content-Type": "application/json" },
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