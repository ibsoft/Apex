"""Conversation summaries: store upsert/checkpoint helpers, the windowed
summarizer, and the leave-conversation endpoint.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class FakeMemory:
    """Minimal MemoryStore stand-in used by the summarizer unit tests."""

    def __init__(self):
        self.sum = {}
        self.replaced = []

    def conversation_summary(self, user_id, conv_id):
        return self.sum.get((user_id, conv_id))

    def replace_conversation_summary(self, user_id, conv_id, text, meta=None):
        self.sum[(user_id, conv_id)] = {"id": "m1", "text": text, "meta": meta or {}}
        self.replaced.append((user_id, conv_id, text, meta))

    def forget_conversation(self, user_id, conv_id):
        self.sum.pop((user_id, conv_id), None)
        return 1


class FakeDB:
    def __init__(self, conv, msgs):
        self.conv = conv
        self.msgs = msgs

    def get_conversation(self, conv_id):
        return self.conv

    def list_messages(self, conv_id):
        return list(self.msgs)


class FakeProvider:
    def __init__(self, replies=None):
        self.prompts = []
        self.replies = list(replies or [])
        self.closed = False

    def chat_stream(self, msgs, tools):
        self.prompts.append(msgs)
        text = self.replies.pop(0) if self.replies else "merged summary"
        return [{"type": "text", "content": text}]

    def close(self):
        self.closed = True


class SummarizerUnitTests(unittest.TestCase):
    def _run(self, messages, *, existing=None, window=60, min_messages=4, replies=None,
             response_language="en", owner="alice", memory=None):
        from memory import summarizer

        memory = memory or FakeMemory()
        if existing:
            memory.sum[("alice", "c1")] = existing
        conv = {"id": "c1", "user_id": "alice", "title": "Kitchen plans"}
        db = FakeDB(conv, messages)
        provider = FakeProvider(replies)
        provider_mgr = SimpleNamespace(bearer="", use_oauth_access=False)
        rt = {"model": "test-model"}
        with patch.object(summarizer, "_build_provider", return_value=provider):
            result = summarizer.summarize_conversation(
                user_id=owner,
                conversation_id="c1",
                db=db,
                memory=memory,
                provider_mgr=provider_mgr,
                provider_name="openai",
                rt=rt,
                response_language=response_language,
                min_messages=min_messages,
                window=window,
            )
        return result, memory, provider

    @staticmethod
    def _msgs(n, first_prefix="Message number", lengths=None):
        out = []
        for i in range(1, n + 1):
            text = lengths[i - 1] if lengths else f"{first_prefix} {i} discussing matters"
            out.append({"id": i, "role": "user" if i % 2 else "assistant", "content": text})
        return out

    def test_full_conversation_is_summarized_and_stored(self):
        msgs = self._msgs(5, lengths=["Let us plan the kitchen renovation", "Great, budget is about 12k",
                                      "We prefer quartz countertops", "And an induction cooktop",
                                      "Let's start next month"])
        result, memory, provider = self._run(msgs)
        self.assertEqual(result, {"status": "summarized"})
        stored = memory.sum[("alice", "c1")]
        self.assertTrue(stored["text"].startswith("[20"))
        self.assertIn("merged summary", stored["text"])
        self.assertEqual(stored["meta"]["last_message_id"], 5)
        self.assertEqual(stored["meta"]["title"], "Kitchen plans")
        self.assertRegex(stored["meta"]["conversation_date"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertTrue(stored["meta"]["conversation_at"])
        self.assertTrue(provider.closed)
        # The prompt contains the transcript, the date and respects the language.
        prompt = provider.prompts[0][0]["content"]
        self.assertIn("kitchen renovation", prompt)
        self.assertIn("conversation happened on", prompt)
        self.assertIn("English", prompt)

    def test_greek_prompt_uses_greek(self):
        msgs = self._msgs(4, lengths=["Let us plan the kitchen renovation", "Great, budget is about 12k",
                                      "We prefer quartz countertops", "And an induction cooktop"])
        _, _, provider = self._run(msgs, response_language="el")
        self.assertIn("Greek", provider.prompts[0][0]["content"])

    def test_trivial_chat_is_skipped(self):
        result, memory, _ = self._run([{"id": 1, "role": "user", "content": "hi"},
                                       {"id": 2, "role": "assistant", "content": "hello!"}])
        self.assertEqual(result, {"status": "skipped"})
        self.assertNotIn(("alice", "c1"), memory.sum)

    def test_first_time_summary_requires_min_messages(self):
        msgs = self._msgs(3)
        result, memory, _ = self._run(msgs, min_messages=4)
        self.assertEqual(result, {"status": "skipped"})
        self.assertNotIn(("alice", "c1"), memory.sum)

    def test_no_new_messages_after_checkpoint_is_a_noop(self):
        existing = {"id": "old", "text": "previous", "meta": {"last_message_id": 4}}
        msgs = self._msgs(4)
        result, memory, provider = self._run(msgs, existing=existing)
        self.assertEqual(result, {"status": "no_new"})
        self.assertEqual(memory.sum[("alice", "c1")], existing)
        self.assertEqual(provider.prompts, [])  # provider never built

    def test_incremental_summary_uses_existing_text_and_new_turns(self):
        existing = {"id": "old", "text": "[2026-09-20 09:00] earlier talks about budget",
                    "meta": {"last_message_id": 2}}
        msgs = self._msgs(4, lengths=["Let us plan the kitchen renovation", "Great, budget is about 12k",
                                      "We prefer quartz countertops", "And an induction cooktop"])
        result, memory, provider = self._run(msgs, existing=existing)
        self.assertEqual(result, {"status": "summarized"})
        prompt = provider.prompts[0][0]["content"]
        self.assertIn("earlier talks about budget", prompt)
        self.assertIn("quartz countertops", prompt)
        self.assertNotIn("kitchen renovation", prompt)  # old turns are not replayed
        self.assertEqual(memory.sum[("alice", "c1")]["meta"]["last_message_id"], 4)

    def test_long_conversations_are_windowed_in_rolling_chunks(self):
        msgs = self._msgs(5)
        result, memory, provider = self._run(msgs, window=2)
        self.assertEqual(result, {"status": "summarized"})
        self.assertEqual(len(provider.prompts), 3)  # ceil(5 / 2)
        self.assertEqual(memory.sum[("alice", "c1")]["meta"]["last_message_id"], 5)
        # The second call reuses the first call's output as its existing summary.
        self.assertIn("merged summary", provider.prompts[1][0]["content"])

    def test_empty_conversation_and_foreign_owner_exit_early(self):
        result, memory, provider = self._run(self._msgs(4), owner="bob")
        self.assertEqual(result, {"status": "not_owner"})
        result, memory, provider = self._run([])
        self.assertEqual(result, {"status": "empty"})

    def test_build_provider_constructs_manager_with_keyword_args(self):
        """Regression: ProviderManager only takes keyword args; passing them
        positionally broke every real (unpatched) summary in production."""
        from memory import summarizer

        with patch("models.providers.ProviderManager") as PM:
            summarizer._build_provider(
                SimpleNamespace(bearer="tok", use_oauth_access=True),
                "openai",
                {"model": "gpt-4o-mini"},
            )
        PM.assert_called_once_with(
            bearer="tok", use_oauth_access=True, runtime={"model": "gpt-4o-mini"}
        )

    def test_repeated_summarize_never_duplicates_memory(self):
        msgs = self._msgs(4, lengths=["Let us plan the kitchen renovation", "Great, budget is about 12k",
                                      "We prefer quartz countertops", "And an induction cooktop"])
        memory = FakeMemory()
        # First pass stores one entry.
        result, memory, provider = self._run(msgs, replies=["summary v1"], memory=memory)
        self.assertEqual(result, {"status": "summarized"})
        # Second pass is a no-op (checkpoint covers all messages): still one entry.
        result, memory, provider = self._run(msgs, replies=["summary v2"], memory=memory)
        self.assertEqual(result, {"status": "no_new"})
        self.assertEqual(len([k for k in memory.sum if k[1] == "c1"]), 1)
        # New messages arrive: the summary is replaced, never appended.
        extra = list(msgs) + [{"id": 5, "role": "user",
                               "content": "We picked the white marble backsplash"}]
        result, memory, provider = self._run(extra, replies=["summary v3"], memory=memory)
        self.assertEqual(result, {"status": "summarized"})
        entries = [k for k in memory.sum if k[1] == "c1"]
        self.assertEqual(len(entries), 1)
        self.assertIn("summary v3", memory.sum[("alice", "c1")]["text"])


class ConversationSummaryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DEV_MODE"] = "true"
        os.environ["DATA_DIR"] = self.tmp.name
        from memory.store import MemoryStore, _embed_manager

        self.mem = MemoryStore(str(Path(self.tmp.name) / "chroma"), _embed_manager())

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def test_replace_keeps_one_summary_per_conversation(self):
        mem = self.mem
        mem.replace_conversation_summary("alice", "c1", "first draft", {"last_message_id": 3})
        mem.replace_conversation_summary("alice", "c1", "second draft", {"last_message_id": 8})
        summary = mem.conversation_summary("alice", "c1")
        self.assertEqual(summary["text"], "second draft")
        self.assertEqual(summary["meta"]["last_message_id"], 8)

    def test_conversations_are_independent(self):
        mem = self.mem
        mem.replace_conversation_summary("alice", "c1", "about kitchen", {"last_message_id": 5})
        mem.replace_conversation_summary("alice", "c2", "about travel", {"last_message_id": 2})
        self.assertEqual(mem.conversation_summary("alice", "c1")["text"], "about kitchen")
        self.assertEqual(mem.conversation_summary("alice", "c2")["text"], "about travel")
        self.assertIsNone(mem.conversation_summary("alice", "missing"))
        self.assertIsNone(mem.conversation_summary("bob", "c1"))

    def test_forget_conversation_removes_only_its_summary(self):
        mem = self.mem
        mem.replace_conversation_summary("alice", "c1", "about kitchen", {"last_message_id": 5})
        mem.replace_conversation_summary("alice", "c2", "about travel", {"last_message_id": 2})
        self.assertEqual(mem.forget_conversation("alice", "c1"), 1)
        self.assertIsNone(mem.conversation_summary("alice", "c1"))
        self.assertEqual(mem.conversation_summary("alice", "c2")["text"], "about travel")


class ConversationSummaryEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_data_dir = os.environ.get("DATA_DIR")
        os.environ["DEV_MODE"] = "true"
        os.environ["DATA_DIR"] = self.tmp.name

        import app as app_module
        from config import config
        from memory import store

        self.app_module = app_module
        self.config = config
        config.DATA_DIR = Path(self.tmp.name)
        config.CHROMA_DIR = str(Path(self.tmp.name) / "chroma")
        config.MEMORY_ENABLED = True
        config.MEMORY_CONVERSATION_SUMMARIZE = True
        store.register_memory_store(None)
        self.app = app_module.create_app()
        self.client = self.app.test_client()
        self.db = app_module.get_db()
        self.db.upsert_user("alice", "Alice", "alice@apex.local", "", None, "")
        self.db.upsert_user("bob", "Bob", "bob@apex.local", "", None, "")
        self._cleanup_locks()
        # Keep auth deterministic: only the explicitly-set session user counts. The
        # local hash embedder keeps the real-world worker test network-free and fast.
        self._config_patches = [
            patch.object(app_module.config, "DEV_MODE", False),
            patch.object(app_module.config, "DEV_AUTO_LOGIN", ""),
            patch.object(app_module.config, "EMBEDDING_BACKEND", "hash"),
        ]
        for p in self._config_patches:
            p.start()
        self.summarize_patch = patch.object(app_module, "run_conversation_summary")
        self.summarize = self.summarize_patch.start()
        app_module._summary_locks.clear()

    def tearDown(self):
        self.summarize_patch.stop()
        for p in reversed(self._config_patches):
            p.stop()
        self._cleanup_locks()
        if self._orig_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._orig_data_dir
        self.tmp.cleanup()

    def _cleanup_locks(self):
        with self.app_module._summary_locks_guard:
            self.app_module._summary_locks.clear()

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id

    def test_summarize_requires_auth(self):
        conv = self.db.create_conversation("alice", title="A")
        resp = self.client.post(f"/api/conversations/{conv['id']}/summarize")
        self.assertEqual(resp.status_code, 401)
        self.summarize.assert_not_called()

    def test_summarize_requires_owner(self):
        self._login("bob")
        conv = self.db.create_conversation("alice", title="A")
        resp = self.client.post(f"/api/conversations/{conv['id']}/summarize")
        self.assertEqual(resp.status_code, 404)
        self.summarize.assert_not_called()

    def test_summarize_starts_worker(self):
        self._login("alice")
        conv = self.db.create_conversation("alice", title="A")
        resp = self.client.post(f"/api/conversations/{conv['id']}/summarize")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json()["status"], "started")
        self.summarize.assert_called_once_with("alice", conv["id"])

    def test_summarize_unavailable_without_memory(self):
        self._login("alice")
        conv = self.db.create_conversation("alice", title="A")
        with patch.object(self.app_module.config, "MEMORY_CONVERSATION_SUMMARIZE", False):
            resp = self.client.post(f"/api/conversations/{conv['id']}/summarize")
        self.assertEqual(resp.status_code, 400)
        self.summarize.assert_not_called()

    def test_delete_conversation_forgets_summary(self):
        self._login("alice")
        conv = self.db.create_conversation("alice", title="A")
        mem = self.app_module.get_memory()
        mem.replace_conversation_summary("alice", conv["id"], "the summary")
        resp = self.client.delete(f"/api/conversations/{conv['id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(mem.conversation_summary("alice", conv["id"]))

    def test_real_worker_stores_date_stamped_summary(self):
        """Regression: the daemon thread must write the summary (no swallowed NameError)."""
        import time as _time

        self._login("alice")
        conv = self.db.create_conversation("alice", title="Real worker")
        for i, t in enumerate(["Let us plan the kitchen renovation budget", "12k is the budget",
                               "Quartz countertops preferred", "Induction cooktop",
                               "Start next month"], 1):
            self.db.add_message(conv["id"], "user" if i % 2 else "assistant", t, {})
        provider = MagicMock()
        provider.chat_stream.return_value = [{"type": "text", "content": "Kitchen plan: 12k, quartz, induction"}]
        self.summarize_patch.stop()  # restore the real worker for this test
        provider_patch = patch("memory.summarizer._build_provider", return_value=provider)
        provider_patch.start()
        try:
            resp = self.client.post(f"/api/conversations/{conv['id']}/summarize")
            self.assertEqual(resp.status_code, 202)
            mem = self.app_module.get_memory()
            summary = None
            for _ in range(50):
                summary = mem.conversation_summary("alice", conv["id"])
                if summary:
                    break
                _time.sleep(0.2)
            self.assertIsNotNone(summary)
            self.assertIn("Kitchen plan", summary["text"])
            self.assertTrue(summary["text"].startswith("[20"))
            self.assertIn("conversation_date", summary["meta"])
        finally:
            provider_patch.stop()
            self.summarize_patch.start()  # tearDown stops this same patcher
            self.app_module._summary_locks.clear()


if __name__ == "__main__":
    unittest.main()