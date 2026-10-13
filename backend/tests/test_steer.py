"""Mid-turn steering: "/steer <instruction>" while a reply is streaming.

A text-only answer is a single model call, so an instruction that arrives mid
stream would otherwise have nowhere to go. The responses engine abandons the
current call when a steer appears, keeps the partial answer, folds the
instruction in and continues - emitting ``steer_applied`` so the browser can
move it from its queue into the transcript. The inbox and the route are the
other two halves; the client's own queue is the safety net when a turn does not
consume a steer.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_soul import isolated_db, signed_in  # noqa: E402  throwaway-DB guarantee

from agent.base import AgentContext  # noqa: E402
from agent.responses import ResponsesEngine  # noqa: E402
from agent.steer import INBOX, SteerInbox  # noqa: E402
from tools.base import Tool, ToolRegistry  # noqa: E402


class SteerInboxTests(unittest.TestCase):
    def test_push_reports_whether_a_consuming_turn_is_running(self):
        inbox = SteerInbox()
        self.assertFalse(inbox.push("c1", "early"), "no active turn -> not consumed")
        inbox.begin("c1")
        self.assertTrue(inbox.push("c1", "do it"), "active turn -> consumed")
        self.assertEqual(inbox.drain("c1"), ["early", "do it"])

    def test_drain_is_fifo_and_empties_the_queue(self):
        inbox = SteerInbox()
        inbox.begin("c1")
        inbox.push("c1", "first")
        inbox.push("c1", "second")
        self.assertEqual(inbox.drain("c1"), ["first", "second"])
        self.assertEqual(inbox.drain("c1"), [])

    def test_end_clears_the_active_flag_and_returns_leftovers(self):
        inbox = SteerInbox()
        inbox.begin("c1")
        inbox.push("c1", "late")
        self.assertEqual(inbox.end("c1"), ["late"])
        self.assertFalse(inbox.is_active("c1"))
        self.assertFalse(inbox.push("c1", "after"), "turn is over")

    def test_blank_input_and_blank_conversation_are_ignored(self):
        inbox = SteerInbox()
        inbox.begin("c1")
        self.assertFalse(inbox.push("c1", "   "))
        self.assertFalse(inbox.push("", "x"))
        self.assertEqual(inbox.drain("c1"), [])

    def test_conversations_do_not_share_a_queue(self):
        inbox = SteerInbox()
        inbox.begin("c1")
        inbox.begin("c2")
        inbox.push("c1", "for one")
        inbox.push("c2", "for two")
        self.assertEqual(inbox.drain("c1"), ["for one"])
        self.assertEqual(inbox.drain("c2"), ["for two"])


class _SteerableProvider:
    """Streams a few text chunks; on its first call it queues a steer once the
    first chunk is out, exactly where an operator's instruction would arrive."""

    kind = "openai"

    def __init__(self, push):
        self.calls: list[list[dict]] = []
        self._push = push
        self._pushed = False

    def chat_stream(self, messages, schemas=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        yield {"type": "text", "content": "Working"}
        if not self._pushed:
            self._pushed = True
            self._push()
            yield {"type": "text", "content": " ..."}
        else:
            yield {"type": "text", "content": " done"}


class SteerEngineTests(unittest.TestCase):
    def setUp(self):
        INBOX.end("conv1")

    def tearDown(self):
        INBOX.end("conv1")

    def test_a_midstream_steer_restarts_the_call_with_the_instruction(self):
        provider = _SteerableProvider(lambda: INBOX.push("conv1", "make it JSON"))
        ctx = AgentContext(
            user_id="u", conversation_id="conv1", system_prompt="p", history=[],
            provider=provider, provider_kind="openai", engine_name="responses",
            tools=ToolRegistry(), skill_tools=[], skill_name="general",
        )
        events = list(ResponsesEngine(ctx).stream())

        applied = [e for e in events if e.get("type") == "steer_applied"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["content"], "make it JSON")

        # The partial answer is kept and the call is re-run with the steer as a
        # user message, so the model actually sees the new instruction.
        self.assertEqual(len(provider.calls), 2)
        second = provider.calls[1]
        self.assertTrue(any(
            m["role"] == "user" and "make it JSON" in (m.get("content") or "")
            for m in second
        ))
        self.assertTrue(any(
            m["role"] == "assistant" and (m.get("content") or "") == "Working"
            for m in second
        ), "the partial answer must survive the restart")
        self.assertTrue(any(e.get("type") == "done" for e in events))

    def test_the_turn_is_marked_inactive_after_it_ends(self):
        provider = _SteerableProvider(lambda: None)
        ctx = AgentContext(
            user_id="u", conversation_id="conv1", system_prompt="p", history=[],
            provider=provider, provider_kind="openai", engine_name="responses",
            tools=ToolRegistry(), skill_tools=[], skill_name="general",
        )
        list(ResponsesEngine(ctx).stream())
        self.assertFalse(INBOX.is_active("conv1"))

    def test_a_steer_during_a_tool_round_is_consumed_before_the_next_call(self):
        """A steer can arrive while a tool runs (no model call in flight to
        catch it), so the post-tool drain must pick it up too."""
        registry = ToolRegistry()
        registry.register(Tool(
            name="noop", description="", parameters={"type": "object", "properties": {}},
            handler=lambda args, ctx: (INBOX.push("conv1", "second thought"), "ok")[1],
        ))
        provider = _SteerableProvider(lambda: None)
        provider.chat_stream = _tool_then_text(provider)
        ctx = AgentContext(
            user_id="u", conversation_id="conv1", system_prompt="p", history=[],
            provider=provider, provider_kind="openai", engine_name="responses",
            tools=registry, skill_tools=[], skill_name="general",
        )
        events = list(ResponsesEngine(ctx).stream())
        applied = [e for e in events if e.get("type") == "steer_applied"]
        self.assertTrue(applied)
        self.assertEqual(applied[0]["content"], "second thought")
        # The instruction reached the next model call as a user message.
        last = provider.calls[-1]
        self.assertTrue(any(
            m["role"] == "user" and "second thought" in (m.get("content") or "")
            for m in last
        ))


def _tool_then_text(provider):
    """A chat_stream that emits a tool call then text, driving two rounds."""
    first = {"done": False}

    def gen(messages, schemas=None, **kwargs):
        provider.calls.append([dict(m) for m in messages])
        if not first["done"]:
            first["done"] = True
            yield {"type": "tool_calls", "calls": [{
                "id": "c1", "name": "noop", "arguments": "{}",
            }]}
        else:
            yield {"type": "text", "content": "fin"}
    return gen


class SteerRouteTests(unittest.TestCase):
    def _client(self, app_module, database, user_id="alice"):
        database.upsert_user(
            user_id=user_id, name=user_id.title(), email="", picture="",
            tokens={}, token_scopes="{}",
        )
        client = app_module.create_app().test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
        return client

    def test_requires_a_signed_in_user(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = app_module.create_app().test_client()
            response = client.post("/api/steer", json={
                "conversation_id": "x", "instruction": "do it",
            })
            self.assertEqual(response.status_code, 401)

    def test_rejects_an_empty_instruction(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            conv = database.create_conversation("alice", title="t")
            response = client.post("/api/steer", json={
                "conversation_id": conv["id"], "instruction": "   ",
            })
            self.assertEqual(response.status_code, 400)

    def test_requires_a_conversation_id(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            response = client.post("/api/steer", json={"instruction": "do it"})
            self.assertEqual(response.status_code, 400)

    def test_another_users_conversation_looks_missing(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            database.upsert_user(user_id="bob", name="Bob", email="", picture="",
                                 tokens={}, token_scopes="{}")
            theirs = database.create_conversation("bob", title="theirs")
            client = self._client(app_module, database, user_id="alice")
            response = client.post("/api/steer", json={
                "conversation_id": theirs["id"], "instruction": "sneak",
            })
            self.assertEqual(response.status_code, 404)

    def test_a_running_turn_reports_active_and_receives_the_instruction(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            conv = database.create_conversation("alice", title="t")
            INBOX.begin(conv["id"])
            try:
                response = client.post("/api/steer", json={
                    "conversation_id": conv["id"], "instruction": "keep going",
                })
                self.assertEqual(response.status_code, 200)
                self.assertIs(response.get_json()["active"], True)
                self.assertEqual(INBOX.drain(conv["id"]), ["keep going"])
            finally:
                INBOX.end(conv["id"])

    def test_no_running_turn_reports_inactive(self):
        import app as app_module
        with isolated_db() as database, signed_in(app_module):
            client = self._client(app_module, database)
            conv = database.create_conversation("alice", title="t")
            response = client.post("/api/steer", json={
                "conversation_id": conv["id"], "instruction": "too late",
            })
            self.assertEqual(response.status_code, 200)
            self.assertIs(response.get_json()["active"], False)
            INBOX.drain(conv["id"])


class SteerPersistenceTests(unittest.TestCase):
    """A steer that lands is recorded as its own user message, so the steering
    survives a reload instead of living only in the stream."""

    def test_the_chat_route_stores_an_applied_steer(self):
        import app as app_module
        db = MagicMock()
        db.get_user.return_value = {"id": "alice", "name": "Alice"}
        db.all_settings.return_value = {"provider": "openai", "model": "test"}
        db.get_conversation.return_value = {
            "id": "conversation", "user_id": "alice", "skill": "general",
        }
        db.list_messages.return_value = []
        skills = MagicMock()
        skills.select.return_value = SimpleNamespace(model="", tools=["web_search"])
        skills.build_system_prompt.return_value = "test"
        engine = MagicMock()
        engine.stream.return_value = iter([
            {"type": "text_delta", "content": "Working"},
            {"type": "steer_applied", "content": "make it JSON"},
            {"type": "text_delta", "content": " done"},
            {"type": "done", "usage": {}},
        ])
        with patch.object(app_module, "get_db", return_value=db), \
             patch.object(app_module, "get_skill_manager", return_value=skills), \
             patch.object(app_module, "bearer_for_api", return_value=None), \
             patch.object(app_module, "is_subscription_access", return_value=False), \
             patch.object(app_module, "ProviderManager"), \
             patch.object(app_module, "make_registry"), \
             patch.object(app_module, "build_engine", return_value=engine), \
             patch.object(app_module.config, "MEMORY_ENABLED", False), \
             patch.object(app_module.config, "MEMORY_SUMMARIZE", False):
            application = app_module.create_app()
            client = application.test_client()
            with client.session_transaction() as session:
                session["user_id"] = "alice"
            response = client.post("/api/chat", json={
                "message": "go", "conversation_id": "conversation",
            })
            self.assertEqual(response.status_code, 200)
            response.get_data(as_text=True)  # drain the stream
            steer_calls = [
                c for c in db.add_message.call_args_list
                if c.args[1] == "user" and (c.args[3] or {}).get("steer")
            ]
            self.assertEqual(len(steer_calls), 1)
            self.assertEqual(steer_calls[0].args[2], "make it JSON")


if __name__ == "__main__":
    unittest.main()