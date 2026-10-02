import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask

from db import Database
from tools.base import ToolContext, ToolRegistry
from tools.cron import (
    CronError,
    describe_schedule,
    next_cron_run,
    next_run,
    parse_cron,
    parse_schedule,
)
from tools.task_tools import build_task_tools
from tools.tasks import (
    TaskRunner,
    create_task,
    public_task,
    register_task_routes,
    task_context_block,
    task_prompt,
    task_skill_and_call_authorization,
    task_status,
)

TUESDAY_MORNING = datetime(2026, 3, 10, 7, 30).timestamp()


def at(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute).timestamp()


class CronTests(unittest.TestCase):
    def test_five_field_expressions_parse(self):
        parsed, restricted = parse_cron("0 8 * * 1-5")
        self.assertEqual(parsed[0], {0})
        self.assertEqual(parsed[1], {8})
        self.assertEqual(parsed[4], {1, 2, 3, 4, 5})
        self.assertEqual(restricted, [True, True, False, False, True])

    def test_six_field_expressions_are_refused_with_a_reason(self):
        with self.assertRaises(CronError) as caught:
            parse_cron("30 0 8 * * 1-5")
        self.assertIn("five fields", str(caught.exception))

    def test_day_of_week_seven_is_sunday(self):
        parsed, _ = parse_cron("0 0 * * 7")
        self.assertEqual(parsed[4], {0})

    def test_month_and_day_names_work(self):
        parsed, _ = parse_cron("0 0 1 jan mon")
        self.assertEqual(parsed[2], {1})
        self.assertEqual(parsed[3], {1})
        self.assertEqual(parsed[4], {1})

    def test_next_run_is_strictly_after_the_given_moment(self):
        parsed, restricted = parse_cron("*/5 * * * *")
        self.assertEqual(
            datetime.fromtimestamp(next_cron_run(parsed, restricted, at(2026, 3, 10, 7, 30))),
            datetime(2026, 3, 10, 7, 35),
        )

    def test_a_schedule_exactly_now_fires_next_time_not_now(self):
        parsed, restricted = parse_cron("0 8 * * *")
        self.assertEqual(
            datetime.fromtimestamp(next_cron_run(parsed, restricted, at(2026, 3, 10, 8, 0))),
            datetime(2026, 3, 11, 8, 0),
        )

    def test_leap_day_schedule_is_found_four_years_out(self):
        parsed, restricted = parse_cron("0 0 29 2 *")
        self.assertEqual(
            datetime.fromtimestamp(next_cron_run(parsed, restricted, at(2026, 3, 10, 7, 30))),
            datetime(2028, 2, 29, 0, 0),
        )

    def test_day_of_month_and_weekday_are_combined_with_or(self):
        # Cron's traditional rule: restricting both means "either".
        parsed, restricted = parse_cron("0 0 13 * fri")
        self.assertTrue(restricted[2] and restricted[4])
        self.assertEqual(
            datetime.fromtimestamp(next_cron_run(parsed, restricted, at(2026, 3, 10, 12, 0))),
            datetime(2026, 3, 13, 0, 0),
        )

    def test_ambiguous_and_impossible_fields_are_refused(self):
        for bad in ("0 25 * * *", "0 8 * xyz *", "0 8 * * 9", "*/0 * * * *", "0 8 5-1 * *"):
            with self.subTest(bad=bad), self.assertRaises(CronError):
                parse_cron(bad)

    def test_loose_phrasing_becomes_a_cron_expression(self):
        cases = {
            "every 15 minutes": "*/15 * * * *",
            "every 2 hours": "0 */2 * * *",
            "every day at 08:30": "30 8 * * *",
            "every weekday at 09:15": "15 9 * * 1-5",
            "every monday at 6": "0 6 * * 1",
            "every weekend at 11:00": "0 11 * * 0,6",
            "@daily": "0 0 * * *",
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(parse_schedule(phrase, TUESDAY_MORNING).cron, expected)

    def test_relative_and_absolute_phrasings_become_one_off_runs(self):
        relative = parse_schedule("in 20 minutes", TUESDAY_MORNING)
        self.assertEqual(relative.kind, "once")
        self.assertAlmostEqual(relative.run_at, TUESDAY_MORNING + 1200, delta=1)

        absolute = parse_schedule("2026-03-12T08:30", TUESDAY_MORNING)
        self.assertEqual(absolute.kind, "once")
        self.assertEqual(datetime.fromtimestamp(absolute.run_at), datetime(2026, 3, 12, 8, 30))

    def test_a_past_one_off_is_refused_rather_than_silently_shifted(self):
        with self.assertRaises(CronError) as caught:
            parse_schedule("2020-01-01T08:00", TUESDAY_MORNING)
        self.assertIn("past", str(caught.exception))

    def test_a_weekly_request_is_not_a_daily_one(self):
        # "every week" used to resolve to `0 0 * * *`, so a weekly summary ran
        # every single day. A week is a weekday in cron, and the weekday has to
        # be the one the request came in on.
        schedule = parse_schedule("every week", TUESDAY_MORNING)
        self.assertEqual(schedule.cron, "0 9 * * 2")  # cron counts Sunday as 0
        # 07:30 on the Tuesday, so 09:00 that same Tuesday is the next firing.
        self.assertEqual(datetime.fromtimestamp(next_run(schedule, TUESDAY_MORNING)),
                         datetime(2026, 3, 10, 9, 0))
        # Every weekday has to land on itself. datetime.weekday() counts Monday
        # as 0 while a cron day-of-week counts Sunday as 0, so an unshifted
        # weekday put every Tuesday's run on Monday.
        for offset in range(7):
            with self.subTest(day=offset):
                moment = datetime(2026, 3, 8 + offset, 7, 30)  # Mar 8 is a Sunday
                weekly = parse_schedule("every week", moment.timestamp())
                fired = datetime.fromtimestamp(next_run(weekly, moment.timestamp()))
                self.assertEqual(fired.weekday(), moment.weekday())
                self.assertLessEqual((fired - moment).total_seconds(), 7 * 86400)
        # The clock in "every week at 18:30" was ignored too, so the same daily
        # expression came back with a midnight time.
        self.assertEqual(parse_schedule("every week at 18:30", TUESDAY_MORNING).cron,
                         "30 18 * * 2")
        # Every other week keeps the fortnight step, and now honours the clock.
        self.assertEqual(parse_schedule("every 2 weeks at 06:00", TUESDAY_MORNING).cron,
                         "0 6 */14 * *")

    def test_the_meridiem_is_read_not_guessed(self):
        # "6:30 pm" reported no meridiem at all, because the old check looked
        # for the substring "am" and found none, and the task was then set for
        # 06:30 in the morning. Silently wrong is the one thing a schedule may
        # not be.
        for phrase, expected in {
            "every 6 pm": "0 18 * * *",
            "every 6:30 pm": "30 18 * * *",
            "every 12 am": "0 0 * * *",
            "every 12 pm": "0 12 * * *",
            "every 6 am": "0 6 * * *",
            "every day at 6:30 pm": "30 18 * * *",
            "every weekday at 5 pm": "0 17 * * 1-5",
            "every monday at 7:45 am": "45 7 * * 1",
        }.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(parse_schedule(phrase, TUESDAY_MORNING).cron, expected)

        # "every 6 hours" must stay an interval, not become 18:00.
        self.assertEqual(parse_schedule("every 6 hours", TUESDAY_MORNING).cron, "0 */6 * * *")
        # A 24-hour reading has no meridiem and keeps its own value.
        self.assertEqual(parse_schedule("every 18:30", TUESDAY_MORNING).cron, "30 18 * * *")
        with self.assertRaises(CronError):
            parse_schedule("every 13 pm", TUESDAY_MORNING)

    def test_a_bare_clock_is_today_or_tomorrow(self):
        for phrase in ("6pm", "at 6 pm", "at 08:30"):
            with self.subTest(phrase=phrase):
                schedule = parse_schedule(phrase, TUESDAY_MORNING)
                self.assertEqual(schedule.kind, "once")
        # 07:30 local is already past, so 18:00 lands today and 06:00 tomorrow.
        self.assertEqual(datetime.fromtimestamp(
            parse_schedule("at 6 pm", TUESDAY_MORNING).run_at), datetime(2026, 3, 10, 18, 0))
        self.assertEqual(datetime.fromtimestamp(
            parse_schedule("6am", TUESDAY_MORNING).run_at), datetime(2026, 3, 11, 6, 0))

    def test_a_utc_offset_is_honoured_rather_than_read_as_local(self):
        # The browser hands back toISOString() for a one-off edit, always with a
        # Z. Reading that as machine-local moved the task by the machine's
        # offset and it simply fired late.
        for stamp in ("2026-03-12T08:30:00.000Z", "2026-03-12T08:30:00Z", "2026-03-12T08:30Z"):
            with self.subTest(stamp=stamp):
                run = parse_schedule(stamp, TUESDAY_MORNING)
                self.assertEqual(run.kind, "once")
                self.assertEqual(datetime.fromtimestamp(run.run_at, timezone.utc),
                                 datetime(2026, 3, 12, 8, 30, tzinfo=timezone.utc))
        # +03:00 at 08:30 is 05:30Z, which is 08:30 on a machine in +03:00.
        offset = parse_schedule("2026-03-12T08:30:00+03:00", TUESDAY_MORNING)
        self.assertEqual(datetime.fromtimestamp(offset.run_at, timezone.utc),
                         datetime(2026, 3, 12, 5, 30, tzinfo=timezone.utc))
        # No offset at all still means machine-local, which is what the editor
        # writes so the field is readable.
        naive = parse_schedule("once at 2026-03-12T08:30", TUESDAY_MORNING)
        self.assertEqual(datetime.fromtimestamp(naive.run_at), datetime(2026, 3, 12, 8, 30))
        with self.assertRaises(CronError):
            parse_schedule("2026-03-12T08:30:00+99:00", TUESDAY_MORNING)

    def test_sub_minute_schedules_are_refused_with_the_shortest_allowed(self):
        with self.assertRaises(CronError) as caught:
            parse_schedule("every 30 seconds", TUESDAY_MORNING)
        self.assertIn("one minute", str(caught.exception))

    def test_nonsense_schedules_explain_what_is_accepted(self):
        with self.assertRaises(CronError) as caught:
            parse_schedule("whenever i feel like it", TUESDAY_MORNING)
        message = str(caught.exception)
        self.assertIn("cron", message)
        self.assertIn("every 15 minutes", message)

    def test_next_run_accepts_a_task_row(self):
        row = {"schedule": "cron", "cron": "0 9 * * *"}
        self.assertEqual(
            datetime.fromtimestamp(next_run(row, TUESDAY_MORNING)),
            datetime(2026, 3, 10, 9, 0),
        )
        once = {"schedule": "once", "run_at": TUESDAY_MORNING + 500}
        self.assertEqual(next_run(once, TUESDAY_MORNING), TUESDAY_MORNING + 500)

    def test_descriptions_report_the_step_not_the_widest_value(self):
        # `*/5` expands to {0,5,...,55}; describing that as "every 55 minutes"
        # would tell the operator their five-minute job was hourly.
        self.assertEqual(describe_schedule("*/5 * * * *"), "every 5 minutes")
        self.assertEqual(describe_schedule("0 8 * * 1-5"), "at 08:00 on weekdays")
        self.assertEqual(describe_schedule("0 0 * * 0,6"), "at 00:00 on Sun, Sat")
        self.assertEqual(describe_schedule("0 0 29 2 *"), "at 00:00 on day 29 in Feb")
        self.assertEqual(describe_schedule("not a cron"), "not a cron")


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "tasks.db")

    def _task(self, **overrides):
        schedule = parse_schedule(overrides.pop("schedule", "0 9 * * *"), time.time())
        return create_task(self.db, "alice", title="Disk check", prompt="df -h /", schedule=schedule, **overrides)

    def test_creating_a_task_computes_its_first_run(self):
        row = self._task()
        self.assertEqual(row["schedule"], "cron")
        self.assertEqual(row["cron"], "0 9 * * *")
        self.assertGreater(row["next_run"], time.time())
        self.assertEqual(row["runs"], 0)
        self.assertEqual(task_status(row), "pending")

    def test_tasks_are_scoped_to_their_owner(self):
        row = self._task()
        self.assertIsNotNone(self.db.get_task(row["id"], "alice"))
        self.assertIsNone(self.db.get_task(row["id"], "bob"))
        self.assertEqual([item["id"] for item in self.db.list_tasks("bob")], [])

    def test_a_disabled_task_is_never_due(self):
        row = self._task()
        self.db.update_task(row["id"], enabled=0, next_run=time.time() - 10)
        self.assertEqual(self.db.due_tasks(time.time()), [])

    def test_public_shape_carries_the_display_fields_the_tab_needs(self):
        row = self._task()
        public = public_task(row)
        self.assertTrue(public["repeating"])
        self.assertEqual(public["schedule_label"], "at 09:00")
        self.assertEqual(public["status"], "pending")
        self.assertIs(public["enabled"], True)

    def test_status_words_cover_pause_running_and_outcome(self):
        row = self._task()
        self.assertEqual(task_status({**row, "enabled": 0}), "paused")
        self.assertEqual(task_status({**row, "last_status": "running"}), "running")
        self.assertEqual(task_status({**row, "last_status": "ok", "runs": 2}), "ok")
        self.assertEqual(task_status({**row, "last_status": "error", "runs": 2}), "error")

    def test_the_context_block_names_tasks_the_model_can_act_on(self):
        row = self._task()
        block = task_context_block("alice", self.db)
        self.assertIn("Disk check", block)
        self.assertIn("#1", block)
        self.assertIn(row["cron"], block)
        self.assertEqual(task_context_block("bob", self.db), "")

    def test_the_due_prompt_forbids_asking_and_asks_for_a_report(self):
        row = self._task(plan="terminal_command: df -h /")
        prompt = task_prompt(row)
        self.assertIn("df -h /", prompt)
        self.assertIn("nobody is watching", prompt.lower())
        self.assertIn("report", prompt.lower())


