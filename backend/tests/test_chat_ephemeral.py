"""Ephemeral chat turns (e.g. autonomous nudges) must not write history."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ChatEphemeralTests(unittest.TestCase):
    def _setup(self):
        import app as app_module
        db = MagicMock()
        db.get_user.return_value = {'id': 'alice', 'name': 'Alice'}
        db.all_settings.return_value = {'provider': 'openai', 'model': 'test'}
        db.get_conversation.return_value = {'id': 'conversation', 'user_id': 'alice', 'skill': 'general'}
        db.list_messages.return_value = []
        skills = MagicMock()
        skills.select.return_value = SimpleNamespace(model='', tools=['web_search'])
        skills.build_system_prompt.return_value = 'test'
        engine = MagicMock()
        engine.stream.return_value = iter([
            {'type': 'text_delta', 'content': 'Hello from autonomous mode.'},
            {'type': 'done', 'usage': {}},
        ])
        return app_module, db, skills, engine

    def test_default_chat_persists_messages(self):
        app_module, db, skills, engine = self._setup()
        with patch.object(app_module, 'get_db', return_value=db), \
             patch.object(app_module, 'get_skill_manager', return_value=skills), \
             patch.object(app_module, 'bearer_for_api', return_value=None), \
             patch.object(app_module, 'is_subscription_access', return_value=False), \
             patch.object(app_module, 'ProviderManager'), \
             patch.object(app_module, 'make_registry'), \
             patch.object(app_module, 'build_engine', return_value=engine), \
             patch.object(app_module.config, 'MEMORY_ENABLED', False), \
             patch.object(app_module.config, 'MEMORY_SUMMARIZE', False):
            application = app_module.create_app()
            client = application.test_client()
            with client.session_transaction() as session:
                session['user_id'] = 'alice'
            response = client.post('/api/chat', json={
                'message': 'find me coffee shops',
                'conversation_id': 'conversation',
            })
            self.assertEqual(response.status_code, 200)
            # Consume the stream so the finally block persists the assistant message.
            response.get_data(as_text=True)
            saved_roles = [call.args[1] for call in db.add_message.call_args_list]
            self.assertIn('user', saved_roles)
            self.assertIn('assistant', saved_roles)

    def test_ephemeral_chat_does_not_persist_anything(self):
        app_module, db, skills, engine = self._setup()
        with patch.object(app_module, 'get_db', return_value=db), \
             patch.object(app_module, 'get_skill_manager', return_value=skills), \
             patch.object(app_module, 'bearer_for_api', return_value=None), \
             patch.object(app_module, 'is_subscription_access', return_value=False), \
             patch.object(app_module, 'ProviderManager'), \
             patch.object(app_module, 'make_registry'), \
             patch.object(app_module, 'build_engine', return_value=engine), \
             patch.object(app_module.config, 'MEMORY_ENABLED', False), \
             patch.object(app_module.config, 'MEMORY_SUMMARIZE', False):
            application = app_module.create_app()
            client = application.test_client()
            with client.session_transaction() as session:
                session['user_id'] = 'alice'
            response = client.post('/api/chat', json={
                'message': 'You are APEX. Propose ONE action.',
                'conversation_id': 'conversation',
                'store_messages': False,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(db.add_message.call_count, 0)
            self.assertEqual(db.create_conversation.call_count, 0)
            self.assertEqual(db.update_conversation.call_count, 0)

    def test_document_context_reaches_the_prompt_but_not_history(self):
        app_module, db, skills, engine = self._setup()
        factory = MagicMock(return_value=engine)
        with patch.object(app_module, 'get_db', return_value=db), \
             patch.object(app_module, 'get_skill_manager', return_value=skills), \
             patch.object(app_module, 'bearer_for_api', return_value=None), \
             patch.object(app_module, 'is_subscription_access', return_value=False), \
             patch.object(app_module, 'ProviderManager'), \
             patch.object(app_module, 'make_registry'), \
             patch.object(app_module, 'build_engine', factory), \
             patch.object(app_module.config, 'MEMORY_ENABLED', False), \
             patch.object(app_module.config, 'MEMORY_SUMMARIZE', False):
            application = app_module.create_app()
            client = application.test_client()
            with client.session_transaction() as session:
                session['user_id'] = 'alice'
            preview = "ATTACHED-DOC-PREVIEW the system shall boot"
            response = client.post('/api/chat', json={
                'message': 'summarize the document',
                'conversation_id': 'conversation',
                'document_context': preview,
            })
            self.assertEqual(response.status_code, 200)
            response.get_data(as_text=True)
            prompt = factory.call_args.args[1].system_prompt
            self.assertIn(preview, prompt)
            self.assertIn('operator-provided data', prompt)
            # Build-time context only: it must never be written to history.
            persisted = [str(arg) for call in db.add_message.call_args_list for arg in call.args]
            self.assertFalse(any(preview in arg for arg in persisted))

    def test_ephemeral_chat_without_conversation_uses_temp_id(self):
        app_module, db, skills, engine = self._setup()
        db.get_conversation.return_value = None
        with patch.object(app_module, 'get_db', return_value=db), \
             patch.object(app_module, 'get_skill_manager', return_value=skills), \
             patch.object(app_module, 'bearer_for_api', return_value=None), \
             patch.object(app_module, 'is_subscription_access', return_value=False), \
             patch.object(app_module, 'ProviderManager'), \
             patch.object(app_module, 'make_registry'), \
             patch.object(app_module, 'build_engine', return_value=engine), \
             patch.object(app_module.config, 'MEMORY_ENABLED', False), \
             patch.object(app_module.config, 'MEMORY_SUMMARIZE', False):
            application = app_module.create_app()
            client = application.test_client()
            with client.session_transaction() as session:
                session['user_id'] = 'alice'
            response = client.post('/api/chat', json={
                'message': 'You are APEX. Propose ONE action.',
                'store_messages': False,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(db.create_conversation.call_count, 0)
            self.assertEqual(db.add_message.call_count, 0)
            data = response.get_data(as_text=True)
            # meta event should still be emitted with a conversation_id
            self.assertIn('"type": "meta"', data)
            self.assertIn('"conversation_id"', data)


class ChatImageAttachmentTests(unittest.TestCase):
    """An attached image reaches the vision-capable chat model as a multimodal
    message, or reaches the chat model as a description from the VISIO vision
    model. Either way the stored message stays plain text."""

    def _run(self, kind, *, capable, describe_result):
        app_module, db, skills, engine = ChatEphemeralTests()._setup()
        factory = MagicMock(return_value=engine)
        with patch.object(app_module, 'get_db', return_value=db), \
             patch.object(app_module, 'get_skill_manager', return_value=skills), \
             patch.object(app_module, 'bearer_for_api', return_value=None), \
             patch.object(app_module, 'is_subscription_access', return_value=False), \
             patch.object(app_module, 'ProviderManager') as PM, \
             patch.object(app_module, 'make_registry'), \
             patch.object(app_module, 'build_engine', factory), \
             patch.object(app_module, 'resolve_attachments',
                          return_value=[("photo.jpg", "image/jpeg", b"x")]), \
             patch.object(app_module, 'is_vision_capable', return_value=capable), \
             patch.object(app_module, 'describe_chat_images', return_value=describe_result), \
             patch.object(app_module.config, 'CHAT_VISION_ENABLED', True), \
             patch.object(app_module.config, 'MEMORY_ENABLED', False), \
             patch.object(app_module.config, 'MEMORY_SUMMARIZE', False):
            PM.return_value.build.return_value = SimpleNamespace(
                kind=kind, chat_stream=lambda *a, **k: iter([])
            )
            application = app_module.create_app()
            client = application.test_client()
            with client.session_transaction() as session:
                session['user_id'] = 'alice'
            response = client.post('/api/chat', json={
                'message': 'what is in this picture?',
                'conversation_id': 'conversation',
                'images': ['token'],
            })
            self.assertEqual(response.status_code, 200)
            response.get_data(as_text=True)
        return factory.call_args.args[1], db

    def test_a_capable_model_gets_the_image_in_the_message(self):
        ctx, _ = self._run("openai", capable=True, describe_result=None)
        content = ctx.history[-1]['content']
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]['type'], 'text')
        self.assertIn('what is in this picture?', content[0]['text'])
        self.assertEqual(content[1]['type'], 'image_url')

    def test_a_blind_model_gets_a_description_instead(self):
        ctx, _ = self._run("torch", capable=False, describe_result=("A red square.", None))
        content = ctx.history[-1]['content']
        self.assertIsInstance(content, str)
        self.assertIn('A red square.', content)

    def test_no_vision_model_says_so_honestly(self):
        ctx, _ = self._run("torch", capable=False,
                           describe_result=(None, "no vision model is configured"))
        content = ctx.history[-1]['content']
        self.assertIn('could not look at them', content)
        self.assertIn('no vision model is configured', content)

    def test_the_stored_message_is_still_plain_text(self):
        _, db = self._run("openai", capable=True, describe_result=None)
        saved = [call.args[2] for call in db.add_message.call_args_list if len(call.args) > 2]
        self.assertIn('what is in this picture?', saved)


class ToolResultSideEffectTests(unittest.TestCase):
    """`skills_changed`, `sudo_password` and `github_token` are emitted from
    the generic tool_result branch of the chat stream.

    They used to sit in `elif ev["type"] == "tool_result"` branches *below* the
    generic one, so the generic branch swallowed every event and none of them
    ever fired: a freshly created skill never appeared in the panel, and the
    VAPT sudo / CODE token popups never opened.
    """

    def _stream(self, events):
        app_module, db, skills, engine = ChatEphemeralTests()._setup()
        engine.stream.return_value = iter(events)
        with patch.object(app_module, 'get_db', return_value=db), \
             patch.object(app_module, 'get_skill_manager', return_value=skills), \
             patch.object(app_module, 'bearer_for_api', return_value=None), \
             patch.object(app_module, 'is_subscription_access', return_value=False), \
             patch.object(app_module, 'ProviderManager'), \
             patch.object(app_module, 'make_registry'), \
             patch.object(app_module, 'build_engine', return_value=engine), \
             patch.object(app_module.config, 'MEMORY_ENABLED', False), \
             patch.object(app_module.config, 'MEMORY_SUMMARIZE', False):
            application = app_module.create_app()
            client = application.test_client()
            with client.session_transaction() as session:
                session['user_id'] = 'alice'
            response = client.post('/api/chat', json={
                'message': 'create the skill',
                'conversation_id': 'conversation',
            })
            self.assertEqual(response.status_code, 200)
            data = response.get_data(as_text=True)
        out = []
        for line in data.splitlines():
            if line.startswith('data: '):
                try:
                    out.append(json.loads(line[6:]))
                except ValueError:
                    pass
        return out

    def test_create_skill_tool_result_emits_skills_changed(self):
        events = self._stream([
            {'type': 'tool_result', 'name': 'create_skill',
             'output': "Skill `demo` created."},
            {'type': 'text_delta', 'content': 'Done.'},
            {'type': 'done', 'usage': {}},
        ])
        types = [e.get('type') for e in events]
        self.assertIn('skills_changed', types)
        # The tool_result itself still reaches the client exactly once.
        self.assertEqual(types.count('tool_result'), 1)

    def test_sudo_marker_emits_sudo_password_event(self):
        from tools.vapt_tools import SUDO_MARKER
        events = self._stream([
            {'type': 'tool_result', 'name': 'vapt_run',
             'output': f"done\n{SUDO_MARKER}sudo credential required"},
            {'type': 'done', 'usage': {}},
        ])
        sudo = [e for e in events if e.get('type') == 'sudo_password']
        self.assertEqual(len(sudo), 1)
        self.assertEqual(sudo[0].get('reason'), 'sudo credential required')

    def test_ordinary_tool_results_emit_no_side_effect_events(self):
        events = self._stream([
            {'type': 'tool_result', 'name': 'web_search', 'output': 'results'},
            {'type': 'done', 'usage': {}},
        ])
        types = [e.get('type') for e in events]
        self.assertNotIn('skills_changed', types)
        self.assertNotIn('sudo_password', types)
        self.assertNotIn('github_token', types)


if __name__ == '__main__':
    unittest.main()
