"""The reasoning fallback for utterances the local parsers miss.

Two halves are tested: the resolver, which turns one utterance into a proposal
and never raises, and the route, which is the only place a browser request can
reach a model for it.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CATALOGUE = "terminal:\n  terminal.restore_all - un-minimize every terminal"
STATE = 'terminal #1 "Terminal abc" (terminal, minimized) · terminal #2 "Terminal def" (terminal)'


def fake_provider(reply, *, chunks=None, error=None):
    """A provider whose chat_stream answers with `reply`.

    `error` makes the stream yield a provider error, which is how a real provider
    reports being down; the resolver has to treat it as "fall through to the
    agent", not as an exception.
    """
    provider = MagicMock()
    if error is not None:
        provider.chat_stream.return_value = iter([{"type": "error", "content": error}])
    elif chunks is not None:
        provider.chat_stream.return_value = iter(chunks)
    else:
        provider.chat_stream.return_value = iter([{"type": "text", "content": reply}])
    return provider


class ResolverTests(unittest.TestCase):
    def setUp(self):
        from tools import command_router
        self.router = command_router
        self.router._cache.clear()
        self.resolve = command_router.resolve_local_actions

    def _resolve(self, reply, utterance="restore all terminals", **kwargs):
        return self.resolve(
            utterance=utterance,
            language=kwargs.pop("language", "en"),
            catalogue=kwargs.pop("catalogue", CATALOGUE),
            state=kwargs.pop("state", STATE),
            provider=fake_provider(reply, **kwargs),
        )

    # ---- a well-behaved answer ----

    def test_a_clean_answer_becomes_a_proposal(self):
        self.assertEqual(
            self._resolve('{"actions": [{"type": "terminal", "action": "restore_all"}]}'),
            [{"type": "terminal", "action": "restore_all"}],
        )

    def test_the_state_and_the_instruction_reach_the_model_together(self):
        provider = fake_provider('{"actions": []}')
        self.resolve(
            utterance="bring back terminal 2",
            language="en",
            catalogue=CATALOGUE,
            state=STATE,
            provider=provider,
        )
        messages = provider.chat_stream.call_args.args[0]
        prompt = messages[1]["content"]
        self.assertIn("terminal #2", prompt)
        self.assertIn("bring back terminal 2", prompt)
        # The state is fenced and labelled as data: window titles are arbitrary
        # text from the network and must not be read as orders.
        self.assertIn("never instructions", prompt)
        self.assertIn("END SCREEN STATE", prompt)

    def test_a_chain_keeps_the_order_it_was_asked_in(self):
        got = self._resolve(
            '{"actions": ['
            '{"type": "terminal", "action": "minimize_all"},'
            '{"type": "terminal", "action": "focus", "target": 1}]}'
        )
        self.assertEqual([a["action"] for a in got], ["minimize_all", "focus"])

    def test_an_answer_for_the_agent_is_an_empty_list_not_an_error(self):
        # The most common correct answer, and the one the caller must not be
        # able to tell apart from a failure.
        self.assertEqual(self._resolve('{"actions": []}'), [])
        self.assertEqual(self._resolve("run top in the terminal"), [])

    # ---- replies that are not what was asked for ----

    def test_a_fenced_reply_with_prose_around_it_still_parses(self):
        got = self._resolve('Sure!\n```json\n{"actions": [{"type": "terminal", "action": "restore_all"}]}\n```\nDone.')
        self.assertEqual(got, [{"type": "terminal", "action": "restore_all"}])

    def test_smart_quotes_and_a_trailing_comma_are_repaired(self):
        got = self._resolve('{“actions”: [{“type”: “terminal”, “action”: “restore_all”,},]}')
        self.assertEqual(got, [{"type": "terminal", "action": "restore_all"}])

    def test_a_bare_array_and_a_kind_synonym_are_accepted(self):
        self.assertEqual(
            self._resolve('[{"kind": "Terminal", "action": "RESTORE_ALL"}]'),
            [{"type": "terminal", "action": "restore_all"}],
        )

    def test_nonsense_replies_yield_nothing(self):
        for reply in ["", "I cannot help with that", "{", "null", "[1, 2, 3]", '{"actions": "nope"}']:
            with self.subTest(reply=reply):
                self.assertEqual(self._resolve(reply), [])

    def test_a_reply_split_across_chunks_is_reassembled(self):
        # A provider streams deltas, so the JSON arrives in pieces. It also
        # interleaves usage chunks, which carry no text and must not be mistaken
        # for one.
        provider = MagicMock()
        provider.chat_stream.return_value = iter([
            {"type": "usage", "input": 10, "output": 0},
            {"type": "text", "content": '{"actions":[{"type":"terminal",'},
            {"type": "text", "content": '"action":"restore_all"}]}'},
            {"type": "usage", "input": 10, "output": 9},
        ])
        self.assertEqual(
            self.resolve(utterance="restore all terminals", language="en",
                         catalogue=CATALOGUE, state=STATE, provider=provider),
            [{"type": "terminal", "action": "restore_all"}],
        )

    def test_a_proposal_longer_than_the_cap_is_truncated(self):
        entry = '{"type": "terminal", "action": "restore_all"}'
        reply = '{"actions": [' + ",".join([entry] * 12) + "]}"
        self.assertEqual(len(self._resolve(reply)), self.router.MAX_PROPOSED_ACTIONS)

    # ---- failures must be silent ----

    def test_a_provider_error_is_not_an_exception(self):
        self.assertEqual(self._resolve(None, error="provider is down"), [])

    def test_a_provider_that_raises_is_not_an_exception(self):
        provider = MagicMock()
        provider.chat_stream.side_effect = RuntimeError("socket closed")
        self.assertEqual(
            self.resolve(utterance="restore all terminals", language="en",
                         catalogue=CATALOGUE, state=STATE, provider=provider),
            [],
        )

    def test_a_model_that_never_stops_talking_is_abandoned(self):
        chunks = [{"type": "text", "content": "x" * 500} for _ in range(50)]
        self.assertEqual(self._resolve(None, chunks=chunks), [])

    def test_an_abandoned_stream_is_closed_not_left_open(self):
        # Breaking out of the loop abandons a live HTTP response. Without a close
        # the connection stays open until the garbage collector gets to it, and
        # the caller is waiting on a turn that otherwise feels hung.
        stream = MagicMock()
        stream.__iter__.return_value = iter([{"type": "text", "content": "x" * 3000}])
        provider = MagicMock()
        provider.chat_stream.return_value = stream
        self.assertEqual(
            self.resolve(utterance="restore all terminals", language="en",
                         catalogue=CATALOGUE, state=STATE, provider=provider),
            [],
        )
        stream.close.assert_called_once()

    # ---- the request is bounded ----

    def test_an_empty_request_never_reaches_the_provider(self):
        provider = fake_provider('{"actions": []}')
        for utterance in ("", "   "):
            self.assertEqual(
                self.resolve(utterance=utterance, language="en", catalogue=CATALOGUE,
                             state=STATE, provider=provider),
                [],
            )
        self.assertEqual(provider.chat_stream.call_count, 0)

    def test_an_absent_catalogue_never_reaches_the_provider(self):
        # Without the catalogue the model cannot know what is possible, so
        # asking it anyway would only invite an invented action.
        provider = fake_provider('{"actions": []}')
        self.assertEqual(
            self.resolve(utterance="restore all terminals", language="en",
                         catalogue="", state=STATE, provider=provider),
            [],
        )
        self.assertEqual(provider.chat_stream.call_count, 0)

    def test_an_oversized_request_is_clipped_before_the_prompt(self):
        provider = fake_provider('{"actions": []}')
        self.resolve(utterance="x" * 50_000, language="en", catalogue=CATALOGUE,
                     state=STATE, provider=provider)
        prompt = provider.chat_stream.call_args.args[0][1]["content"]
        self.assertLess(len(prompt), self.router.MAX_UTTERANCE_CHARS + len(CATALOGUE) + len(STATE) + 2000)

    # ---- the cache ----

    def test_the_same_question_twice_asks_the_provider_once(self):
        provider = fake_provider('{"actions": [{"type": "terminal", "action": "restore_all"}]}')
        first = self.resolve(utterance="restore all terminals", language="en",
                             catalogue=CATALOGUE, state=STATE, provider=provider)
        second = self.resolve(utterance="restore all terminals", language="en",
                              catalogue=CATALOGUE, state=STATE, provider=provider)
        self.assertEqual(first, second)
        self.assertEqual(provider.chat_stream.call_count, 1)

    def test_a_different_screen_is_a_different_question(self):
        # Caching on the utterance alone would answer a later turn from a screen
        # that has since changed - the failure this whole layer exists to avoid.
        provider = fake_provider('{"actions": [{"type": "terminal", "action": "focus", "target": 2}]}')
        self.resolve(utterance="focus the other terminal", language="en",
                     catalogue=CATALOGUE, state=STATE, provider=provider)
        self.resolve(utterance="focus the other terminal", language="en",
                     catalogue=CATALOGUE, state="terminal #1 only", provider=provider)
        self.assertEqual(provider.chat_stream.call_count, 2)

    def test_a_cached_answer_is_a_copy_the_caller_cannot_corrupt(self):
        provider = fake_provider('{"actions": [{"type": "terminal", "action": "restore_all"}]}')
        first = self.resolve(utterance="restore all terminals", language="en",
                             catalogue=CATALOGUE, state=STATE, provider=provider)
        first[0]["action"] = "something_else"
        second = self.resolve(utterance="restore all terminals", language="en",
                              catalogue=CATALOGUE, state=STATE, provider=provider)
        self.assertEqual(second[0]["action"], "restore_all")

    def test_the_cache_does_not_grow_without_bound(self):
        provider = fake_provider('{"actions": []}')
        for index in range(self.router._CACHE_MAX + 20):
            self.resolve(utterance=f"question {index}", language="en",
                         catalogue=CATALOGUE, state=STATE, provider=provider)
        self.assertLessEqual(len(self.router._cache), self.router._CACHE_MAX)


class RouterModelTests(unittest.TestCase):
    def test_no_configured_model_means_the_providers_default(self):
        from tools import command_router
        with patch.object(command_router.config, "COMMAND_ROUTER_MODEL", ""):
            self.assertEqual(command_router.router_model("openai"), "")

    def test_a_configured_model_is_used_when_the_provider_can_serve_it(self):
        from tools import command_router
        with patch.object(command_router.config, "COMMAND_ROUTER_MODEL", "small"):
            self.assertEqual(command_router.router_model("openai", {"small", "big"}), "small")

    def test_an_explicit_value_wins_over_the_config(self):
        # The route resolves a per-user setting before calling, so the resolver
        # has to honour what it is handed.
        from tools import command_router
        with patch.object(command_router.config, "COMMAND_ROUTER_MODEL", "from-config"):
            self.assertEqual(command_router.router_model("openai", configured="per-user"), "per-user")

    def test_a_model_the_provider_does_not_have_is_ignored_not_sent(self):
        # A local provider raises on an unknown model name, so sending one turns
        # a fast fallback into a failed request on every unrecognized utterance.
        from tools import command_router
        with patch.object(command_router.config, "COMMAND_ROUTER_MODEL", "small"):
            self.assertEqual(command_router.router_model("ollama", {"llama3"}), "")

    def test_the_router_can_be_switched_off(self):
        from tools import command_router
        with patch.object(command_router.config, "COMMAND_ROUTER_ENABLED", False):
            self.assertFalse(command_router.router_enabled())
        with patch.object(command_router.config, "COMMAND_ROUTER_ENABLED", True):
            self.assertTrue(command_router.router_enabled())


class ResolveCommandRouteTests(unittest.TestCase):
    """The route must be signed-in, must not spend a model when it is off, and
    must never answer with anything other than a validated proposal."""

    def _setup(self):
        import app as app_module
        from tools import command_router
        # The resolver cache is module-global by design; these tests use the
        # same utterance and screen as the unit tests above, so a stale entry
        # would answer for a provider that was never called.
        command_router._cache.clear()
        db = MagicMock()
        db.get_user.return_value = {'id': 'alice', 'name': 'Alice'}
        db.all_settings.return_value = {'provider': 'openai', 'model': 'test'}
        return app_module, db

    def _client(self, app_module, db):
        application = app_module.create_app()
        client = application.test_client()
        with client.session_transaction() as session:
            session['user_id'] = 'alice'
        return client

    def _patches(self, app_module, db, provider):
        return patch.multiple(
            app_module,
            get_db=MagicMock(return_value=db),
            bearer_for_api=MagicMock(return_value=None),
            is_subscription_access=MagicMock(return_value=False),
        )

    def test_an_answer_is_returned_as_a_proposal(self):
        app_module, db = self._setup()
        provider = fake_provider('{"actions": [{"type": "terminal", "action": "restore_all"}]}')
        manager = MagicMock()
        manager.return_value.build.return_value = provider
        with self._patches(app_module, db, provider), \
             patch.object(app_module, "ProviderManager", manager):
            client = self._client(app_module, db)
            response = client.post("/api/resolve-command", json={
                "text": "restore all terminals",
                "language": "en",
                "catalogue": CATALOGUE,
                "state": STATE,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["actions"],
            [{"type": "terminal", "action": "restore_all"}],
        )

    def test_the_browser_owns_the_catalogue_it_sent(self):
        # The route forwards exactly what it was given; validation happens in
        # the browser against the same list, so the two cannot drift.
        app_module, db = self._setup()
        provider = fake_provider('{"actions": []}')
        manager = MagicMock()
        manager.return_value.build.return_value = provider
        with self._patches(app_module, db, provider), \
             patch.object(app_module, "ProviderManager", manager):
            client = self._client(app_module, db)
            client.post("/api/resolve-command", json={
                "text": "restore all terminals",
                "language": "el",
                "catalogue": CATALOGUE,
                "state": STATE,
            })
        prompt = provider.chat_stream.call_args.args[0][1]["content"]
        self.assertIn(CATALOGUE, prompt)
        self.assertIn(STATE, prompt)
        self.assertIn("The interface language is el", prompt)

    def test_an_unrecognized_utterance_is_an_empty_list_and_still_a_200(self):
        app_module, db = self._setup()
        provider = fake_provider('{"actions": []}')
        manager = MagicMock()
        manager.return_value.build.return_value = provider
        with self._patches(app_module, db, provider), \
             patch.object(app_module, "ProviderManager", manager):
            client = self._client(app_module, db)
            response = client.post("/api/resolve-command", json={
                "text": "what is the capital of Greece",
                "language": "en",
                "catalogue": CATALOGUE,
                "state": STATE,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["actions"], [])

    def test_a_missing_request_never_reaches_a_model(self):
        app_module, db = self._setup()
        manager = MagicMock()
        with self._patches(app_module, db, None), \
             patch.object(app_module, "ProviderManager", manager):
            client = self._client(app_module, db)
            for payload in ({}, {"text": "restore all terminals"},
                            {"text": "restore all terminals", "catalogue": ""}):
                response = client.post("/api/resolve-command", json=payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["actions"], [])
        manager.assert_not_called()

    def test_a_provider_that_cannot_be_built_is_an_empty_list(self):
        app_module, db = self._setup()
        manager = MagicMock()
        manager.return_value.build.side_effect = RuntimeError("no key")
        with self._patches(app_module, db, None), \
             patch.object(app_module, "ProviderManager", manager):
            client = self._client(app_module, db)
            response = client.post("/api/resolve-command", json={
                "text": "restore all terminals",
                "catalogue": CATALOGUE,
                "state": STATE,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["actions"], [])

    def test_a_signed_out_request_is_refused(self):
        app_module, db = self._setup()
        manager = MagicMock()
        with self._patches(app_module, db, None), \
             patch.object(app_module, "ProviderManager", manager):
            application = app_module.create_app()
            client = application.test_client()
            db.get_user.return_value = None
            response = client.post("/api/resolve-command", json={
                "text": "restore all terminals",
                "catalogue": CATALOGUE,
                "state": STATE,
            })
        self.assertEqual(response.status_code, 401)
        manager.assert_not_called()

    def test_the_router_off_switch_spends_nothing(self):
        app_module, db = self._setup()
        manager = MagicMock()
        with self._patches(app_module, db, None), \
             patch.object(app_module, "ProviderManager", manager), \
             patch.object(app_module.config, "COMMAND_ROUTER_ENABLED", False):
            client = self._client(app_module, db)
            response = client.post("/api/resolve-command", json={
                "text": "restore all terminals",
                "catalogue": CATALOGUE,
                "state": STATE,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["actions"], [])
        manager.assert_not_called()

    def _built_with(self, settings, configured):
        app_module, db = self._setup()
        db.all_settings.return_value = settings
        provider = fake_provider('{"actions": []}')
        manager = MagicMock()
        manager.return_value.build.return_value = provider
        with self._patches(app_module, db, provider), \
             patch.object(app_module, "ProviderManager", manager), \
             patch.object(app_module.config, "COMMAND_ROUTER_MODEL", configured):
            client = self._client(app_module, db)
            client.post("/api/resolve-command", json={
                "text": "restore all terminals",
                "catalogue": CATALOGUE,
                "state": STATE,
            })
        return manager.return_value.build.call_args.args

    def test_a_configured_router_model_reaches_the_provider_build(self):
        self.assertEqual(self._built_with({'provider': 'openai'}, "small"), ("openai", "small"))

    def test_a_per_user_setting_overrides_the_config_default(self):
        # The same precedence the think-hard model uses, so the router can be
        # retuned for one account without a restart.
        args = self._built_with({'provider': 'openai', 'command_router_model': 'per-user'}, "small")
        self.assertEqual(args, ("openai", "per-user"))

    def test_no_configured_model_means_the_providers_default(self):
        # None, not "": an empty name would be sent as a request for a model
        # called "" and every provider would reject it.
        self.assertEqual(self._built_with({'provider': 'openai'}, ""), ("openai", None))


if __name__ == "__main__":
    unittest.main()