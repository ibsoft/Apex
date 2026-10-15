"""SubAgentManager: owner-scoped CRUD, workspace provisioning and run
orchestration for the named autonomous workers in the AGENTS tab.

One manager per process (keyed on the config object's identity, exactly like the
task runner in :mod:`tools.tasks`), so a test with a throwaway config gets its own
instance and its own concurrency slots instead of sharing the live one.

Two rules this module keeps, both learned the hard way elsewhere in APEX:

* Ownership is a filter, never a check done afterwards. Another user's agent must
  be indistinguishable from an id that does not exist.
* A run's concurrency is capped process-wide. Each run is a full agent turn with
  tools, so a fan-out must not spawn a stampede.
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from pathlib import Path

from config import config as default_config

#: Deliberately permissive: a display name, not a filesystem path. Only the
#: shape that could confuse the UI or a filename is rejected.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-()]{0,59}$")

_MAX_PROMPT_CHARS = 8000
_MAX_OUTPUT_CHARS = 20000


class SubAgentError(ValueError):
    """A validation failure the routes turn into a 400."""


def _loads(raw, default):
    if not raw:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class SubAgentManager:
    def __init__(self, db, config=default_config):
        self.db = db
        self.config = config
        self._slots = threading.BoundedSemaphore(
            max(1, int(getattr(config, "SUBAGENT_MAX_CONCURRENT", 2) or 2))
        )
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self._known_tools: list[str] | None = None

    # ---- paths ------------------------------------------------------------
    def workspace_root(self) -> Path:
        raw = getattr(self.config, "SUBAGENTS_DIR", None)
        if not raw:
            raw = Path(getattr(self.config, "DATA_DIR", Path(__file__).resolve().parents[1] / "data")) / "subagents"
        return Path(raw).expanduser().resolve()

    def default_workspace(self, user_id: str, agent_id: str) -> Path:
        return self.workspace_root() / str(user_id) / str(agent_id)

    def ensure_workspace(self, user_id: str, agent_id: str, path: str = "") -> Path:
        """Create (idempotently) and return the agent's working directory.

        An operator-supplied path is honoured (it is their machine); the default
        lives under ``SUBAGENTS_DIR/<user>/<agent>`` so a bare agent still has a
        private scratch directory.
        """
        if path:
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = self.workspace_root() / target
            target = target.resolve()
        else:
            target = self.default_workspace(user_id, agent_id)
        target.mkdir(parents=True, exist_ok=True)
        try:
            target.chmod(0o700)
        except OSError:
            pass
        return target

    # ---- shapes -----------------------------------------------------------
    def public_run(self, run: dict | None) -> dict | None:
        if not run:
            return None
        out = dict(run)
        out["unread"] = bool(run.get("unread"))
        out["ok"] = (run.get("status") == "ok")
        return out

    def public(self, agent: dict, with_runs: bool = False) -> dict:
        out = dict(agent)
        out["enabled"] = bool(agent.get("enabled"))
        out["tools"] = _loads(agent.get("tools"), [])
        out["workspace"] = agent.get("workspace") or str(
            self.default_workspace(agent["user_id"], agent["id"])
        )
        runs = self.db.list_subagent_runs(agent["id"], 50)
        out["last_run"] = self.public_run(runs[0]) if runs else None
        out["unread"] = sum(1 for r in runs if r.get("unread"))
        if with_runs:
            out["runs"] = [self.public_run(r) for r in runs]
        return out

    # ---- validation -------------------------------------------------------
    def known_tool_names(self) -> list[str]:
        """Every tool name the live registry offers (for the AGENTS tab picker
        and for validating an agent's tool list). Cached: the registry is static
        after startup and rebuilding it on every create/update is wasteful."""
        if self._known_tools is not None:
            return self._known_tools
        try:
            from agent.factory import make_registry

            mem = None
            if getattr(self.config, "MEMORY_ENABLED", False):
                try:
                    from memory.store import get_memory

                    mem = get_memory()
                except Exception:
                    mem = None
            self._known_tools = sorted(make_registry(memory=mem).names())
        except Exception:
            self._known_tools = []
        return self._known_tools

    def _validate_tools(self, tools) -> list[str]:
        if tools is None:
            return []
        if isinstance(tools, str):
            names = [t.strip() for t in tools.split(",") if t.strip()]
        else:
            names = [str(t).strip() for t in tools if str(t).strip()]
        if not names:
            return []
        known = set(self.known_tool_names())
        if known:
            unknown = sorted(n for n in names if n not in known)
            if unknown:
                raise SubAgentError("Unknown tool(s): " + ", ".join(unknown))
        return names

    def _validate(self, data: dict, *, partial: bool = False) -> dict:
        out: dict = {}
        if not partial or "name" in data:
            name = str(data.get("name") or "").strip()
            if not name:
                raise SubAgentError("A name is required.")
            if not _NAME_RE.match(name):
                raise SubAgentError(
                    "Name may use letters, numbers, spaces and . _ - ( ) only."
                )
            out["name"] = name
        for key in ("description", "role"):
            if key in data:
                out[key] = str(data.get(key) or "")
        for key in ("provider", "model"):
            if key in data:
                out[key] = str(data.get(key) or "").strip()
        if "enabled" in data:
            out["enabled"] = bool(data["enabled"])
        if "tools" in data:
            out["tools"] = self._validate_tools(data["tools"])
        if "workspace" in data:
            out["workspace"] = str(data.get("workspace") or "").strip()
        return out

    # ---- CRUD -------------------------------------------------------------
    def create(self, user_id: str, data: dict) -> dict:
        fields = self._validate(data)
        limit = int(getattr(self.config, "SUBAGENT_MAX_PER_USER", 25) or 25)
        if len(self.db.list_subagents(user_id)) >= limit:
            raise SubAgentError(f"You already have {limit} sub-agents.")
        fields["tools"] = json.dumps(fields.get("tools") or [])
        agent = self.db.create_subagent(user_id, **fields)
        ws = self.ensure_workspace(user_id, agent["id"], fields.get("workspace") or "")
        agent = self.db.update_subagent(agent["id"], workspace=str(ws)) or agent
        return self.public(agent)

    def update(self, agent_id: str, user_id: str, data: dict) -> dict | None:
        agent = self.db.get_subagent(agent_id, user_id)
        if agent is None:
            return None
        fields = self._validate(data, partial=True)
        if "tools" in fields:
            fields["tools"] = json.dumps(fields["tools"])
        if "workspace" in fields:
            fields["workspace"] = str(
                self.ensure_workspace(user_id, agent_id, fields["workspace"])
            )
        updated = self.db.update_subagent(agent_id, **fields)
        return self.public(updated) if updated else None

    def delete(self, agent_id: str, user_id: str) -> bool:
        if self.db.get_subagent(agent_id, user_id) is None:
            return False
        # The workspace files are left on disk on purpose: an agent's work is not
        # something deleting a settings row should silently destroy.
        return self.db.delete_subagent(agent_id)

    # ---- running ----------------------------------------------------------
    def run_now(self, agent_id: str, user_id: str, prompt: str, *,
                background: bool = True, conversation_id: str = "",
                emit=None, timeout: int | None = None):
        """Start a run. Returns ``(run_row, error)``; on success the row carries
        the live/final status. ``background=True`` returns immediately; the
        synchronous path (used by the delegate tool) blocks and returns the
        finished row."""
        agent = self.db.get_subagent(agent_id, user_id)
        if agent is None:
            return None, "not found"
        if not bool(agent.get("enabled")):
            return None, "This sub-agent is paused."
        prompt = (prompt or "").strip()
        if not prompt:
            return None, "Give the agent something to do."
        prompt = prompt[:_MAX_PROMPT_CHARS]
        conv_id = conversation_id or agent.get("conversation_id") or ""
        run = self.db.create_subagent_run(agent_id, user_id, prompt, conv_id)
        if background:
            threading.Thread(
                target=self._guarded_execute, args=(run["id"],),
                daemon=True, name=f"apex-subagent-{agent_id[:8]}",
            ).start()
        else:
            self._execute(run["id"], emit=emit, timeout=timeout)
            run = self.db.get_subagent_run(run["id"])
        return run, ""

    def run_many(self, jobs: list[dict], user_id: str, *, emit=None,
                 timeout: int | None = None) -> list[tuple[dict, dict | None, str]]:
        """Run several already-resolved jobs concurrently, blocking until every
        one is done. ``jobs`` is ``[{"agent": <row>, "prompt": str}, ...]``.

        Concurrency is still bounded by the process-wide cap: this runs at most
        that many at once and *queues* the rest (a fan-out must not be failed just
        because it has more jobs than slots - unlike a background Run Now, whose
        caller is not waiting). Returns ``[(job, finished_run_or_None, error)]``
        in input order; ``error`` is the up-front refusal (paused, empty prompt),
        while a run that failed carries its reason in its own row.
        """
        prepared: list[tuple[dict, dict | None, str]] = []
        for job in jobs:
            agent = job["agent"]
            prompt = (job.get("prompt") or "").strip()
            if not bool(agent.get("enabled")):
                prepared.append((job, None, "This sub-agent is paused."))
                continue
            if not prompt:
                prepared.append((job, None, "Give the agent something to do."))
                continue
            run = self.db.create_subagent_run(
                agent["id"], user_id, prompt[:_MAX_PROMPT_CHARS],
                agent.get("conversation_id") or "",
            )
            prepared.append((job, run, ""))

        runnable = [(job, run) for job, run, err in prepared if run is not None]
        cap = max(1, int(getattr(self.config, "SUBAGENT_MAX_CONCURRENT", 2) or 2))
        if runnable:
            from concurrent.futures import ThreadPoolExecutor

            def work(item):
                _job, run = item
                self._slots.acquire()
                try:
                    self._execute(run["id"], emit=emit, timeout=timeout)
                except Exception:
                    # _execute records its own failure; this is belt-and-braces
                    # so one job can never abort the batch.
                    traceback.print_exc()
                finally:
                    self._slots.release()

            with ThreadPoolExecutor(max_workers=min(cap, len(runnable))) as pool:
                list(pool.map(work, runnable))

        return [
            (job, self.db.get_subagent_run(run["id"]) if run else None, err)
            for job, run, err in prepared
        ]

    def stale_agents(self, user_id: str, older_than_days: float) -> list[tuple[dict, float]]:
        """Agents whose last activity is older than the cutoff. "Activity" is the
        finish time of the most recent run, or the creation time for one that has
        never run - a brand-new idle agent is not stale."""
        cutoff = time.time() - max(0.0, float(older_than_days)) * 86400.0
        out: list[tuple[dict, float]] = []
        for agent in self.db.list_subagents(user_id):
            runs = self.db.list_subagent_runs(agent["id"], 1)
            if runs:
                last = runs[0].get("finished_at") or runs[0].get("started_at") or 0.0
            else:
                last = agent.get("created_at") or 0.0
            if last < cutoff:
                out.append((agent, last))
        return out

    def cleanup_stale(self, user_id: str, older_than_days: float = 30.0) -> list[dict]:
        """Delete agents unused for ``older_than_days``. Returns what was removed."""
        removed: list[dict] = []
        for agent, last in self.stale_agents(user_id, older_than_days):
            if self.delete(agent["id"], user_id):
                removed.append({
                    "id": agent["id"], "name": agent.get("name") or "agent",
                    "last_used": last,
                })
        return removed

    def running_run_ids(self) -> set[str]:
        with self._lock:
            return set(self._active)

    def _guarded_execute(self, run_id: str) -> None:
        if not self._slots.acquire(blocking=False):
            self.db.update_subagent_run(
                run_id, status="error", error="too many sub-agents are running; try again shortly",
                finished_at=time.time(), unread=1,
            )
            return
        try:
            self._execute(run_id)
        except Exception:
            traceback.print_exc()
        finally:
            self._slots.release()

    def _execute(self, run_id: str, emit=None, timeout: int | None = None):
        from .runner import run_subagent_turn

        run = self.db.get_subagent_run(run_id)
        if run is None:
            return "", "run not found"
        agent = self.db.get_subagent(run["agent_id"])
        if agent is None:
            self.db.update_subagent_run(
                run_id, status="error", error="the sub-agent was deleted",
                finished_at=time.time(), unread=1,
            )
            return "", "the sub-agent was deleted"
        timeout = int(timeout or getattr(self.config, "SUBAGENT_TIMEOUT_SECONDS", 900) or 900)
        with self._lock:
            self._active.add(run_id)
        if emit:
            emit({"type": "subagent_started", "agent": agent.get("name"),
                  "agent_id": agent["id"], "run_id": run_id})
        try:
            report, error = run_subagent_turn(
                agent, run["prompt"], user_id=run["user_id"], run_id=run_id,
                conversation_id=run.get("conversation_id") or "", emit=emit, timeout=timeout,
            )
        except Exception as exc:
            traceback.print_exc()
            report, error = "", f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._active.discard(run_id)
        now = time.time()
        if error:
            self.db.update_subagent_run(
                run_id, status="error", output=(report or "")[:_MAX_OUTPUT_CHARS],
                error=str(error)[:1000], finished_at=now, unread=1,
            )
        else:
            self.db.update_subagent_run(
                run_id, status="ok", output=(report or "")[:_MAX_OUTPUT_CHARS],
                error="", finished_at=now, unread=1,
            )
        if emit:
            emit({"type": "subagent_report", "agent": agent.get("name"),
                  "agent_id": agent["id"], "run_id": run_id,
                  "ok": not error, "text": (report or "")[:4000], "error": error or ""})
        return report, error


_MANAGERS: dict[int, SubAgentManager] = {}
_MANAGER_LOCK = threading.Lock()


def subagent_context_block(user_id: str, db=None, *, limit_agents: int = 20,
                           limit_reports: int = 5) -> str:
    """What the model is told about the operator's sub-agents at the top of a
    turn: the roster it can delegate to, the rules for using them on its own
    initiative, and any finished run whose report the operator has not seen yet.

    A background run ends with no browser attached, so nothing in the chat's own
    history mentions it - this block is how APEX learns it happened and can relay
    the result, which is the reporting contract (the sub-agent reports to APEX,
    APEX speaks to the operator).

    Returned even when the roster is empty: the whole point is that APEX decides
    by itself to create agents for a complex job, which it cannot do if it is
    never told the capability exists.
    """
    if db is None:
        from db import get_db

        db = get_db()
    try:
        agents = db.list_subagents(user_id)
    except Exception:
        return ""

    names = {a["id"]: (a.get("name") or "agent") for a in agents}
    lines = ["[Sub-agents]"]
    if not agents:
        lines.append("You have no sub-agents yet.")
    for a in agents[:limit_agents]:
        tools = _loads(a.get("tools"), [])
        scope = f"{len(tools)} tool(s)" if tools else "all tools"
        desc = (a.get("description") or a.get("role") or "").strip()
        paused = "" if a.get("enabled") else " [paused]"
        lines.append(
            f'- id={a["id"]} "{a.get("name")}" - {desc[:120]} '
            f'({scope}, model {a.get("model") or "default"}){paused}'
        )
    lines.append(
        "You may hand work to these agents on your own initiative when a request "
        "is complex or has independent parts - the operator does not have to ask. "
        "Rules:\n"
        "1. Reuse first. If an agent above already fits a part, delegate to it. "
        "Only call subagent_create when no existing agent fits, and do it silently "
        "(the operator need not confirm).\n"
        "2. Split and fan out. Break a multi-part job into self-contained tasks "
        "and run them together with subagent_fanout, which returns every agent's "
        "report at once; compose the operator's answer from those reports. Use "
        "subagent_delegate for a single task.\n"
        "3. Don't over-delegate. Do a small or quick task yourself - spawning an "
        "agent for it is slower, not faster.\n"
        "4. Tidy up. When the roster has agents you created for a one-off that are "
        "now unused, remove them with subagent_cleanup."
    )

    try:
        reports = db.list_unread_runs(user_id, limit_reports)
    except Exception:
        reports = []
    if reports:
        lines.append("")
        lines.append(
            "[Sub-agent results the operator has not seen yet - relay these in "
            "your reply]"
        )
        for r in reports:
            text = (r.get("output") or r.get("error") or "").strip()
            label = names.get(r.get("agent_id"), "sub-agent")
            lines.append(f'- "{label}" (run {str(r.get("id"))[:8]}): {text[:600]}')
    return "\n".join(lines)


def get_manager(config=default_config) -> SubAgentManager:
    """The process-wide manager, keyed on the config object's identity (same
    reasoning as ``tools.tasks.get_runner``: a test that passes a stub gets its
    own instance instead of sharing the live one)."""
    from db import get_db

    with _MANAGER_LOCK:
        manager = _MANAGERS.get(id(config))
        if manager is None:
            manager = SubAgentManager(get_db(), config)
            _MANAGERS[id(config)] = manager
        return manager
