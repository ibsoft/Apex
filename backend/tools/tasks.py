"""Scheduled tasks: the agent running its own work on a cron schedule.

A task is a prompt plus a schedule. Nothing else decides what it does - when the
schedule comes due the task's prompt is handed to a normal agent turn with the
same tools the operator has, so "check disk every morning" becomes whatever
commands the model decides are right that morning.

Three pieces live here:

* :func:`register_task_routes` - the REST surface the Tasks tab talks to.
* :class:`TaskRunner` - one daemon thread that wakes up, asks the database which
  rows are due, and runs each one.
* :func:`task_context_block` - the short prompt block appended to every chat
  turn so the model knows what it has scheduled and how the last runs went.

Reporting is a round trip, not a push: a run marks the row ``unread`` and the
browser collects that on its next poll. The alternative - holding an SSE stream
open for a task that may fire at 03:00 for a browser that is closed - would
either lose the result or pin a worker for no reason.
"""

from __future__ import annotations

import json
import threading
import time
import traceback

from flask import jsonify, request

from tools.cron import CronError, Schedule, describe_schedule, next_run, parse_schedule

MAX_OUTPUT_CHARS = 6000
MAX_LIST = 100


# ---- store -------------------------------------------------------------


def create_task(db, user_id: str, *, title: str, prompt: str, schedule: Schedule,
                plan: str = "", skill: str = "", enabled: bool = True) -> dict:
    """Persist a new task and compute its first firing."""
    first = next_run(schedule)
    return db.create_task(
        user_id,
        title=title,
        prompt=prompt,
        plan=plan,
        skill=skill,
        schedule=schedule.kind,
        cron=schedule.cron,
        run_at=schedule.run_at if not schedule.repeating else None,
        next_run=first if schedule.repeating or first > time.time() else None,
        enabled=enabled,
    )


def public_task(row: dict | None) -> dict | None:
    """A task row shaped for the browser, with the derived display fields."""
    if not row:
        return None
    out = dict(row)
    out["enabled"] = bool(row.get("enabled"))
    out["unread"] = bool(row.get("unread"))
    out["repeating"] = (row.get("schedule") or "cron") == "cron"
    out["schedule_label"] = (
        describe_schedule(row.get("cron") or "") if out["repeating"]
        else _once_label(row.get("run_at"))
    )
    out["status"] = task_status(row)
    # The stored output is only ever shown truncated; the full text lives in the
    # task's own conversation under History.
    if isinstance(out.get("last_output"), str):
        out["last_output"] = out["last_output"][:MAX_OUTPUT_CHARS]
    return out


def _once_label(run_at) -> str:
    if not run_at:
        return "one-off"
    from datetime import datetime

    return "once, at " + datetime.fromtimestamp(float(run_at)).strftime("%Y-%m-%d %H:%M")


def task_status(row: dict) -> str:
    """One word for the badge: pending / running / ok / error / paused."""
    if not row.get("enabled"):
        return "paused"
    status = (row.get("last_status") or "").strip().lower()
    return status if status in ("running", "ok", "error") else "pending"


# ---- prompt context ----------------------------------------------------


def task_context_block(user_id: str, db=None, limit: int = 8) -> str:
    """What the model is told about its own tasks at the top of every turn.

    Without this the model has no way to answer "did the disk check run?" - the
    runs happen in a background thread with no browser attached, so the history
    of the chat the request arrived in says nothing about them.
    """
    if db is None:
        from db import get_db

        db = get_db()
    try:
        rows = db.list_tasks(user_id)[:limit]
    except Exception:
        return ""
    if not rows:
        return ""

    lines = ["[Scheduled tasks you own]"]
    for index, row in enumerate(rows, start=1):
        when = (
            "next " + time.strftime("%Y-%m-%d %H:%M", time.localtime(row["next_run"]))
            if row.get("next_run") else "no further runs"
        )
        lines.append(
            f'- #{index} "{row["title"]}" [{task_status(row)}] '
            f'{row.get("cron") or "one-off"} ({when}), {int(row.get("runs") or 0)} run(s)'
        )
        if row.get("last_status") == "error" and row.get("last_error"):
            lines.append(f"  last error: {str(row['last_error'])[:200]}")
    lines.append(
        "Use task_schedule/task_update/task_run_now/task_status to change these, and say "
        "plainly when you ran one or when one failed."
    )
    return "\n".join(lines)


