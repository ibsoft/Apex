"use client";

/* TasksPanel - the TASKS tab: everything APEX will do on its own.
 *
 * Two things live here that live nowhere else:
 *
 *  1. The manual editor. A task can be created by hand because "ask APEX to
 *     schedule it" is not the only way to want recurring work, and requiring a
 *     model turn to fill in a form is a worse path than the form itself.
 *  2. The list, with the per-row controls (run now, pause, edit, delete).
 *
 * The list is sorted by the shared helpers in ../lib/tasks, NOT by what the
 * backend happened to return, because the number painted on each row is what
 * the operator says out loud ("run task 2"). The provider resolves a spoken
 * number against the same sort; if this component sorted differently, a spoken
 * command would hit a different row than the one the operator was looking at.
 */

import { useEffect, useState } from "react";
import { useApex } from "./ApexProvider";
import {
  SCHEDULE_PRESETS,
  lastRunLabel,
  nextRunLabel,
  oneOffLabel,
  sortTasks,
  statusTone,
  type Task,
  type TaskDraft,
} from "../lib/tasks";

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

const emptyDraft: TaskDraft = { title: "", prompt: "", schedule: "0 8 * * *" };

export default function TasksPanel() {
  const a = useApex();
  const [editing, setEditing] = useState<Task | null>(null);
  const [draft, setDraft] = useState<TaskDraft>(emptyDraft);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  // The tab is the first thing that looks at the list, so this is where the
  // initial load happens; the provider's poll keeps it fresh afterwards.
  useEffect(() => {
    void a.loadTasks();
  }, [a.loadTasks]);

  const rows = sortTasks(a.tasks);

  const startCreate = () => {
    setEditing(null);
    setDraft(emptyDraft);
    setProblem(null);
    setShowForm(true);
  };

  const startEdit = (task: Task) => {
    setEditing(task);
    setDraft({
      title: task.title,
      prompt: task.prompt,
      // A one-off is shown as the local wall clock the user is looking at, not
      // as the UTC instant toISOString hands back. The backend reads a
      // schedule without an offset as machine-local, so this form round-trips
      // to the same minute instead of shifting the task by the UTC offset - and
      // the field is something the user can read and correct.
      schedule: task.repeating ? task.cron : oneOffLabel(task.run_at),
      plan: task.plan,
      skill: task.skill,
      enabled: task.enabled,
    });
    setProblem(null);
    setShowForm(true);
  };

  const save = async () => {
    setBusy(true);
    setProblem(null);
    try {
      if (editing) await a.updateTask(editing.id, draft);
      else await a.createTask(draft);
      setShowForm(false);
      setEditing(null);
      setDraft(emptyDraft);
    } catch (err) {
      // The schedule parser explains itself ("Schedule rejected: 'hour' is not
      // a valid minute value"), and that message is more useful than any
      // generic failure text, so it is shown verbatim.
      setProblem(err instanceof Error ? err.message : "Could not save the task.");
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
          SCHEDULED TASKS
        </span>
        <span style={{ marginLeft: "auto" }} />
        <button onClick={() => void a.loadTasks()} disabled={a.tasksLoading}
          style={btn(false)} title="Refresh">
          {a.tasksLoading ? "…" : "REFRESH"}
        </button>
        <button onClick={startCreate} disabled={busy} style={btn(true)}>
          + NEW TASK
        </button>
      </div>

      {a.tasksError && (
        <div style={{ ...banner, color: C.red, borderColor: "rgba(255,107,107,0.35)" }}>{a.tasksError}</div>
      )}
      {problem && (
        <div style={{ ...banner, color: C.gold, borderColor: "rgba(255,200,97,0.35)" }}>{problem}</div>
      )}

      {showForm && (
        <div style={{ ...card, borderColor: C.cyan }}>
          <div style={{ fontSize: 9.5, letterSpacing: "0.12em", color: C.cyan, fontFamily: "var(--font-mono)" }}>
            {editing ? `EDIT TASK #${rows.findIndex((t) => t.id === editing.id) + 1}` : "NEW TASK"}
          </div>

          <Field label="TITLE">
            <input
              value={draft.title}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              placeholder="Disk check"
              style={input}
            />
          </Field>

          <Field label="WHAT SHOULD APEX DO" hint="Instructions for the run. It works on its own, so write it as if nobody will answer.">
            <textarea
              value={draft.prompt}
              onChange={(e) => setDraft({ ...draft, prompt: e.target.value })}
              placeholder="Check free disk space and tell me if any mount is above 90%."
              rows={3}
              className="apex-scroll-slim"
              style={{ ...input, resize: "vertical", lineHeight: 1.45 }}
            />
          </Field>

          <Field label="SCHEDULE" hint="Five-field cron, or plain words like &quot;every weekday at 09:00&quot; or &quot;in 20 minutes&quot;.">
            <input
              value={draft.schedule}
              onChange={(e) => setDraft({ ...draft, schedule: e.target.value })}
              placeholder="0 9 * * 1-5"
              style={{ ...input, fontFamily: "var(--font-mono)" }}
            />
          </Field>

          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {SCHEDULE_PRESETS.map((preset) => (
              <button
                key={preset.value}
                onClick={() => setDraft({ ...draft, schedule: preset.value })}
                style={btn(draft.schedule === preset.value)}
              >
                {preset.label}
              </button>
            ))}
          </div>

          <Field label="PLAN" hint="Optional: the tool calls to make, so a run does not re-decide them. e.g. terminal_command: df -h /">
            <input
              value={draft.plan ?? ""}
              onChange={(e) => setDraft({ ...draft, plan: e.target.value })}
              placeholder="terminal_command: df -h /"
              style={{ ...input, fontFamily: "var(--font-mono)" }}
            />
          </Field>

          <div style={{ display: "flex", gap: 8, marginTop: 2 }}>
            <button onClick={() => void save()} disabled={busy || !draft.prompt.trim() || !draft.schedule.trim()} style={btn(true)}>
              {busy ? "SAVING…" : editing ? "SAVE CHANGES" : "CREATE TASK"}
            </button>
            <button
              onClick={() => { setShowForm(false); setEditing(null); setProblem(null); }}
              style={btn(false)}
            >
              CANCEL
            </button>
          </div>
        </div>
      )}

      {rows.length === 0 && !showForm && (
        <div style={{ padding: 26, textAlign: "center", color: C.dim, fontSize: 10, fontFamily: "var(--font-mono)", lineHeight: 1.9, letterSpacing: "0.1em" }}>
          NO SCHEDULED TASKS
          <br />
          <span style={{ fontSize: 9, letterSpacing: "0.04em" }}>
            Ask APEX in chat — "every morning check the disk" — or add one by hand.
          </span>
        </div>
      )}

      {rows.map((task, index) => {
        const tone = TONE[statusTone(task.status)];
        return (
          <div key={task.id} style={card}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: C.dim, minWidth: 22 }}>
                #{index + 1}
              </span>
              <span style={{ color: C.text, fontSize: 12, flex: 1, minWidth: 0, wordBreak: "break-word" }}>
                {task.title}
              </span>
              {task.unread && (
                <button
                  onClick={() => a.clearTaskNotice(task.id)}
                  title="Mark as seen"
                  style={{ ...badge, color: C.gold, borderColor: "rgba(255,200,97,0.4)", background: "rgba(255,200,97,0.08)", cursor: "pointer" }}
                >
                  NEW
                </button>
              )}
              <span style={{ ...badge, color: tone, borderColor: `${tone}55`, background: `${tone}12` }}>
                {task.status.toUpperCase()}
              </span>
            </div>

            <div style={{ margin: "7px 0 0 30px", display: "flex", flexWrap: "wrap", gap: 10, fontSize: 9.5, fontFamily: "var(--font-mono)", color: C.dim }}>
              <span style={{ color: C.cyan }}>{task.schedule_label}</span>
              <span>next {nextRunLabel(task)}</span>
              <span>last {lastRunLabel(task)}</span>
              {task.runs > 0 && (
                <span>
                  {task.runs} run{task.runs === 1 ? "" : "s"}
                  {task.failures ? ` · ${task.failures} failed` : ""}
                </span>
              )}
            </div>

            {(task.last_error || task.last_output) && (
              <pre className="apex-scroll-slim"
                style={{
                  margin: "8px 0 0 30px", padding: 8, maxHeight: 130, overflow: "auto",
                  background: "rgba(0,0,0,0.3)", border: `1px solid ${C.line}`, borderRadius: 6,
                  fontSize: 9.5, fontFamily: "var(--font-mono)", color: task.last_status === "error" ? C.red : C.text,
                  whiteSpace: "pre-wrap", wordBreak: "break-word",
                }}
              >
                {(task.last_error || task.last_output || "").trim()}
              </pre>
            )}

            <div style={{ margin: "9px 0 0 30px", display: "flex", flexWrap: "wrap", gap: 5 }}>
              <button onClick={() => void guard(() => a.runTaskNow(task.id))} disabled={busy} style={btn(false)}>
                RUN NOW
              </button>
              <button
                onClick={() => void guard(() => a.updateTask(task.id, { enabled: !task.enabled }))}
                disabled={busy}
                style={btn(false)}
              >
                {task.enabled ? "PAUSE" : "RESUME"}
              </button>
              <button onClick={() => startEdit(task)} disabled={busy} style={btn(false)}>
                EDIT
              </button>
              <button
                onClick={() => void guard(() => a.deleteTask(task.id))}
                disabled={busy}
                style={{ ...btn(false), color: C.red, borderColor: "rgba(255,107,107,0.35)" }}
              >
                DELETE
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

const card: React.CSSProperties = {
  padding: 11,
  borderRadius: 10,
  background: "rgba(255,255,255,0.03)",
  border: `1px solid ${C.line}`,
  display: "flex",
  flexDirection: "column",
  gap: 2,
};

const banner: React.CSSProperties = {
  padding: "7px 10px",
  borderRadius: 8,
  fontSize: 10,
  fontFamily: "var(--font-mono)",
  lineHeight: 1.5,
  border: "1px solid transparent",
  background: "rgba(255,255,255,0.03)",
};

const badge: React.CSSProperties = {
  fontSize: 8.5,
  letterSpacing: "0.1em",
  fontFamily: "var(--font-mono)",
  padding: "2px 7px",
  borderRadius: 9,
  border: "1px solid transparent",
};

const input: React.CSSProperties = {
  width: "100%",
  padding: "6px 8px",
  borderRadius: 7,
  fontSize: 11,
  background: "rgba(0,0,0,0.3)",
  border: `1px solid ${C.line}`,
  color: C.text,
  outline: "none",
};

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 8 }}>
      <span style={{ fontSize: 8.5, letterSpacing: "0.12em", color: C.dim, fontFamily: "var(--font-mono)" }}>
        {label}
      </span>
      {children}
      {hint && (
        <span style={{ fontSize: 9, color: C.dim, lineHeight: 1.5 }}>{hint}</span>
      )}
    </label>
  );
}

function btn(active: boolean): React.CSSProperties {
  return {
    padding: "5px 10px",
    borderRadius: 7,
    cursor: "pointer",
    fontFamily: "var(--font-mono)",
    fontSize: 9,
    letterSpacing: "0.1em",
    background: active ? "rgba(0,229,255,0.12)" : "transparent",
    border: `1px solid ${active ? C.cyan : C.line}`,
    color: active ? C.cyan : C.dim,
  };
}
