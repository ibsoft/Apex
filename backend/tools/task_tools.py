"""Scheduled-task tools: the agent creating, editing and running its own work.

This is where "every weekday at eight, check the disk" becomes a cron
expression and a plan. The conversion is the model's job, not a regex's, so
`task_schedule` accepts whatever the operator actually said and only insists on a
schedule it can honour.

The `plan` argument is not decoration. It is the translation of the prompt into
concrete commands, written down once so the run four weeks later does not have
to re-interpret the same sentence - and so the operator can read in the Tasks tab
exactly what APEX decided it was going to do, and correct it before it happens.
"""

from __future__ import annotations

import json

from tools.base import Tool, ToolContext
from tools.cron import CronError, describe_schedule, next_run, parse_schedule
from tools.tasks import MAX_OUTPUT_CHARS, create_task, public_task, task_status

SCHEDULE_GUIDE = """\
`schedule` accepts any of these:
  - a five-field cron expression: "*/5 * * * *", "0 8 * * 1-5", "30 9 * * mon-fri",
    "0 9-17 * * *" (minute hour day-of-month month day-of-week, server local time,
    day-of-week 0-6 with 0=Sunday). Six fields with seconds are rejected.
  - a macro: "@hourly", "@daily", "@midnight", "@weekly", "@monthly", "@yearly".
  - a one-off wall-clock time: "2026-03-12T08:30" or "2026-03-12 08:30".
  - a relative delay for a one-off: "in 30 minutes", "in 2 hours".
  - a repeating phrase: "every 15 minutes", "every 2 hours", "every day at 08:30",
    "every weekday at 09:00", "every monday at 06:00".
Prefer a cron expression whenever the operator named a time of day, and say the
concrete time back to them so a wrong conversion is caught immediately. The
shortest schedule is one minute. If the operator gave no time at all, pick a
sensible one and tell them which."""

SCHEDULE_DESC = (
    "Schedule a recurring or one-off job that you run yourself. Convert the "
    "operator's words into `schedule` yourself and pass it as a cron expression "
    "whenever they named a time of day. " + SCHEDULE_GUIDE
)

PLAN_DESC = (
    "The concrete steps you will take, written as commands or tool calls "
    "(e.g. `terminal_command: df -h /` then report anything over 90%). Read by "
    "the operator in the Tasks tab and shown to you at the start of every run."
)


def _db():
    from db import get_db

    return get_db()


def _require_user(ctx: ToolContext) -> str:
    return "" if ctx.user_id else "Sign in to manage scheduled tasks."


def _numbered(db, user_id: str) -> list[dict]:
    """Tasks in the same order the Tasks tab renders them.

    The number is what the operator says out loud ("run task 2"), so it has to be
    derived from the visible order rather than from creation order or the
    backend's internal one.
    """
    return db.list_tasks(user_id)


def _find(rows: list[dict], task_id: str) -> tuple[dict | None, str]:
    """Resolve a task id, a unique id prefix, or the visible list number.

    Prefixes are resolved because the Tasks tab shows short ids and an operator
    dictating one inevitably shortens it.
    """
    wanted = (task_id or "").strip()
    if wanted.isdigit():
        index = int(wanted)
        if 1 <= index <= len(rows):
            return rows[index - 1], ""
        return None, f"There is no task #{index}. You have {len(rows)}."
    for row in rows:
        if row["id"] == wanted:
            return row, ""
    matches = [row for row in rows if str(row["id"]).startswith(wanted)]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, f"{wanted!r} matches {len(matches)} tasks; use more of the id."
    return None, f"No task matches {task_id!r}."


