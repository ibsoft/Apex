"use client";

import { useEffect, useState } from "react";
import { useApex } from "./ApexProvider";
import {
  statusTone,
  lastRunLabel,
  toolScopeLabel,
  type SubAgent,
  type SubAgentDraft,
} from "../lib/subagents";

const C = {
  cyan: "#00e5ff",
  gold: "#ffc861",
  red: "#ff6b6b",
  line: "rgba(0,229,255,0.16)",
  text: "rgba(235,244,255,0.92)",
  dim: "rgba(170,192,215,0.5)",
};

const TONE: Record<ReturnType<typeof statusTone>, string> = {
  cyan: C.cyan,
  gold: C.gold,
  red: C.red,
  dim: C.dim,
};

const emptyDraft: SubAgentDraft = { name: "", description: "", model: "", tools: [] };

export default function AgentsPanel() {
  const a = useApex();
  const [editing, setEditing] = useState<SubAgent | null>(null);
  const [draft, setDraft] = useState<SubAgentDraft>(emptyDraft);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [toolNames, setToolNames] = useState<string[]>([]);
  const [runFor, setRunFor] = useState<string | null>(null);
  const [runPrompt, setRunPrompt] = useState("");

  useEffect(() => {
    void a.loadSubagents();
    void a.subagentToolNames().then(setToolNames).catch(() => {});
    // A run is a whole agent turn; its progress events are buffered until the
    // blocking tool returns, so while this tab is open poll quietly to show a
    // run as it happens.
    const timer = window.setInterval(() => {
      void a.loadSubagents({ quiet: true });
    }, 5000);
    return () => window.clearInterval(timer);
  }, []);

  const rows = a.subagents;

  const startCreate = () => {
    setEditing(null);
    setDraft(emptyDraft);
    setProblem(null);
    setShowForm(true);
  };

  const startEdit = (agent: SubAgent) => {
    setEditing(agent);
    setDraft({
      name: agent.name,
      description: agent.description,
      role: agent.role,
      model: agent.model,
      tools: agent.tools,
      enabled: agent.enabled,
    });
    setProblem(null);
    setShowForm(true);
  };

  const save = async () => {
    setBusy(true);
    setProblem(null);
    try {
      if (editing) await a.updateSubAgent(editing.id, draft);
      else await a.createSubAgent(draft);
      setShowForm(false);
      setEditing(null);
      setDraft(emptyDraft);
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "Could not save the agent.");
    } finally {
      setBusy(false);
    }
  };

  const guard = async (work: () => Promise<unknown>) => {
    setBusy(true);
    setProblem(null);
    try {
      await work();
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="apex-scroll" style={{ padding: 12, overflowY: "auto", flex: 1, display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ fontSize: 9.5, letterSpacing: "0.14em", fontFamily: "var(--font-mono)", color: C.dim, textTransform: "uppercase" }}>
          SUB-AGENTS
        </span>
        <span style={{ marginLeft: "auto" }} />
        <button onClick={() => void a.loadSubagents()} disabled={a.subagentsLoading} style={btn(false)} title="Refresh">
          {a.subagentsLoading ? "…" : "REFRESH"}
        </button>
        <button onClick={startCreate} disabled={busy} style={btn(true)}>
          + NEW AGENT
        </button>
      </div>

      {a.subagentsError && <div style={{ ...banner, color: C.red, borderColor: "rgba(255,107,107,0.35)" }}>{a.subagentsError}</div>}
      {problem && <div style={{ ...banner, color: C.gold, borderColor: "rgba(255,200,97,0.35)" }}>{problem}</div>}

      {showForm && (
        <div style={{ ...card, borderColor: C.cyan }}>
          <div style={{ fontSize: 9.5, letterSpacing: "0.12em", color: C.cyan, fontFamily: "var(--font-mono)" }}>
            {editing ? `EDIT AGENT` : "NEW AGENT"}
          </div>
          <Field label="NAME">
            <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="Log watcher" style={input} />
          </Field>
          <Field label="DESCRIPTION" hint="One line on what the agent is for. Shown to APEX so it knows when to delegate.">
            <input value={draft.description ?? ""} onChange={(e) => setDraft({ ...draft, description: e.target.value })} placeholder="Tails logs and summarizes" style={input} />
          </Field>
          <Field label="INSTRUCTIONS" hint="Standing orders for the agent, added to its prompt on every run.">
            <textarea
              value={draft.role ?? ""}
              onChange={(e) => setDraft({ ...draft, role: e.target.value })}
              placeholder={"Watch /var/log/syslog for errors.\nReport only problems, with timestamps."}
              rows={4}
              style={{ ...input, resize: "vertical", fontFamily: "inherit" }}
            />
          </Field>
          <Field label="MODEL (empty = default)">
            <input value={draft.model ?? ""} onChange={(e) => setDraft({ ...draft, model: e.target.value })} placeholder="gpt-5" style={input} />
          </Field>
          <Field label="TOOLS" hint="None ticked = the agent may use every tool. The terminal and sub-agent tools are always excluded.">
            <div className="apex-scroll-slim" style={{ maxHeight: 150, overflow: "auto", border: `1px solid ${C.line}`, borderRadius: 7, padding: 6, display: "flex", flexWrap: "wrap", gap: 5 }}>
              {toolNames.length === 0 && <span style={{ fontSize: 9.5, color: C.dim }}>No tool list available.</span>}
              {toolNames.map((name) => {
                const on = (draft.tools ?? []).includes(name);
                return (
                  <button
                    key={name}
                    type="button"
                    onClick={() =>
                      setDraft((d) => {
                        const cur = d.tools ?? [];
                        return { ...d, tools: on ? cur.filter((t) => t !== name) : [...cur, name] };
                      })
                    }
                    style={{ ...chip, ...(on ? chipOn : null) }}
                  >
                    {name}
                  </button>
                );
              })}
            </div>
          </Field>
          <div style={{ display: "flex", gap: 8, marginTop: 2 }}>
            <button onClick={() => void save()} disabled={busy || !draft.name.trim()} style={btn(true)}>
              {busy ? "SAVING…" : editing ? "SAVE CHANGES" : "CREATE AGENT"}
            </button>
            <button onClick={() => { setShowForm(false); setEditing(null); setProblem(null); }} style={btn(false)}>
              CANCEL
            </button>
          </div>
        </div>
      )}

      {rows.length === 0 && !showForm && (
        <div style={{ padding: 26, textAlign: "center", color: C.dim, fontSize: 10, fontFamily: "var(--font-mono)", lineHeight: 1.9, letterSpacing: "0.1em" }}>
          NO SUB-AGENTS
        </div>
      )}

      {rows.map((agent) => {
        const run = agent.last_run;
        const tone = TONE[statusTone(run?.status ?? null)];
        return (
          <div key={agent.id} style={card}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ color: C.text, fontSize: 12, flex: 1, minWidth: 0, wordBreak: "break-word" }}>
                {agent.name}
              </span>
              {agent.unread > 0 && <span style={{ ...badge, color: C.gold, borderColor: "rgba(255,200,97,0.4)", background: "rgba(255,200,97,0.08)" }}>UNSEEN {agent.unread}</span>}
              <span style={{ ...badge, color: tone, borderColor: `${tone}55`, background: `${tone}12` }}>
                {run ? run.status.toUpperCase() : "IDLE"}
              </span>
            </div>
            <div style={{ margin: "7px 0 0 0", display: "flex", flexWrap: "wrap", gap: 10, fontSize: 9.5, fontFamily: "var(--font-mono)", color: C.dim }}>
              <span style={{ color: C.cyan }}>{toolScopeLabel(agent.tools)}</span>
              <span>{lastRunLabel(agent)}</span>
            </div>
            {run && (run.error || run.output) && (
              <pre className="apex-scroll-slim"
                style={{
                  margin: "8px 0 0 0", padding: 8, maxHeight: 130, overflow: "auto",
                  background: "rgba(0,0,0,0.3)", border: `1px solid ${C.line}`, borderRadius: 6,
                  fontSize: 9.5, fontFamily: "var(--font-mono)", color: run.status === "error" ? C.red : C.text,
                  whiteSpace: "pre-wrap", wordBreak: "break-word",
                }}
              >
                {(run.error || run.output || "").trim()}
              </pre>
            )}
            <div style={{ margin: "9px 0 0 0", display: "flex", flexWrap: "wrap", gap: 5 }}>
              <button onClick={() => { setRunFor(agent.id); setRunPrompt(""); setProblem(null); }} disabled={busy || !agent.enabled} style={btn(true)}>RUN NOW</button>
              <button onClick={() => startEdit(agent)} disabled={busy} style={btn(false)}>EDIT</button>
              <button onClick={() => void guard(() => a.deleteSubAgent(agent.id))} disabled={busy} style={{ ...btn(false), color: C.red, borderColor: "rgba(255,107,107,0.35)" }}>DELETE</button>
            </div>
            {runFor === agent.id && (
              <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 5 }}>
                <textarea
                  value={runPrompt}
                  onChange={(e) => setRunPrompt(e.target.value)}
                  placeholder="What should this agent do?"
                  rows={2}
                  style={{ ...input, resize: "vertical", fontFamily: "inherit" }}
                />
                <div style={{ display: "flex", gap: 5 }}>
                  <button
                    onClick={() => void guard(async () => { await a.runSubAgent(agent.id, runPrompt); setRunFor(null); setRunPrompt(""); })}
                    disabled={busy || !runPrompt.trim()}
                    style={btn(true)}
                  >
                    {busy ? "STARTING…" : "START"}
                  </button>
                  <button onClick={() => { setRunFor(null); setRunPrompt(""); }} style={btn(false)}>CANCEL</button>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

const card: React.CSSProperties = { padding: 11, borderRadius: 10, background: "rgba(255,255,255,0.03)", border: `1px solid ${C.line}`, display: "flex", flexDirection: "column", gap: 2 };
const banner: React.CSSProperties = { padding: "7px 10px", borderRadius: 8, fontSize: 10, fontFamily: "var(--font-mono)", lineHeight: 1.5, border: "1px solid transparent", background: "rgba(255,255,255,0.03)" };
const badge: React.CSSProperties = { fontSize: 8.5, letterSpacing: "0.1em", fontFamily: "var(--font-mono)", padding: "2px 7px", borderRadius: 9, border: "1px solid transparent" };
const input: React.CSSProperties = { width: "100%", padding: "6px 8px", borderRadius: 7, fontSize: 11, background: "rgba(0,0,0,0.3)", border: `1px solid ${C.line}`, color: C.text, outline: "none" };
const chip: React.CSSProperties = { padding: "3px 7px", borderRadius: 6, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 8.5, letterSpacing: "0.04em", background: "transparent", border: `1px solid ${C.line}`, color: "rgba(170,192,215,0.5)" };
const chipOn: React.CSSProperties = { background: "rgba(0,229,255,0.12)", borderColor: C.cyan, color: C.cyan };

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 8 }}>
      <span style={{ fontSize: 8.5, letterSpacing: "0.12em", color: C.dim, fontFamily: "var(--font-mono)" }}>{label}</span>
      {children}
      {hint && <span style={{ fontSize: 9, color: C.dim, lineHeight: 1.5 }}>{hint}</span>}
    </label>
  );
}

function btn(active: boolean): React.CSSProperties {
  return { padding: "5px 10px", borderRadius: 7, cursor: "pointer", fontFamily: "var(--font-mono)", fontSize: 9, letterSpacing: "0.1em", background: active ? "rgba(0,229,255,0.12)" : "transparent", border: `1px solid ${active ? C.cyan : C.line}`, color: active ? C.cyan : C.dim };
}