class TaskSkillRoutingTests(unittest.TestCase):
    def setUp(self):
        self.skills = SimpleNamespace(all=lambda: [SimpleNamespace(name="SIP")])

    def test_scheduled_call_me_prompt_selects_sip_and_authorizes_that_task(self):
        row = {"prompt": "Check free disk space hourly and call me if it exceeds 80%"}
        self.assertEqual(task_skill_and_call_authorization(row, self.skills), ("SIP", True))

    def test_unrelated_tasks_keep_their_selected_skill(self):
        row = {"prompt": "Check free disk space", "skill": "general"}
        self.assertEqual(task_skill_and_call_authorization(row, self.skills), ("general", False))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "tasks.db")
        self.config = SimpleNamespace(
            TASKS_ENABLED=True, TASKS_MAX_CONCURRENT=1, TASKS_TICK_SECONDS=3600,
            TASKS_TIMEOUT_SECONDS=30, TASKS_MAX_PER_USER=50, TASKS_CATCH_UP=False,
        )
        self.runner = TaskRunner(self.config)

    def _task(self, **overrides):
        schedule = parse_schedule(overrides.pop("schedule", "0 9 * * *"), time.time())
        return create_task(self.db, "alice", title="Disk check", prompt="df -h /", schedule=schedule, **overrides)

    def test_a_due_task_is_claimed_exactly_once_per_tick(self):
        row = self._task()
        self.db.update_task(row["id"], next_run=time.time() - 1)
        runner = TaskRunner(self.config)
        # run_task is replaced so the test never talks to a model provider.
        runner.run_task = lambda db, task_id: db.update_task(task_id, last_status="ok")
        claimed = []
        original = runner._guarded_run

        def record(db, task_id):
            claimed.append(task_id)
            original(db, task_id)

        runner._guarded_run = record
        runner.tick(self.db)
        for _ in range(30):
            time.sleep(0.02)
            if claimed and task_status(self.db.get_task(row["id"])) == "ok":
                break
        self.assertEqual(claimed, [row["id"]])
        # The next_run moved on, so a second tick does not fire it again.
        self.assertGreater(self.db.get_task(row["id"])["next_run"], time.time())

    def test_a_task_that_is_not_due_is_left_alone(self):
        row = self._task()
        self.assertEqual(self.runner.tick(self.db), [])
        self.assertIsNone(self.db.get_task(row["id"])["last_run"])

    def test_recover_missed_re_arms_once_not_once_per_missed_slot(self):
        row = self._task()
        stale = time.time() - 4 * 86400
        self.db.update_task(row["id"], next_run=stale)
        self.assertEqual(self.runner.recover_missed(self.db), 1)
        after = self.db.get_task(row["id"])["next_run"]
        self.assertLess(abs(after - time.time()), 30)
        # A second call finds nothing stale any more.
        self.assertEqual(self.runner.recover_missed(self.db), 0)

    def test_a_failure_is_recorded_as_an_error_and_counted(self):
        row = self._task()

        def boom(db, task_id):
            db.update_task(task_id, last_status="error", last_error="nope",
                           runs=1, failures=1, unread=1)

        self.runner.run_task = boom
        self.runner.run_task(self.db, row["id"])
        stored = self.db.get_task(row["id"])
        self.assertEqual(stored["last_status"], "error")
        self.assertEqual(stored["last_error"], "nope")
        self.assertEqual(stored["failures"], 1)


class TaskToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = SimpleNamespace(TASKS_ENABLED=True, TASKS_MAX_PER_USER=50)
        self.tools = {tool.name: tool for tool in build_task_tools(self.config)}
        self.ctx = ToolContext(user_id="alice")
        import db as db_module

        self._original_db = db_module.get_db
        db_module._db = Database(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(setattr, db_module, "_db", self._original_db)

    def call(self, name, args, ctx=None):
        return self.tools[name].call(args, ctx or self.ctx)

    def test_schedule_creates_the_task_and_says_when_it_first_runs(self):
        result = self.call("task_schedule", {
            "title": "Disk check", "prompt": "run df -h /",
            "schedule": "0 9 * * 1-5", "plan": "terminal_command: df -h /",
        })
        self.assertIn("Scheduled", result)
        self.assertIn("weekdays", result)
        listed = self.call("task_list", {})
        self.assertIn("Disk check", listed)

    def test_a_bad_schedule_is_refused_with_the_reason(self):
        result = self.call("task_schedule", {"prompt": "x", "schedule": "0 25 * * *"})
        self.assertIn("Schedule rejected", result)
        self.assertIn("hour", result)

    def test_schedule_requires_a_prompt(self):
        self.assertIn("needs a prompt", self.call("task_schedule", {"schedule": "@daily"}))

    def test_signed_out_users_get_no_tasks(self):
        anonymous = ToolContext()
        self.assertIn("Sign in", self.call("task_schedule", {"prompt": "x", "schedule": "@daily"}, anonymous))
        self.assertIn("Sign in", self.call("task_list", {}, anonymous))

    def test_tasks_can_be_addressed_by_their_visible_number(self):
        self.call("task_schedule", {"title": "First", "prompt": "a", "schedule": "@daily"})
        self.call("task_schedule", {"title": "Second", "prompt": "b", "schedule": "@daily"})
        self.assertIn('Second', self.call("task_delete", {"task_id": "2"}))
        self.assertIn("First", self.call("task_list", {}))
        self.assertNotIn("Second", self.call("task_list", {}))

    def test_an_out_of_range_number_says_how_many_tasks_there_are(self):
        self.call("task_schedule", {"title": "Only", "prompt": "a", "schedule": "@daily"})
        self.assertIn("no task #7", self.call("task_status", {"task_id": "7"}))

    def test_status_reports_the_last_outcome(self):
        self.call("task_schedule", {"title": "Disk", "prompt": "df", "schedule": "@daily"})
        import db as db_module

        row = db_module.get_db().list_tasks("alice")[0]
        db_module.get_db().update_task(row["id"], last_status="ok", last_output="root is 41% full",
                                       runs=2, unread=1)
        result = self.call("task_status", {"task_id": "1"})
        self.assertIn("root is 41% full", result)
        self.assertIn("2", result)

    def test_update_pauses_and_resumes(self):
        self.call("task_schedule", {"title": "Disk", "prompt": "df", "schedule": "0 9 * * *"})
        self.assertIn("paused", self.call("task_update", {"task_id": "1", "enabled": False}))
        self.assertIn("updated", self.call("task_update", {"task_id": "1", "enabled": True}))
        # Resuming re-arms: a paused row's next_run is in the past, and leaving
        # it there would fire the whole backlog.
        import db as db_module

        self.assertGreater(db_module.get_db().list_tasks("alice")[0]["next_run"], time.time())

    def test_update_can_replace_the_schedule(self):
        self.call("task_schedule", {"title": "Disk", "prompt": "df", "schedule": "0 9 * * *"})
        result = self.call("task_update", {"task_id": "1", "schedule": "*/30 * * * *"})
        self.assertIn("updated", result)
        import db as db_module

        row = db_module.get_db().list_tasks("alice")[0]
        self.assertEqual(row["cron"], "*/30 * * * *")
        # Asserted on the stored next_run rather than on the human label, which
        # used to be `assertIn("30", ...)`. That passed only while the clock was
        # in the first half hour: `*/30` also fires on the hour, so a run at
        # 10:56 correctly reads "11:00" and the substring check failed on a
        # correct answer. The schedule this is really about is the boundary.
        self.assertIn(datetime.fromtimestamp(row["next_run"]).minute, (0, 30))

    def test_another_users_task_is_not_reachable(self):
        self.call("task_schedule", {"title": "Mine", "prompt": "x", "schedule": "@daily"})
        import db as db_module

        row = db_module.get_db().list_tasks("alice")[0]
        bob = ToolContext(user_id="bob")
        self.assertIn("No task matches", self.call("task_status", {"task_id": row["id"]}, bob))
        self.assertIn("no task #1", self.call("task_status", {"task_id": "1"}, bob))

    def test_a_registration_broadcast_reaches_the_browser(self):
        seen = []
        ctx = ToolContext(user_id="alice", emit=seen.append)
        self.call("task_schedule", {"title": "Disk", "prompt": "df", "schedule": "@daily"}, ctx)
        self.assertEqual([event["type"] for event in seen], ["task_changed"])
        self.assertEqual(seen[0]["action"], "created")


class TaskRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import db as db_module

        self._original_db = db_module._db
        db_module._db = Database(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(setattr, db_module, "_db", self._original_db)
        self.config = SimpleNamespace(
            TASKS_ENABLED=True, TASKS_MAX_CONCURRENT=1, TASKS_TICK_SECONDS=3600,
            TASKS_TIMEOUT_SECONDS=5, TASKS_MAX_PER_USER=50, TASKS_CATCH_UP=False,
        )
        self.app = Flask(__name__)
        self.app.secret_key = "test-secret"

        def require_user():
            from flask import session

            uid = session.get("user_id")
            return db_module.get_db().get_user(uid) if uid else None

        register_task_routes(self.app, require_user, self.config)
        # The runner thread is irrelevant to routing and only makes the test
        # suite slower, so it is told never to tick.
        from tools.tasks import get_runner

        get_runner(self.config).stop()
        self.client = self.app.test_client()

    def login(self, uid="alice"):
        import db as db_module

        # require_user() looks the session's user id up in the users table, so
        # a session with only an id in it is signed out as far as the API is
        # concerned. Mirrors what the real login route does.
        db_module.get_db().upsert_user(
            user_id=uid, name=f"Test {uid}", email=f"{uid}@apex.local",
            picture="", tokens=None, token_scopes="",
        )
        with self.client.session_transaction() as session:
            session.clear()
            session["user_id"] = uid
            session["sid"] = "test-sid"

    def create(self, **body):
        return self.client.post("/api/tasks", json={
            "title": "Disk check", "prompt": "df -h /", "schedule": "0 9 * * *", **body,
        })

    def test_the_api_needs_a_session(self):
        self.assertEqual(self.client.get("/api/tasks").status_code, 401)
        self.assertEqual(self.create().status_code, 401)

    def test_a_task_can_be_created_listed_edited_and_deleted_by_hand(self):
        self.login()
        created = self.create(plan="df -h /")
        self.assertEqual(created.status_code, 201)
        task_id = created.get_json()["id"]

        listed = self.client.get("/api/tasks").get_json()["tasks"]
        self.assertEqual([row["id"] for row in listed], [task_id])
        self.assertEqual(listed[0]["schedule_label"], "at 09:00")

        edited = self.client.patch(f"/api/tasks/{task_id}", json={
            "title": "Disk usage", "schedule": "*/30 * * * *", "enabled": False,
        })
        self.assertEqual(edited.status_code, 200)
        body = edited.get_json()
        self.assertEqual(body["title"], "Disk usage")
        self.assertEqual(body["cron"], "*/30 * * * *")
        self.assertEqual(body["status"], "paused")

        self.assertEqual(self.client.delete(f"/api/tasks/{task_id}").status_code, 200)
        self.assertEqual(self.client.get("/api/tasks").get_json()["tasks"], [])

    def test_a_task_with_no_prompt_is_refused(self):
        self.login()
        response = self.client.post("/api/tasks", json={"prompt": "  ", "schedule": "@daily"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("needs something to do", response.get_json()["error"])

    def test_a_bad_schedule_is_refused_with_the_reason(self):
        self.login()
        response = self.client.post("/api/tasks", json={"prompt": "df", "schedule": "whenever"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("cron", response.get_json()["error"])

    def test_someone_elses_task_is_indistinguishable_from_a_missing_one(self):
        self.login("alice")
        task_id = self.create().get_json()["id"]
        self.login("bob")
        self.assertEqual(self.client.get("/api/tasks").get_json()["tasks"], [])
        self.assertEqual(self.client.patch(f"/api/tasks/{task_id}", json={"title": "mine now"}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/tasks/{task_id}").status_code, 404)
        self.assertEqual(self.client.post(f"/api/tasks/{task_id}/run").status_code, 404)

    def test_the_per_user_cap_is_enforced(self):
        self.login()
        self.config.TASKS_MAX_PER_USER = 1
        self.assertEqual(self.create().status_code, 201)
        second = self.client.post("/api/tasks", json={"prompt": "b", "schedule": "@daily"})
        self.assertEqual(second.status_code, 400)
        self.assertIn("already have 1 task", second.get_json()["error"])
        # The refusal is about the limit, not a schedule problem.
        self.assertNotIn("cron", second.get_json()["error"])

    def test_the_cap_counts_only_my_tasks(self):
        self.config.TASKS_MAX_PER_USER = 1
        self.login("alice")
        self.assertEqual(self.create().status_code, 201)
        self.login("bob")
        self.assertEqual(self.create().status_code, 201)

    def test_reading_the_list_clears_nothing_until_it_is_acked(self):
        self.login()
        task_id = self.create().get_json()["id"]
        import db as db_module

        db_module.get_db().update_task(task_id, last_status="ok", last_output="all clear", unread=1)
        self.assertTrue(self.client.get("/api/tasks").get_json()["tasks"][0]["unread"])
        self.assertEqual(self.client.post("/api/tasks/ack", json={"ids": [task_id]}).status_code, 200)
        self.assertFalse(self.client.get("/api/tasks").get_json()["tasks"][0]["unread"])

    def test_run_now_starts_the_task_and_marks_it_running(self):
        self.login()
        task_id = self.create().get_json()["id"]
        import db as db_module
        from tools.tasks import get_runner

        runner = get_runner(self.config)
        self.addCleanup(runner.stop)
        runner.run_task = lambda db, tid: db.update_task(tid, last_status="ok", last_output="done")
        response = self.client.post(f"/api/tasks/{task_id}/run")
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.get_json()["status"], ("running", "ok"))
        for _ in range(50):
            time.sleep(0.02)
            if self.client.get("/api/tasks").get_json()["tasks"][0]["status"] == "ok":
                break
        self.assertEqual(self.client.get("/api/tasks").get_json()["tasks"][0]["last_output"], "done")


class TaskToolAvailabilityTests(unittest.TestCase):
    def test_disabling_tasks_removes_the_tools_entirely(self):
        self.assertEqual(build_task_tools(SimpleNamespace(TASKS_ENABLED=False)), [])

    def test_disabling_tasks_stops_the_runner_thread_too(self):
        # TASKS_ENABLED has to be a real off switch. A thread left ticking would
        # keep firing tasks created before the flag was flipped, which is the one
        # thing an operator turning it off is trying to prevent.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        import db as db_module

        original = db_module._db
        db_module._db = Database(Path(tmp.name) / "tasks.db")
        self.addCleanup(setattr, db_module, "_db", original)
        disabled = SimpleNamespace(
            TASKS_ENABLED=False, TASKS_MAX_CONCURRENT=1, TASKS_TICK_SECONDS=3600,
            TASKS_TIMEOUT_SECONDS=5, TASKS_MAX_PER_USER=5, TASKS_CATCH_UP=False,
        )
        flask_app = Flask(__name__)
        register_task_routes(flask_app, lambda: {"id": "alice"}, disabled)
        from tools.tasks import get_runner

        runner = get_runner(disabled)
        self.addCleanup(runner.stop)
        self.assertIsNone(runner._thread)

    def test_every_skill_can_reach_the_task_tools(self):
        from agent.base import ALWAYS_ON_TOOLS, AgentContext

        self.assertIn("task_schedule", ALWAYS_ON_TOOLS)
        registry = ToolRegistry()
        for tool in build_task_tools(SimpleNamespace(TASKS_ENABLED=True, TASKS_MAX_PER_USER=5)):
            registry.register(tool)
        ctx = AgentContext(
            user_id="alice", conversation_id="c", system_prompt="", history=[],
            provider=None, provider_kind="test", engine_name="test",
            tools=registry, skill_tools=["something_else"],
        )
        names = [tool.name for tool in ctx.active_tools()]
        self.assertIn("task_schedule", names)
        self.assertIn("task_list", names)


if __name__ == "__main__":
    unittest.main()