def task_prompt(row: dict) -> str:
    """The prompt handed to the agent when a task comes due."""
    title = (row.get("title") or "task").strip()
    parts = [f'Scheduled task "{title}" is due now.']
    parts.append(f"What the operator asked for: {row.get('prompt') or ''}".strip())
    if (row.get("plan") or "").strip():
        parts.append(f"Plan agreed when it was created: {str(row['plan']).strip()}")
    previous = str(row.get("last_output") or "").strip()
    if previous and row.get("last_run"):
        parts.append(f"What you reported last time: {previous[:1500]}")
    parts.append(
        "Carry it out now using your tools. Nobody is watching and there is nobody to "
        "answer a question, so decide and act instead of asking. Use the terminal for "
        "shell work rather than describing what the user could type. End with a short "
        "report of what you did and what you found - that report is what the operator "
        "is shown, so put the actual result in it. If you could not do it, say exactly "
        "what failed and why."
    )
    return "\n\n".join(parts)


def task_skill_and_call_authorization(row: dict, skill_manager) -> tuple[str, bool]:
    """Route a scheduled phone-call request to SIP and authorize that task's call."""
    from skills.manager import force_sip_skill

    prompt = str(row.get("prompt") or "")
    forced = force_sip_skill(prompt, skill_manager.all())
    if forced == "SIP":
        return "SIP", True
    return (str(row.get("skill") or "general").strip() or "general"), False


# ---- runner ------------------------------------------------------------


class TaskRunner:
    """One daemon thread for the whole process.

    A single thread owns the "which rows are due" decision so two ticks cannot
    claim the same task, and each run is handed to its own worker thread so a
    task that takes ten minutes does not hold up the others. The concurrency cap
    is what stops a laptop waking from sleep with a week of backlog from firing
    every missed run at once.
    """

    def __init__(self, config):
        self.config = config
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, int(getattr(config, "TASKS_MAX_CONCURRENT", 1) or 1)))
        self._tick_seconds = max(2, int(getattr(config, "TASKS_TICK_SECONDS", 20) or 20))

    # -- lifecycle --

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="apex-tasks", daemon=True)
        self._thread.start()
        if bool(getattr(self.config, "TASKS_CATCH_UP", True)):
            self.recover_missed()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running_task_ids(self) -> set[str]:
        with self._lock:
            return set(self._active)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            self._stop.wait(self._tick_seconds)

    # -- claiming --

    def tick(self, db=None, now: float | None = None) -> list[str]:
        """Claim and start every due task. Returns the ids it started."""
        if db is None:
            from db import get_db

            db = get_db()
        now = float(now if now is not None else time.time())
        started: list[str] = []
        for row in db.due_tasks(now):
            task_id = str(row.get("id") or "")
            if not task_id:
                continue
            with self._lock:
                if task_id in self._active:
                    continue
                self._active.add(task_id)
            # Advance next_run *before* the run, not after: a crash mid-task must
            # not leave a row permanently due, which would re-fire it on every
            # tick from then on.
            db.update_task(
                task_id,
                last_status="running",
                last_run=now,
                next_run=next_run(row, now),
            )
            started.append(task_id)
            worker = threading.Thread(target=self._guarded_run, args=(db, task_id), daemon=True)
            worker.start()
        return started

    def recover_missed(self, db=None) -> int:
        """Give tasks whose moment passed while the backend was down one run now.

        Only once, whatever the size of the gap: a task that was due four times
        overnight is owed one catch-up, not four.
        """
        if db is None:
            from db import get_db

            db = get_db()
        now = time.time()
        repaired = 0
        for row in db.list_all_tasks():
            if row.get("enabled") and row.get("next_run") and row["next_run"] < now - 60:
                db.update_task(row["id"], next_run=now)
                repaired += 1
        if repaired:
            _log(f"re-armed {repaired} task(s) that came due while the backend was down")
        return repaired

    # -- running one task --

    def _guarded_run(self, db, task_id: str) -> None:
        try:
            if not self._slots.acquire(blocking=False):
                # Over the cap: put it back in the queue instead of firing a
                # stampede of agent turns.
                db.update_task(task_id, last_status=None, last_run=None, next_run=time.time() + 60)
                return
            try:
                self.run_task(db, task_id)
            finally:
                self._slots.release()
        except Exception:
            traceback.print_exc()
            try:
                db.update_task(task_id, last_status="error", last_error=traceback.format_exc()[-600:])
            except Exception:
                pass
        finally:
            with self._lock:
                self._active.discard(task_id)

    def run_task(self, db, task_id: str) -> dict | None:
        """Execute one task now and record the outcome. Never raises."""
        row = db.get_task(task_id)
        if row is None:
            return None
        timeout = int(getattr(self.config, "TASKS_TIMEOUT_SECONDS", 600) or 600)
        started = time.time()
        try:
            text, error = run_task_turn(row, timeout=timeout)
        except Exception:
            traceback.print_exc()
            text, error = "", traceback.format_exc()[-1000:]
        if error:
            db.update_task(
                task_id,
                last_status="error",
                last_error=str(error)[:1000],
                last_output="",
                runs=int(row.get("runs") or 0) + 1,
                failures=int(row.get("failures") or 0) + 1,
                unread=1,
            )
            return db.get_task(task_id)
        db.update_task(
            task_id,
            last_status="ok",
            last_error="",
            last_output=(text or "")[:MAX_OUTPUT_CHARS],
            runs=int(row.get("runs") or 0) + 1,
            unread=1,
        )
        _log(f"task {task_id} finished in {time.time() - started:.1f}s")
        return db.get_task(task_id)

    def run_now(self, db, task_id: str, user_id: str) -> tuple[dict | None, str]:
        """Start a task immediately from the UI. Returns ``(task, error)``."""
        row = db.get_task(task_id, user_id)
        if row is None:
            return None, "not found"
        if not bool(getattr(self.config, "TASKS_ENABLED", True)):
            return None, "Scheduled tasks are disabled on this server (TASKS_ENABLED)."
        with self._lock:
            if task_id in self._active:
                return row, "already running"
            self._active.add(task_id)
        db.update_task(task_id, last_status="running")
        threading.Thread(target=self._guarded_run, args=(db, task_id), daemon=True).start()
        return db.get_task(task_id), ""