def build_task_tools(config) -> list[Tool]:
    if not bool(getattr(config, "TASKS_ENABLED", True)):
        return []

    def t_task_schedule(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        title = (args.get("title") or "").strip()
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "A task needs a prompt: what should you do when it comes due?"
        try:
            schedule = parse_schedule(str(args.get("schedule") or ""))
        except CronError as exc:
            return f"Schedule rejected: {exc}"
        db = _db()
        limit = int(getattr(config, "TASKS_MAX_PER_USER", 50) or 50)
        if len(db.list_tasks(ctx.user_id)) >= limit:
            return f"You already have {limit} tasks; delete one before adding another."
        row = create_task(
            db, ctx.user_id,
            title=title or prompt[:60],
            prompt=prompt,
            schedule=schedule,
            plan=str(args.get("plan") or "").strip(),
            skill=str(args.get("skill") or "").strip(),
        )
        if ctx.emit:
            ctx.emit({"type": "task_changed", "action": "created", "task_id": row["id"],
                      "title": row["title"]})
        when = _when_text(row)
        # The model is told to repeat the schedule in plain words, so it is
        # handed the words: "0 9 * * 1-5" in a reply is not something it can
        # safely paraphrase, and a guess would be the user trusting a wrong
        # schedule.
        label = public_task(row)["schedule_label"]
        return (
            f"Scheduled \"{row['title']}\" (#{_position(row)}) - {label}, "
            f"next run {when}. It runs on its own; the operator does not need to "
            f"ask again. Repeat the schedule in plain words and say when it "
            f"first runs."
        )

    def t_task_list(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        rows = _numbered(_db(), ctx.user_id)
        if not rows:
            return "No scheduled tasks. Create one with task_schedule when the operator asks for something recurring."
        lines = []
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"#{index} {row['title']!r} [{task_status(row)}] "
                f"{row.get('cron') or 'one-off'} ({_when_text(row)}), "
                f"{int(row.get('runs') or 0)} run(s), id={row['id']}"
            )
        return "\n".join(lines)

    def t_task_status(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        rows = _numbered(_db(), ctx.user_id)
        row, error = _find(rows, str(args.get("task_id") or ""))
        if row is None:
            return error
        parts = [
            f"Task #{_position(row, rows)} {row['title']!r}: {task_status(row)}.",
            f"Schedule: {row.get('cron') or 'one-off'} ({describe_schedule(row.get('cron') or '')}).",
            f"{_when_text(row)}.",
            f"Runs: {int(row.get('runs') or 0)} ({int(row.get('failures') or 0)} failed).",
        ]
        if row.get("last_run"):
            parts.append(f"Last ran: {row['last_run']}")
        if row.get("last_error"):
            parts.append(f"Last error: {str(row['last_error'])[:400]}")
        if row.get("last_output"):
            parts.append(f"Last report: {str(row['last_output'])[:MAX_OUTPUT_CHARS]}")
        return "\n".join(parts)

    def t_task_update(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        db = _db()
        rows = _numbered(db, ctx.user_id)
        row, error = _find(rows, str(args.get("task_id") or ""))
        if row is None:
            return error
        patch: dict = {}
        for key in ("title", "prompt", "plan", "skill"):
            if args.get(key):
                patch[key] = str(args[key]).strip()
        if str(args.get("schedule") or "").strip():
            try:
                schedule = parse_schedule(str(args["schedule"]))
            except CronError as exc:
                return f"Schedule rejected: {exc}"
            patch["schedule"] = schedule.kind
            patch["cron"] = schedule.cron
            patch["run_at"] = schedule.run_at if not schedule.repeating else None
            patch["next_run"] = next_run(schedule)
        if args.get("enabled") is not None:
            patch["enabled"] = 1 if args["enabled"] else 0
        if not patch:
            return "Nothing to change. Pass at least one of title, prompt, plan, schedule, skill, enabled."
        updated = db.update_task(row["id"], **patch)
        if patch.get("enabled") and not row.get("enabled"):
            # A paused task's next_run is in the past; re-arm it or it fires the
            # whole backlog the instant it comes back.
            updated = db.update_task(row["id"], next_run=next_run(updated)) or updated
        if ctx.emit:
            ctx.emit({"type": "task_changed", "action": "updated", "task_id": row["id"],
                      "title": updated["title"]})
        return f"Task #{_position(updated, rows)} \"{updated['title']}\" updated - {_when_text(updated)}."

    def t_task_delete(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        db = _db()
        row, error = _find(_numbered(db, ctx.user_id), str(args.get("task_id") or ""))
        if row is None:
            return error
        db.delete_task(row["id"])
        if ctx.emit:
            ctx.emit({"type": "task_changed", "action": "deleted", "task_id": row["id"],
                      "title": row["title"]})
        return f"Deleted task \"{row['title']}\"."

    def t_task_run_now(args: dict, ctx: ToolContext):
        if problem := _require_user(ctx):
            return problem
        db = _db()
        rows = _numbered(db, ctx.user_id)
        row, error = _find(rows, str(args.get("task_id") or ""))
        if row is None:
            return error
        from tools.tasks import get_runner

        started, problem = get_runner(config).run_now(db, row["id"], ctx.user_id)
        if started is None:
            return problem
        if ctx.emit:
            ctx.emit({"type": "task_changed", "action": "started", "task_id": row["id"],
                      "title": row["title"]})
        return (
            f"Task #{_position(row, rows)} \"{row['title']}\" is running now in the background. "
            "It will report to the operator when it finishes; do not wait for it or "
            "invent its result."
        )

    def t_task_names(args: dict, ctx: ToolContext):
        """The tools a task may run, for when the operator asks what it can do."""
        rows = _numbered(_db(), ctx.user_id)
        return json.dumps([
            {"n": index, "id": row["id"], "title": row["title"], "status": task_status(row)}
            for index, row in enumerate(rows, start=1)
        ], ensure_ascii=False)

    schedule_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short name for the Tasks tab, e.g. 'Disk usage check'."},
            "prompt": {"type": "string", "description": "What to do when this comes due, in your own words. This is the whole instruction."},
            "schedule": {"type": "string", "description": SCHEDULE_GUIDE},
            "plan": {"type": "string", "description": PLAN_DESC},
            "skill": {"type": "string", "description": "Optional skill to run it under; call tasks are automatically routed to SIP."},
        },
        "required": ["prompt", "schedule"],
        "additionalProperties": False,
    }
    id_only = {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}
    numbered_id = dict(id_only, description="Task id, the start of one, or its number in the Tasks tab.")

    return [
        Tool("task_schedule", SCHEDULE_DESC, schedule_schema, t_task_schedule),
        Tool("task_list", "List the scheduled tasks you own, with their schedule, status and run count.",
             {"type": "object", "properties": {}}, t_task_list),
        Tool("task_status", "What happened on the last run of one task: status, error and the report it produced.",
             numbered_id, t_task_status),
        Tool("task_update", "Edit a task: retitle it, change what it does or when it runs, or pause/resume it.",
             {
                 "type": "object",
                 "properties": {
                     **id_only["properties"],
                     "title": {"type": "string"},
                     "prompt": {"type": "string"},
                     "plan": {"type": "string", "description": PLAN_DESC},
                     "schedule": {"type": "string", "description": "The new schedule, in any accepted form."},
                     "skill": {"type": "string"},
                     "enabled": {"type": "boolean", "description": "false pauses it, true resumes it."},
                 },
                 "required": ["task_id"],
             }, t_task_update),
        Tool("task_delete", "Delete a scheduled task for good.",
             numbered_id, t_task_delete),
        Tool("task_run_now", "Run a task immediately instead of waiting for its schedule.",
             numbered_id, t_task_run_now),
        Tool("task_index", "The numbered list of task ids, for addressing tasks by position.",
             {"type": "object", "properties": {}}, t_task_names),
    ]


# ---- helpers -----------------------------------------------------------


def _position(row: dict, rows: list[dict] | None = None) -> int:
    """The task's number in the visible list, or 0 when it is not the caller's."""
    if rows is None:
        try:
            rows = _db().list_tasks(str(row.get("user_id") or ""))
        except Exception:
            return 0
    for index, candidate in enumerate(rows, start=1):
        if candidate["id"] == row.get("id"):
            return index
    return 0


def _when_text(row: dict) -> str:
    """When it next runs, or that it has finished, in words."""
    from datetime import datetime

    if not row.get("enabled"):
        return "paused"
    if (row.get("schedule") or "cron") == "once" and row.get("last_run"):
        return "already run, one-off"
    if not row.get("next_run"):
        return "no further runs scheduled"
    moment = datetime.fromtimestamp(float(row["next_run"]))
    return "next run " + moment.strftime("%Y-%m-%d %H:%M")
