/** Scheduled tasks: the browser-side model and the pure helpers the TASKS tab,
 * the local command handler and the tests all share.
 *
 * The backend is the source of truth (`tools/tasks.py::public_task`), and it
 * already derives `status`, `schedule_label` and `repeating`. This module owns
 * only what the browser adds: display strings, grouping and the text the voice
 * layer speaks. Keeping it separate from the component is what makes those
 * helpers testable without a DOM, and it is the same split `windows.ts` and
 * `notepad.ts` use.
 */

export type TaskStatus = "pending" | "running" | "ok" | "error" | "paused";

export type Task = {
  id: number;
  title: string;
  prompt: string;
  plan: string;
  skill: string;
  schedule: "cron" | "once";
  cron: string;
  run_at: number | null;
  next_run: number | null;
  last_run: number | null;
  last_status: string | null;
  last_output: string | null;
  last_error: string | null;
  runs: number;
  failures: number;
  enabled: boolean;
  unread: boolean;
  created: number;
  /* derived by the backend */
  repeating: boolean;
  schedule_label: string;
  status: TaskStatus;
  conversation_id: string | null;
};

export type TaskDraft = {
  title: string;
  prompt: string;
  schedule: string;
  plan?: string;
  skill?: string;
  enabled?: boolean;
};

export type TaskPatch = Partial<TaskDraft>;

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Badge colour key, matched against the app's palette in the panel. */
export function statusTone(status: TaskStatus): "cyan" | "gold" | "red" | "dim" {
  if (status === "running") return "gold";
  if (status === "error") return "red";
  if (status === "ok") return "cyan";
  return "dim";
}

/** A short, human "when did this last run / when does it next" line. */
export function relativeTime(stamp: number | null, now = Date.now(), language = "en"): string {
  if (!stamp) return language === "el" ? "ποτέ" : "never";
  const delta = stamp - now;
  const future = delta > 0;
  const seconds = Math.round(Math.abs(delta) / 1000);
  const phrase = (count: number, unit: string) => {
    /* [singular, plural] per unit. Greek has three forms and this only needs
       two, so a count of 1 gets the singular and everything else the plural -
       the alternative ("1 ώρες") is a grammatical error in every one of these
       units, and it would be read aloud. */
    const units = language === "el"
      ? { s: ["δευτερόλεπτο", "δευτερόλεπτα"], m: ["λεπτό", "λεπτά"], h: ["ώρα", "ώρες"], d: ["μέρα", "μέρες"] }
      : { s: ["second", "seconds"], m: ["minute", "minutes"], h: ["hour", "hours"], d: ["day", "days"] };
    const forms = units[unit as keyof typeof units];
    return `${count} ${count === 1 ? forms[0] : forms[1]}`;
  };
  let text: string;
  if (seconds < 60) text = phrase(Math.max(1, seconds), "s");
  else if (seconds < 3600) text = phrase(Math.round(seconds / 60), "m");
  else if (seconds < 86400) text = phrase(Math.round(seconds / 3600), "h");
  else text = phrase(Math.round(seconds / 86400), "d");
  if (language === "el") return future ? `σε ${text}` : `πριν ${text}`;
  return future ? `in ${text}` : `${text} ago`;
}

export function nextRunLabel(task: Task, now = Date.now(), language = "en"): string {
  if (task.status === "paused") return language === "el" ? "σε παύση" : "paused";
  if (!task.next_run) return language === "el" ? "χωρίς επόμενη εκτέλεση" : "no run scheduled";
  return relativeTime(task.next_run, now, language);
}

export function lastRunLabel(task: Task, now = Date.now(), language = "en"): string {
  if (!task.last_run) return language === "el" ? "ποτέ δεν εκτελέστηκε" : "never ran";
  const when = relativeTime(task.last_run, now, language);
  if (task.last_status === "error") {
    return language === "el" ? `απέτυχε ${when}` : `failed ${when}`;
  }
  return when;
}

/** What the voice layer says after a local task command. */
export function describeTasks(tasks: Task[], language = "en"): string {
  if (tasks.length === 0) {
    return language === "el"
      ? "Δεν έχεις εργασίες σε αναμονή."
      : "You have no scheduled tasks.";
  }
  const parts = tasks.slice(0, 6).map((task, index) => {
    const state = task.status === "running"
      ? (language === "el" ? "τρέχει τώρα" : "running now")
      : task.status === "error"
        ? (language === "el" ? "απέτυχε" : "failed")
        : task.status === "paused"
          ? (language === "el" ? "σε παύση" : "paused")
          : (language === "el" ? "περιμένει" : "waiting");
    return language === "el"
      ? `${index + 1}. ${task.title} (${state})`
      : `${index + 1}. ${task.title} (${state})`;
  });
  const tail = tasks.length > parts.length
    ? (language === "el"
      ? ` και ${tasks.length - parts.length} άλλες`
      : ` and ${tasks.length - parts.length} more`)
    : "";
  return language === "el"
    ? `Έχεις ${tasks.length} εργασίες: ${parts.join(", ")}${tail}.`
    : `You have ${tasks.length} task${tasks.length === 1 ? "" : "s"}: ${parts.join(", ")}${tail}.`;
}

