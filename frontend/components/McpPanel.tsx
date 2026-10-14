"use client";

/* McpPanel - the MCP tab: the operator's connections to the outside world.
 *
 * One panel manages every connection kind the backend supports - MCP servers
 * (stdio / http / sse), SQL databases (PostgreSQL, MySQL, SQL Server, SQLite,
 * Oracle), REST APIs (from OpenAPI) and GraphQL endpoints. The backend is the
 * source of truth: this panel only ever sends the connection shape the routes
 * accept and renders what comes back. Secrets are never round-tripped - the
 * server returns only `credentials_set` (the *names* of the secrets it holds),
 * and the form leaves those fields blank.
 *
 * The query console is deliberately a thin pass-through: it sends whatever the
 * operator typed. Every guard (dialect validation, table allow/deny lists,
 * read-only enforcement, row caps, timeouts) lives on the server, so the same
 * request is filtered no matter whether it came from this box, the agent, or a
 * scheduled task.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type IntegrationAuditEntry,
  type IntegrationConnection,
  type IntegrationDraft,
  type IntegrationKind,
} from "../lib/api";
import { useApex } from "./ApexProvider";

const C = {
  cyan: "#00e5ff",
  gold: "#ffc861",
  red: "#ff6b6b",
  green: "#5be49b",
  line: "rgba(0,229,255,0.16)",
  text: "rgba(235,244,255,0.92)",
  dim: "rgba(170,192,215,0.5)",
  panel: "rgba(255,255,255,0.03)",
  field: "#0b1728",
  fieldLine: "#284359",
};

const card: React.CSSProperties = {
  border: `1px solid ${C.line}`,
  background: C.panel,
  borderRadius: 10,
  padding: 10,
  display: "flex",
  flexDirection: "column",
  gap: 8,
};

const input: React.CSSProperties = {
  background: C.field,
  color: "#dbeafa",
  border: `1px solid ${C.fieldLine}`,
  borderRadius: 7,
  padding: "6px 8px",
  width: "100%",
  fontFamily: "var(--font-mono)",
  fontSize: 11,
};

const button = (tone: "plain" | "cyan" | "red" = "plain"): React.CSSProperties => ({
  background: "transparent",
  border: `1px solid ${tone === "cyan" ? C.cyan : tone === "red" ? C.red : C.line}`,
  color: tone === "cyan" ? C.cyan : tone === "red" ? C.red : C.dim,
  borderRadius: 7,
  padding: "5px 9px",
  cursor: "pointer",
  fontSize: 9.5,
  fontFamily: "var(--font-mono)",
  letterSpacing: "0.08em",
  textTransform: "uppercase",
});

const backdrop: React.CSSProperties = {
  position: "fixed", inset: 0, zIndex: 200,
  display: "flex", alignItems: "center", justifyContent: "center",
  background: "radial-gradient(ellipse 90% 80% at 50% 45%, rgba(4,8,15,0.55) 0%, rgba(4,8,15,0.9) 100%)",
  backdropFilter: "blur(4px)",
};

const shell: React.CSSProperties = {
  width: "min(440px, 92vw)", padding: "22px 22px 18px", borderRadius: 16,
  background: "rgba(6,10,20,0.96)", border: "1px solid rgba(0,229,255,0.2)",
  boxShadow: "0 0 60px rgba(0,229,255,0.1), 0 18px 50px rgba(0,0,0,0.65)",
};

type Confirm = {
  title: string;
  message: string;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
};

type Field = { key: string; label: string; placeholder?: string; numeric?: boolean };

const CONFIG_FIELDS: Record<string, Field[]> = {
  mcp: [
    { key: "command", label: "command", placeholder: "npx" },
    { key: "args", label: "args (space separated)", placeholder: "-y @modelcontextprotocol/server-filesystem /tmp" },
    { key: "cwd", label: "working directory", placeholder: "optional" },
    { key: "url", label: "url (http / sse transport)", placeholder: "https://host/mcp" },
  ],
  postgres: [
    { key: "host", label: "host", placeholder: "127.0.0.1" },
    { key: "port", label: "port", placeholder: "5432", numeric: true },
    { key: "database", label: "database", placeholder: "app" },
    { key: "user", label: "user", placeholder: "readonly" },
    { key: "sslmode", label: "sslmode", placeholder: "prefer" },
  ],
  mysql: [
    { key: "host", label: "host", placeholder: "127.0.0.1" },
    { key: "port", label: "port", placeholder: "3306", numeric: true },
    { key: "database", label: "database", placeholder: "app" },
    { key: "user", label: "user", placeholder: "readonly" },
  ],
  sqlserver: [
    { key: "host", label: "host", placeholder: "127.0.0.1" },
    { key: "port", label: "port", placeholder: "1433", numeric: true },
    { key: "database", label: "database", placeholder: "app" },
    { key: "user", label: "user", placeholder: "sa" },
    { key: "driver", label: "ODBC driver", placeholder: "ODBC Driver 18 for SQL Server" },
  ],
  oracle: [
    { key: "host", label: "host", placeholder: "127.0.0.1" },
    { key: "port", label: "port", placeholder: "1521", numeric: true },
    { key: "service_name", label: "service name", placeholder: "ORCLPDB1" },
    { key: "user", label: "user", placeholder: "readonly" },
  ],
  sqlite: [{ key: "database", label: "database file", placeholder: "/path/to/data.db" }],
  rest: [
    { key: "base_url", label: "base url", placeholder: "https://api.example.com" },
    { key: "openapi_url", label: "OpenAPI document url", placeholder: "defaults to <base>/openapi.json" },
  ],
  graphql: [{ key: "url", label: "endpoint url", placeholder: "https://api.example.com/graphql" }],
};

const TRANSPORTS = ["stdio", "http", "sse"];

const OPERATIONS = ["select", "insert", "update", "delete"];

const SQL_KINDS = ["postgres", "mysql", "sqlserver", "sqlite", "oracle"];
const isSql = (kind: string) => SQL_KINDS.includes(kind);

type Draft = {
  kind: string;
  name: string;
  description: string;
  config: Record<string, string>;
  credentials: Record<string, string>;
  system_prompt: string;
  enabled: boolean;
  readonly: boolean;
  allowed_tables: string;
  denied_tables: string;
  allowed_operations: string[];
  max_rows: string;
  timeout_seconds: string;
  skills: string;
};

function emptyDraft(kind = "sqlite"): Draft {
  return {
    kind,
    name: "",
    description: "",
    config: {},
    credentials: {},
    system_prompt: "",
    enabled: true,
    readonly: true,
    allowed_tables: "",
    denied_tables: "",
    allowed_operations: ["select"],
    max_rows: "500",
    timeout_seconds: "10",
    skills: "",
  };
}

function draftFromConnection(conn: IntegrationConnection): Draft {
  const config: Record<string, string> = {};
  for (const f of CONFIG_FIELDS[conn.kind] ?? []) {
    const raw = conn.config?.[f.key];
    config[f.key] = Array.isArray(raw) ? raw.join(" ") : raw == null ? "" : String(raw);
  }
  const env = conn.config?.env;
  if (env && typeof env === "object" && !Array.isArray(env)) {
    config.env = Object.entries(env).map(([k, v]) => `${k}=${v}`).join("\n");
  }
  return {
    kind: conn.kind,
    name: conn.name,
    description: conn.description || "",
    config,
    credentials: {},
    system_prompt: conn.system_prompt || "",
    enabled: conn.enabled,
    readonly: conn.readonly,
    allowed_tables: (conn.allowed_tables || []).join(", "),
    denied_tables: (conn.denied_tables || []).join(", "),
    allowed_operations: conn.allowed_operations?.length ? conn.allowed_operations : ["select"],
    max_rows: String(conn.max_rows ?? 500),
    timeout_seconds: String(conn.timeout_seconds ?? 10),
    skills: (conn.skills || []).join(", "),
  };
}

function splitList(value: string): string[] {
  return value.split(",").map((v) => v.trim()).filter(Boolean);
}

function parseEnv(text: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq > 0) env[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1).trim();
  }
  return env;
}

function draftToPayload(draft: Draft): IntegrationDraft {
  const config: Record<string, any> = {};
  for (const f of CONFIG_FIELDS[draft.kind] ?? []) {
    const value = (draft.config[f.key] ?? "").trim();
    if (!value) continue;
    if (f.key === "args") config[f.key] = value.split(/\s+/);
    else if (f.numeric) config[f.key] = Number(value);
    else config[f.key] = value;
  }
  if (draft.kind === "mcp" && (draft.config.env ?? "").trim()) {
    const env = parseEnv(draft.config.env);
    if (Object.keys(env).length) config.env = env;
  }
  const credentials: Record<string, string> = {};
  for (const [k, v] of Object.entries(draft.credentials)) {
    if (v.trim()) credentials[k] = v.trim();
  }
  const payload: IntegrationDraft = {
    kind: draft.kind,
    name: draft.name.trim(),
    description: draft.description.trim(),
    config,
    system_prompt: draft.system_prompt,
    enabled: draft.enabled,
    readonly: draft.readonly,
    allowed_tables: splitList(draft.allowed_tables),
    denied_tables: splitList(draft.denied_tables),
    allowed_operations: draft.allowed_operations,
    max_rows: Number(draft.max_rows) || 500,
    timeout_seconds: Number(draft.timeout_seconds) || 10,
    skills: splitList(draft.skills),
  };
  if (Object.keys(credentials).length) payload.credentials = credentials;
  return payload;
}

function healthTone(status: string | null | undefined): string {
  if (status === "ok") return C.green;
  if (status === "error") return C.red;
  return C.dim;
}

export default function McpPanel() {
  const a = useApex();
  const [kinds, setKinds] = useState<IntegrationKind[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [connections, setConnections] = useState<IntegrationConnection[]>([]);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<IntegrationConnection | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyDraft());

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [consoleText, setConsoleText] = useState("");
  const [consoleResult, setConsoleResult] = useState<string>("");
  const [audit, setAudit] = useState<IntegrationAuditEntry[]>([]);
  const [confirm, setConfirm] = useState<Confirm | null>(null);

  const selected = useMemo(
    () => connections.find((c) => c.id === selectedId) ?? null,
    [connections, selectedId],
  );

  const load = useCallback(async () => {
    setError("");
    try {
      const [k, list] = await Promise.all([api.integrations.kinds(), api.integrations.list()]);
      setKinds(k.kinds);
      setEnabled(k.enabled);
      setConnections(list.connections);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read integrations.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!confirm) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setConfirm(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirm]);

  const fmt = (value: unknown): string =>
    typeof value === "string" ? value : JSON.stringify(value, null, 2);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed.");
    } finally {
      setBusy(false);
    }
  };

  const openCreate = () => {
    setEditing(null);
    setDraft(emptyDraft(kinds[0]?.kind ?? "sqlite"));
    setFormOpen(true);
    setNote("");
  };

  const openEdit = (conn: IntegrationConnection) => {
    setEditing(conn);
    setDraft(draftFromConnection(conn));
    setFormOpen(true);
    setNote("");
  };

  const submit = () => void run(async () => {
    const payload = draftToPayload(draft);
    if (!payload.name) {
      setError("A name is required.");
      return;
    }
    if (editing) await api.integrations.update(editing.id, payload);
    else await api.integrations.create(payload);
    setFormOpen(false);
    setNote(editing ? "Connection saved." : "Connection added.");
    await load();
  });

  const remove = (conn: IntegrationConnection) => setConfirm({
    title: "Delete connection",
    message: `Delete "${conn.name}"? Its stored credentials are removed permanently.`,
    confirmLabel: "Delete",
    danger: true,
    onConfirm: () => void run(async () => {
      await api.integrations.remove(conn.id);
      if (selectedId === conn.id) setSelectedId(null);
      await load();
    }),
  });

  const toggleEnabled = (conn: IntegrationConnection) => void run(async () => {
    await api.integrations.update(conn.id, { enabled: !conn.enabled });
    await load();
  });

  const clearSecrets = (conn: IntegrationConnection) => setConfirm({
    title: "Clear stored credentials",
    message: `Forget the stored credentials for "${conn.name}"? This cannot be undone.`,
    confirmLabel: "Clear secrets",
    danger: true,
    onConfirm: () => void run(async () => {
      await api.integrations.update(conn.id, { credentials_clear: conn.credentials_set });
      setNote("Stored credentials cleared.");
      await load();
    }),
  });

  const showResult = (id: string, value: string) => {
    setSelectedId(id);
    setAudit([]);
    setConsoleResult(value);
  };

  const test = (conn: IntegrationConnection) => void run(async () => {
    const { health } = await api.integrations.test(conn.id);
    showResult(conn.id, `health: ${health.last_status}${health.last_error ? ` — ${health.last_error}` : ""}`);
    await load();
  });

  const discover = (conn: IntegrationConnection) => void run(async () => {
    const { discovery } = await api.integrations.discover(conn.id, true);
    showResult(conn.id, fmt(discovery));
  });

  const runQuery = (conn: IntegrationConnection) => void run(async () => {
    const text = consoleText.trim();
    if (!text) return;
    let payload: Record<string, any>;
    const sql = isSql(conn.kind);
    if (sql) payload = { sql: text };
    else if (conn.kind === "graphql") payload = { query: text };
    else {
      payload = { tool: text, arguments: {} };
      // "tool {json}" lets a REST/MCP call carry arguments without a second box.
      const brace = text.indexOf("{");
      if (brace > 0 && text.endsWith("}")) {
        try {
          payload = { tool: text.slice(0, brace).trim(), arguments: JSON.parse(text.slice(brace)) };
        } catch {
          /* keep the plain tool name */
        }
      }
    }
    const result = await api.integrations.query(conn.id, payload);
    showResult(conn.id, fmt(result));
  });

  const ask = (conn: IntegrationConnection) => void run(async () => {
    const text = consoleText.trim();
    if (!text) return;
    const result = await api.integrations.ask(conn.id, text, true);
    showResult(conn.id, fmt(result));
  });

  const loadAudit = (conn: IntegrationConnection) => void run(async () => {
    const { audit: entries } = await api.integrations.audit(conn.id, 50);
    setSelectedId(conn.id);
    setAudit(entries);
  });

  const kindSecrets = kinds.find((k) => k.kind === draft.kind)?.secrets ?? [];
  const configFields = CONFIG_FIELDS[draft.kind] ?? [];
  const isSqlKind = isSql(draft.kind);

  return (
    <div className="apex-scroll" style={{ padding: 12, overflowY: "auto", flex: 1, display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{ flex: 1, minWidth: 0, fontSize: 9.5, letterSpacing: "0.16em", color: C.dim, fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>
          Connections
        </span>
        <button type="button" style={button("cyan")} onClick={openCreate}>+ Add</button>
        <button type="button" style={button()} disabled={busy} onClick={() => void load()}>Refresh</button>
      </div>

      {!enabled && (
        <div style={{ ...card, borderColor: C.gold, color: C.gold, fontSize: 11 }}>
          Integrations are disabled on this server (<code>INTEGRATIONS_ENABLED=false</code>).
        </div>
      )}

      {error && (
        <div role="alert" style={{ ...card, borderColor: C.red, color: C.red, fontSize: 11 }}>{error}</div>
      )}
      {note && !error && (
        <div role="status" style={{ ...card, fontSize: 11, color: C.dim }}>{note}</div>
      )}

      {connections.length === 0 && enabled && (
        <div style={{ ...card, color: C.dim, fontSize: 11, textAlign: "center", padding: 20 }}>
          No connections yet. Add an MCP server, a database, or an API.
        </div>
      )}

      {connections.map((conn) => {
        const status = conn.health?.last_status ?? conn.last_status ?? null;
        const open = selectedId === conn.id;
        return (
          <div key={conn.id} style={{ ...card, opacity: conn.enabled ? 1 : 0.55 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", minWidth: 0 }}>
              <span title={status ?? "unknown"} style={{ width: 8, height: 8, borderRadius: 8, flexShrink: 0, background: healthTone(status), boxShadow: `0 0 7px ${healthTone(status)}` }} />
              <span style={{ flex: "1 1 140px", minWidth: 0, fontSize: 12, color: C.text, overflowWrap: "anywhere" }}>
                {conn.name}
                <span style={{ marginLeft: 6, fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>{conn.kind}</span>
                {!conn.enabled && <span style={{ marginLeft: 6, fontSize: 9, color: C.gold }}>disabled</span>}
                {conn.readonly && <span style={{ marginLeft: 6, fontSize: 9, color: C.cyan }}>read-only</span>}
              </span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
              <button type="button" style={button()} disabled={busy} onClick={() => test(conn)}>Test</button>
              <button type="button" style={button()} disabled={busy} onClick={() => discover(conn)}>Discover</button>
              <button type="button" style={button()} onClick={() => openEdit(conn)}>Edit</button>
              {conn.credentials_set?.length ? (
                <button type="button" style={button()} disabled={busy} onClick={() => clearSecrets(conn)}>Clear secrets</button>
              ) : null}
              <button type="button" style={button()} disabled={busy} onClick={() => toggleEnabled(conn)}>{conn.enabled ? "Disable" : "Enable"}</button>
              <button type="button" style={button("red")} disabled={busy} onClick={() => remove(conn)}>Delete</button>
            </div>

            {conn.health?.last_error && (
              <div style={{ fontSize: 10, color: C.red }}>{conn.health.last_error}</div>
            )}

            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <textarea
                style={{ ...input, minHeight: 54, resize: "vertical" }}
                placeholder={
                  isSql(conn.kind) ? "SELECT ... — guarded by the connection's read-only policy"
                  : conn.kind === "graphql" ? "{ users { id name } }"
                  : conn.kind === "rest" ? "operationId   or   operationId {\"arg\":\"value\"}"
                  : "mcp tool   or   tool {\"arg\":\"value\"}"
                }
                value={open ? consoleText : ""}
                onChange={(e) => { setSelectedId(conn.id); setConsoleText(e.target.value); }}
              />
              <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                <button type="button" style={button("cyan")} disabled={busy || !open} onClick={() => runQuery(conn)}>Run</button>
                <button type="button" style={button()} disabled={busy || !open} onClick={() => ask(conn)}>Ask (NL)</button>
                <button type="button" style={button()} disabled={busy || !open} onClick={() => loadAudit(conn)}>Audit</button>
                {conn.skills?.length ? (
                  <span style={{ fontSize: 9, color: C.dim, fontFamily: "var(--font-mono)", overflowWrap: "anywhere" }}>skills: {conn.skills.join(", ")}</span>
                ) : null}
              </div>
            </div>

            {open && consoleResult && (
              <pre className="apex-scroll" style={{ margin: 0, maxHeight: 240, overflow: "auto", fontSize: 10.5, lineHeight: 1.45, color: C.text, background: "rgba(0,0,0,0.25)", borderRadius: 7, padding: 8 }}>
                {consoleResult}
              </pre>
            )}

            {open && audit.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                {audit.map((entry) => (
                  <div key={entry.id} style={{ display: "flex", gap: 8, fontSize: 10, color: entry.ok ? C.dim : C.red, fontFamily: "var(--font-mono)" }}>
                    <span style={{ width: 60, flexShrink: 0 }}>{new Date(entry.ts * 1000).toLocaleTimeString()}</span>
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{entry.detail || entry.kind}</span>
                    <span>{entry.rows > 0 ? `${entry.rows}r` : ""}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}

      {formOpen && (
        <div style={{ ...card, borderColor: C.cyan, gap: 9 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ flex: 1, fontSize: 9.5, letterSpacing: "0.16em", color: C.cyan, fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>
              {editing ? `Edit ${editing.name}` : "New connection"}
            </span>
            <button type="button" style={button()} onClick={() => setFormOpen(false)}>Cancel</button>
          </div>

          <label style={{ display: "grid", gap: 4 }}>Name
            <input style={input} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
          </label>

          <label style={{ display: "grid", gap: 4 }}>Kind
            <select style={input} value={draft.kind} disabled={!!editing}
              onChange={(e) => setDraft({ ...draft, kind: e.target.value, config: {}, credentials: {} })}>
              {kinds.map((k) => <option key={k.kind} value={k.kind}>{k.kind}</option>)}
            </select>
          </label>

          <label style={{ display: "grid", gap: 4 }}>Description
            <input style={input} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
          </label>

          {draft.kind === "mcp" && (
            <label style={{ display: "grid", gap: 4 }}>Transport
              <select style={input} value={draft.config.transport ?? "stdio"}
                onChange={(e) => setDraft({ ...draft, config: { ...draft.config, transport: e.target.value } })}>
                {(kinds.find((k) => k.kind === "mcp")?.transports ?? TRANSPORTS).map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
          )}

          {configFields.map((f) => (
            <label key={f.key} style={{ display: "grid", gap: 4 }}>{f.label}
              <input style={input} placeholder={f.placeholder} spellCheck={false} autoComplete="off"
                value={draft.config[f.key] ?? ""}
                onChange={(e) => setDraft({ ...draft, config: { ...draft.config, [f.key]: e.target.value } })} />
            </label>
          ))}

          {draft.kind === "mcp" && (
            <label style={{ display: "grid", gap: 4 }}>Environment for a stdio server (KEY=value per line; values are stored plaintext in the config)
              <textarea style={{ ...input, minHeight: 50, resize: "vertical" }} spellCheck={false} autoComplete="off"
                placeholder={"API_KEY=...\nLOG_LEVEL=info"}
                value={draft.config.env ?? ""}
                onChange={(e) => setDraft({ ...draft, config: { ...draft.config, env: e.target.value } })} />
            </label>
          )}

          {kindSecrets.map((secret) => {
            const stored = !!editing?.credentials_set?.includes(secret);
            return (
              <label key={secret} style={{ display: "grid", gap: 4 }}>{secret}
                <input style={input} type="password" autoComplete="new-password" spellCheck={false}
                  placeholder={stored ? "stored — type to replace" : "not set"}
                  value={draft.credentials[secret] ?? ""}
                  onChange={(e) => setDraft({ ...draft, credentials: { ...draft.credentials, [secret]: e.target.value } })} />
              </label>
            );
          })}

          <label style={{ display: "grid", gap: 4 }}>System prompt (added to the agent's context for this connection)
            <textarea style={{ ...input, minHeight: 50, resize: "vertical" }} value={draft.system_prompt}
              onChange={(e) => setDraft({ ...draft, system_prompt: e.target.value })} />
          </label>

          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", fontSize: 11, color: C.text }}>
            <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> enabled
            </label>
            <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={draft.readonly} onChange={(e) => setDraft({ ...draft, readonly: e.target.checked })} /> read-only
            </label>
          </div>

          {!draft.readonly && (
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: 11 }}>
              {OPERATIONS.map((op) => (
                <label key={op} style={{ display: "flex", gap: 5, alignItems: "center" }}>
                  <input type="checkbox" checked={draft.allowed_operations.includes(op)}
                    onChange={(e) => setDraft({
                      ...draft,
                      allowed_operations: e.target.checked
                        ? [...draft.allowed_operations, op]
                        : draft.allowed_operations.filter((x) => x !== op),
                    })} />
                  {op}
                </label>
              ))}
            </div>
          )}

          <div style={{ display: "flex", gap: 8 }}>
            <label style={{ display: "grid", gap: 4, flex: 1 }}>Max rows
              <input style={input} inputMode="numeric" value={draft.max_rows} onChange={(e) => setDraft({ ...draft, max_rows: e.target.value })} />
            </label>
            <label style={{ display: "grid", gap: 4, flex: 1 }}>Timeout (s)
              <input style={input} inputMode="numeric" value={draft.timeout_seconds} onChange={(e) => setDraft({ ...draft, timeout_seconds: e.target.value })} />
            </label>
          </div>

          {isSqlKind && (
            <>
              <label style={{ display: "grid", gap: 4 }}>Allowed tables (blank = all, comma separated)
                <input style={input} value={draft.allowed_tables} onChange={(e) => setDraft({ ...draft, allowed_tables: e.target.value })} />
              </label>
              <label style={{ display: "grid", gap: 4 }}>Denied tables (comma separated)
                <input style={input} value={draft.denied_tables} onChange={(e) => setDraft({ ...draft, denied_tables: e.target.value })} />
              </label>
            </>
          )}

          <label style={{ display: "grid", gap: 4 }}>Limit to skills (blank = every skill, comma separated)
            <input style={input} list="apex-integration-skills" value={draft.skills} onChange={(e) => setDraft({ ...draft, skills: e.target.value })} />
            <datalist id="apex-integration-skills">
              {a.skills.map((s) => <option key={s.name} value={s.name} />)}
            </datalist>
          </label>

          <button type="button" style={button("cyan")} disabled={busy} onClick={submit}>{editing ? "Save changes" : "Add connection"}</button>
        </div>
      )}

      {confirm && (
        <div role="dialog" aria-modal="true" aria-label={confirm.title}
          onMouseDown={(e) => { if (e.target === e.currentTarget) setConfirm(null); }}
          style={backdrop}>
          <div style={shell}>
            <div style={{ fontSize: 11, letterSpacing: "0.16em", textTransform: "uppercase", fontFamily: "var(--font-mono)", color: confirm.danger ? C.red : C.cyan, marginBottom: 12 }}>
              {confirm.title}
            </div>
            <div style={{ fontSize: 13, lineHeight: 1.5, color: C.text, marginBottom: 20 }}>
              {confirm.message}
            </div>
            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
              <button type="button" style={button()} onClick={() => setConfirm(null)}>Cancel</button>
              <button type="button" autoFocus style={confirm.danger ? button("red") : button("cyan")}
                onClick={() => { const fn = confirm.onConfirm; setConfirm(null); fn(); }}>
                {confirm.confirmLabel}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
