"""Sub-agents: manager CRUD/validation, run orchestration, the model-facing
tools, the AGENTS tab REST surface, and the strict tool-scoping a sub-agent turn
relies on.

The engine/provider is the only thing stubbed: ``subagents.runner.run_subagent_turn``
is replaced in the orchestration tests, because a real turn needs a model. Every
other seam is the real code.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask

import subagents.routes as routes_mod
import subagents.runner as runner_mod
from db import Database
from subagents.manager import SubAgentError, SubAgentManager, subagent_context_block
from tools.base import ToolContext, ToolRegistry
from tools.subagent_tools import build_subagent_tools


def make_config(tmp: str, **overrides):
    values = dict(
        DATA_DIR=tmp,
        SUBAGENTS_DIR=Path(tmp) / "subagents",
        SUBAGENTS_ENABLED=True,
        SUBAGENT_TIMEOUT_SECONDS=30,
        SUBAGENT_MAX_CONCURRENT=2,
        SUBAGENT_MAX_PER_USER=25,
        SUBAGENT_HISTORY_WINDOW=20,
        MEMORY_ENABLED=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class SubAgentTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(str(Path(self.tmp) / "apex.db"))
        with self.db._lock, self.db._connect() as conn:
            conn.execute(
                "INSERT INTO users (id, name, created_at) VALUES (?, ?, ?)",
                ("u1", "Yannis", 0.0),
            )
            conn.execute(
                "INSERT INTO users (id, name, created_at) VALUES (?, ?, ?)",
                ("u2", "Someone", 0.0),
            )
        self.config = make_config(self.tmp)
        self.manager = SubAgentManager(self.db, self.config)
        self._orig_runner = runner_mod.run_subagent_turn
        # The model tools and the runner resolve their DB through ``db.get_db()``
        # (the process singleton). Point it at the temp DB, or the tool tests
        # create and delete rows in the operator's live database.
        import db as db_mod

        self._orig_get_db = db_mod.get_db
        db_mod.get_db = lambda: self.db
        self.addCleanup(setattr, db_mod, "get_db", self._orig_get_db)
        # Each test builds a fresh config; a manager is cached per config identity,
        # so clear the cache to stop a stale (freed-temp-dir) manager being reused.
        import subagents.manager as manager_mod

        self._orig_managers = dict(manager_mod._MANAGERS)
        manager_mod._MANAGERS.clear()
        self.addCleanup(lambda: (manager_mod._MANAGERS.clear(), manager_mod._MANAGERS.update(self._orig_managers)))

    def tearDown(self):
        runner_mod.run_subagent_turn = self._orig_runner


# ---- manager ---------------------------------------------------------------


class ManagerTests(SubAgentTestCase):
    def test_create_assigns_a_private_workspace(self):
        agent = self.manager.create("u1", {"name": "Log watcher"})
        ws = Path(agent["workspace"])
        self.assertTrue(ws.is_dir())
        self.assertTrue(str(ws).startswith(str(Path(self.tmp) / "subagents")))
        self.assertEqual(ws.name, agent["id"])

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(SubAgentError):
            self.manager.create("u1", {"name": "x", "tools": ["not_a_real_tool"]})

    def test_known_tool_is_accepted_and_stored_as_list(self):
        agent = self.manager.create("u1", {"name": "x", "tools": ["run_shell"]})
        self.assertEqual(agent["tools"], ["run_shell"])

    def test_empty_tools_means_all(self):
        agent = self.manager.create("u1", {"name": "x"})
        self.assertEqual(agent["tools"], [])

    def test_bad_name_rejected(self):
        with self.assertRaises(SubAgentError):
            self.manager.create("u1", {"name": "bad/name"})

    def test_max_per_user_enforced(self):
        config = make_config(self.tmp, SUBAGENT_MAX_PER_USER=1)
        manager = SubAgentManager(self.db, config)
        manager.create("u1", {"name": "one"})
        with self.assertRaises(SubAgentError):
            manager.create("u1", {"name": "two"})

    def test_update_requires_ownership(self):
        agent = self.manager.create("u1", {"name": "mine"})
        self.assertIsNone(self.manager.update(agent["id"], "u2", {"name": "stolen"}))
        self.assertIsNone(self.db.get_subagent(agent["id"], "u2"))

    def test_update_pause_toggle(self):
        agent = self.manager.create("u1", {"name": "mine"})
        paused = self.manager.update(agent["id"], "u1", {"enabled": False})
        self.assertFalse(paused["enabled"])

    def test_delete_is_owner_scoped(self):
        agent = self.manager.create("u1", {"name": "mine"})
        self.assertFalse(self.manager.delete(agent["id"], "u2"))
        self.assertIsNotNone(self.db.get_subagent(agent["id"], "u1"))
        self.assertTrue(self.manager.delete(agent["id"], "u1"))

    def test_custom_workspace_is_honoured(self):
        custom = Path(self.tmp) / "custom-workdir"
        agent = self.manager.create("u1", {"name": "x", "workspace": str(custom)})
        self.assertEqual(Path(agent["workspace"]), custom.resolve())
        self.assertTrue(custom.is_dir())


# ---- context block ---------------------------------------------------------


class ContextBlockTests(SubAgentTestCase):
    def test_an_empty_roster_still_explains_the_capability(self):
        # APEX decides by itself to create agents, so the capability must be
        # described even before any agent exists.
        block = subagent_context_block("u1", self.db)
        self.assertIn("no sub-agents yet", block)
        self.assertIn("subagent_fanout", block)

    def test_block_lists_agents_and_never_another_owner(self):
        self.manager.create("u1", {"name": "Mine"})
        self.manager.create("u2", {"name": "Theirs"})
        block = subagent_context_block("u1", self.db)
        self.assertIn("Mine", block)
        self.assertNotIn("Theirs", block)
        self.assertIn("subagent_delegate", block)

    def test_the_block_tells_apex_to_reuse_and_to_clean_up(self):
        self.manager.create("u1", {"name": "Mine"})
        block = subagent_context_block("u1", self.db)
        self.assertIn("Reuse first", block)
        self.assertIn("subagent_cleanup", block)

    def test_unseen_reports_are_surfaced(self):
        agent = self.manager.create("u1", {"name": "Mine"})
        run = self.db.create_subagent_run(agent["id"], "u1", "do it")
        self.db.update_subagent_run(run["id"], status="ok", output="all clear",
                                    finished_at=time.time(), unread=1)
        block = subagent_context_block("u1", self.db)
        self.assertIn("all clear", block)
        self.assertIn("not seen yet", block)


# ---- run orchestration (the model is stubbed) ------------------------------


class RunTests(SubAgentTestCase):
    def _fake_turn(self, report="done", error="", on_call=None):
        def fake(agent, prompt, *, user_id, run_id="", conversation_id="", emit=None, timeout=None):
            if on_call:
                on_call(agent, prompt, emit)
            return report, error
        runner_mod.run_subagent_turn = fake

    def test_sync_run_returns_the_report(self):
        agent = self.manager.create("u1", {"name": "Mine"})
        self._fake_turn(report="the disk is fine")
        run, err = self.manager.run_now(agent["id"], "u1", "check the disk", background=False)
        self.assertEqual(err, "")
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["output"], "the disk is fine")
        self.assertTrue(run["unread"])

    def test_sync_run_emits_started_and_report(self):
        agent = self.manager.create("u1", {"name": "Mine"})
        self._fake_turn(report="ok")
        events: list[dict] = []
        self.manager.run_now(agent["id"], "u1", "go", background=False,
                             emit=events.append)
        kinds = [e["type"] for e in events]
        self.assertIn("subagent_started", kinds)
        self.assertIn("subagent_report", kinds)

    def test_error_run_is_recorded_as_error(self):
        agent = self.manager.create("u1", {"name": "Mine"})
        self._fake_turn(report="", error="boom")
        run, _ = self.manager.run_now(agent["id"], "u1", "go", background=False)
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["error"], "boom")

    def test_paused_agent_does_not_run(self):
        agent = self.manager.create("u1", {"name": "Mine", "enabled": False})
        run, err = self.manager.run_now(agent["id"], "u1", "go", background=False)
        self.assertIsNone(run)
        self.assertIn("paused", err)

    def test_empty_prompt_is_refused(self):
        agent = self.manager.create("u1", {"name": "Mine"})
        run, err = self.manager.run_now(agent["id"], "u1", "   ", background=False)
        self.assertIsNone(run)

    def test_foreign_agent_cannot_be_run(self):
        agent = self.manager.create("u1", {"name": "Mine"})
        run, err = self.manager.run_now(agent["id"], "u2", "go", background=False)
        self.assertIsNone(run)

    def test_background_run_completes_and_sets_unread(self):
        agent = self.manager.create("u1", {"name": "Mine"})
        self._fake_turn(report="background result")
        run, err = self.manager.run_now(agent["id"], "u1", "go", background=True)
        self.assertEqual(err, "")
        for _ in range(100):
            row = self.db.get_subagent_run(run["id"])
            if row["status"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(self.db.get_subagent_run(run["id"])["status"], "ok")

    def test_concurrency_cap_reports_instead_of_queueing(self):
        config = make_config(self.tmp, SUBAGENT_MAX_CONCURRENT=1)
        manager = SubAgentManager(self.db, config)
        agent = manager.create("u1", {"name": "Mine"})
        run = self.db.create_subagent_run(agent["id"], "u1", "go")
        manager._slots.acquire()
        try:
            manager._guarded_execute(run["id"])
        finally:
            manager._slots.release()
        row = self.db.get_subagent_run(run["id"])
        self.assertEqual(row["status"], "error")
        self.assertIn("too many", row["error"])

    def test_run_many_returns_every_report(self):
        first = self.manager.create("u1", {"name": "A"})
        second = self.manager.create("u1", {"name": "B"})
        self._fake_turn(report="done")
        results = self.manager.run_many(
            [{"agent": first, "prompt": "one"}, {"agent": second, "prompt": "two"}], "u1",
        )
        self.assertEqual(len(results), 2)
        for _job, run, err in results:
            self.assertEqual(err, "")
            self.assertIsNotNone(run)
            self.assertEqual(run["status"], "ok")
            self.assertEqual(run["output"], "done")

    def test_run_many_queues_beyond_the_cap_instead_of_failing(self):
        config = make_config(self.tmp, SUBAGENT_MAX_CONCURRENT=1)
        manager = SubAgentManager(self.db, config)
        agents = [manager.create("u1", {"name": f"A{i}"}) for i in range(3)]
        self._fake_turn(report="done")
        results = manager.run_many([{"agent": a, "prompt": "go"} for a in agents], "u1")
        self.assertEqual(len(results), 3)
        self.assertTrue(all(run and run["status"] == "ok" for _j, run, _e in results))

    def test_run_many_refuses_a_paused_agent_without_blocking_the_rest(self):
        live = self.manager.create("u1", {"name": "Live"})
        paused = self.manager.create("u1", {"name": "Paused", "enabled": False})
        self._fake_turn(report="done")
        results = self.manager.run_many(
            [{"agent": paused, "prompt": "go"}, {"agent": live, "prompt": "go"}], "u1",
        )
        self.assertIsNone(results[0][1])
        self.assertIn("paused", results[0][2])
        self.assertEqual(results[1][1]["status"], "ok")

    def test_cleanup_removes_only_stale_agents(self):
        old = self.manager.create("u1", {"name": "Old"})
        fresh = self.manager.create("u1", {"name": "Fresh"})
        run = self.db.create_subagent_run(old["id"], "u1", "x")
        self.db.update_subagent_run(
            run["id"], status="ok", output="y",
            finished_at=time.time() - 40 * 86400, unread=1,
        )
        removed = self.manager.cleanup_stale("u1", 30)
        names = [r["name"] for r in removed]
        self.assertIn("Old", names)
        self.assertNotIn("Fresh", names)
        self.assertIsNone(self.db.get_subagent(old["id"], "u1"))
        self.assertIsNotNone(self.db.get_subagent(fresh["id"], "u1"))


# ---- strict tool scoping (AgentContext) ------------------------------------


class StrictToolsTests(unittest.TestCase):
    def _ctx(self):
        from agent.base import AgentContext

        registry = ToolRegistry()
        for name in ("run_shell", "terminal_command", "notepad_control", "subagent_list"):
            from tools.base import Tool

            registry.register(Tool(name, "", {"type": "object"}, lambda a, c: ""))
        return AgentContext(
            user_id="u1", conversation_id="c1", system_prompt="", history=[],
            provider=object(), provider_kind="openai", engine_name="responses",
            tools=registry, skill_tools=[],
        )

    def test_strict_tools_are_exact_and_not_force_included(self):
        ctx = self._ctx()
        ctx.strict_tools = True
        ctx.skill_tools = ["run_shell"]
        ctx.exclude_tools = ["terminal_command"]
        names = {t.name for t in ctx.active_tools()}
        self.assertEqual(names, {"run_shell"})

    def test_strict_tools_empty_means_none(self):
        ctx = self._ctx()
        ctx.strict_tools = True
        ctx.skill_tools = []
        self.assertEqual(ctx.active_tools(), [])

    def test_non_strict_force_includes_subagent_tools(self):
        ctx = self._ctx()
        ctx.skill_tools = ["run_shell"]
        names = {t.name for t in ctx.active_tools()}
        self.assertIn("subagent_list", names)

    def test_workspace_reaches_the_tool_context(self):
        ctx = self._ctx()
        ctx.workspace = "/tmp/somewhere"
        self.assertEqual(ctx.make_tool_context().workspace, "/tmp/somewhere")


# ---- workspace plumbing in run_shell ---------------------------------------


class RunShellWorkspaceTests(unittest.TestCase):
    def test_run_shell_runs_in_the_workspace(self):
        registry = ToolRegistry()
        cfg = SimpleNamespace(ENABLE_RUN_SHELL=True, RUN_SHELL_TIMEOUT=10)
        from tools.core_tools import build_core_tools

        for tool in build_core_tools(registry, cfg):
            registry.register(tool)
        run_shell = registry.get("run_shell")
        with tempfile.TemporaryDirectory() as ws:
            out = run_shell.handler({"command": "pwd"}, ToolContext(workspace=ws))
            self.assertEqual(os.path.realpath(out.strip()), os.path.realpath(ws))


# ---- model-facing tools ----------------------------------------------------


class ModelToolTests(SubAgentTestCase):
    def _tools(self):
        return {t.name: t for t in build_subagent_tools(self.config)}

    def test_builds_the_expected_tools(self):
        names = set(self._tools())
        self.assertEqual(names, {
            "subagent_list", "subagent_create", "subagent_update",
            "subagent_delete", "subagent_status", "subagent_delegate",
            "subagent_fanout", "subagent_cleanup", "subagent_tools",
        })

    def test_create_then_list(self):
        tools = self._tools()
        created = tools["subagent_create"].handler(
            {"name": "Disk checker", "description": "checks disks"},
            ToolContext(user_id="u1"),
        )
        self.assertIn("Disk checker", created)
        listed = tools["subagent_list"].handler({}, ToolContext(user_id="u1"))
        self.assertIn("Disk checker", listed)

    def test_requires_a_signed_in_user(self):
        tools = self._tools()
        self.assertIn("Sign in", tools["subagent_list"].handler({}, ToolContext()))

    def test_delegate_returns_the_report(self):
        tools = self._tools()
        tools["subagent_create"].handler({"name": "Mine"}, ToolContext(user_id="u1"))
        self._fake(report="the answer is 42")
        out = tools["subagent_delegate"].handler(
            {"agent": "1", "task": "compute"}, ToolContext(user_id="u1"),
        )
        self.assertIn("the answer is 42", out)

    def test_delegate_reports_a_failure_honestly(self):
        tools = self._tools()
        tools["subagent_create"].handler({"name": "Mine"}, ToolContext(user_id="u1"))
        self._fake(error="the network is down")
        out = tools["subagent_delegate"].handler(
            {"agent": "1", "task": "compute"}, ToolContext(user_id="u1"),
        )
        self.assertIn("failed", out)

    def test_delegate_does_not_run_in_the_operators_conversation(self):
        # Regression: the delegate tool used to pass ctx.conversation_id, and the
        # runner reuses any conversation the user owns - so the sub-agent's private
        # turn was written into the operator's chat history. The run must not carry
        # the operator's conversation id.
        tools = self._tools()
        tools["subagent_create"].handler({"name": "Mine"}, ToolContext(user_id="u1"))
        self._fake(report="ok")
        tools["subagent_delegate"].handler(
            {"agent": "1", "task": "go"},
            ToolContext(user_id="u1", conversation_id="operator-conversation"),
        )
        agent = self.db.list_subagents("u1")[0]
        run = self.db.list_subagent_runs(agent["id"], 5)[0]
        self.assertNotEqual(run["conversation_id"], "operator-conversation")

    def _fake(self, report="", error=""):
        def fake(agent, prompt, *, user_id, run_id="", conversation_id="", emit=None, timeout=None):
            return report, error
        runner_mod.run_subagent_turn = fake

    def test_fanout_runs_every_job_in_one_call(self):
        tools = self._tools()
        tools["subagent_create"].handler({"name": "A"}, ToolContext(user_id="u1"))
        tools["subagent_create"].handler({"name": "B"}, ToolContext(user_id="u1"))
        self._fake(report="did it")
        out = tools["subagent_fanout"].handler(
            {"jobs": [{"agent": "1", "task": "one"}, {"agent": "2", "task": "two"}]},
            ToolContext(user_id="u1"),
        )
        self.assertIn("Fan-out complete", out)
        self.assertIn('"A"', out)
        self.assertIn('"B"', out)
        self.assertIn("did it", out)

    def test_fanout_needs_jobs(self):
        tools = self._tools()
        out = tools["subagent_fanout"].handler({"jobs": []}, ToolContext(user_id="u1"))
        self.assertIn("jobs", out)

    def test_fanout_rejects_an_unknown_tool_name(self):
        tools = self._tools()
        tools["subagent_create"].handler({"name": "A"}, ToolContext(user_id="u1"))
        out = tools["subagent_fanout"].handler(
            {"jobs": [{"agent": "1", "task": "go"}, {"agent": "99", "task": "go"}]},
            ToolContext(user_id="u1"),
        )
        self.assertIn("Job 2", out)

    def test_cleanup_tool_says_when_there_is_nothing_to_do(self):
        tools = self._tools()
        tools["subagent_create"].handler({"name": "Fresh"}, ToolContext(user_id="u1"))
        out = tools["subagent_cleanup"].handler({}, ToolContext(user_id="u1"))
        self.assertIn("No sub-agents", out)


# ---- the agent's own conversation (runner) ---------------------------------


class RunnerConversationTests(SubAgentTestCase):
    """``run_subagent_turn`` must only reuse the agent's own conversation. The
    provider and engine are the only stubs; the conversation plumbing is real."""

    def setUp(self):
        super().setUp()
        self.agent = self.manager.create("u1", {"name": "Mine"})

        import agent.factory as factory
        import auth
        import models.providers as providers
        import subagents.runner as runner

        class fake_provider:
            def close(self):
                pass

        class fake_engine:
            def stream(self):
                yield {"type": "text_delta", "content": "done"}

        self._orig_pm = providers.ProviderManager
        self._orig_engine = factory.build_engine
        self._orig_bearer = auth.bearer_for_api
        self._orig_sub = auth.is_subscription_access
        providers.ProviderManager = lambda **kw: SimpleNamespace(
            build=lambda name, model: fake_provider()
        )
        factory.build_engine = lambda name, ctx: fake_engine()
        auth.bearer_for_api = lambda uid: None
        auth.is_subscription_access = lambda uid: False
        self.addCleanup(setattr, providers, "ProviderManager", self._orig_pm)
        self.addCleanup(setattr, factory, "build_engine", self._orig_engine)
        self.addCleanup(setattr, auth, "bearer_for_api", self._orig_bearer)
        self.addCleanup(setattr, auth, "is_subscription_access", self._orig_sub)
        self._orig_cfg = runner.default_config
        runner.default_config = make_config(self.tmp)
        self.addCleanup(lambda: setattr(runner, "default_config", self._orig_cfg))

    def test_a_foreign_conversation_is_not_reused(self):
        operator = self.db.create_conversation("u1", title="Chat")
        report, error = runner_mod.run_subagent_turn(
            self.agent, "do the thing", user_id="u1",
            conversation_id=operator["id"],
        )
        self.assertEqual(error, "")
        self.assertEqual(report, "done")
        # The operator's thread is untouched...
        self.assertEqual(self.db.list_messages(operator["id"]), [])
        # ...and the agent now owns its own, separate thread.
        own = self.db.get_subagent(self.agent["id"])["conversation_id"]
        self.assertNotEqual(own, operator["id"])
        roles = [m["role"] for m in self.db.list_messages(own)]
        self.assertEqual(roles, ["user", "assistant"])


# ---- routes ----------------------------------------------------------------

class RouteTests(SubAgentTestCase):
    def setUp(self):
        super().setUp()
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.state = {"user": {"id": "u1"}}

        def require_user():
            return self.state["user"]

        self._orig_get_manager = routes_mod.get_manager
        routes_mod.get_manager = lambda cfg: self.manager
        self.addCleanup(lambda: setattr(routes_mod, "get_manager", self._orig_get_manager))
        routes_mod.register_subagent_routes(self.app, require_user, self.config)
        self.client = self.app.test_client()

    def test_requires_a_session(self):
        self.state["user"] = None
        self.assertEqual(self.client.get("/api/subagents").status_code, 401)
        self.assertEqual(self.client.post("/api/subagents", json={"name": "x"}).status_code, 401)

    def test_create_list_get(self):
        created = self.client.post("/api/subagents", json={"name": "Mine"})
        self.assertEqual(created.status_code, 201)
        agent_id = created.get_json()["agent"]["id"]
        listed = self.client.get("/api/subagents").get_json()["agents"]
        self.assertEqual(len(listed), 1)
        got = self.client.get(f"/api/subagents/{agent_id}").get_json()["agent"]
        self.assertIn("runs", got)

    def test_invalid_tool_is_a_400(self):
        resp = self.client.post("/api/subagents", json={"name": "x", "tools": ["nope"]})
        self.assertEqual(resp.status_code, 400)

    def test_another_owner_gets_404_not_403(self):
        created = self.client.post("/api/subagents", json={"name": "Mine"}).get_json()["agent"]
        self.state["user"] = {"id": "u2"}
        self.assertEqual(self.client.get(f"/api/subagents/{created['id']}").status_code, 404)
        self.assertEqual(
            self.client.delete(f"/api/subagents/{created['id']}").status_code, 404
        )

    def test_patch_and_delete(self):
        agent_id = self.client.post(
            "/api/subagents", json={"name": "Mine"}
        ).get_json()["agent"]["id"]
        patched = self.client.patch(
            f"/api/subagents/{agent_id}", json={"enabled": False}
        ).get_json()["agent"]
        self.assertFalse(patched["enabled"])
        self.assertEqual(self.client.delete(f"/api/subagents/{agent_id}").status_code, 200)

    def test_run_now_starts_a_run(self):
        agent_id = self.client.post(
            "/api/subagents", json={"name": "Mine"}
        ).get_json()["agent"]["id"]
        runner_mod.run_subagent_turn = lambda *a, **k: ("done", "")
        resp = self.client.post(f"/api/subagents/{agent_id}/run", json={"prompt": "go"})
        self.assertEqual(resp.status_code, 202)

    def test_run_without_a_prompt_is_a_400(self):
        agent_id = self.client.post(
            "/api/subagents", json={"name": "Mine"}
        ).get_json()["agent"]["id"]
        self.assertEqual(
            self.client.post(f"/api/subagents/{agent_id}/run", json={}).status_code, 400
        )

    def test_ack_clears_unread(self):
        agent_id = self.client.post(
            "/api/subagents", json={"name": "Mine"}
        ).get_json()["agent"]["id"]
        run = self.db.create_subagent_run(agent_id, "u1", "go")
        self.db.update_subagent_run(run["id"], status="ok", output="x",
                                    finished_at=time.time(), unread=1)
        self.assertEqual(self.client.post("/api/subagents/ack", json={}).status_code, 200)
        self.assertEqual(self.db.get_subagent_run(run["id"])["unread"], 0)

    def test_tools_endpoint_lists_real_names(self):
        names = self.client.get("/api/subagents/tools").get_json()["tools"]
        self.assertIn("run_shell", names)


if __name__ == "__main__":
    unittest.main()