export function describeOneTask(task: Task, language = "en"): string {
  const el = language === "el";
  const when = el ? `Επόμενη εκτέλεση ${relativeTime(task.next_run, Date.now(), language)}` : `Next run ${relativeTime(task.next_run, Date.now(), language)}`;
  const runs = el
    ? `Έχει εκτελεστεί ${task.runs} φορές${task.failures ? `, ${task.failures} αποτυχίες` : ""}.`
    : `It has run ${task.runs} time${task.runs === 1 ? "" : "s"}${task.failures ? `, ${task.failures} failed` : ""}.`;
  const result = task.last_status === "error" && task.last_error
    ? (el ? ` Τελευταίο σφάλμα: ${task.last_error}` : ` Last error: ${task.last_error}`)
    : task.last_output
      ? (el ? ` Τελευταίο αποτέλεσμα: ${task.last_output}` : ` Last result: ${task.last_output}`)
      : "";
  return el
    ? `Εργασία «${task.title}»: ${task.schedule_label}. ${when}. ${runs}${result}`
    : `Task "${task.title}": ${task.schedule_label}. ${when}. ${runs}${result}`;
}

/** The compact result the provider posts into chat after a run. */
export function taskNotification(task: Task, language = "en"): string {
  const el = language === "el";
  const ok = task.status !== "error";
  const when = relativeTime(task.last_run, Date.now(), language);
  const head = el
    ? `Εργασία «${task.title}» ${ok ? "ολοκληρώθηκε" : "απέτυχε"} ${when}.`
    : `Task "${task.title}" ${ok ? "finished" : "failed"} ${when}.`;
  const detail = (task.last_error || task.last_output || "").trim();
  if (!detail) return head;
  return `${head} ${detail.length > 400 ? `${detail.slice(0, 400)}…` : detail}`;
}

/** Sorts so the things needing attention come first, then by next run. */
export function sortTasks(tasks: Task[]): Task[] {
  const rank: Record<TaskStatus, number> = { error: 0, running: 1, pending: 2, ok: 3, paused: 4 };
  return [...tasks].sort((a, b) => {
    const byStatus = rank[a.status] - rank[b.status];
    if (byStatus !== 0) return byStatus;
    const aNext = a.next_run ?? Infinity;
    const bNext = b.next_run ?? Infinity;
    if (aNext !== bNext) return aNext - bNext;
    return (b.created ?? 0) - (a.created ?? 0);
  });
}

export function filterTasks(tasks: Task[], filter: string | undefined): Task[] {
  const sorted = sortTasks(tasks);
  switch (filter) {
    case "running": return sorted.filter((t) => t.status === "running");
    case "paused": return sorted.filter((t) => t.status === "paused");
    case "error": return sorted.filter((t) => t.status === "error");
    case "enabled": return sorted.filter((t) => t.enabled);
    default: return sorted;
  }
}

/** Resolve a spoken number (-1 = "the last one") against the loaded list. */
export function resolveTarget(tasks: Task[], target: number | undefined): Task | null {
  if (!target) return null;
  if (target === -1) return tasks.length ? tasks[tasks.length - 1] : null;
  return tasks[target - 1] ?? null;
}

/** Schedules offered as one-tap presets in the editor, next_run as ms. */
export const SCHEDULE_PRESETS: Array<{ label: string; value: string }> = [
  { label: "every 15 min", value: "*/15 * * * *" },
  { label: "hourly", value: "0 * * * *" },
  { label: "daily 08:00", value: "0 8 * * *" },
  { label: "weekdays 09:00", value: "0 9 * * 1-5" },
  { label: "weekly (Mon)", value: "0 8 * * 1" },
  { label: "in 10 minutes", value: "in 10 minutes" },
  { label: "in 1 hour", value: "in 1 hour" },
];

/** The editable text for a task that fires once, as a local wall clock.
 *
 * `toISOString` would give UTC with a trailing `Z`, which two things get wrong:
 * it is unreadable in a text field, and it is a different minute from the one
 * the user is looking at in the list. The backend reads a schedule that carries
 * no offset as machine-local time, so this form is exact. */
export function oneOffLabel(runAt: number | null | undefined): string {
  if (!runAt) return "";
  const when = new Date(runAt * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `once at ${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`
    + `T${pad(when.getHours())}:${pad(when.getMinutes())}`;
}

export { MINUTE, HOUR, DAY };
