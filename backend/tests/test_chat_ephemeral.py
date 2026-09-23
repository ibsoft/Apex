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


if __name__ == '__main__':
    unittest.main()