def _log(message: str) -> None:
    print(f"[tasks] {message}", flush=True)


def run_task_turn(row: dict, timeout: int = 600) -> tuple[str, str]:
    """Run one agent turn for a task. Returns ``(report, error)``.

    Deliberately not a Flask request: there is no session, no CSRF token and no
    browser. The task's conversation is its own, persisted under History, so a
    task that fails can be read afterwards and the next run has the previous one
    for context.

    The turn runs on its own thread so the timeout is real. A model provider
    that never answers would otherwise hold a runner slot for ever and the task
    would sit in `running` with nobody left to notice.
    """
    from auth import bearer_for_api, is_subscription_access
    from agent.base import AgentContext
    from agent.factory import build_engine, make_registry
    from config import config
    from db import get_db
    from memory.store import get_memory
    from models.providers import ProviderError, ProviderManager
    from skills.manager import get_skill_manager

    db = get_db()
    uid = str(row["user_id"])
    prompt = task_prompt(row)
    runtime = db.all_settings()

    conv_id = str(row.get("conversation_id") or "")
    conv = db.get_conversation(conv_id) if conv_id else None
    if conv is None or conv.get("user_id") != uid:
        conv = db.create_conversation(uid, title=f"Task: {row.get('title') or 'scheduled'}")
    db.update_task(row["id"], conversation_id=conv["id"])

    provider_name = (runtime.get("provider") or "openai").lower()
    engine_name = (runtime.get("engine") or "responses").lower()
    skill_manager = get_skill_manager()
    skill_name, call_authorized = task_skill_and_call_authorization(row, skill_manager)
    skill_obj = skill_manager.select(skill_name)

    try:
        provider = ProviderManager(
            bearer=bearer_for_api(uid),
            use_oauth_access=is_subscription_access(uid),
            runtime=runtime,
        ).build(provider_name, str(runtime.get("model") or ""))
    except ProviderError as exc:
        return "", f"no model provider available: {exc}"

    memory = None
    if bool(getattr(config, "MEMORY_ENABLED", False)):
        try:
            memory = get_memory()
        except Exception:
            memory = None

    user_row = db.get_user(uid) or {}
    system_prompt = get_skill_manager().build_system_prompt(
        skill_name,
        voice_mode=False,
        user_name=user_row.get("name") or "",
        response_language=runtime.get("response_language") or getattr(config, "RESPONSE_LANGUAGE", "en"),
        extra="",
    )
    system_prompt = system_prompt.rstrip() + (
        "\n\nYou are running unattended on a timer. There is no operator watching and "
        "no one to answer a question, so never ask for confirmation: act, then report."
    )
    if call_authorized:
        system_prompt += (
            "\n\nThe operator explicitly scheduled this phone call. This task itself "
            "authorizes the call, so do not request another confirmation. For a "
            "one-way notification, use the configured call-me number and let the "
            "tool end the call after the message. Do not call any other destination."
        )

    window = int(getattr(config, "TASKS_HISTORY_WINDOW", 20) or 20)
    history = [
        {"role": message["role"], "content": message["content"]}
        for message in db.list_messages(conv["id"])[-window:]
        if message["role"] in ("user", "assistant") and message.get("content")
    ]

    ctx = AgentContext(
        user_id=uid,
        conversation_id=conv["id"],
        system_prompt=system_prompt,
        history=history,
        provider=provider,
        provider_kind=provider_name,
        engine_name=engine_name,
        tools=make_registry(memory=memory),
        skill_tools=list(getattr(skill_obj, "tools", []) or []),
        require_tool=bool(getattr(skill_obj, "require_tool", False)),
        exclude_tools=list(getattr(skill_obj, "exclude_tools", ()) or ()),
        memory=memory,
        runtime=runtime,
        voice_mode=False,
        user_name=user_row.get("name") or "",
        autonomous_call_authorized=call_authorized,
    )

    db.add_message(conv["id"], "user", prompt, {"task_id": row["id"], "autonomous": True})
    report, error = _collect_stream(build_engine(engine_name, ctx), timeout)
    db.add_message(
        conv["id"], "assistant", report or "",
        {"task_id": row["id"], "autonomous": True, **({"error": error} if error else {})},
    )
    if error:
        return "", error
    if not report.strip():
        return "", "the model returned nothing"
    return report, ""


