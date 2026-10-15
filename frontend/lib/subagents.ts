/** Sub-agents: the browser-side model for the AGENTS tab.
 *
 * The backend is the source of truth (`subagents/manager.py::public` and
 * `public_run`). This module owns only what the browser adds: types and the
 * small display helpers the panel and the tests share, kept out of the
 * component so they are testable without a DOM — the same split `tasks.ts` and
 * `windows.ts` use.
 */

export type SubAgentRunStatus = "running" | "ok" | "error";

export type SubAgentRun = {
  id: string;
  agent_id: string;
  user_id: string;
  prompt: string;
  status: SubAgentRunStatus;
  output: string;
  error: string;
  conversation_id: string;
  started_at: number;
  finished_at: number | null;
  unread: boolean;
  /** convenience mirror of `status === "ok"`, set by the backend */
  ok: boolean;
};

export type SubAgent = {
  id: string;
  user_id: string;
  name: string;
  description: string;
  role: string;
  provider: string;
  model: string;
  tools: string[];
  workspace: string;
  enabled: boolean;
  conversation_id: string;
  created_at: number;
  updated_at: number;
  last_run: SubAgentRun | null;
  unread: number;
  /* only present on the single-agent GET */
  runs?: SubAgentRun[];
};

export type SubAgentDraft = {
  name: string;
  description?: string;
  role?: string;
  provider?: string;
  model?: string;
  tools?: string[];
  workspace?: string;
  enabled?: boolean;
};

/** Badge colour key, matched against the app's palette in the panel. */
export function statusTone(status: SubAgentRunStatus | null | undefined): "cyan" | "gold" | "red" | "dim" {
  if (status === "running") return "gold";
  if (status === "error") return "red";
  if (status === "ok") return "cyan";
  return "dim";
}

/** A one-line state for an agent's last run. */
export function lastRunLabel(agent: SubAgent): string {
  const run = agent.last_run;
  if (!run) return "never ran";
  if (run.status === "running") return "running now";
  if (run.status === "error") return "last run failed";
  return "last run succeeded";
}

/** How an agent's tool scope reads in the list: a scope, not a count of all. */
export function toolScopeLabel(tools: string[] | undefined): string {
  const count = (tools ?? []).length;
  if (count === 0) return "all tools";
  return `${count} tool${count === 1 ? "" : "s"}`;
}