def _collect_stream(engine, timeout: int) -> tuple[str, str]:
    """Drain an engine on a worker thread so `timeout` is enforced."""
    parts: list[str] = []
    failure: list[str] = []

    def drain() -> None:
        try:
            for event in engine.stream():
                kind = event.get("type")
                if kind == "text_delta":
                    parts.append(str(event.get("content") or ""))
                elif kind == "error":
                    failure.append(str(event.get("message") or "agent error"))
                    return
        except Exception as exc:
            failure.append(f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=drain, daemon=True, name="apex-task-turn")
    worker.start()
    worker.join(timeout if timeout and timeout > 0 else None)
    if worker.is_alive():
        return "", f"the task did not finish within {timeout}s"
    return "".join(parts).strip(), (failure[0] if failure else "")


# ---- routes ------------------------------------------------------------


def _schedule_fields(text: str, now: float | None = None) -> tuple[Schedule, str]:
    try:
        return parse_schedule(text, now), ""
    except CronError as exc:
        return Schedule("once", run_at=0), str(exc)


def register_task_routes(app, require_user, config) -> None:
    """The Tasks tab's REST surface, plus the runner that keeps it ticking."""

    def db():
        from db import get_db

        return get_db()

    def not_found():
        # A task belonging to somebody else has to look exactly like one that
        # does not exist: answering 403 told a stranger the id was real.
        return jsonify({"error": "task not found"}), 404

    @app.get("/api/tasks")
    def list_tasks():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        rows = db().list_tasks(user["id"])
        return jsonify({
            "tasks": [public_task(row) for row in rows],
            "running": sorted(get_runner(config).running_task_ids & {row["id"] for row in rows}),
        })

    @app.post("/api/tasks")
    def create_task_route():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        if not bool(getattr(config, "TASKS_ENABLED", True)):
            return jsonify({"error": "Scheduled tasks are disabled on this server."}), 403
        data = request.get_json(silent=True) or {}
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "A task needs something to do."}), 400
        title = str(data.get("title") or "").strip() or prompt[:60]
        schedule, error = _schedule_fields(str(data.get("schedule") or "").strip())
        if error:
            return jsonify({"error": error}), 400
        limit = int(getattr(config, "TASKS_MAX_PER_USER", 50) or 50)
        if len(db().list_tasks(user["id"])) >= limit:
            return jsonify({"error": f"You already have {limit} tasks."}), 400
        row = create_task(
            db(), user["id"],
            title=title,
            prompt=prompt,
            schedule=schedule,
            plan=str(data.get("plan") or ""),
            skill=str(data.get("skill") or ""),
            enabled=bool(data.get("enabled", True)),
        )
        return jsonify(public_task(row)), 201

    @app.patch("/api/tasks/<task_id>")
    def update_task_route(task_id):
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        existing = db().get_task(task_id, user["id"])
        if not existing:
            return not_found()
        data = request.get_json(silent=True) or {}
        patch: dict = {}
        for key in ("title", "prompt", "plan", "skill"):
            if key in data:
                patch[key] = str(data[key] or "").strip()
        if "enabled" in data:
            patch["enabled"] = 1 if data["enabled"] else 0
        if str(data.get("schedule") or "").strip():
            schedule, error = _schedule_fields(str(data["schedule"]).strip())
            if error:
                return jsonify({"error": error}), 400
            patch["schedule"] = schedule.kind
            patch["cron"] = schedule.cron
            patch["run_at"] = schedule.run_at if not schedule.repeating else None
            patch["next_run"] = next_run(schedule)
        row = db().update_task(task_id, **patch) if patch else existing
        # Turning a task back on has to re-arm it: a paused row's next_run is in
        # the past, and leaving it there fires the whole backlog the moment it
        # is enabled.
        if patch.get("enabled") and not existing.get("enabled"):
            row = db().update_task(task_id, next_run=next_run(row)) or row
        return jsonify(public_task(row))

    @app.delete("/api/tasks/<task_id>")
    def delete_task_route(task_id):
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        if not db().get_task(task_id, user["id"]):
            return not_found()
        db().delete_task(task_id)
        return jsonify({"ok": True})

    @app.post("/api/tasks/<task_id>/run")
    def run_task_route(task_id):
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        row, error = get_runner(config).run_now(db(), task_id, user["id"])
        if row is None:
            return jsonify({"error": error or "task not found"}), 404
        return jsonify(public_task(row))

    @app.post("/api/tasks/ack")
    def ack_tasks():
        """Clear the unread flags. The browser calls this once it has shown the
        result, so a run is reported once no matter how many tabs are open."""
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        ids = data.get("ids")
        rows = db().list_tasks(user["id"])
        wanted = {str(value) for value in ids} if isinstance(ids, list) and ids else None
        for row in rows:
            if wanted is None or row["id"] in wanted:
                if row.get("unread"):
                    db().update_task(row["id"], unread=0)
        return jsonify({"ok": True})

    # A disabled deployment must not have a thread asking the database which
    # tasks are due: TASKS_ENABLED is meant to be a real off switch, not just a
    # refusal on the write routes. Reading it here too means a task created
    # before the flag was flipped stops firing.
    if bool(getattr(config, "TASKS_ENABLED", True)):
        get_runner(config).start()


_RUNNERS: dict[int, TaskRunner] = {}
_RUNNER_LOCK = threading.Lock()


def get_runner(config) -> TaskRunner:
    """The process-wide runner, keyed on the config object's identity.

    Keying on identity rather than the config *values* means a test that passes a
    throwaway stub gets its own runner (and its own thread) instead of sharing
    the live one, which would start firing real tasks during a unit test.
    """
    with _RUNNER_LOCK:
        runner = _RUNNERS.get(id(config))
        if runner is None:
            runner = TaskRunner(config)
            _RUNNERS[id(config)] = runner
        return runner


def schedule_summary(row: dict) -> str:
    """One line about a task for a spoken answer or a prompt block."""
    return json.dumps({
        "id": row.get("id"),
        "title": row.get("title"),
        "schedule": row.get("cron") or row.get("schedule"),
        "status": task_status(row),
    }, ensure_ascii=False)